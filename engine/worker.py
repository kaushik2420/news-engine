"""The always-on worker. Run exactly one per deployment:

    python -m engine.worker              # forever
    python -m engine.worker --once       # one full cycle, then exit (cron-style hosting)
    python -m engine.worker --once --fixtures   # offline demo against tests/fixtures

Each tick: claim due sources -> ingest concurrently -> enrich -> tag -> webhooks -> (every 6h) retention.
Sources are claimed with a lease, so a crash never leaves a source stuck and two workers never double-fetch.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from datetime import timedelta

import yaml
from sqlalchemy import or_, select

from . import db, ingest, webhooks
from . import settings as S
from .fetch import Fetcher

log = logging.getLogger("engine.worker")
LEASE_MIN = 10


def seed_sources() -> int:
    """Insert sources from config/sources.yaml that aren't in the DB yet. Never overwrites API edits."""
    path = S.settings.config_dir / "sources.yaml"
    if not path.exists():
        return 0
    items = yaml.safe_load(path.read_text()).get("sources", [])
    added = 0
    with db.get_engine().begin() as c:
        have = {r.id for r in c.execute(select(db.sources.c.id)).all()}
        for s in items:
            if s["id"] in have:
                continue
            c.execute(db.sources.insert().values(
                id=s["id"], name=s.get("name", s["id"]), homepage=s.get("homepage"), rss_url=s.get("rss_url"),
                sitemap_url=s.get("sitemap_url"), listing_url=s.get("listing_url"), link_pattern=s.get("link_pattern"),
                region=s.get("region"), language=s.get("language", "en"), default_category=s.get("default_category"),
                interval_min=s.get("interval_min", S.settings.default_interval_min), enabled=s.get("enabled", True),
                autodiscover=s.get("autodiscover", True), next_check_at=db.utcnow(), created_at=db.utcnow()))
            added += 1
    return added


def claim_due(limit: int) -> list[str]:
    now = db.utcnow()
    with db.get_engine().begin() as c:
        ids = [r.id for r in c.execute(select(db.sources.c.id).where(
            db.sources.c.enabled.is_(True), or_(db.sources.c.next_check_at.is_(None), db.sources.c.next_check_at <= now))
            .order_by(db.sources.c.next_check_at).limit(limit)).all()]
        claimed = []
        for sid in ids:
            res = c.execute(db.sources.update().where(db.sources.c.id == sid, or_(
                db.sources.c.next_check_at.is_(None), db.sources.c.next_check_at <= now))
                .values(next_check_at=now + timedelta(minutes=LEASE_MIN)))
            if res.rowcount:
                claimed.append(sid)
    return claimed


async def cycle(fetcher: Fetcher, force_all: bool = False) -> dict:
    cfg = S.settings
    if force_all:
        with db.get_engine().begin() as c:
            c.execute(db.sources.update().where(db.sources.c.enabled.is_(True)).values(next_check_at=db.utcnow()))
    due = claim_due(limit=cfg.source_concurrency * 3)
    sem = asyncio.Semaphore(cfg.source_concurrency)

    async def run(sid):
        async with sem:
            return await ingest.ingest_source(fetcher, sid)

    results = await asyncio.gather(*(run(s) for s in due), return_exceptions=True)
    report = {"sources": [r if isinstance(r, dict) else {"error": repr(r)} for r in results]}
    with ingest.Run("enrich") as run_:
        report["enrich"] = run_.detail = await ingest.enrich_pending(fetcher)
    with ingest.Run("tag") as run_:
        report["tag"] = run_.detail = await ingest.tag_pending()
    report["webhooks"] = await webhooks.dispatch(fetcher.client)

    last, _ = db.get_state("retention_at", None)
    if not last or db.utcnow() - db.datetime.fromisoformat(last) > timedelta(hours=6):
        with ingest.Run("retention") as run_:
            report["retention"] = run_.detail = ingest.retention()
        db.set_state("retention_at", db.utcnow().isoformat())
    return report


async def run_forever(fetcher: Fetcher) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    log.info("worker started; seeded %d sources", seed_sources())
    ticks = 0
    while not stop.is_set():
        ticks += 1
        try:
            report = await cycle(fetcher)
            new = sum(s.get("new_articles", 0) for s in report["sources"] if isinstance(s, dict))
            db.set_state("worker", {"status": "running", "ticks": ticks, "last_tick_at": db.iso(db.utcnow()),
                                    "last_sources_checked": len(report["sources"]), "last_new_articles": new,
                                    "last_tag": report["tag"], "last_enrich": report["enrich"]})
            if report["sources"]:
                log.info("tick %d: %d sources, %d new articles, tag=%s", ticks, len(report["sources"]), new, report["tag"])
        except Exception:
            log.exception("tick failed; continuing")
            db.set_state("worker", {"status": "error", "ticks": ticks, "last_tick_at": db.iso(db.utcnow())})
        try:
            await asyncio.wait_for(stop.wait(), timeout=S.settings.tick_seconds)
        except asyncio.TimeoutError:
            pass
    db.set_state("worker", {"status": "stopped", "ticks": ticks, "last_tick_at": db.iso(db.utcnow())})
    await fetcher.close()


def fixture_fetcher() -> Fetcher:
    from tests.fixtures_transport import transport
    return Fetcher(transport=transport(), respect_robots=True, per_domain_interval=0)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("trafilatura").setLevel(logging.ERROR)   # "discarding data" is routine; og: tags still captured
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true", help="run one cycle and exit")
    p.add_argument("--fixtures", action="store_true", help="use offline fixtures instead of the network")
    a = p.parse_args()
    if a.fixtures:
        S.settings.max_article_age_hours = 10 ** 6   # fixture headlines are from 26 Nov 2026
    fetcher = fixture_fetcher() if a.fixtures else Fetcher()
    if a.once:
        seed_sources()
        report = asyncio.run(cycle(fetcher, force_all=True))
        for s in report["sources"]:
            log.info("%s", s)
        log.info("enrich=%s tag=%s webhooks=%s", report["enrich"], report["tag"], report["webhooks"])
    else:
        asyncio.run(run_forever(fetcher))


if __name__ == "__main__":
    main()
