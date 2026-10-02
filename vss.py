"""Thin async client for the team's VSS retrieval backend."""
import asyncio
import hashlib
import json
import time
from pathlib import Path

import httpx

import settings

SEARCH_CACHE_SECONDS = 6 * 3600


class VSS:
    def __init__(self, base: str = settings.VSS_URL, username: str | None = settings.VSS_USERNAME,
                 password: str | None = settings.VSS_PASSWORD):
        if not (base and username and password):
            raise RuntimeError("VSS_URL/INGRESS_URL, username and password must be set in the environment")
        self.base = base
        self._creds = {"username": username, "password": password}
        self._token: str | None = None
        self._lock = asyncio.Lock()
        # The shared backend is memory-limited; more parallel searches than this can OOM it.
        self._search_slots = asyncio.Semaphore(2)
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(180, connect=15))

    async def close(self):
        await self.http.aclose()

    async def token(self, refresh: bool = False) -> str:
        async with self._lock:
            if refresh or not self._token:
                for attempt in range(4):
                    r = await self.http.post(f"{self.base}/api/v1/auth/login", json=self._creds)
                    if r.status_code < 500 or attempt == 3:
                        break
                    await asyncio.sleep(2.0 * (attempt + 1))
                r.raise_for_status()
                self._token = r.json()["access_token"]
            return self._token

    async def _call(self, method: str, path: str, **kw):
        extra_headers = kw.pop("headers", {})
        refresh = False
        for attempt in range(3):
            tok = await self.token(refresh=refresh)
            try:
                r = await self.http.request(method, f"{self.base}{path}",
                                            headers={"Authorization": f"Bearer {tok}", **extra_headers}, **kw)
            except httpx.TransportError:
                if attempt == 2:
                    raise
                await asyncio.sleep(1.5)
                continue
            if r.status_code == 401 and not refresh:
                refresh = True
                continue
            if r.status_code >= 500 and attempt < 2:
                await asyncio.sleep(5.0 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()

    async def search(self, query: str, top_k: int = 24, min_similarity: float = 0.22,
                     metadata_filters: dict | None = None) -> dict:
        body = {
            "query": query,
            "top_k": top_k,
            "llm_top_n": 1,
            "min_similarity": min_similarity,
            "metadata_filters": metadata_filters or {"location": settings.LOCATION},
            "include_public": True,
        }
        key = hashlib.sha1(json.dumps(body, sort_keys=True).encode()).hexdigest()[:20]
        cached = settings.DATA_DIR / "search-cache" / f"{key}.json"
        if cached.exists() and time.time() - cached.stat().st_mtime < SEARCH_CACHE_SECONDS:
            return json.loads(cached.read_text())
        async with self._search_slots:
            result = await self._call("POST", "/api/v1/tools/search", json=body)
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps(result))
        return result

    async def segment(self, source: str) -> dict:
        d = await self._call("GET", "/api/v1/tools/segment", params={"source": source})
        return d.get("segment", d)

    async def explore_all(self, location: str = settings.LOCATION) -> list[dict]:
        out, offset = [], 0
        while True:
            d = await self._call("GET", "/api/v1/videos/explore",
                                 params={"scope": "all", "limit": 48, "offset": offset, "location": location})
            batch = d.get("chunks") or []
            out += batch
            if len(batch) < 48 or len(out) >= d.get("total", 0):
                return out
            offset += 48

    async def download(self, source: str, dest: Path) -> Path:
        if dest.exists() and dest.stat().st_size > 0:
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        for attempt in (0, 1):
            tok = await self.token(refresh=attempt == 1)
            async with self.http.stream("GET", f"{self.base}/api/v1/videos/stream",
                                        params={"source": source, "token": tok}) as r:
                if r.status_code == 401:
                    continue
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    async for chunk in r.aiter_bytes(1 << 20):
                        f.write(chunk)
            tmp.rename(dest)
            return dest
        raise RuntimeError(f"download unauthorized: {source}")
