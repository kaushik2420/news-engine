"""MCP server: lets any Claude (Claude Code, Claude app connectors, Agent SDK) drive the engine.

Mounted at /mcp on the same service as the REST API (Streamable HTTP transport, stateless).
Every tool wraps a function in engine.service, so MCP and REST always agree.
"""
from __future__ import annotations

from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer

from engine import digest, ingest, llm, runtime, service

from .auth import require_admin

INSTRUCTIONS = """This is the News Engine: the backend brain of a social-content app. It continuously collects news
(news sitemaps + RSS + page crawling, no publisher APIs), merges duplicate coverage into stories, tags each story
with a fixed taxonomy, ranks personalised feeds, and turns stories into briefs for LinkedIn/X/Instagram posts.

Typical flows:
- Show a user's feed: upsert_profile once (their app user id), then get_feed(profile_id=...).
- Browse: list_stories / search_stories / get_trending with taxonomy filters (get_taxonomy lists valid values).
- Write a post: get_post_brief(story_id, platform, profile_id) then write the post from the brief yourself
  (or generate_post if the engine has an LLM key). Always attribute facts to the sources in the brief.
- Learn: record_event(profile_id, story_id, type) for click/save/posted/dismiss to personalise future feeds.
- Operate: engine_status, list_sources, add_source (homepage alone is enough), check_source_now, list_runs.
- Add a site by sitemap: check_sitemap(url) first, then add_sitemap(url, name, region, default_category).
Cards: show `thumbnail.wide` (or `.square`), `card_title` or `headline`, and `dek`. Reading view: the `digest`
(engine-written, 250-500 words; get it with get_story_digest) with its `sources` credited and linked.
`excerpt` and `image` are publisher material: short, attributed, optional.
"""

mcp = MCPServer(name="news-engine", title="News Engine", instructions=INSTRUCTIONS, version="1.0.0")


async def _run(fn, *args, **kwargs) -> Any:
    try:
        return await anyio.to_thread.run_sync(lambda: fn(*args, **kwargs))
    except (service.NotFound, service.Invalid, PermissionError) as e:
        return {"error": str(e)}


# ------------------------------------------------------------------ engine
@mcp.tool(description="Health of the engine: worker heartbeat, LLM on/off, counts, queue sizes, failing sources.")
async def engine_status() -> dict:
    return await _run(service.status)


@mcp.tool(description="Valid filter values: category, news_type, audience, region, tone, shelf_life, platforms, event_types.")
async def get_taxonomy() -> dict:
    return await _run(service.taxonomy)


# ------------------------------------------------------------------ stories
@mcp.tool(description="List recent stories with optional taxonomy filters. sort: latest | trending | postability. "
                      "Filters take lists, e.g. category=['AI','Fintech'].")
async def list_stories(category: list[str] | None = None, news_type: list[str] | None = None,
                       audience: list[str] | None = None, region: list[str] | None = None,
                       tone: list[str] | None = None, source_id: str | None = None, sort: str = "latest",
                       since_hours: int = 72, min_sources: int = 1, min_postability: int = 0,
                       limit: int = 20, offset: int = 0) -> dict:
    return await _run(service.list_stories, category=category, news_type=news_type, audience=audience, region=region,
                      tone=tone, source_id=source_id, sort=sort, since_hours=since_hours, min_sources=min_sources,
                      min_postability=min_postability, limit=min(limit, 100), offset=offset)


@mcp.tool(description="Keyword search across headlines, summaries and named entities of recent stories.")
async def search_stories(query: str, since_hours: int = 168, limit: int = 20, category: list[str] | None = None) -> dict:
    return await _run(service.list_stories, q=query, since_hours=since_hours, limit=min(limit, 100), category=category)


@mcp.tool(description="One story in full: tags, suggested angles, every source article with links.")
async def get_story(story_id: str) -> dict:
    return await _run(service.get_story, story_id)


@mcp.tool(description="The engine-written digest of a story (250-500 words, synthesised from every outlet, with "
                      "card_title, dek, key_points and source links) plus its thumbnail URLs. Generates it now if "
                      "missing (10-30s) unless generate=false. If digest is null, `reason` says why in plain words "
                      "(e.g. not written yet, AI paused for billing) - show it or fall back to the source links.")
async def get_story_digest(story_id: str, generate: bool = True) -> dict:
    try:
        current = await anyio.to_thread.run_sync(service.get_digest, story_id)
        if generate and current["status"] in ("not_ready", "updating"):
            await digest.generate_on_demand(runtime.get_fetcher(), story_id)
            current = await anyio.to_thread.run_sync(service.get_digest, story_id)
        return current
    except service.NotFound as e:
        return {"error": str(e)}


@mcp.tool(description="Stories covered by 2+ outlets in the last N hours, most-covered first.")
async def get_trending(hours: int = 24, limit: int = 15, category: list[str] | None = None) -> dict:
    return await _run(service.trending, hours=hours, limit=min(limit, 100), category=category)


# ------------------------------------------------------------------ feed + profiles
@mcp.tool(description="Personalised, ranked feed. Pass profile_id (stored profile, learns from events) or an inline "
                      "profile dict {role, niche, goal, tone, categories, audiences, regions, platforms}. "
                      "Each item has rank.score with its parts so you can explain why it ranks there.")
async def get_feed(profile_id: str | None = None, profile: dict | None = None, category: list[str] | None = None,
                   region: list[str] | None = None, since_hours: int = 72, limit: int = 20,
                   include_digest: bool = False) -> dict:
    filters = {"category": category, "region": region}
    return await _run(service.feed, profile_id=profile_id, profile=profile, filters=filters,
                      since_hours=since_hours, limit=min(limit, 100), include_digest=include_digest)


@mcp.tool(description="Create or update a user profile (profile_id = the app's user id). Values must come from get_taxonomy.")
async def upsert_profile(profile_id: str, role: str = "", niche: str = "", goal: str = "", tone: str = "",
                         categories: list[str] | None = None, audiences: list[str] | None = None,
                         regions: list[str] | None = None, platforms: list[str] | None = None) -> dict:
    data = {"role": role, "niche": niche, "goal": goal, "tone": tone, "categories": categories or [],
            "audiences": audiences or [], "regions": regions or [], "platforms": platforms or []}
    return await _run(service.upsert_profile, profile_id, data)


@mcp.tool(description="Read a stored profile and its learned category weights.")
async def get_profile(profile_id: str) -> dict:
    return await _run(service.get_profile, profile_id)


@mcp.tool(description="Record what a user did with a story so their feed learns. type: view | click | save | posted | dismiss.")
async def record_event(profile_id: str, story_id: str, type: str, platform: str | None = None) -> dict:
    return await _run(service.record_event, profile_id, story_id, type, platform)


# ------------------------------------------------------------------ posts
@mcp.tool(description="Everything needed to write a post about a story: system rules, a prompt with the user's profile, "
                      "story facts, angle and format spec, plus sources to attribute. platform: linkedin | x | "
                      "instagram_carousel | reel | story. Write the post yourself from this brief.")
async def get_post_brief(story_id: str, platform: str, profile_id: str | None = None, angle: str | None = None) -> dict:
    return await _run(service.post_brief, story_id, platform, profile_id=profile_id, angle=angle)


@mcp.tool(description="Have the engine write the draft itself (only if the engine has an LLM key; otherwise use get_post_brief).")
async def generate_post(story_id: str, platform: str, profile_id: str | None = None, angle: str | None = None) -> dict:
    return await _run(service.generate_post, story_id, platform, profile_id=profile_id, angle=angle)


# ------------------------------------------------------------------ sources + operations
@mcp.tool(description="All news sources with health (ok/degraded/failing/pending/disabled), last check, errors, counts.")
async def list_sources() -> list[dict] | dict:
    return await _run(service.list_sources)


@mcp.tool(description="One source with its last 10 ingest runs.")
async def get_source(source_id: str) -> dict:
    return await _run(service.get_source, source_id)


@mcp.tool(description="Add a news source (admin). A homepage alone is enough: the engine auto-discovers RSS and news "
                      "sitemaps. id is a lowercase slug. default_category must be a taxonomy category.")
async def add_source(id: str, name: str, homepage: str | None = None, sitemap_url: str | None = None,
                     rss_url: str | None = None, region: str | None = None, default_category: str | None = None,
                     interval_min: int = 20) -> dict:
    def go():
        require_admin()
        data = {k: v for k, v in dict(id=id, name=name, homepage=homepage, sitemap_url=sitemap_url, rss_url=rss_url,
                                      region=region, default_category=default_category, interval_min=interval_min).items() if v is not None}
        return service.create_source(data)
    return await _run(go)


@mcp.tool(description="Dry run (admin): fetch a sitemap URL and report if the engine can use it: robots permission, "
                      "kind (news/standard/index/rss), how many fresh articles, a sample, and any problems. Saves nothing.")
async def check_sitemap(sitemap_url: str) -> dict:
    try:
        require_admin()
        return await ingest.probe_sitemap(runtime.get_fetcher(), sitemap_url.strip())
    except PermissionError as e:
        return {"error": str(e)}


@mcp.tool(description="Add a news source from its sitemap URL (admin): checks it, creates the source and runs the first "
                      "ingest. Optional: name, region and default_category (taxonomy values), rss_url, interval_min.")
async def add_sitemap(sitemap_url: str, name: str | None = None, region: str | None = None,
                      default_category: str | None = None, rss_url: str | None = None,
                      interval_min: int | None = None) -> dict:
    try:
        require_admin()
        data = {k: v for k, v in dict(sitemap_url=sitemap_url, name=name, region=region, default_category=default_category,
                                      rss_url=rss_url, interval_min=interval_min).items() if v is not None}
        return await ingest.add_sitemap(runtime.get_fetcher(), data)
    except (PermissionError, service.Invalid) as e:
        return {"error": str(e)}


@mcp.tool(description="Change a source (admin): any of name, homepage, rss_url, sitemap_url, listing_url, link_pattern, "
                      "region, default_category, interval_min, enabled, autodiscover.")
async def update_source(source_id: str, changes: dict) -> dict:
    def go():
        require_admin()
        return service.update_source(source_id, changes)
    return await _run(go)


@mcp.tool(description="Admin: give stories whose AI tagging gave up a fresh set of attempts (e.g. after the engine's "
                      "Anthropic account was topped up). Returns how many were requeued.")
async def requeue_failed_tags() -> dict:
    try:
        require_admin()
        return await _run(service.requeue_failed_tags)
    except PermissionError as e:
        return {"error": str(e)}


@mcp.tool(description="Fetch a source right now and return what was found (admin). Useful right after add_source.")
async def check_source_now(source_id: str) -> dict:
    try:
        require_admin()
        return await ingest.ingest_source(runtime.get_fetcher(), source_id)
    except PermissionError as e:
        return {"error": str(e)}
    except KeyError:
        return {"error": f"source {source_id} not found"}


@mcp.tool(description="Raw articles the engine discovered recently (optionally for one source), newest first.")
async def list_recent_articles(source_id: str | None = None, since_hours: int = 24, limit: int = 30) -> dict:
    return await _run(service.list_articles, source_id=source_id, since_hours=since_hours, limit=min(limit, 200))


@mcp.tool(description="Recent engine work log. kind: source | enrich | tag | retention.")
async def list_runs(kind: str | None = None, limit: int = 20) -> list[dict] | dict:
    return await _run(service.list_runs, kind, min(limit, 200))
