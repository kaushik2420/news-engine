"""Feed ranking.

score = relevance(profile) x freshness(shelf life) x (1 + 0.5 * trending) x postability
where trending = log2(outlets covering the story). Topic match dominates; coverage breaks ties.
then a diversity pass (max 2 in a row from the same top category).
Relevance blends the profile's chosen interests with weights learned from that user's events.
"""
from __future__ import annotations

import math
from datetime import datetime

HALF_LIFE_HOURS = {"Breaking": 12, "This week": 48, "Evergreen": 24 * 14}
TRENDING_WEIGHT = 0.5
EVENT_WEIGHTS = {"view": 0.0, "click": 0.02, "save": 0.05, "posted": 0.10, "dismiss": -0.06}


def relevance(tags: dict, profile: dict | None) -> float:
    if not profile:
        return 1.0
    data, learned = profile.get("data") or profile, profile.get("weights") or {}
    s = 0.1
    cats = set(tags.get("category") or [])
    if set(data.get("categories") or []) & cats:
        s += 0.5
    if set(data.get("audiences") or []) & set(tags.get("audience") or []):
        s += 0.2
    regions = set(data.get("regions") or [])
    if not regions or regions & set(tags.get("region") or []) or "Global" in (tags.get("region") or []):
        s += 0.1
    s += sum(learned.get(c, 0.0) for c in cats)
    return max(0.05, s)


def freshness(updated: datetime, shelf_life: str | None, now: datetime) -> float:
    age_h = max(0.0, (now - updated).total_seconds() / 3600)
    return 0.5 ** (age_h / HALF_LIFE_HOURS.get(shelf_life or "This week", 48))


def score_parts(story: dict, profile: dict | None, now: datetime) -> dict:
    tags = story.get("tags") or {}
    rel = relevance(tags, profile)
    fresh = freshness(story["_updated"], tags.get("shelf_life"), now)
    trend = math.log2(max(1, story.get("source_count", 1)))
    post = 0.5 + (tags.get("postability") or 50) / 200
    return {"relevance": round(rel, 3), "freshness": round(fresh, 3), "trending": round(trend, 3),
            "postability": round(post, 3), "score": round(rel * fresh * (1 + TRENDING_WEIGHT * trend) * post, 4)}


def matches(tags: dict, filters: dict) -> bool:
    for key, wanted in (filters or {}).items():
        if not wanted:
            continue
        have = tags.get(key)
        have = set(have) if isinstance(have, list) else {have}
        if not have & set(wanted):
            return False
    return True


def build_feed(stories: list[dict], profile: dict | None, filters: dict | None, limit: int, now: datetime,
               exclude: set[str] | None = None) -> list[dict]:
    pool = [s for s in stories if matches(s.get("tags") or {}, filters or {}) and s["id"] not in (exclude or set())]
    for s in pool:
        s["rank"] = score_parts(s, profile, now)
    pool.sort(key=lambda s: s["rank"]["score"], reverse=True)
    out, recent = [], []
    while pool and len(out) < limit:
        idx = next((i for i, s in enumerate(pool) if recent[-2:].count(_top(s)) < 2), 0)
        pick = pool.pop(idx)
        out.append(pick)
        recent.append(_top(pick))
    return out


def _top(s: dict) -> str:
    return ((s.get("tags") or {}).get("category") or ["?"])[0]


def learn(weights: dict, categories: list[str], event_type: str) -> dict:
    w = dict(weights or {})
    delta = EVENT_WEIGHTS.get(event_type, 0.0)
    for c in categories:
        w[c] = round(max(-0.3, min(0.6, w.get(c, 0.0) + delta)), 3)
    return w
