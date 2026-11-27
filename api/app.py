"""REST API + MCP endpoint for the News Engine.

    uvicorn api.app:app --host 0.0.0.0 --port 8000
    Interactive docs: /docs        MCP endpoint: /mcp

Every route is versioned under /v1. Errors: 400 invalid input, 401 bad key, 403 needs admin key, 404 not found,
503 LLM not configured.
"""
from __future__ import annotations

import contextlib
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
import anyio
from pydantic import BaseModel, Field

from engine import db, digest, ingest, llm, runtime, service, thumbnail, worker
from engine.generate import FORMATS

from .auth import AuthMiddleware, require_admin, scope_var
from .mcp_server import mcp

router = APIRouter()


def create_app() -> FastAPI:
    """Build the app. Each instance gets its own MCP session manager (they can only start once)."""
    mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", stateless_http=True, json_response=True, host="0.0.0.0")
    session_manager = mcp.session_manager

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        db.get_engine()
        worker.seed_sources()
        async with session_manager.run():
            yield
        await runtime.close()

    application = FastAPI(title="News Engine", version="1.0.0", lifespan=lifespan,
                          description="Continuous news collection, story clustering, tagging, personalised feeds and post "
                                      "briefs. Authenticate with `Authorization: Bearer <ENGINE_API_KEY>`. MCP endpoint: `/mcp`.")
    application.include_router(router)
    application.router.routes.extend(mcp_app.routes)          # serve /mcp from the same app
    for exc, code in ((service.NotFound, 404), (service.Invalid, 400), (llm.LLMUnavailable, 503), (PermissionError, 403)):
        application.add_exception_handler(exc, _handler(code))
    application.add_middleware(AuthMiddleware)
    return application


def _handler(code: int):
    async def handle(_: Request, e: Exception):
        return JSONResponse({"detail": str(e)}, status_code=code)
    return handle


# ------------------------------------------------------------------ auth
def admin() -> None:
    require_admin()


Platform = Literal["linkedin", "x", "instagram_carousel", "reel", "story"]
EventType = Literal["view", "click", "save", "posted", "dismiss"]


# ------------------------------------------------------------------ models
class ProfileIn(BaseModel):
    name: str = ""
    role: str = ""
    niche: str = ""
    goal: str = ""
    tone: str = ""
    categories: list[str] = []
    audiences: list[str] = []
    regions: list[str] = []
    platforms: list[str] = []


class FeedIn(BaseModel):
    profile_id: str | None = None
    profile: ProfileIn | None = None
    filters: dict[str, list[str]] = {}
    since_hours: int = Field(72, ge=1, le=24 * 30)
    limit: int = Field(30, ge=1, le=200)
    exclude_seen: bool = True
    include_digest: bool = False


class EventIn(BaseModel):
    story_id: str
    type: EventType
    platform: str | None = None


class BriefIn(BaseModel):
    platform: Platform
    profile_id: str | None = None
    profile: ProfileIn | None = None
    angle: str | None = None


class SourceIn(BaseModel):
    id: str
    name: str | None = None
    homepage: str | None = None
    sitemap_url: str | None = None
    rss_url: str | None = None
    listing_url: str | None = None
    link_pattern: str | None = None
    region: str | None = None
    language: str | None = "en"
    default_category: str | None = None
    interval_min: int | None = None
    enabled: bool = True
    autodiscover: bool = True


class SourcePatch(BaseModel):
    name: str | None = None
    homepage: str | None = None
    sitemap_url: str | None = None
    rss_url: str | None = None
    listing_url: str | None = None
    link_pattern: str | None = None
    region: str | None = None
    language: str | None = None
    default_category: str | None = None
    interval_min: int | None = None
    enabled: bool | None = None
    autodiscover: bool | None = None


class SitemapCheckIn(BaseModel):
    sitemap_url: str = Field(..., description="News sitemap URL (best), a standard sitemap or sitemap index, or an RSS feed")


class SitemapIn(SitemapCheckIn):
    name: str | None = Field(None, description="Display name, e.g. 'Al Jazeera'. Default: from the domain")
    id: str | None = Field(None, description="Lowercase slug. Default: from the domain")
    region: str | None = Field(None, description="Home region from the taxonomy, e.g. 'Europe'")
    default_category: str | None = Field(None, description="Taxonomy category for stories the tagger can't place")
    rss_url: str | None = Field(None, description="Optional RSS feed of the same site (adds summaries and images)")
    homepage: str | None = None
    interval_min: int | None = Field(None, ge=5, le=1440, description="How often to check, in minutes (default 20)")


class WebhookIn(BaseModel):
    url: str
    events: list[str] | None = None


# ------------------------------------------------------------------ meta
@router.get("/", tags=["meta"])
def root():
    return {"name": "news-engine", "version": "1.0.0", "docs": "/docs", "mcp": "/mcp", "api": "/v1"}


@router.get("/health", tags=["meta"])
def health():
    s = service.status()
    ok = s["worker"]["alive"]
    # booleans only: says whether each process sees an AI key, never anything about the key itself
    return JSONResponse({"ok": ok, "worker_alive": ok, "last_heartbeat": s["worker"]["last_heartbeat"],
                         "api_llm_enabled": llm.available(), "worker_llm_enabled": s["worker"].get("llm_enabled"),
                         "llm_paused": llm.paused()},
                        status_code=200 if ok else 503)


@router.get("/v1/whoami", tags=["meta"])
def whoami():
    return {"scope": scope_var.get()}


@router.get("/v1/status", tags=["engine"])
def status():
    return service.status()


@router.get("/v1/taxonomy", tags=["engine"])
def taxonomy():
    return service.taxonomy()


# ------------------------------------------------------------------ stories
@router.get("/v1/stories", tags=["stories"])
def list_stories(category: list[str] = Query([]), news_type: list[str] = Query([]), audience: list[str] = Query([]),
                 region: list[str] = Query([]), tone: list[str] = Query([]), shelf_life: list[str] = Query([]),
                 source_id: str | None = None, q: str | None = None, since_hours: int = Query(72, ge=1, le=720),
                 min_sources: int = 1, min_postability: int = 0, sort: Literal["latest", "trending", "postability"] = "latest",
                 include_untagged: bool = True, include_roundups: bool = False, include_digest: bool = False,
                 limit: int = Query(30, ge=1, le=200), offset: int = Query(0, ge=0)):
    return service.list_stories(category=category, news_type=news_type, audience=audience, region=region, tone=tone,
                                shelf_life=shelf_life, source_id=source_id, q=q, since_hours=since_hours,
                                min_sources=min_sources, min_postability=min_postability, sort=sort,
                                include_untagged=include_untagged, include_roundups=include_roundups,
                                include_digest=include_digest, limit=limit, offset=offset)


@router.get("/v1/stories/trending", tags=["stories"])
def trending(hours: int = Query(24, ge=1, le=168), limit: int = Query(20, ge=1, le=100), category: list[str] = Query([])):
    return service.trending(hours=hours, limit=limit, category=category)


@router.get("/v1/search", tags=["stories"])
def search(q: str = Query(..., min_length=2), since_hours: int = Query(168, ge=1, le=720), limit: int = Query(20, ge=1, le=100),
           category: list[str] = Query([])):
    return service.list_stories(q=q, since_hours=since_hours, limit=limit, category=category)


@router.get("/v1/stories/{story_id}", tags=["stories"])
def get_story(story_id: str):
    return service.get_story(story_id)


@router.get("/v1/stories/{story_id}/digest", tags=["digests"])
async def get_digest(story_id: str, generate: bool = True):
    """The engine-written 250-500 word account. Generated now if missing (takes ~10-30s) when generate=true."""
    current = service.get_digest(story_id)
    if generate and current["status"] in ("not_ready", "updating"):
        await digest.generate_on_demand(runtime.get_fetcher(), story_id)
        current = service.get_digest(story_id)
    return current


@router.post("/v1/stories/{story_id}/digest", tags=["digests"], dependencies=[Depends(admin)])
async def regenerate_digest(story_id: str):
    """Write the digest again from the latest material."""
    service.get_story(story_id)
    with db.get_engine().begin() as c:
        c.execute(db.stories.update().where(db.stories.c.id == story_id).values(digest_attempts=0))
    await digest.generate(runtime.get_fetcher(), story_id)
    return service.get_digest(story_id)


@router.get("/thumbnails/{story_id}.png", tags=["thumbnails"], response_class=Response,
            responses={200: {"content": {"image/png": {}}}})
async def get_thumbnail(story_id: str, request: Request, size: Literal["wide", "square"] = "wide"):
    """Original card image for a story: wide 1200x630 or square 1080x1080. Public (no key), cacheable."""
    story = service.story_for_thumbnail(story_id)
    tag = thumbnail.etag(story, size)
    headers = {"ETag": tag, "Cache-Control": "public, max-age=3600"}
    if request.headers.get("if-none-match") == tag:
        return Response(status_code=304, headers=headers)
    png = await anyio.to_thread.run_sync(thumbnail.render_cached, story, size)
    return Response(png, media_type="image/png", headers=headers)


@router.post("/v1/stories/{story_id}/brief", tags=["posts"])
def brief(story_id: str, body: BriefIn):
    return service.post_brief(story_id, body.platform, body.profile_id, body.profile.model_dump() if body.profile else None, body.angle)


@router.post("/v1/stories/{story_id}/generate", tags=["posts"])
def generate(story_id: str, body: BriefIn):
    return service.generate_post(story_id, body.platform, body.profile_id, body.profile.model_dump() if body.profile else None, body.angle)


@router.get("/v1/platforms", tags=["posts"])
def platforms():
    return {k: v["label"] for k, v in FORMATS.items()}


@router.post("/v1/admin/requeue-failed-tags", tags=["admin"], dependencies=[Depends(admin)])
def requeue_failed_tags():
    """Give stories whose AI tagging gave up a fresh set of attempts (e.g. after topping up API credits)."""
    return service.requeue_failed_tags()


@router.post("/v1/stories/{story_id}/retag", tags=["admin"], dependencies=[Depends(admin)])
def retag(story_id: str):
    return service.retag_story(story_id)


# ------------------------------------------------------------------ feed + profiles
@router.post("/v1/feed", tags=["feed"])
def feed(body: FeedIn):
    return service.feed(profile_id=body.profile_id, profile=body.profile.model_dump() if body.profile else None,
                        filters=body.filters, since_hours=body.since_hours, limit=body.limit, exclude_seen=body.exclude_seen,
                        include_digest=body.include_digest)


@router.get("/v1/profiles/{profile_id}/feed", tags=["feed"])
def profile_feed(profile_id: str, category: list[str] = Query([]), region: list[str] = Query([]),
                 since_hours: int = Query(72, ge=1, le=720), limit: int = Query(30, ge=1, le=200),
                 include_digest: bool = False):
    return service.feed(profile_id=profile_id, filters={"category": category, "region": region},
                        since_hours=since_hours, limit=limit, include_digest=include_digest)


@router.put("/v1/profiles/{profile_id}", tags=["feed"])
def put_profile(profile_id: str, body: ProfileIn):
    return service.upsert_profile(profile_id, body.model_dump())


@router.get("/v1/profiles/{profile_id}", tags=["feed"])
def get_profile(profile_id: str):
    return service.get_profile(profile_id)


@router.delete("/v1/profiles/{profile_id}", tags=["feed"])
def delete_profile(profile_id: str):
    return service.delete_profile(profile_id)


@router.post("/v1/profiles/{profile_id}/events", tags=["feed"])
def event(profile_id: str, body: EventIn):
    return service.record_event(profile_id, body.story_id, body.type, body.platform)


# ------------------------------------------------------------------ sources
@router.get("/v1/sources", tags=["sources"])
def list_sources():
    return service.list_sources()


@router.post("/v1/sources", tags=["sources"], status_code=201, dependencies=[Depends(admin)])
def create_source(body: SourceIn):
    return service.create_source(body.model_dump(exclude_none=True))


@router.get("/v1/sources/{source_id}", tags=["sources"])
def get_source(source_id: str):
    return service.get_source(source_id)


@router.patch("/v1/sources/{source_id}", tags=["sources"], dependencies=[Depends(admin)])
def patch_source(source_id: str, body: SourcePatch):
    return service.update_source(source_id, body.model_dump(exclude_none=True))


@router.delete("/v1/sources/{source_id}", tags=["sources"], dependencies=[Depends(admin)])
def delete_source(source_id: str, delete_articles: bool = False):
    return service.delete_source(source_id, delete_articles)


@router.post("/v1/sources/{source_id}/schedule", tags=["sources"], dependencies=[Depends(admin)])
def schedule_source(source_id: str):
    return service.schedule_source_now(source_id)


@router.post("/v1/sources/{source_id}/ingest", tags=["sources"], dependencies=[Depends(admin)])
async def ingest_now(source_id: str):
    try:
        return await ingest.ingest_source(runtime.get_fetcher(), source_id)
    except KeyError:
        raise HTTPException(404, f"source {source_id} not found")


# ------------------------------------------------------------------ sitemaps (add a source from a sitemap URL)
@router.post("/v1/sitemaps/check", tags=["sitemaps"], dependencies=[Depends(admin)])
async def check_sitemap(body: SitemapCheckIn):
    """Dry run: fetch the sitemap and report whether the engine can use it. Saves nothing."""
    return await ingest.probe_sitemap(runtime.get_fetcher(), body.sitemap_url.strip())


@router.post("/v1/sitemaps", tags=["sitemaps"], status_code=201, dependencies=[Depends(admin)])
async def add_sitemap(body: SitemapIn):
    """Check the sitemap, create a source for it, and run its first ingest right away."""
    return await ingest.add_sitemap(runtime.get_fetcher(), body.model_dump(exclude_none=True))


# ------------------------------------------------------------------ articles
@router.get("/v1/articles", tags=["articles"])
def list_articles(source_id: str | None = None, story_id: str | None = None, since_hours: int = Query(48, ge=1, le=720),
                  limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    return service.list_articles(source_id, story_id, since_hours, limit, offset)


# ------------------------------------------------------------------ webhooks
@router.get("/v1/webhooks", tags=["webhooks"], dependencies=[Depends(admin)])
def list_webhooks():
    return service.list_webhooks()


@router.post("/v1/webhooks", tags=["webhooks"], status_code=201, dependencies=[Depends(admin)])
def create_webhook(body: WebhookIn):
    return service.create_webhook(body.url, body.events)


@router.delete("/v1/webhooks/{webhook_id}", tags=["webhooks"], dependencies=[Depends(admin)])
def delete_webhook(webhook_id: str):
    return service.delete_webhook(webhook_id)


@router.post("/v1/webhooks/{webhook_id}/test", tags=["webhooks"], dependencies=[Depends(admin)])
def test_webhook(webhook_id: str):
    return service.test_webhook(webhook_id)


@router.get("/v1/webhooks/deliveries", tags=["webhooks"], dependencies=[Depends(admin)])
def deliveries(webhook_id: str | None = None, limit: int = Query(50, ge=1, le=500)):
    return service.list_deliveries(webhook_id, limit)


# ------------------------------------------------------------------ admin / ops
@router.get("/v1/runs", tags=["admin"])
def runs(kind: str | None = None, limit: int = Query(50, ge=1, le=500)):
    return service.list_runs(kind, limit)


@router.post("/v1/admin/cycle", tags=["admin"], dependencies=[Depends(admin)])
async def run_cycle(all_sources: bool = False):
    """Run one worker cycle in this process now (the background worker keeps running independently)."""
    return await worker.cycle(runtime.get_fetcher(), force_all=all_sources)


@router.post("/v1/admin/enrich", tags=["admin"], dependencies=[Depends(admin)])
async def run_enrich(limit: int = Query(25, ge=1, le=200)):
    return await ingest.enrich_pending(runtime.get_fetcher(), limit)


@router.post("/v1/admin/tag", tags=["admin"], dependencies=[Depends(admin)])
async def run_tag(limit: int = Query(10, ge=1, le=100)):
    return await ingest.tag_pending(limit)


@router.post("/v1/admin/retention", tags=["admin"], dependencies=[Depends(admin)])
def run_retention():
    return ingest.retention()


app = create_app()
