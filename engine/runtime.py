"""Process-wide shared objects for the API process (one polite Fetcher for on-demand ingests)."""
from __future__ import annotations

from .fetch import Fetcher

_fetcher: Fetcher | None = None


def get_fetcher() -> Fetcher:
    global _fetcher
    if _fetcher is None:
        _fetcher = Fetcher()
    return _fetcher


def set_fetcher(f: Fetcher | None) -> None:
    global _fetcher
    _fetcher = f


async def close() -> None:
    global _fetcher
    if _fetcher is not None:
        await _fetcher.close()
        _fetcher = None
