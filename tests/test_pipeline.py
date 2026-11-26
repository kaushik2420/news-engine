from sqlalchemy import func, select

from engine import db, ingest, service, worker
from tests.conftest import full_cycle, run


def _stories_by_headline():
    return {s["headline"]: s for s in service.list_stories(since_hours=72, limit=200, include_roundups=True)["items"]}


def test_full_cycle_sitemap_first(engine_env):
    report = full_cycle(engine_env["fetcher"])
    by_src = {r["source"]: r for r in report["sources"]}
    assert by_src["inc42"]["sitemap"] == 11 and by_src["inc42"]["rss"] == 10      # sitemap sees more than RSS
    assert by_src["yourstory"]["discovered"] == {"rss_url": "https://yourstory.com/feed",
                                                 "sitemap_url": "https://yourstory.com/sitemap_index.xml"}
    with db.get_engine().connect() as c:
        assert c.execute(select(func.count()).select_from(db.articles)).scalar() == 35
        assert c.execute(select(func.count()).select_from(db.articles).where(db.articles.c.story_id.is_(None))).scalar() == 0
        payu = c.execute(select(db.articles).where(db.articles.c.title.like("PayU%"))).first()
        assert payu.discovered_via == "sitemap"                                   # only the sitemap had it
        ys_enriched = c.execute(select(func.count()).select_from(db.articles).where(
            db.articles.c.source_id == "yourstory", db.articles.c.discovered_via == "sitemap")).scalar()
        assert ys_enriched == 4                                                   # titles came from page enrichment


def test_clustering_merges_right_things(engine_env):
    full_cycle(engine_env["fetcher"])
    stories = _stories_by_headline()
    multi = {h: s for h, s in stories.items() if s["source_count"] > 1}
    assert len(multi) == 2
    assert any("WhatsApp" in h for h in multi) and any("Krishnamurthy" in h for h in multi)
    assert sum("Jensen Huang" in h for h in stories) == 2                         # different events stay apart
    roundup = next(s for h, s in stories.items() if h.endswith("& More"))
    assert roundup["is_roundup"] and roundup["article_count"] == 1                # digests never absorb stories
    assert all(s["tags"].get("category") for s in stories.values())


def test_second_cycle_is_cheap_and_ids_stable(engine_env):
    full_cycle(engine_env["fetcher"])
    ids_before = {s["id"] for s in _stories_by_headline().values()}
    engine_env["log"].clear()
    report = run(worker.cycle(engine_env["fetcher"], force_all=True))
    assert sum(r.get("new_articles", 0) for r in report["sources"]) == 0
    assert {s["id"] for s in _stories_by_headline().values()} == ids_before
    # ETag/304: feeds were requested conditionally and nothing was re-parsed
    assert sum(r["sitemap"] + r["rss"] for r in report["sources"]) == 0


def test_failing_source_backs_off(engine_env):
    full_cycle(engine_env["fetcher"])
    service.create_source({"id": "broken", "name": "Broken", "sitemap_url": "https://broken.example/news.xml"})
    out = run(ingest.ingest_source(engine_env["fetcher"], "broken"))
    assert out.get("sitemap") == 0
    src = service.get_source("broken")
    assert src["health"] in ("ok", "degraded")       # 404 sitemap = empty, not a crash
    service.create_source({"id": "nochannel", "name": "No channel", "homepage": "https://nowhere.example/"})
    run(ingest.ingest_source(engine_env["fetcher"], "nochannel"))
    s = service.get_source("nochannel")
    assert s["consecutive_failures"] == 1 and s["last_error"] and s["next_check_at"] > s["last_checked_at"]


def test_robots_disallow_is_respected(engine_env):
    from engine.fetch import Fetcher
    from tests.fixtures_transport import transport
    f = Fetcher(transport=transport(extra={"https://blocked.example/robots.txt": ("text/plain", "User-agent: *\nDisallow: /")}),
                per_domain_interval=0)
    r = run(f.get("https://blocked.example/news"))
    assert r.status == 451 and not r.ok


def test_retention_runs(engine_env):
    full_cycle(engine_env["fetcher"])
    out = ingest.retention()
    assert set(out) >= {"bodies_cleared", "stories_deleted"}


def test_autodiscovery_tries_common_feed_paths_and_retries(engine_env):
    """Live YourStory case: homepage has no <link rel=alternate>, but /feed exists."""
    from engine.fetch import Fetcher
    from scripts.build_fixtures import rss
    from tests.fixtures_transport import RAW, transport
    pages = {"https://nolink.example/": ("text/html", "<html><head></head><body>no feed link</body></html>")}
    f = Fetcher(transport=transport(extra=pages), per_domain_interval=0)
    service.create_source({"id": "nolink", "name": "No link", "homepage": "https://nolink.example/"})
    run(ingest.ingest_source(f, "nolink"))
    s = service.get_source("nolink")
    assert s["consecutive_failures"] == 1 and s["rss_url"] is None
    with db.get_engine().connect() as c:       # nothing found -> discovery NOT marked done, so it retries
        assert c.execute(select(db.sources.c.discovered_at).where(db.sources.c.id == "nolink")).scalar() is None

    with db.get_engine().begin() as c:         # simulate a row stuck by older code: marked discovered, no channel
        c.execute(db.sources.update().where(db.sources.c.id == "nolink").values(discovered_at=db.utcnow()))
    pages["https://nolink.example/feed"] = ("application/rss+xml", rss("nolink", RAW["yourstory"][:3]))
    f2 = Fetcher(transport=transport(extra=pages), per_domain_interval=0)
    out = run(ingest.ingest_source(f2, "nolink"))
    assert out["discovered"]["rss_url"] == "https://nolink.example/feed" and out["rss"] == 3
    assert service.get_source("nolink")["consecutive_failures"] == 0
