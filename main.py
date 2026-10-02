"""CityPulse: question -> grounded 30 s film, streamed as live directing events."""
import asyncio
import json
import os
import re
import time
from contextlib import asynccontextmanager

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import settings
from director import Director
from render import render
from vss import VSS

STATIC = settings.ROOT / "static"
SOURCES_CACHE = settings.DATA_DIR / "sources.json"
SOURCES_TTL = 24 * 3600
STREET_RE = re.compile(
    r"\b((?:[A-Z][a-z]+\s){1,2}(?:Street|Avenue|Road|Boulevard|Drive|Lane|Crescent|Way|Place|Square|St|Ave|Rd)\b"
    r"(?:\s(?:East|West|North|South|E|W))?)")
SEGMENT_RE = re.compile(r"^s3://[\w.-]+/segments/[\w.-]+_segment_\d{3}_of_\d{3}\.mp4$")
YOLO_CLASSES = ["person", "car", "bicycle", "motorcycle", "bus", "truck", "train"]

state: dict = {}
detections_cache: dict[str, dict] = {}
detection_slots = asyncio.Semaphore(6)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings.RENDER_DIR.mkdir(parents=True, exist_ok=True)
    state["vss"] = VSS()
    state["job_lock"] = asyncio.Lock()
    state["warm"] = asyncio.create_task(warm_up())
    yield
    await state["vss"].close()


async def warm_up():
    try:
        await load_sources()
    except Exception as e:
        print(f"[warm-up] sources unavailable: {e}")
    try:
        import tts
        await asyncio.get_running_loop().run_in_executor(None, tts.load_voice)
    except Exception as e:
        print(f"[warm-up] voice unavailable: {e}")


app = FastAPI(title="CityPulse", lifespan=lifespan)


async def load_sources() -> dict:
    if "sources" in state:
        return state["sources"]
    if SOURCES_CACHE.exists() and time.time() - SOURCES_CACHE.stat().st_mtime < SOURCES_TTL:
        state["sources"] = json.loads(SOURCES_CACHE.read_text())
        return state["sources"]
    items = await state["vss"].explore_all(settings.LOCATION)
    drives: dict[str, list] = {}
    for it in items:
        m = re.search(r"_(set\d+)_video_chunk_(\d+)", it.get("filename", ""))
        if not m:
            continue
        caption = (it.get("reasoning_content") or "").strip()
        drives.setdefault(m[1], []).append({
            "chunk_index": int(m[2]),
            "filename": it.get("filename"),
            "caption": caption[:220],
            "streets": sorted(set(STREET_RE.findall(caption))),
        })
    data = {
        "location": settings.LOCATION,
        "chunk_seconds": settings.CHUNK_SECONDS,
        "drives": [{"drive_set": d, "chunks": sorted(c, key=lambda x: x["chunk_index"])}
                   for d, c in sorted(drives.items())],
    }
    SOURCES_CACHE.parent.mkdir(parents=True, exist_ok=True)
    SOURCES_CACHE.write_text(json.dumps(data))
    state["sources"] = data
    return data


def sse(event: str, data) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/api/sources")
async def sources():
    return await load_sources()


@app.get("/api/direct")
async def direct(q: str = Query(..., min_length=3, max_length=300)):
    queue: asyncio.Queue = asyncio.Queue()

    async def emit(stage: str, data: dict):
        await queue.put((stage, data))

    async def job():
        try:
            async with state["job_lock"]:
                edl = await Director(state["vss"]).run(q, emit=emit)
                edl = await render(state["vss"], edl, emit=emit)
                await emit("done", edl)
        except asyncio.CancelledError:
            await emit("superseded", {"message": "A newer question took over."})
        except Exception as e:
            await emit("error", {"message": str(e)[:500]})
        finally:
            await queue.put(None)

    # Latest request wins: the previous job is cancelled and releases the lock as it unwinds.
    previous = state.get("job")
    if previous and not previous.done():
        previous.cancel()
    task = asyncio.create_task(job())
    state["job"] = task

    async def stream():
        yield sse("accepted", {"question": q})
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=10)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            if item is None:
                break
            yield sse(*item)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/films")
async def films(limit: int = 8):
    out = []
    for edl_path in sorted(settings.RENDER_DIR.glob("*/edl.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            e = json.loads(edl_path.read_text())
        except json.JSONDecodeError:
            continue
        out.append({"id": e["id"], "title": e.get("title"), "question": e.get("question"),
                    "poster": e.get("media", {}).get("poster"), "created_at": e.get("created_at")})
        if len(out) >= limit:
            break
    return out


@app.get("/api/films/{film_id}")
async def film(film_id: str):
    if not re.fullmatch(r"[0-9a-f]{12}", film_id):
        raise HTTPException(404)
    path = settings.RENDER_DIR / film_id / "edl.json"
    if not path.exists():
        raise HTTPException(404)
    return JSONResponse(json.loads(path.read_text()))


@app.get("/api/clip")
async def clip(request: Request, source: str):
    """Preview a retrieved segment without exposing VSS credentials to the browser."""
    if not SEGMENT_RE.match(source):
        raise HTTPException(400, "not a segment source")
    cached = settings.CACHE_DIR / source.rsplit("/", 1)[-1]
    if cached.exists():
        return FileResponse(cached, media_type="video/mp4")
    vss: VSS = state["vss"]
    headers = {"Range": request.headers["range"]} if "range" in request.headers else {}
    req = vss.http.build_request("GET", f"{vss.base}/api/v1/videos/stream",
                                 params={"source": source, "token": await vss.token()}, headers=headers)
    upstream = await vss.http.send(req, stream=True)
    if upstream.status_code >= 400:
        await upstream.aclose()
        raise HTTPException(upstream.status_code)
    passthrough = {k: v for k, v in upstream.headers.items()
                   if k.lower() in ("content-length", "content-range", "accept-ranges")}

    async def body():
        try:
            async for chunk in upstream.aiter_bytes(1 << 16):
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(body(), status_code=upstream.status_code, media_type="video/mp4", headers=passthrough)


@app.get("/api/detections")
async def detections(source: str):
    """Stored YOLO11 sidecar for one segment, reduced to street classes with boxes normalised to 0-1."""
    if not SEGMENT_RE.match(source):
        raise HTTPException(400, "not a segment source")
    if source in detections_cache:
        return detections_cache[source]
    try:
        async with detection_slots:
            d = await state["vss"]._call("GET", "/api/v1/videos/detections", params={"source": source})
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return {"available": False}
        raise HTTPException(502, "detections unavailable")
    except httpx.HTTPError:
        raise HTTPException(502, "detections unavailable")
    h, w = (d.get("video_shape") or [1080, 1920])[:2]
    frames = []
    for f in sorted(d.get("frames") or [], key=lambda f: f.get("frame_index", 0)):
        boxes = []
        for det in f.get("detections") or []:
            if det.get("label") in YOLO_CLASSES and det.get("confidence", 0) >= 0.35 and len(det.get("bbox") or []) == 4:
                x1, y1, x2, y2 = det["bbox"]
                boxes.append([YOLO_CLASSES.index(det["label"]),
                              round(x1 / w, 4), round(y1 / h, 4), round(x2 / w, 4), round(y2 / h, 4)])
        frames.append(boxes)
    counts = {k: int(v) for k, v in (d.get("object_counts") or {}).items() if k in YOLO_CLASSES and v}
    out = {"available": True, "model": d.get("source", "yolo11"), "fps": d.get("fps") or 30.0,
           "classes": YOLO_CLASSES, "counts": counts, "frames": frames}
    if len(detections_cache) > 512:
        detections_cache.clear()
    detections_cache[source] = out
    return out


@app.get("/renders/{film_id}/{name:path}")
async def render_file(film_id: str, name: str):
    if not re.fullmatch(r"[0-9a-f]{12}", film_id) or ".." in name:
        raise HTTPException(404)
    path = settings.RENDER_DIR / film_id / name
    if not path.is_file():
        raise HTTPException(404)
    media = {"mp4": "video/mp4", "jpg": "image/jpeg", "vtt": "text/vtt", "json": "application/json"}
    return FileResponse(path, media_type=media.get(path.suffix.lstrip("."), "application/octet-stream"))


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_level="info")
