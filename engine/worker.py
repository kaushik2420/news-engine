"""The always-on worker. Run exactly one per deployment:

    python -m engine.worker              # forever
    python -m engine.worker --once       # one full cycle, then exit (cron-style hosting)
    python -m engine.worker --once --fixtures   # offline demo against tests/fixtures

The live worker runs independent loops: sources (up to SOURCE_CONCURRENCY at once, each capped at 5 minutes),
enrichment, tagging, webhooks, retention (every 6h) and a heartbeat every 15s. A slow site only ever holds
its own slot. Sources are claimed with a lease, so a crash never leaves a source stuck and two workers never
double-fetch. `--once` (and the admin API) run one ordered pass of everything instead.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from datetime import datetime, timedelta

import yaml
from sqlalchemy import or_, select

from . import db, digest, ingest, webhooks
from . import settings as S
from .fetch import Fetcher

log = logging.getLogger("engine.worker")
LEASE_MIN = 10
SOURCE_DEADLINE_SEC = 300        # one source can never hold up a cycle for longer than this


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


def apply_config_changes() -> list[str]:
    """One-off source changes from config/sources.yaml `changes:` (each applied once, tracked by its id).

    Lets operators fix or retire live sources through a deploy, without the admin API.
    """
    path = S.settings.config_dir / "sources.yaml"
    if not path.exists():
        return []
    changes = yaml.safe_load(path.read_text()).get("changes") or []
    done, _ = db.get_state("config_changes_applied", [])
    done = list(done or [])
    applied = []
    allowed = {c.name for c in db.sources.columns} - {"id", "created_at"}
    for ch in changes:
        cid, sid = ch.get("id"), ch.get("source")
        if not cid or cid in done:
            continue
        values = {k: v for k, v in (ch.get("set") or {}).items() if k in allowed}
        if values:
            values.setdefault("next_check_at", db.utcnow())
            values.setdefault("consecutive_failures", 0)
            with db.get_engine().begin() as c:
                c.execute(db.sources.update().where(db.sources.c.id == sid).values(**values))
        done.append(cid)
        applied.append(cid)
    if applied:
        db.set_state("config_changes_applied", done)
    return applied


def close_orphaned_runs() -> int:
    """Runs left open by a process that was stopped (e.g. during a deploy) are marked interrupted."""
    with db.get_engine().begin() as c:
        return c.execute(db.runs.update().where(db.runs.c.finished_at.is_(None)).values(
            finished_at=db.utcnow(), ok=False, detail={"error": "interrupted (worker restarted)"})).rowcount


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


async def run_source(fetcher: Fetcher, sid: str) -> dict:
    """Ingest one source with a hard time cap. A timeout counts as a failure and backs the source off."""
    try:
        return await asyncio.wait_for(ingest.ingest_source(fetcher, sid), SOURCE_DEADLINE_SEC)
    except asyncio.TimeoutError:
        log.warning("source %s took over %ss; abandoned this round", sid, SOURCE_DEADLINE_SEC)
        with db.get_engine().begin() as c:
            row = c.execute(select(db.sources.c.consecutive_failures, db.sources.c.interval_min)
                            .where(db.sources.c.id == sid)).first()
            if row:
                fails = (row.consecutive_failures or 0) + 1
                backoff = min(24 * 60, (row.interval_min or 20) * 2 ** min(fails, 6))
                c.execute(db.sources.update().where(db.sources.c.id == sid).values(
                    last_checked_at=db.utcnow(), consecutive_failures=fails,
                    last_error=f"timed out after {SOURCE_DEADLINE_SEC}s (site too slow or not answering)",
                    next_check_at=db.utcnow() + timedelta(minutes=backoff)))
        return {"source": sid, "error": f"timed out after {SOURCE_DEADLINE_SEC}s"}
    except KeyError:
        return {"source": sid, "error": "source deleted"}


def record_run(kind: str, started, detail: dict) -> None:
    with db.get_engine().begin() as c:
        c.execute(db.runs.insert().values(kind=kind, started_at=started, finished_at=db.utcnow(), ok=True, detail=detail))


def retention_due() -> bool:
    last, _ = db.get_state("retention_at", None)
    return not last or db.utcnow() - datetime.fromisoformat(last) > timedelta(hours=6)


async def cycle(fetcher: Fetcher, force_all: bool = False) -> dict:
    """One pass of everything, in order. Used by --once, the admin API and tests; the live worker runs loops."""
    cfg = S.settings
    if force_all:
        with db.get_engine().begin() as c:
            c.execute(db.sources.update().where(db.sources.c.enabled.is_(True)).values(next_check_at=db.utcnow()))
    due = claim_due(limit=cfg.source_concurrency * 3)
    sem = asyncio.Semaphore(cfg.source_concurrency)

    async def run(sid):
        async with sem:
            return await run_source(fetcher, sid)

    results = await asyncio.gather(*(run(s) for s in due), return_exceptions=True)
    report = {"sources": [r if isinstance(r, dict) else {"error": repr(r)} for r in results]}
    with ingest.Run("enrich") as run_:
        report["enrich"] = run_.detail = await ingest.enrich_pending(fetcher)
    with ingest.Run("tag") as run_:
        report["tag"] = run_.detail = await ingest.tag_pending()
    report["digest"] = await digest.digest_pending(fetcher)
    report["webhooks"] = await webhooks.dispatch(fetcher.client)
    if retention_due():
        with ingest.Run("retention") as run_:
            report["retention"] = run_.detail = ingest.retention()
        db.set_state("retention_at", db.utcnow().isoformat())
    return report


class Worker:
    """The live worker: independent loops so a slow site never stalls enrichment, tagging or the heartbeat."""

    def __init__(self, fetcher: Fetcher):
        self.fetcher, self.stop = fetcher, asyncio.Event()
        self.running: dict[str, asyncio.Task] = {}
        self.slot_free = asyncio.Event()            # set when a source finishes, so its slot is reused at once
        self.stats = {"sources_checked": 0, "new_articles": 0, "enriched": 0, "tagged": 0, "digests": 0,
                      "webhooks_sent": 0, "loop_errors": 0}
        self.started_at = db.utcnow()

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    async def _guard(self, name: str, step, idle: float, busy: float = 1.0) -> None:
        """Run step() forever; sleep `busy` after work, `idle` after none. Errors are logged, never fatal."""
        while not self.stop.is_set():
            try:
                did_work = await step()
            except Exception:
                self.stats["loop_errors"] += 1
                log.exception("%s loop failed; retrying", name)
                did_work = False
            await self._sleep(busy if did_work else idle)

    async def _source_loop(self) -> None:
        """Keep every slot busy: claim due sources whenever a slot frees up, else re-check every 5s."""
        while not self.stop.is_set():
            try:
                free = S.settings.source_concurrency - len(self.running)
                for sid in claim_due(free) if free > 0 else []:
                    task = asyncio.create_task(run_source(self.fetcher, sid), name=f"source:{sid}")
                    self.running[sid] = task
                    task.add_done_callback(lambda t, sid=sid: self._source_done(sid, t))
            except Exception:
                self.stats["loop_errors"] += 1
                log.exception("sources loop failed; retrying")
            self.slot_free.clear()
            waits = [asyncio.create_task(self.stop.wait()), asyncio.create_task(self.slot_free.wait())]
            await asyncio.wait(waits, timeout=5, return_when=asyncio.FIRST_COMPLETED)
            for w in waits:
                w.cancel()

    def _source_done(self, sid: str, task: asyncio.Task) -> None:
        self.running.pop(sid, None)
        self.slot_free.set()
        if task.cancelled() or task.exception():
            return
        res = task.result()
        self.stats["sources_checked"] += 1
        self.stats["new_articles"] += res.get("new_articles", 0)
        if res.get("new_articles"):
            log.info("%s: %d new articles, %d new stories", sid, res["new_articles"], res.get("new_stories", 0))

    async def _enrich(self) -> bool:
        started = db.utcnow()
        res = await ingest.enrich_pending(self.fetcher)
        if res.get("attempted"):
            self.stats["enriched"] += res["enriched"]
            record_run("enrich", started, res)
            return True
        return False

    async def _tag(self) -> bool:
        started = db.utcnow()
        res = await ingest.tag_pending()
        done = res["tagged"] + res["rules"] + res["failed"]
        if done:
            self.stats["tagged"] += done
            record_run("tag", started, res)
        return bool(done)

    async def _digest(self) -> bool:
        started = db.utcnow()
        res = await digest.digest_pending(self.fetcher)
        if res.get("results"):
            self.stats["digests"] += res["made"]
            record_run("digest", started, res)
        return bool(res.get("results"))

    async def _webhooks(self) -> bool:
        res = await webhooks.dispatch(self.fetcher.client)
        self.stats["webhooks_sent"] += res["sent"]
        return bool(res["sent"] or res["failed"])

    async def _retention(self) -> bool:
        if retention_due():
            started = db.utcnow()
            res = await asyncio.to_thread(ingest.retention)
            record_run("retention", started, res)
            db.set_state("retention_at", db.utcnow().isoformat())
        return False

    async def _heartbeat(self) -> bool:
        db.set_state("worker", {"status": "running", "since": db.iso(self.started_at), "last_tick_at": db.iso(db.utcnow()),
                                "checking_now": sorted(self.running), **self.stats})
        return False

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop.set)
            except NotImplementedError:
                pass
        log.info("worker started; seeded %d sources, applied config changes %s, closed %d interrupted runs",
                 seed_sources(), apply_config_changes(), close_orphaned_runs())
        tick = S.settings.tick_seconds
        loops = [
            self._guard("heartbeat", self._heartbeat, idle=15),
            self._source_loop(),
            self._guard("enrich", self._enrich, idle=tick, busy=1),
            self._guard("tag", self._tag, idle=tick, busy=1),
            self._guard("digest", self._digest, idle=60, busy=2),
            self._guard("webhooks", self._webhooks, idle=10, busy=1),
            self._guard("retention", self._retention, idle=600),
        ]
        await asyncio.gather(*loops)
        for t in list(self.running.values()):
            t.cancel()
        db.set_state("worker", {"status": "stopped", "last_tick_at": db.iso(db.utcnow()), **self.stats})
        await self.fetcher.close()


async def run_forever(fetcher: Fetcher) -> None:
    await Worker(fetcher).run()


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
        apply_config_changes()
        report = asyncio.run(cycle(fetcher, force_all=True))
        for s in report["sources"]:
            log.info("%s", s)
        log.info("enrich=%s tag=%s webhooks=%s", report["enrich"], report["tag"], report["webhooks"])
    else:
        asyncio.run(run_forever(fetcher))


if __name__ == "__main__":
    main()
