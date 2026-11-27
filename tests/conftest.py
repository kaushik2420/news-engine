import asyncio
import os
from datetime import datetime

import pytest

ADMIN, READ = "test-admin-key", "test-read-key"
FROZEN_NOW = datetime(2026, 11, 26, 13, 15)


@pytest.fixture()
def engine_env(tmp_path, monkeypatch):
    """Fresh SQLite engine with offline web, keys set, everything ingested once."""
    # set TEST_DATABASE_URL=postgresql+psycopg://... to run the suite against Postgres
    monkeypatch.setenv("DATABASE_URL", os.getenv("TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'engine.db'}")
    monkeypatch.setenv("ENGINE_API_KEY", ADMIN)
    monkeypatch.setenv("ENGINE_READ_KEY", READ)
    monkeypatch.setenv("PER_DOMAIN_INTERVAL_SEC", "0")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from engine import db, runtime, settings, tagger
    monkeypatch.setattr(db, "utcnow", lambda: FROZEN_NOW)
    from engine import llm as _llm
    _llm._pause.update(until=None, reason=None, checked=0.0)     # AI pause is process-wide; start each test clean     # fixture headlines are from 25-26 Nov 2026
    settings.reload()
    db.reset_engine()
    if os.getenv("TEST_DATABASE_URL"):
        db.metadata.drop_all(db.get_engine())
        db.reset_engine()
    tagger.taxonomy.cache_clear()
    from engine.fetch import Fetcher
    from tests.fixtures_transport import transport
    log: list = []
    fetcher = Fetcher(transport=transport(log=log), per_domain_interval=0)
    runtime.set_fetcher(fetcher)
    yield {"fetcher": fetcher, "log": log}
    runtime.set_fetcher(None)
    db.reset_engine()


def run(coro):
    return asyncio.run(coro)


def full_cycle(fetcher):
    """Seed sources and run worker cycles until the tag queue is empty."""
    from engine import ingest, worker
    worker.seed_sources()
    report = run(worker.cycle(fetcher, force_all=True))
    for _ in range(10):
        if run(ingest.tag_pending(50))["rules"] == 0:
            break
    return report
