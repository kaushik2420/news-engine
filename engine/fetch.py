"""Polite async HTTP: robots.txt, per-domain pacing, retries with backoff, persisted ETag/Last-Modified, size caps.

One Fetcher is shared by the whole worker so pacing is global per domain.
"""
from __future__ import annotations

import asyncio
import logging
import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from . import db
from . import settings as S

log = logging.getLogger("engine.fetch")
ROBOTS_TTL = 6 * 3600


@dataclass
class Response:
    url: str
    status: int
    content: bytes = b""
    not_modified: bool = False
    content_type: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.content)


class Fetcher:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, respect_robots: bool = True,
                 per_domain_interval: float | None = None):
        cfg = S.settings
        self.ua = cfg.user_agent
        self.interval = cfg.per_domain_interval if per_domain_interval is None else per_domain_interval
        self.max_bytes = cfg.max_response_bytes
        self.respect_robots = respect_robots
        self.client = httpx.AsyncClient(
            headers={"User-Agent": self.ua, "Accept": "text/html,application/xhtml+xml,application/xml,application/rss+xml,*/*;q=0.8"},
            timeout=httpx.Timeout(20.0, connect=10.0), follow_redirects=True, transport=transport,
            limits=httpx.Limits(max_connections=40, max_keepalive_connections=20))
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}
        self._robots: dict[str, tuple[float, urllib.robotparser.RobotFileParser | None, list[str]]] = {}

    async def close(self) -> None:
        await self.client.aclose()

    # ---- robots ---------------------------------------------------------------
    async def robots(self, url: str) -> tuple[urllib.robotparser.RobotFileParser | None, list[str]]:
        """Returns (parser, sitemap urls declared in robots.txt)."""
        p = urlsplit(url)
        root = f"{p.scheme}://{p.netloc}"
        cached = self._robots.get(root)
        if cached and time.time() - cached[0] < ROBOTS_TTL:
            return cached[1], cached[2]
        rp, maps = None, []
        try:
            await self._pace(root)
            r = await self.client.get(root + "/robots.txt")
            if r.status_code == 200:
                lines = r.text.splitlines()
                rp = urllib.robotparser.RobotFileParser()
                rp.parse(lines)
                maps = [ln.split(":", 1)[1].strip() for ln in lines if ln.lower().startswith("sitemap:")]
            elif r.status_code in (401, 403):
                rp = urllib.robotparser.RobotFileParser()
                rp.parse(["User-agent: *", "Disallow: /"])
        except httpx.HTTPError as e:
            log.debug("robots unreachable for %s: %s", root, e)
        self._robots[root] = (time.time(), rp, maps)
        return rp, maps

    async def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        rp, _ = await self.robots(url)
        return True if rp is None else rp.can_fetch(self.ua, url)

    # ---- pacing -----------------------------------------------------------------
    async def _pace(self, url: str) -> None:
        domain = urlsplit(url).netloc
        lock = self._locks.setdefault(domain, asyncio.Lock())
        async with lock:
            wait = self._last.get(domain, 0) + self.interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[domain] = time.monotonic()

    # ---- conditional GET validators (persisted so restarts stay polite) -------
    def _validators(self, url: str) -> dict:
        with db.get_engine().connect() as c:
            row = c.execute(select(db.fetch_cache).where(db.fetch_cache.c.url == url[:1000])).first()
        return {"etag": row.etag, "last_modified": row.last_modified} if row else {}

    def _save_validators(self, url: str, etag: str | None, last_modified: str | None) -> None:
        if not etag and not last_modified:
            return
        t = db.fetch_cache
        with db.get_engine().begin() as c:
            if c.execute(select(t.c.url).where(t.c.url == url[:1000])).first():
                c.execute(t.update().where(t.c.url == url[:1000]).values(etag=etag, last_modified=last_modified, updated_at=db.utcnow()))
            else:
                c.execute(t.insert().values(url=url[:1000], etag=etag, last_modified=last_modified, updated_at=db.utcnow()))

    # ---- fetch -------------------------------------------------------------------
    async def get(self, url: str, conditional: bool = False, retries: int = 2) -> Response:
        if not url.startswith(("http://", "https://")):
            return Response(url, 0)
        if not await self.allowed(url):
            return Response(url, 451)    # "unavailable for legal reasons" - our marker for robots-disallowed
        headers = {}
        if conditional:
            v = self._validators(url)
            if v.get("etag"):
                headers["If-None-Match"] = v["etag"]
            if v.get("last_modified"):
                headers["If-Modified-Since"] = v["last_modified"]

        for attempt in range(retries + 1):
            await self._pace(url)
            try:
                async with self.client.stream("GET", url, headers=headers) as r:
                    if r.status_code == 304:
                        return Response(url, 304, not_modified=True)
                    if r.status_code in (429, 500, 502, 503, 504) and attempt < retries:
                        delay = r.headers.get("Retry-After", "")
                        await asyncio.sleep(min(int(delay) if delay.isdigit() else 2 ** (attempt + 1), 60))
                        continue
                    if r.status_code >= 400:
                        return Response(url, r.status_code)
                    chunks, size = [], 0
                    async for chunk in r.aiter_bytes():
                        size += len(chunk)
                        if size > self.max_bytes:
                            log.warning("response too large, truncated: %s", url)
                            break
                        chunks.append(chunk)
                    if conditional:
                        self._save_validators(url, r.headers.get("ETag"), r.headers.get("Last-Modified"))
                    return Response(str(r.url), r.status_code, b"".join(chunks),
                                    content_type=r.headers.get("Content-Type", ""))
            except httpx.HTTPError as e:
                if attempt == retries:
                    log.info("fetch failed %s: %s", url, e)
                    return Response(url, 0)
                await asyncio.sleep(2 ** (attempt + 1))
        return Response(url, 0)
