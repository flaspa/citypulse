"""EDL -> 30 s narrated film (MP4), WebVTT narration subtitles, poster and shot thumbnails."""
import asyncio
import json
import shutil
import textwrap
import wave
from pathlib import Path

import settings
import timeline
import tts
from vss import VSS

WIDTH, HEIGHT = 1280, 720
# 2.2:1 picture inside a 16:9 frame; the crop is biased slightly upward to drop the dashboard.
PICTURE_H = 582
CROP_Y = 80
GRADE = "eq=contrast=1.07:saturation=1.1:brightness=-0.015,vignette=angle=PI/5"
FADE_IN, FADE_OUT = 0.7, 1.1
MIN_GAP, MAX_GAP = 0.25, 0.95
CHARS_PER_SECOND = 17.3


def ffmpeg_exe() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


async def run_ffmpeg(*args: str):
    proc = await asyncio.create_subprocess_exec(
        ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await proc.communicate()
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
        raise
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {err.decode(errors='replace')[-800:]}")


def picture_filter(offset: float, duration: float, first: bool, last: bool) -> str:
    crop_h = round(1920 / (WIDTH / PICTURE_H))
    pad_y = (HEIGHT - PICTURE_H) // 2
    parts = [
        f"trim=start={offset:.3f}:duration={duration:.3f}",
        "setpts=PTS-STARTPTS",
        f"crop=1920:{crop_h}:0:{CROP_Y}",
        f"scale={WIDTH}:{PICTURE_H}:flags=lanczos",
        GRADE,
        f"pad={WIDTH}:{HEIGHT}:0:{pad_y}:black",
        "fps=30",
    ]
    if first:
        parts.append(f"fade=t=in:st=0:d={FADE_IN}")
    if last:
        parts.append(f"fade=t=out:st={max(0.0, duration - FADE_OUT):.3f}:d={FADE_OUT}")
    parts.append("format=yuv420p")
    return ",".join(parts)


async def voice_narration(sentences: list[dict], workdir: Path) -> tuple[list[Path], list[float], float, float]:
    """Synthesize each sentence, then pick speed and pauses so speech fills the film naturally."""
    loop = asyncio.get_running_loop()
    paths = [workdir / f"line_{i}.wav" for i in range(len(sentences))]

    async def synth_all(scale: float) -> list[float]:
        out = []
        for s, p in zip(sentences, paths):
            out.append(await loop.run_in_executor(None, tts.synthesize, s["text"], p, scale))
        return out

    n = len(sentences)
    window = settings.FILM_SECONDS - timeline.LEAD_IN - timeline.TAIL
    ideal_gap = 0.55

    def pick_scale(natural: float) -> float:
        return min(1.3, max(0.85, (window - ideal_gap * (n - 1)) / natural * 0.98))

    # Synthesis is the slowest CPU step, so guess the speed from text length and only
    # re-voice when the guess leaves the pauses outside their allowed range.
    scale = pick_scale(sum(len(s["text"]) for s in sentences) / CHARS_PER_SECOND)
    durations = await synth_all(scale)
    spare = window - sum(durations)
    if not MIN_GAP * (n - 1) <= spare <= (MAX_GAP + 0.2) * (n - 1):
        corrected = pick_scale(sum(durations) / scale)
        if abs(corrected - scale) > 0.02:
            scale = corrected
            durations = await synth_all(scale)
            spare = window - sum(durations)
    gap = min(MAX_GAP, max(MIN_GAP, spare / max(1, n - 1)))
    return paths, durations, gap, scale


def mix_narration(paths: list[Path], sentences: list[dict], dest: Path, total: float):
    with wave.open(str(paths[0]), "rb") as w:
        rate, width, channels = w.getframerate(), w.getsampwidth(), w.getnchannels()
    frame_bytes = width * channels
    buf = bytearray(int(total * rate) * frame_bytes)
    for p, s in zip(paths, sentences):
        with wave.open(str(p), "rb") as w:
            pcm = w.readframes(w.getnframes())
        start = int(s["start"] * rate) * frame_bytes
        pcm = pcm[: max(0, len(buf) - start)]
        buf[start:start + len(pcm)] = pcm
    with wave.open(str(dest), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        w.writeframes(bytes(buf))


def _ts(t: float) -> str:
    h, rem = divmod(max(0.0, t), 3600)
    m, s = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{s:06.3f}"


def subtitle_cues(sentences: list[dict], width: int = 46) -> list[dict]:
    """Phrase-sized cues; each sentence's time is shared by character count."""
    cues = []
    for i, s in enumerate(sentences):
        lines = textwrap.wrap(s["text"], width) or [s["text"]]
        total_chars = sum(len(l) for l in lines)
        t = s["start"]
        span = s["end"] - s["start"]
        for line in lines:
            d = span * len(line) / total_chars
            cues.append({"start": round(t, 3), "end": round(t + d, 3), "text": line, "sentence": i})
            t += d
    return cues


def write_vtt(cues: list[dict], dest: Path):
    lines = ["WEBVTT", ""]
    for i, c in enumerate(cues, 1):
        lines += [str(i), f"{_ts(c['start'])} --> {_ts(c['end'])}", c["text"], ""]
    dest.write_text("\n".join(lines))


def possible_segments(shot: dict) -> list[str]:
    """Every segment a shot could need for any allowed length, so downloads can start early."""
    lo = timeline.source_window(shot["match_window"][0], timeline.MAX_SHOT)[0]
    hi = timeline.source_window(shot["match_window"][0], timeline.MAX_SHOT)[1]
    lo = min(lo, timeline.source_window(shot["match_window"][0], timeline.MIN_SHOT)[0])
    hi = max(hi, timeline.source_window(shot["match_window"][0], timeline.MIN_SHOT)[1])
    return [f"{shot['segment_stem']}_segment_{n:03d}_of_{shot['total_segments']:03d}.mp4"
            for n in timeline.segments_for_window(lo, hi, shot["total_segments"])]


async def cut_shot(shot: dict, files: list[Path], dest: Path, first: bool, last: bool, workdir: Path):
    listing = workdir / f"shot_{shot['index']}.txt"
    listing.write_text("".join(f"file '{f.resolve()}'\n" for f in files))
    offset = shot["in"] - shot["segments"][0]["start"]
    threads = ("-threads", str(settings.FFMPEG_THREADS)) if settings.FFMPEG_THREADS else ()
    await run_ffmpeg(
        *threads, "-f", "concat", "-safe", "0", "-i", str(listing),
        "-vf", picture_filter(offset, shot["duration"], first, last),
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-r", "30", *threads,
        str(dest))


async def render(vss: VSS, edl: dict, emit=None) -> dict:
    emit = emit or (lambda *_: asyncio.sleep(0))
    out = settings.RENDER_DIR / edl["id"]
    work = out / "work"
    work.mkdir(parents=True, exist_ok=True)
    (out / "thumbs").mkdir(exist_ok=True)
    sentences = edl["narration"]["sentences"]
    shots = edl["shots"]

    slots = asyncio.Semaphore(4)

    async def fetch(source: str) -> Path:
        async with slots:
            return await vss.download(source, settings.CACHE_DIR / source.rsplit("/", 1)[-1])

    await emit("rendering", {"step": "voicing narration + gathering footage", "progress": 0.05})
    prefetch = asyncio.gather(*(fetch(src) for src in {src for s in shots for src in possible_segments(s)}),
                              return_exceptions=True)
    paths, durations, gap, scale = await voice_narration(sentences, work)
    await prefetch
    timeline.layout(shots, sentences, durations, gap=gap)
    edl["narration"]["timing_basis"] = "tts"
    edl["narration"]["voice"] = {"name": settings.PIPER_VOICE, "length_scale": round(scale, 3),
                                 "gap": round(gap, 3)}
    await emit("rendering", {"step": "timing cuts to the voice", "progress": 0.35})

    files = await asyncio.gather(*(asyncio.gather(*(fetch(seg["source"]) for seg in shot["segments"]))
                                   for shot in shots))

    await emit("rendering", {"step": "cutting shots", "progress": 0.45})
    shot_files = [work / f"shot_{s['index']}.mp4" for s in shots]
    await asyncio.gather(*(
        cut_shot(s, list(f), dest, i == 0, i == len(shots) - 1, work)
        for i, (s, f, dest) in enumerate(zip(shots, files, shot_files))))

    await emit("rendering", {"step": "mixing narration", "progress": 0.8})
    narration_wav = work / "narration.wav"
    mix_narration(paths, sentences, narration_wav, settings.FILM_SECONDS)
    concat_list = work / "shots.txt"
    concat_list.write_text("".join(f"file '{p.resolve()}'\n" for p in shot_files))
    film = out / "film.mp4"
    await run_ffmpeg(
        "-f", "concat", "-safe", "0", "-i", str(concat_list), "-i", str(narration_wav),
        "-map", "0:v", "-map", "1:a", "-c:v", "copy",
        "-af", f"loudnorm=I=-16:TP=-1.5:LRA=11,afade=t=out:st={settings.FILM_SECONDS - 0.6}:d=0.6",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-t", f"{settings.FILM_SECONDS:.3f}", "-movflags", "+faststart", str(film))

    await emit("rendering", {"step": "poster", "progress": 0.92})
    hero = max(shots, key=lambda s: s["score"])
    hero_file = shot_files[shots.index(hero)]
    await run_ffmpeg("-ss", f"{hero['duration'] / 2:.2f}", "-i", str(hero_file), "-frames:v", "1",
                     "-q:v", "2", str(out / "poster.jpg"))
    await asyncio.gather(*(
        run_ffmpeg("-ss", f"{s['duration'] / 2:.2f}", "-i", str(f), "-frames:v", "1",
                   "-vf", "scale=480:-2", "-q:v", "3", str(out / "thumbs" / f"shot_{s['index']}.jpg"))
        for s, f in zip(shots, shot_files)))

    cues = subtitle_cues(sentences)
    write_vtt(cues, out / "narration.vtt")
    base = f"renders/{edl['id']}"
    edl["subtitles"] = cues
    edl["media"] = {
        "video": f"{base}/film.mp4",
        "poster": f"{base}/poster.jpg",
        "vtt": f"{base}/narration.vtt",
        "thumbs": [f"{base}/thumbs/shot_{s['index']}.jpg" for s in shots],
        "hero_shot": hero["index"],
    }
    (out / "edl.json").write_text(json.dumps(edl, indent=2))
    shutil.rmtree(work, ignore_errors=True)
    await emit("rendering", {"step": "done", "progress": 1.0})
    return edl
