"""All configuration comes from environment variables (12-factor). See .env.example."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


@dataclass
class Settings:
    # storage: postgresql+psycopg://user:pass@host/db in production, sqlite for local/tests
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", f"sqlite:///{ROOT / 'engine.db'}"))

    # auth: full-access key (admin) and optional read-only key (feeds, stories, briefs)
    api_key: str = field(default_factory=lambda: os.getenv("ENGINE_API_KEY", ""))
    read_key: str = field(default_factory=lambda: os.getenv("ENGINE_READ_KEY", ""))

    # LLM (optional: without a key the engine tags with keyword rules and only returns post briefs)
    anthropic_api_key: str = field(default_factory=lambda: os.getenv("ANTHROPIC_API_KEY", ""))
    tag_model: str = field(default_factory=lambda: os.getenv("TAG_MODEL", "claude-haiku-5-5"))
    write_model: str = field(default_factory=lambda: os.getenv("WRITE_MODEL", "claude-sonnet-5-5"))

    # crawling behaviour
    user_agent: str = field(default_factory=lambda: os.getenv(
        "ENGINE_USER_AGENT", "NewsEngineBot/1.0 (+https://engine.backyardsaas.com/bot)"))
    per_domain_interval: float = field(default_factory=lambda: _float("PER_DOMAIN_INTERVAL_SEC", 2.0))
    source_concurrency: int = field(default_factory=lambda: _int("SOURCE_CONCURRENCY", 6))
    default_interval_min: int = field(default_factory=lambda: _int("DEFAULT_SOURCE_INTERVAL_MIN", 20))
    max_article_age_hours: int = field(default_factory=lambda: _int("MAX_ARTICLE_AGE_HOURS", 72))
    max_response_bytes: int = field(default_factory=lambda: _int("MAX_RESPONSE_BYTES", 8_000_000))
    enrich_batch: int = field(default_factory=lambda: _int("ENRICH_BATCH", 25))
    tag_batch: int = field(default_factory=lambda: _int("TAG_BATCH", 10))

    # clustering
    cluster_window_hours: int = field(default_factory=lambda: _int("CLUSTER_WINDOW_HOURS", 72))
    cluster_threshold: float = field(default_factory=lambda: _float("CLUSTER_THRESHOLD", 0.42))
    trending_source_count: int = field(default_factory=lambda: _int("TRENDING_SOURCE_COUNT", 3))

    # retention
    keep_body_days: int = field(default_factory=lambda: _int("KEEP_BODY_DAYS", 7))
    keep_story_days: int = field(default_factory=lambda: _int("KEEP_STORY_DAYS", 30))

    # worker loop
    tick_seconds: float = field(default_factory=lambda: _float("WORKER_TICK_SEC", 15))

    config_dir: Path = ROOT / "config"


settings = Settings()


def reload() -> Settings:
    """Re-read env vars (used by tests)."""
    global settings
    settings = Settings()
    return settings
