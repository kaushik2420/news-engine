"""Database schema and access. SQLAlchemy Core so the same code runs on Postgres (prod) and SQLite (dev/tests)."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, Column, DateTime, Float, ForeignKey, Index, Integer, MetaData, String, Table,
                        Text, create_engine, event)
from sqlalchemy.engine import Engine

from . import settings as S

metadata = MetaData()


def utcnow() -> datetime:
    """All timestamps are stored as naive UTC."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(dt: datetime | None) -> str | None:
    return dt.replace(microsecond=0).isoformat() + "Z" if dt else None


sources = Table(
    "sources", metadata,
    Column("id", String(64), primary_key=True),
    Column("name", String(200), nullable=False),
    Column("homepage", Text),
    Column("rss_url", Text),
    Column("sitemap_url", Text),            # news sitemap (or sitemap index) - primary discovery channel
    Column("listing_url", Text),            # fallback: section page to crawl for links
    Column("link_pattern", Text),           # regex an article URL must match (listing + generic sitemaps)
    Column("region", String(64)),
    Column("language", String(16), default="en"),
    Column("default_category", String(64)),
    Column("interval_min", Integer, default=20),
    Column("enabled", Boolean, default=True),
    Column("autodiscover", Boolean, default=True),   # find RSS/sitemaps from homepage + robots.txt
    Column("discovered_at", DateTime),
    Column("next_check_at", DateTime),
    Column("last_checked_at", DateTime),
    Column("last_success_at", DateTime),
    Column("last_error", Text),
    Column("consecutive_failures", Integer, default=0),
    Column("articles_total", Integer, default=0),
    Column("last_new_articles", Integer, default=0),
    Column("created_at", DateTime, default=utcnow),
)

fetch_cache = Table(
    "fetch_cache", metadata,
    Column("url", String(1000), primary_key=True),
    Column("etag", Text),
    Column("last_modified", Text),
    Column("updated_at", DateTime, default=utcnow),
)

stories = Table(
    "stories", metadata,
    Column("id", String(32), primary_key=True),      # stable: never changes once created
    Column("headline", Text, nullable=False),
    Column("summary", Text),
    Column("published_at", DateTime, index=True),    # earliest article
    Column("updated_at", DateTime, index=True),      # latest article joined
    Column("source_count", Integer, default=1),
    Column("article_count", Integer, default=1),
    Column("tokens", JSON),                          # for incremental clustering
    Column("tag_status", String(16), default="pending", index=True),   # pending | done | rules | failed
    Column("tag_attempts", Integer, default=0),
    Column("tags", JSON),
    Column("postability", Integer, default=0),
    Column("trending_notified", Boolean, default=False),
    Column("created_at", DateTime, default=utcnow),
)

articles = Table(
    "articles", metadata,
    Column("id", String(32), primary_key=True),      # sha1(canonical url)
    Column("source_id", String(64), ForeignKey("sources.id", ondelete="CASCADE"), index=True),
    Column("url", Text, nullable=False),
    Column("title", Text),
    Column("summary", Text),
    Column("image", Text),
    Column("author", Text),
    Column("published_at", DateTime, index=True),
    Column("discovered_via", String(16)),            # sitemap | rss | listing
    Column("needs_enrich", Boolean, default=True, index=True),
    Column("enrich_attempts", Integer, default=0),
    Column("body", Text),                            # internal only, never served
    Column("story_id", String(32), ForeignKey("stories.id", ondelete="SET NULL"), index=True),
    Column("created_at", DateTime, default=utcnow, index=True),
)

profiles = Table(
    "profiles", metadata,
    Column("id", String(128), primary_key=True),     # the app's own user id
    Column("data", JSON),                            # role, niche, goal, tone, categories, audiences, regions, platforms
    Column("weights", JSON),                         # learned per-category weights from events
    Column("created_at", DateTime, default=utcnow),
    Column("updated_at", DateTime, default=utcnow),
)

events = Table(
    "events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("profile_id", String(128), index=True),
    Column("story_id", String(32), index=True),
    Column("type", String(32)),                      # view | click | save | dismiss | posted
    Column("platform", String(32)),
    Column("at", DateTime, default=utcnow),
)

webhooks = Table(
    "webhooks", metadata,
    Column("id", String(32), primary_key=True),
    Column("url", Text, nullable=False),
    Column("secret", Text, nullable=False),
    Column("events", JSON),                          # ["story.created", "story.trending", "story.tagged"]
    Column("active", Boolean, default=True),
    Column("failures", Integer, default=0),
    Column("created_at", DateTime, default=utcnow),
)

deliveries = Table(
    "webhook_deliveries", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("webhook_id", String(32), ForeignKey("webhooks.id", ondelete="CASCADE"), index=True),
    Column("event", String(64)),
    Column("payload", JSON),
    Column("status", String(16), default="pending", index=True),     # pending | sent | failed
    Column("attempts", Integer, default=0),
    Column("next_attempt_at", DateTime, default=utcnow),
    Column("last_error", Text),
    Column("created_at", DateTime, default=utcnow),
)

runs = Table(
    "runs", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("kind", String(32), index=True),          # source | enrich | tag | webhooks | retention
    Column("ref", String(64)),
    Column("started_at", DateTime, default=utcnow, index=True),
    Column("finished_at", DateTime),
    Column("ok", Boolean),
    Column("detail", JSON),
)

kv = Table(
    "engine_state", metadata,
    Column("key", String(64), primary_key=True),
    Column("value", JSON),
    Column("updated_at", DateTime, default=utcnow),
)

Index("ix_stories_tag_queue", stories.c.tag_status, stories.c.updated_at)

_engine: Engine | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        url = S.settings.database_url
        kw = {"pool_pre_ping": True, "future": True}
        if url.startswith("sqlite"):
            kw["connect_args"] = {"check_same_thread": False, "timeout": 30}
        _engine = create_engine(url, **kw)
        if url.startswith("sqlite"):
            @event.listens_for(_engine, "connect")
            def _pragma(conn, _):
                cur = conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
        metadata.create_all(_engine)
    return _engine


def reset_engine() -> None:
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


def set_state(key: str, value) -> None:
    from sqlalchemy import select
    with get_engine().begin() as c:
        if c.execute(select(kv.c.key).where(kv.c.key == key)).first():
            c.execute(kv.update().where(kv.c.key == key).values(value=value, updated_at=utcnow()))
        else:
            c.execute(kv.insert().values(key=key, value=value, updated_at=utcnow()))


def get_state(key: str, default=None):
    from sqlalchemy import select
    with get_engine().connect() as c:
        row = c.execute(select(kv.c.value, kv.c.updated_at).where(kv.c.key == key)).first()
    return (row.value, row.updated_at) if row else (default, None)
