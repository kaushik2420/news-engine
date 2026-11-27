"""The engine's public operations. REST routes and MCP tools are thin wrappers around these functions,
so both interfaces always behave identically.

Copyright rule baked in: stories expose our own LLM-written `summary` and only a short, clearly labelled
`excerpt` of publisher text plus links. Article bodies are never returned.
"""
from __future__ import annotations

import re
import secrets
from datetime import timedelta

from sqlalchemy import delete, func, or_, select

from . import db, generate, llm, rank, tagger, webhooks
from . import settings as S

PLATFORMS = sorted(generate.FORMATS)
EVENT_TYPES = sorted(rank.EVENT_WEIGHTS)
SOURCE_FIELDS = ("name", "homepage", "rss_url", "sitemap_url", "listing_url", "link_pattern", "region", "language",
                 "default_category", "interval_min", "enabled", "autodiscover")


class NotFound(LookupError):
    pass


class Invalid(ValueError):
    pass


# ============================================================================ serialisation
def thumbnail_urls(story_id: str) -> dict:
    base = S.settings.public_base_url
    path = f"{base}/thumbnails/{story_id}.png"
    return {"wide": path, "square": path + "?size=square"}


def _digest_of(r) -> dict | None:
    dg = getattr(r, "digest", None)
    if isinstance(dg, str):
        import json as _json
        try:
            dg = _json.loads(dg)
        except ValueError:
            dg = None
    if not dg or "error" in dg or not dg.get("body"):
        return None
    return dg


def _digest_state(r) -> str:
    st = getattr(r, "digest_status", None)
    if st in ("done", "stale") and _digest_of(r):
        return "ready" if st == "done" else "updating"
    if st in ("skipped", "failed"):
        return "unavailable"
    return "not_ready"


def _story_out(r, arts: list, full: bool = False, include_digest: bool = False) -> dict:
    tags = dict(r.tags or {})
    llm_summary = tags.pop("summary", None) if r.tag_status == "done" else None
    tags.pop("summary", None)
    is_roundup = bool(getattr(r, "is_roundup", False) or tags.pop("is_roundup", False))
    tags.pop("is_roundup", None)
    excerpt = (r.summary or "")[:240]
    out = {
        "id": r.id, "headline": r.headline,
        "summary": llm_summary,                     # engine-written, safe to display
        "excerpt": excerpt or None,                 # publisher text, short; show with attribution or not at all
        "published_at": db.iso(r.published_at), "updated_at": db.iso(r.updated_at),
        "source_count": r.source_count, "article_count": r.article_count,
        "sources": sorted({a["source_name"] for a in arts}),
        "image": next((a["image"] for a in arts if a.get("image")), None),
        "is_trending": r.source_count >= S.settings.trending_source_count,
        "is_roundup": is_roundup,
        "tag_status": r.tag_status, "tags": tags,
        "card_title": None,
        "thumbnail": thumbnail_urls(r.id),
        "image_credit": next((a["source_name"] for a in arts if a.get("image")), None),
        "digest_status": _digest_state(r),
        "links": [{"source": a["source_name"], "source_id": a["source_id"], "url": a["url"]} for a in arts],
    }
    dg = _digest_of(r)
    if dg:
        out["card_title"] = dg.get("card_title")
        out["dek"] = dg.get("dek")
    if full or include_digest:
        out["digest"] = dg
        out["articles"] = [{k: a[k] for k in ("id", "source_id", "source_name", "url", "title", "published_at", "via", "image")}
                           for a in arts]
    return out


def _articles_for(c, story_ids: list[str]) -> dict[str, list]:
    if not story_ids:
        return {}
    rows = c.execute(select(db.articles.c.id, db.articles.c.story_id, db.articles.c.source_id, db.articles.c.url,
                            db.articles.c.title, db.articles.c.image, db.articles.c.published_at,
                            db.articles.c.discovered_via, db.sources.c.name)
                     .join(db.sources, db.sources.c.id == db.articles.c.source_id)
                     .where(db.articles.c.story_id.in_(story_ids)).order_by(db.articles.c.published_at)).all()
    out: dict[str, list] = {}
    for a in rows:
        out.setdefault(a.story_id, []).append({"id": a.id, "source_id": a.source_id, "source_name": a.name, "url": a.url,
                                               "title": a.title, "image": a.image, "published_at": db.iso(a.published_at),
                                               "via": a.discovered_via})
    return out


FEED_POOL = 3000       # newest matching stories the feed ranker considers
LIST_FILTERS = {"category": "cat_text", "region": "region_text", "audience": "audience_text"}
EXACT_FILTERS = ("news_type", "tone", "shelf_life")


def _where(*, since_hours: int | None, filters: dict | None = None, q: str | None = None, source_id: str | None = None,
           min_sources: int = 1, min_postability: int = 0, include_untagged: bool = True,
           include_roundups: bool = False, exclude: set[str] | None = None) -> list:
    """SQL conditions for a story query. List labels match whole values: cat_text LIKE '%|AI|%'."""
    st = db.stories.c
    cond = []
    if since_hours is not None:
        cond.append(st.updated_at >= db.utcnow() - timedelta(hours=since_hours))
    for key, col in LIST_FILTERS.items():
        wanted = [v for v in (filters or {}).get(key) or [] if v]
        if wanted:
            cond.append(or_(*[getattr(st, col).like(f"%|{v}|%") for v in wanted]))
    for key in EXACT_FILTERS:
        wanted = [v for v in (filters or {}).get(key) or [] if v]
        if wanted:
            cond.append(getattr(st, key).in_(wanted))
    for w in [w for w in re.findall(r"\w+", (q or "").lower()) if len(w) > 1][:8]:
        cond.append(st.search_text.like(f"%{w}%"))
    if source_id:
        cond.append(st.id.in_(select(db.articles.c.story_id).where(db.articles.c.source_id == source_id)))
    if min_sources > 1:
        cond.append(st.source_count >= min_sources)
    if min_postability > 0:
        cond.append(st.postability >= min_postability)
    if not include_untagged:
        cond.append(st.tag_status != "pending")
    if not include_roundups:
        cond.append(or_(st.is_roundup.is_(None), st.is_roundup.is_(False)))
    if exclude:
        cond.append(st.id.notin_(list(exclude)))
    return cond


def _fetch(cond: list, order: list, limit: int, offset: int = 0, full: bool = False,
           include_digest: bool = False) -> list[dict]:
    with db.get_engine().connect() as c:
        rows = c.execute(select(db.stories).where(*cond).order_by(*order).limit(limit).offset(offset)).all()
        arts = _articles_for(c, [r.id for r in rows])
    out = []
    for r in rows:
        item = _story_out(r, arts.get(r.id, []), full=full, include_digest=include_digest)
        item["_updated"] = r.updated_at
        out.append(item)
    return out


def _strip(items: list[dict]) -> list[dict]:
    for s in items:
        s.pop("_updated", None)
    return items


# ============================================================================ engine
def status() -> dict:
    worker, worker_at = db.get_state("worker", {})
    now = db.utcnow()
    with db.get_engine().connect() as c:
        count = lambda t, *w: c.execute(select(func.count()).select_from(t).where(*w)).scalar()  # noqa: E731
        srcs = c.execute(select(db.sources)).all()
        out = {
            "worker": {**(worker or {}), "alive": bool(worker_at and now - worker_at < timedelta(minutes=5)),
                       "last_heartbeat": db.iso(worker_at)},
            "llm": {"enabled": llm.available(), "tag_model": S.settings.tag_model if llm.available() else None},
            "counts": {
                "sources": len(srcs), "sources_enabled": sum(1 for s in srcs if s.enabled),
                "sources_failing": sum(1 for s in srcs if (s.consecutive_failures or 0) >= 3),
                "articles": count(db.articles), "articles_24h": count(db.articles, db.articles.c.created_at >= now - timedelta(hours=24)),
                "stories": count(db.stories), "stories_24h": count(db.stories, db.stories.c.created_at >= now - timedelta(hours=24)),
                "stories_multi_source": count(db.stories, db.stories.c.source_count > 1),
            },
            "queues": {
                "enrich": count(db.articles, db.articles.c.needs_enrich.is_(True), db.articles.c.enrich_attempts < 3),
                "tag_pending": count(db.stories, db.stories.c.tag_status == "pending"),
                "tag_rules_only": count(db.stories, db.stories.c.tag_status == "rules"),
                "webhooks_pending": count(db.deliveries, db.deliveries.c.status == "pending"),
            },
            "now": db.iso(now),
        }
    return out


def taxonomy() -> dict:
    return {**tagger.taxonomy(), "platforms": {k: v["label"] for k, v in generate.FORMATS.items()},
            "event_types": EVENT_TYPES}


# ============================================================================ stories
def list_stories(*, category=None, news_type=None, audience=None, region=None, tone=None, shelf_life=None,
                 source_id: str | None = None, q: str | None = None, since_hours: int = 72, min_sources: int = 1,
                 min_postability: int = 0, sort: str = "latest", include_untagged: bool = True,
                 include_roundups: bool = False, include_digest: bool = False, limit: int = 30,
                 offset: int = 0) -> dict:
    st = db.stories.c
    orders = {"latest": [st.updated_at.desc()], "trending": [st.source_count.desc(), st.updated_at.desc()],
              "postability": [st.postability.desc(), st.updated_at.desc()]}
    if sort not in orders:
        raise Invalid("sort must be latest, trending or postability")
    filters = {"category": category, "news_type": news_type, "audience": audience, "region": region,
               "tone": tone, "shelf_life": shelf_life}
    cond = _where(since_hours=since_hours, filters=filters, q=q, source_id=source_id, min_sources=min_sources,
                  min_postability=min_postability, include_untagged=include_untagged, include_roundups=include_roundups)
    with db.get_engine().connect() as c:
        total = c.execute(select(func.count()).select_from(db.stories).where(*cond)).scalar()
    items = _fetch(cond, orders[sort] + [st.id], limit, offset, include_digest=include_digest)
    return {"total": total, "offset": offset, "limit": limit, "items": _strip(items)}


def get_story(story_id: str) -> dict:
    items = _fetch([db.stories.c.id == story_id], [db.stories.c.id], 1, full=True)
    if not items:
        raise NotFound(f"story {story_id} not found")
    return _strip(items)[0]


def trending(hours: int = 24, limit: int = 20, **filters) -> dict:
    return list_stories(since_hours=hours, min_sources=2, sort="trending", limit=limit, **filters)


def digest_reason(status: str, story: dict | None = None) -> str | None:
    """Plain-English reason a digest is missing, so callers (and their Claude) don't guess."""
    if status in ("ready", "updating"):
        return None
    if not llm.available():
        return ("Digests are switched off: the engine has no ANTHROPIC_API_KEY. Set it on both Render services "
                "(news-engine-api and news-engine-worker) and redeploy.")
    why = llm.paused()
    if why:
        return f"Digests are paused because {why}. The engine retries by itself every {llm.PAUSE_MINUTES} minutes."
    if status == "unavailable":
        err = ((story or {}).get("_digest_error")) or "no usable article text"
        return f"No digest for this story: {err}. Show the source links instead."
    return "Not written yet. Request it again with generate=true, or wait for the background writer."


def get_digest(story_id: str) -> dict:
    s = get_story(story_id)
    with db.get_engine().connect() as c:
        raw = c.execute(select(db.stories.c.digest).where(db.stories.c.id == story_id)).scalar()
    err = raw.get("error") if isinstance(raw, dict) else None
    return {"story_id": story_id, "status": s["digest_status"],
            "reason": digest_reason(s["digest_status"], {"_digest_error": err}), "llm_enabled": llm.available(),
            "llm_paused": llm.paused(),
            "headline": s["headline"], "card_title": s["card_title"], "digest": s.get("digest"),
            "links": s["links"], "thumbnail": s["thumbnail"]}


def story_for_thumbnail(story_id: str) -> dict:
    with db.get_engine().connect() as c:
        r = c.execute(select(db.stories).where(db.stories.c.id == story_id)).first()
        if not r:
            raise NotFound(f"story {story_id} not found")
        arts = _articles_for(c, [story_id]).get(story_id, [])
    dg = _digest_of(r)
    return {"id": r.id, "headline": r.headline, "card_title": dg.get("card_title") if dg else None,
            "tags": r.tags or {}, "source_count": r.source_count,
            "sources": list(dict.fromkeys(a["source_name"] for a in arts))}


# ============================================================================ profiles + feed
def _clean_profile(data: dict) -> dict:
    tax = tagger.taxonomy()
    out = {k: str(data.get(k) or "")[:300] for k in ("role", "niche", "goal", "tone", "name")}
    out["categories"] = [c for c in data.get("categories") or [] if c in tax["category"]]
    out["audiences"] = [a for a in data.get("audiences") or [] if a in tax["audience"]]
    out["regions"] = [r for r in data.get("regions") or [] if r in tax["region"]]
    out["platforms"] = [p for p in data.get("platforms") or [] if p in generate.FORMATS]
    bad = [k for k in ("categories", "audiences", "regions", "platforms") if len(out[k]) != len(data.get(k) or [])]
    if bad:
        raise Invalid(f"unknown values in {bad}; see GET /v1/taxonomy")
    return out


def upsert_profile(profile_id: str, data: dict) -> dict:
    if not re.fullmatch(r"[\w.@:-]{1,128}", profile_id or ""):
        raise Invalid("profile_id must be 1-128 chars of letters, digits, . _ - : @")
    clean = _clean_profile(data)
    with db.get_engine().begin() as c:
        row = c.execute(select(db.profiles).where(db.profiles.c.id == profile_id)).first()
        if row:
            c.execute(db.profiles.update().where(db.profiles.c.id == profile_id).values(data=clean, updated_at=db.utcnow()))
        else:
            c.execute(db.profiles.insert().values(id=profile_id, data=clean, weights={}, created_at=db.utcnow(), updated_at=db.utcnow()))
    return get_profile(profile_id)


def get_profile(profile_id: str) -> dict:
    with db.get_engine().connect() as c:
        row = c.execute(select(db.profiles).where(db.profiles.c.id == profile_id)).first()
    if not row:
        raise NotFound(f"profile {profile_id} not found")
    return {"id": row.id, "data": row.data, "learned_weights": row.weights or {},
            "created_at": db.iso(row.created_at), "updated_at": db.iso(row.updated_at)}


def delete_profile(profile_id: str) -> dict:
    with db.get_engine().begin() as c:
        n = c.execute(delete(db.profiles).where(db.profiles.c.id == profile_id)).rowcount
        c.execute(delete(db.events).where(db.events.c.profile_id == profile_id))
    if not n:
        raise NotFound(f"profile {profile_id} not found")
    return {"deleted": profile_id}


def record_event(profile_id: str, story_id: str, type: str, platform: str | None = None) -> dict:
    if type not in rank.EVENT_WEIGHTS:
        raise Invalid(f"type must be one of {EVENT_TYPES}")
    story = get_story(story_id)
    with db.get_engine().begin() as c:
        row = c.execute(select(db.profiles).where(db.profiles.c.id == profile_id)).first()
        if not row:
            raise NotFound(f"profile {profile_id} not found")
        weights = rank.learn(row.weights or {}, story["tags"].get("category") or [], type)
        c.execute(db.profiles.update().where(db.profiles.c.id == profile_id).values(weights=weights, updated_at=db.utcnow()))
        c.execute(db.events.insert().values(profile_id=profile_id, story_id=story_id, type=type, platform=platform, at=db.utcnow()))
    return {"recorded": type, "learned_weights": weights}


def feed(*, profile_id: str | None = None, profile: dict | None = None, filters: dict | None = None,
         since_hours: int = 72, limit: int = 30, exclude_seen: bool = True, include_untagged: bool = False,
         include_digest: bool = False) -> dict:
    prof, seen = None, set()
    if profile_id:
        p = get_profile(profile_id)
        prof = {"data": p["data"], "weights": p["learned_weights"]}
        if exclude_seen:
            with db.get_engine().connect() as c:
                seen = {r.story_id for r in c.execute(select(db.events.c.story_id).where(
                    db.events.c.profile_id == profile_id, db.events.c.type.in_(["dismiss", "posted"]))).all()}
    elif profile:
        prof = {"data": _clean_profile(profile), "weights": {}}
    cond = _where(since_hours=since_hours, filters=filters, include_untagged=include_untagged, exclude=seen)
    pool = _fetch(cond, [db.stories.c.updated_at.desc()], FEED_POOL, include_digest=include_digest)
    ranked = rank.build_feed(pool, prof, {}, limit, db.utcnow())
    return {"profile_id": profile_id, "count": len(ranked), "items": _strip(ranked)}


# ============================================================================ posts
def _resolve_profile(profile_id: str | None, profile: dict | None) -> dict | None:
    if profile_id:
        return get_profile(profile_id)["data"]
    return _clean_profile(profile) if profile else None


def post_brief(story_id: str, platform: str, profile_id: str | None = None, profile: dict | None = None,
               angle: str | None = None) -> dict:
    try:
        return generate.brief(get_story(story_id), platform, _resolve_profile(profile_id, profile), angle)
    except ValueError as e:
        if isinstance(e, Invalid):
            raise
        raise Invalid(str(e)) from e


def generate_post(story_id: str, platform: str, profile_id: str | None = None, profile: dict | None = None,
                  angle: str | None = None) -> dict:
    if not llm.available():
        raise Invalid("engine has no ANTHROPIC_API_KEY; use post_brief and generate with your own model")
    if platform not in generate.FORMATS:
        raise Invalid(f"platform must be one of {PLATFORMS}")
    return generate.generate(get_story(story_id), platform, _resolve_profile(profile_id, profile), angle)


# ============================================================================ sources
def _source_out(r) -> dict:
    health = "ok"
    if not r.enabled:
        health = "disabled"
    elif (r.consecutive_failures or 0) >= 3:
        health = "failing"
    elif r.consecutive_failures:
        health = "degraded"
    elif not r.last_success_at:
        health = "pending"
    return {"id": r.id, "name": r.name, "homepage": r.homepage, "rss_url": r.rss_url, "sitemap_url": r.sitemap_url,
            "listing_url": r.listing_url, "link_pattern": r.link_pattern, "region": r.region, "language": r.language,
            "default_category": r.default_category, "interval_min": r.interval_min, "enabled": r.enabled,
            "autodiscover": r.autodiscover, "health": health, "last_checked_at": db.iso(r.last_checked_at),
            "last_success_at": db.iso(r.last_success_at), "next_check_at": db.iso(r.next_check_at),
            "last_error": r.last_error, "consecutive_failures": r.consecutive_failures,
            "articles_total": r.articles_total, "last_new_articles": r.last_new_articles}


def list_sources() -> list[dict]:
    with db.get_engine().connect() as c:
        return [_source_out(r) for r in c.execute(select(db.sources).order_by(db.sources.c.id)).all()]


def get_source(source_id: str) -> dict:
    with db.get_engine().connect() as c:
        r = c.execute(select(db.sources).where(db.sources.c.id == source_id)).first()
        if not r:
            raise NotFound(f"source {source_id} not found")
        runs = c.execute(select(db.runs).where(db.runs.c.kind == "source", db.runs.c.ref == source_id)
                         .order_by(db.runs.c.id.desc()).limit(10)).all()
    out = _source_out(r)
    out["recent_runs"] = [{"started_at": db.iso(x.started_at), "ok": x.ok, "detail": x.detail} for x in runs]
    return out


def _validate_source(data: dict, partial: bool) -> dict:
    out = {k: data[k] for k in SOURCE_FIELDS if k in data}
    for k in ("homepage", "rss_url", "sitemap_url", "listing_url"):
        if out.get(k) and not str(out[k]).startswith(("https://", "http://")):
            raise Invalid(f"{k} must be an http(s) URL")
    if out.get("link_pattern"):
        try:
            re.compile(out["link_pattern"])
        except re.error as e:
            raise Invalid(f"link_pattern is not a valid regex: {e}") from e
    if out.get("default_category") and out["default_category"] not in tagger.taxonomy()["category"]:
        raise Invalid("default_category must be a taxonomy category")
    if "interval_min" in out and not 5 <= int(out["interval_min"]) <= 1440:
        raise Invalid("interval_min must be between 5 and 1440")
    if not partial and not any(out.get(k) for k in ("homepage", "rss_url", "sitemap_url", "listing_url")):
        raise Invalid("give at least one of homepage, sitemap_url, rss_url, listing_url")
    return out


def create_source(data: dict) -> dict:
    sid = str(data.get("id") or "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,62}", sid):
        raise Invalid("id must be a lowercase slug, e.g. 'economic-times'")
    vals = _validate_source(data, partial=False)
    vals.setdefault("name", sid)
    vals.setdefault("interval_min", S.settings.default_interval_min)
    with db.get_engine().begin() as c:
        if c.execute(select(db.sources.c.id).where(db.sources.c.id == sid)).first():
            raise Invalid(f"source {sid} already exists")
        c.execute(db.sources.insert().values(id=sid, next_check_at=db.utcnow(), created_at=db.utcnow(),
                                             enabled=vals.pop("enabled", True), autodiscover=vals.pop("autodiscover", True), **vals))
    return get_source(sid)


def update_source(source_id: str, data: dict) -> dict:
    vals = _validate_source(data, partial=True)
    if not vals:
        raise Invalid(f"nothing to update; editable fields: {list(SOURCE_FIELDS)}")
    if any(k in vals for k in ("homepage", "rss_url", "sitemap_url")):
        vals["discovered_at"] = None if vals.get("homepage") else db.utcnow()
    with db.get_engine().begin() as c:
        n = c.execute(db.sources.update().where(db.sources.c.id == source_id).values(**vals)).rowcount
    if not n:
        raise NotFound(f"source {source_id} not found")
    return get_source(source_id)


def delete_source(source_id: str, delete_articles: bool = False) -> dict:
    with db.get_engine().begin() as c:
        if delete_articles:
            c.execute(delete(db.articles).where(db.articles.c.source_id == source_id))
        else:
            c.execute(db.articles.update().where(db.articles.c.source_id == source_id).values(source_id=None))
        n = c.execute(delete(db.sources).where(db.sources.c.id == source_id)).rowcount
    if not n:
        raise NotFound(f"source {source_id} not found")
    return {"deleted": source_id}


def schedule_source_now(source_id: str) -> dict:
    with db.get_engine().begin() as c:
        n = c.execute(db.sources.update().where(db.sources.c.id == source_id)
                      .values(next_check_at=db.utcnow(), consecutive_failures=0)).rowcount
    if not n:
        raise NotFound(f"source {source_id} not found")
    return {"scheduled": source_id, "note": "the worker picks it up on its next tick"}


# ============================================================================ articles
def list_articles(source_id: str | None = None, story_id: str | None = None, since_hours: int = 48,
                  limit: int = 50, offset: int = 0) -> dict:
    q = select(db.articles.c.id, db.articles.c.source_id, db.articles.c.url, db.articles.c.title, db.articles.c.image,
               db.articles.c.published_at, db.articles.c.discovered_via, db.articles.c.story_id,
               db.articles.c.needs_enrich, db.articles.c.created_at)
    if story_id:
        q = q.where(db.articles.c.story_id == story_id)
    else:
        q = q.where(db.articles.c.created_at >= db.utcnow() - timedelta(hours=since_hours))
    if source_id:
        q = q.where(db.articles.c.source_id == source_id)
    with db.get_engine().connect() as c:
        rows = c.execute(q.order_by(db.articles.c.created_at.desc()).limit(limit).offset(offset)).all()
    return {"items": [{"id": r.id, "source_id": r.source_id, "url": r.url, "title": r.title, "image": r.image,
                       "published_at": db.iso(r.published_at), "via": r.discovered_via, "story_id": r.story_id,
                       "enriched": not r.needs_enrich, "discovered_at": db.iso(r.created_at)} for r in rows]}


# ============================================================================ webhooks, runs, admin
def list_webhooks() -> list[dict]:
    with db.get_engine().connect() as c:
        return [{"id": r.id, "url": r.url, "events": r.events, "active": r.active, "failures": r.failures,
                 "created_at": db.iso(r.created_at)} for r in c.execute(select(db.webhooks)).all()]


def create_webhook(url: str, events: list[str] | None = None) -> dict:
    if not url.startswith("https://") and not url.startswith("http://localhost"):
        raise Invalid("webhook url must be https")
    events = events or webhooks.EVENTS
    if set(events) - set(webhooks.EVENTS):
        raise Invalid(f"events must be from {webhooks.EVENTS}")
    wid, secret = secrets.token_hex(8), secrets.token_urlsafe(32)
    with db.get_engine().begin() as c:
        c.execute(db.webhooks.insert().values(id=wid, url=url, secret=secret, events=events, active=True, created_at=db.utcnow()))
    return {"id": wid, "url": url, "events": events, "secret": secret,
            "note": "store the secret; verify X-Engine-Signature = sha256=HMAC_SHA256(secret, raw_body)"}


def delete_webhook(webhook_id: str) -> dict:
    with db.get_engine().begin() as c:
        n = c.execute(delete(db.webhooks).where(db.webhooks.c.id == webhook_id)).rowcount
    if not n:
        raise NotFound(f"webhook {webhook_id} not found")
    return {"deleted": webhook_id}


def test_webhook(webhook_id: str) -> dict:
    with db.get_engine().begin() as c:
        if not c.execute(select(db.webhooks.c.id).where(db.webhooks.c.id == webhook_id)).first():
            raise NotFound(f"webhook {webhook_id} not found")
        c.execute(db.deliveries.insert().values(webhook_id=webhook_id, event="ping", payload={"hello": "from the engine"},
                                                status="pending", attempts=0, next_attempt_at=db.utcnow()))
    return {"queued": "ping"}


def list_deliveries(webhook_id: str | None = None, limit: int = 50) -> list[dict]:
    q = select(db.deliveries).order_by(db.deliveries.c.id.desc()).limit(limit)
    if webhook_id:
        q = q.where(db.deliveries.c.webhook_id == webhook_id)
    with db.get_engine().connect() as c:
        return [{"id": r.id, "webhook_id": r.webhook_id, "event": r.event, "status": r.status, "attempts": r.attempts,
                 "last_error": r.last_error, "created_at": db.iso(r.created_at)} for r in c.execute(q).all()]


def list_runs(kind: str | None = None, limit: int = 50) -> list[dict]:
    q = select(db.runs).order_by(db.runs.c.id.desc()).limit(limit)
    if kind:
        q = q.where(db.runs.c.kind == kind)
    with db.get_engine().connect() as c:
        return [{"id": r.id, "kind": r.kind, "ref": r.ref, "started_at": db.iso(r.started_at),
                 "finished_at": db.iso(r.finished_at), "ok": r.ok, "detail": r.detail} for r in c.execute(q).all()]


def requeue_failed_tags() -> dict:
    """Give every story whose AI tagging gave up ("failed") a fresh set of AI attempts. They keep their rule
    tags (and stay in feeds) until the AI re-tags them; IDs and clusters are untouched."""
    with db.get_engine().begin() as c:
        n = c.execute(db.stories.update().where(db.stories.c.tag_status == "failed")
                      .values(tag_status="rules", tag_attempts=0)).rowcount
    return {"requeued": n}


def retag_story(story_id: str) -> dict:
    with db.get_engine().begin() as c:
        n = c.execute(db.stories.update().where(db.stories.c.id == story_id).values(tag_status="pending", tag_attempts=0)).rowcount
    if not n:
        raise NotFound(f"story {story_id} not found")
    return {"queued_for_tagging": story_id}
