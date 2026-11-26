"""The work itself. Each function is one unit the worker schedules, and each can be triggered via API/MCP.

  ingest_source()   discover -> merge sitemap + RSS + listing candidates -> store -> cluster
  enrich_pending()  fetch article pages to fill titles/summaries/images/body
  tag_pending()     label stories (LLM or rules), emit webhooks
  retention()       purge old bodies, stories, runs, deliveries
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from urllib.parse import urljoin

from sqlalchemy import and_, delete, func, or_, select

from . import db, llm, tagger, webhooks
from . import settings as S
from .cluster import Clusterer
from .discover import (Candidate, feeds_in_html, from_listing, from_rss, is_news_sitemap, parse_sitemap,
                       pick_news_sitemaps, rank_sitemap_children)
from .extract import extract
from .fetch import Fetcher
from .text import canonical_url, url_id

log = logging.getLogger("engine.ingest")
MAX_NEW_PER_PASS = 80
COMMON_FEED_PATHS = ("/feed", "/feed/", "/rss", "/rss.xml", "/feed.xml", "/index.xml", "/atom.xml", "/rss/")
REDISCOVER_DAYS = 7


# ---------------------------------------------------------------------------- runs log
class Run:
    def __init__(self, kind: str, ref: str | None = None):
        self.kind, self.ref, self.detail, self.id = kind, ref, {}, None

    def __enter__(self):
        with db.get_engine().begin() as c:
            self.id = c.execute(db.runs.insert().values(kind=self.kind, ref=self.ref, started_at=db.utcnow())).inserted_primary_key[0]
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc:
            self.detail["error"] = f"{exc_type.__name__}: {exc}"[:500]
        with db.get_engine().begin() as c:
            c.execute(db.runs.update().where(db.runs.c.id == self.id).values(
                finished_at=db.utcnow(), ok=exc is None, detail=self.detail))
        return False


# ---------------------------------------------------------------------------- discovery
async def autodiscover(fetcher: Fetcher, src) -> dict:
    """Find RSS (homepage <link rel=alternate>) and news sitemaps (robots.txt). Never overwrites manual URLs."""
    found = {}
    home = src.homepage
    if not home:
        return found
    if not src.rss_url:
        r = await fetcher.get(home)
        if r.ok:
            feeds = feeds_in_html(r.content, r.url)
            if feeds:
                found["rss_url"] = feeds[0]
        if "rss_url" not in found:          # many sites don't advertise their feed: try the usual paths
            for path in COMMON_FEED_PATHS:
                url = urljoin(home, path)
                rf = await fetcher.get(url)
                if rf.ok and from_rss(rf.content):
                    found["rss_url"] = rf.url
                    break
    if not src.sitemap_url:
        _, maps = await fetcher.robots(home)
        for url in pick_news_sitemaps(maps)[:3]:
            r = await fetcher.get(url)
            if not r.ok:
                continue
            kind, items = parse_sitemap(r.content)
            if kind == "urlset" and is_news_sitemap(r.content):
                found["sitemap_url"] = url
                break
            if kind == "index":
                kids = rank_sitemap_children(items, limit=1)
                if kids:
                    rk = await fetcher.get(kids[0])
                    if rk.ok and parse_sitemap(rk.content)[0] == "urlset":
                        found["sitemap_url"] = url      # keep the index: its children rotate over time
                        break
    if found:   # only mark discovery done when it found something; otherwise retry on the next check
        with db.get_engine().begin() as c:
            c.execute(db.sources.update().where(db.sources.c.id == src.id).values(discovered_at=db.utcnow(), **found))
    return found


async def collect_sitemap(fetcher: Fetcher, url: str, depth: int = 0) -> list[Candidate]:
    r = await fetcher.get(url, conditional=True)
    if r.not_modified or not r.ok:
        return []
    kind, items = parse_sitemap(r.content)
    if kind == "index" and depth == 0:
        out = []
        for child in rank_sitemap_children(items, limit=2):
            out += await collect_sitemap(fetcher, child, depth + 1)
        return out
    return items if kind == "urlset" else []


# ---------------------------------------------------------------------------- store
def upsert_candidates(conn, src, cands: list[Candidate]) -> tuple[int, list[str]]:
    """Insert new articles / fill gaps in known ones. Returns (new_count, ids_ready_to_cluster)."""
    cutoff = db.utcnow() - timedelta(hours=S.settings.max_article_age_hours)
    merged: dict[str, Candidate] = {}
    for c in cands:                                   # same URL from sitemap + RSS: combine
        key = url_id(c.url)
        prev = merged.get(key)
        if prev is None:
            merged[key] = c
        else:
            prev.title = prev.title or c.title
            prev.summary = prev.summary or c.summary
            prev.image = prev.image or c.image
            prev.published = prev.published or c.published
            prev.author = prev.author or c.author
            if c.via == "rss" or prev.via == "rss":
                prev.via = "sitemap+rss" if "sitemap" in (prev.via, c.via) else "rss"

    new, ready = 0, []
    existing = {r.id: r for r in conn.execute(select(db.articles).where(db.articles.c.id.in_(list(merged)))).all()} if merged else {}
    for aid, c in merged.items():
        if c.published and c.published < cutoff:
            continue
        if c.via == "sitemap" and not c.published:     # generic sitemap entry without a date: unknown age, skip
            continue
        row = existing.get(aid)
        if row:
            fill = {k: v for k, v in (("title", c.title), ("summary", c.summary), ("image", c.image),
                                      ("published_at", c.published), ("author", c.author)) if v and not getattr(row, k)}
            if fill:
                conn.execute(db.articles.update().where(db.articles.c.id == aid).values(**fill))
                if row.story_id is None and (fill.get("title") or row.title):
                    ready.append(aid)
            continue
        if new >= MAX_NEW_PER_PASS:
            continue
        conn.execute(db.articles.insert().values(
            id=aid, source_id=src.id, url=canonical_url(c.url), title=c.title or None, summary=c.summary or None,
            image=c.image if (c.image or "").startswith("https://") else None, author=c.author,
            published_at=c.published, discovered_via=c.via,
            needs_enrich=not (c.title and c.summary and c.image), enrich_attempts=0, created_at=db.utcnow()))
        new += 1
        if c.title:
            ready.append(aid)
    return new, ready


def cluster_articles(conn, ids: list[str]) -> dict:
    if not ids:
        return {"new_stories": 0, "joined": 0}
    cl = Clusterer(conn)
    rows = conn.execute(select(db.articles).where(db.articles.c.id.in_(ids), db.articles.c.story_id.is_(None),
                                                  db.articles.c.title.isnot(None))
                        .order_by(db.articles.c.published_at)).all()
    created = joined = 0
    trending_at = S.settings.trending_source_count
    for r in rows:
        sid, is_new = cl.assign(conn, {"id": r.id, "source_id": r.source_id, "title": r.title, "summary": r.summary,
                                       "published_at": r.published_at or r.created_at})
        if is_new:
            created += 1
            webhooks.enqueue(conn, "story.created", {"story_id": sid, "headline": r.title})
        else:
            joined += 1
            st = conn.execute(select(db.stories.c.source_count, db.stories.c.trending_notified, db.stories.c.headline)
                              .where(db.stories.c.id == sid)).first()
            if st.source_count >= trending_at and not st.trending_notified:
                conn.execute(db.stories.update().where(db.stories.c.id == sid).values(trending_notified=True))
                webhooks.enqueue(conn, "story.trending", {"story_id": sid, "headline": st.headline,
                                                          "source_count": st.source_count})
    return {"new_stories": created, "joined": joined}


# ---------------------------------------------------------------------------- one source
async def ingest_source(fetcher: Fetcher, source_id: str) -> dict:
    with db.get_engine().connect() as c:
        src = c.execute(select(db.sources).where(db.sources.c.id == source_id)).first()
    if src is None:
        raise KeyError(source_id)
    stats = {"source": source_id, "sitemap": 0, "rss": 0, "listing": 0}
    with Run("source", source_id) as run:
        try:
            no_channel = not (src.rss_url or src.sitemap_url or src.listing_url)
            stale = no_channel or not src.discovered_at or db.utcnow() - src.discovered_at > timedelta(days=REDISCOVER_DAYS)
            if src.autodiscover and src.homepage and stale and not (src.rss_url and src.sitemap_url):
                stats["discovered"] = await autodiscover(fetcher, src)
                with db.get_engine().connect() as c:
                    src = c.execute(select(db.sources).where(db.sources.c.id == source_id)).first()

            cands: list[Candidate] = []
            if src.sitemap_url:
                got = await collect_sitemap(fetcher, src.sitemap_url)
                if src.link_pattern and not any(g.title for g in got):
                    import re
                    rx = re.compile(src.link_pattern)
                    got = [g for g in got if rx.search(g.url)]
                stats["sitemap"] = len(got)
                cands += got
            if src.rss_url:
                r = await fetcher.get(src.rss_url, conditional=True)
                if r.ok:
                    got = from_rss(r.content)
                    stats["rss"] = len(got)
                    cands += got
            if src.listing_url and src.link_pattern:
                r = await fetcher.get(src.listing_url)
                if r.ok:
                    got = from_listing(r.content, r.url, src.link_pattern)[:40]
                    stats["listing"] = len(got)
                    cands += got
            if not (src.sitemap_url or src.rss_url or src.listing_url):
                raise RuntimeError("no discovery channel: set rss_url, sitemap_url, listing_url or a homepage to auto-discover")

            with db.get_engine().begin() as c:
                new, ready = upsert_candidates(c, src, cands)
                stats.update(new_articles=new, **cluster_articles(c, ready))
                total = c.execute(select(func.count()).select_from(db.articles).where(db.articles.c.source_id == source_id)).scalar()
                c.execute(db.sources.update().where(db.sources.c.id == source_id).values(
                    last_checked_at=db.utcnow(), last_success_at=db.utcnow(), last_error=None, consecutive_failures=0,
                    articles_total=total, last_new_articles=new,
                    next_check_at=db.utcnow() + timedelta(minutes=src.interval_min or S.settings.default_interval_min)))
            run.detail = stats
            return stats
        except Exception as e:
            fails = (src.consecutive_failures or 0) + 1
            backoff = min(24 * 60, (src.interval_min or 20) * 2 ** min(fails, 6))   # exponential, capped at a day
            with db.get_engine().begin() as c:
                c.execute(db.sources.update().where(db.sources.c.id == source_id).values(
                    last_checked_at=db.utcnow(), last_error=str(e)[:500], consecutive_failures=fails,
                    next_check_at=db.utcnow() + timedelta(minutes=backoff)))
            run.detail = {**stats, "error": str(e)[:300], "retry_in_min": backoff}
            log.warning("source %s failed (%d in a row): %s", source_id, fails, e)
            return {**stats, "error": str(e)}


# ---------------------------------------------------------------------------- enrichment
async def enrich_pending(fetcher: Fetcher, limit: int | None = None) -> dict:
    limit = limit or S.settings.enrich_batch
    since = db.utcnow() - timedelta(hours=48)
    with db.get_engine().begin() as c:
        rows = c.execute(select(db.articles.c.id, db.articles.c.url).where(
            db.articles.c.needs_enrich.is_(True), db.articles.c.enrich_attempts < 3, db.articles.c.created_at >= since)
            # untitled articles first: they can't join a story until enriched; image/summary fills are cosmetic
            .order_by(db.articles.c.title.isnot(None), db.articles.c.created_at.desc()).limit(limit)).all()
        if rows:   # claim
            c.execute(db.articles.update().where(db.articles.c.id.in_([r.id for r in rows]))
                      .values(enrich_attempts=db.articles.c.enrich_attempts + 1))
    if not rows:
        return {"enriched": 0}

    async def one(r):
        resp = await fetcher.get(r.url)
        return r.id, (extract(resp.content, resp.url) if resp.ok and "html" in (resp.content_type or "html") else None)

    results = await asyncio.gather(*(one(r) for r in rows))
    done, ready = 0, []
    with db.get_engine().begin() as c:
        for aid, data in results:
            if not data:
                continue
            row = c.execute(select(db.articles).where(db.articles.c.id == aid)).first()
            fill = {"needs_enrich": False, "body": data["body"] or row.body}
            for k_row, k_data in (("title", "title"), ("summary", "summary"), ("image", "image"),
                                  ("published_at", "published"), ("author", "author")):
                if not getattr(row, k_row) and data.get(k_data):
                    fill[k_row] = data[k_data]
            c.execute(db.articles.update().where(db.articles.c.id == aid).values(**fill))
            done += 1
            if row.story_id is None and (row.title or fill.get("title")):
                ready.append(aid)
            elif row.story_id and fill.get("summary"):
                c.execute(db.stories.update().where(db.stories.c.id == row.story_id, or_(
                    db.stories.c.summary.is_(None), db.stories.c.summary == "")).values(summary=fill["summary"]))
        cl = cluster_articles(c, ready)
    return {"enriched": done, "attempted": len(rows), **cl}


# ---------------------------------------------------------------------------- tagging
def _story_context(c, sid: str) -> dict:
    arts = c.execute(select(db.articles.c.title, db.articles.c.summary, db.articles.c.body, db.sources.c.name,
                            db.sources.c.region, db.sources.c.default_category)
                     .join(db.sources, db.sources.c.id == db.articles.c.source_id)
                     .where(db.articles.c.story_id == sid)).all()
    return {"sources": sorted({a.name for a in arts}), "regions": sorted({a.region for a in arts if a.region}),
            "fallback": next((a.default_category for a in arts if a.default_category), None),
            "text": "\n\n".join((a.body or a.summary or a.title or "")[:2500] for a in arts)[:6000]}


async def tag_pending(limit: int | None = None) -> dict:
    limit = limit or S.settings.tag_batch
    use_llm = llm.available()
    statuses = ["pending", "rules"] if use_llm else ["pending"]
    with db.get_engine().connect() as c:
        rows = c.execute(select(db.stories.c.id, db.stories.c.headline, db.stories.c.summary, db.stories.c.source_count,
                                db.stories.c.tag_attempts, db.stories.c.tags)
                         .where(db.stories.c.tag_status.in_(statuses), db.stories.c.tag_attempts < 3)
                         .order_by(db.stories.c.updated_at.desc()).limit(limit)).all()
    out = {"tagged": 0, "rules": 0, "failed": 0, "mode": "llm" if use_llm else "rules"}
    for r in rows:
        with db.get_engine().connect() as c:
            ctx = _story_context(c, r.id)
        status, tags = "done", None
        if use_llm:
            try:
                tags = await asyncio.to_thread(tagger.llm_tag, r.headline, r.summary or "", ctx["sources"], ctx["text"], ctx["fallback"])
            except Exception as e:
                log.warning("LLM tagging failed for %s: %s", r.id, e)
                status = "pending" if r.tag_attempts < 2 else "failed"
        if tags is None and status != "pending":
            tags = tagger.rules_tag(r.headline, r.summary or "", ctx["regions"], r.source_count, ctx["fallback"])
            status = "rules" if not use_llm else "failed"
        with db.get_engine().begin() as c:
            values = {"tag_attempts": r.tag_attempts + 1, "tag_status": status}
            if tags:
                if (r.tags or {}).get("is_roundup"):
                    tags = {**tags, "is_roundup": True}      # structural flags survive re-tagging
                values.update(tags=tags, postability=tags["postability"])
            c.execute(db.stories.update().where(db.stories.c.id == r.id).values(**values))
            if tags:
                webhooks.enqueue(c, "story.tagged", {"story_id": r.id, "headline": r.headline,
                                                     "category": tags["category"], "postability": tags["postability"]})
        key = {"done": "tagged", "rules": "rules", "failed": "failed"}.get(status)
        if key:
            out[key] += 1
    return out


# ---------------------------------------------------------------------------- retention
def retention() -> dict:
    cfg = S.settings
    now = db.utcnow()
    with db.get_engine().begin() as c:
        bodies = c.execute(db.articles.update().where(db.articles.c.created_at < now - timedelta(days=cfg.keep_body_days),
                                                      db.articles.c.body.isnot(None)).values(body=None)).rowcount
        old = select(db.stories.c.id).where(db.stories.c.updated_at < now - timedelta(days=cfg.keep_story_days))
        arts = c.execute(delete(db.articles).where(or_(db.articles.c.story_id.in_(old), and_(
            db.articles.c.story_id.is_(None), db.articles.c.created_at < now - timedelta(days=3))))).rowcount
        stories_n = c.execute(delete(db.stories).where(db.stories.c.updated_at < now - timedelta(days=cfg.keep_story_days))).rowcount
        runs_n = c.execute(delete(db.runs).where(db.runs.c.started_at < now - timedelta(days=14))).rowcount
        deliv = c.execute(delete(db.deliveries).where(db.deliveries.c.status != "pending",
                                                      db.deliveries.c.created_at < now - timedelta(days=7))).rowcount
        cache = c.execute(delete(db.fetch_cache).where(db.fetch_cache.c.updated_at < now - timedelta(days=30))).rowcount
    return {"bodies_cleared": bodies, "articles_deleted": arts, "stories_deleted": stories_n, "runs_deleted": runs_n,
            "deliveries_deleted": deliv, "cache_deleted": cache}
