import gzip
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text

from engine import db, ingest, service
from engine.fetch import Fetcher
from tests.conftest import ADMIN, full_cycle, run
from tests.fixtures_transport import news_sitemap, plain_sitemap, transport

A = {"Authorization": f"Bearer {ADMIN}"}
WORLD = [  # fresh relative to the frozen clock (26 Nov 2026 13:15 UTC)
    {"title": "Floods force thousands from homes in eastern Spain", "link": "https://world.example/news/spain-floods",
     "date": "Thu, 26 Nov 2026 09:00:00 +0000", "summary": "Heavy rain flooded towns around Valencia."},
    {"title": "India beat Australia by six wickets to clinch Test series", "link": "https://world.example/sport/ind-aus",
     "date": "Thu, 26 Nov 2026 08:00:00 +0000", "summary": "India won the third Test in Adelaide."},
    {"title": "Fed holds interest rates steady as inflation cools", "link": "https://world.example/business/fed",
     "date": "Thu, 26 Nov 2026 07:00:00 +0000", "summary": "The US central bank kept rates unchanged."},
]
OLD = [{"title": "An old story", "link": "https://archive.example/2019/old", "date": "Mon, 01 Jul 2019 10:00:00 +0000",
        "summary": "x"}]


def web(extra=None):
    from scripts.build_fixtures import rss
    routes = {
        "https://world.example/news-sitemap.xml": ("application/xml", news_sitemap(WORLD)),
        "https://world.example/feed": ("application/rss+xml", rss("world", WORLD)),
        "https://archive.example/sitemap.xml": ("application/xml", plain_sitemap(OLD)),
        "https://blocked.example/robots.txt": ("text/plain", "User-agent: *\nDisallow: /"),
        "https://blocked.example/news.xml": ("application/xml", news_sitemap(WORLD)),
        "https://idx.example/sitemap_index.xml": ("application/xml", """<?xml version="1.0"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://idx.example/news-1.xml</loc></sitemap><sitemap><loc>https://idx.example/tags.xml</loc></sitemap>
</sitemapindex>"""),
        "https://idx.example/news-1.xml": ("application/xml", news_sitemap(WORLD[:2])),
        "https://notasitemap.example/page": ("text/html", "<html><body>hello</body></html>"),
    }
    routes.update(extra or {})
    return Fetcher(transport=transport(extra=routes), per_domain_interval=0)


@pytest.fixture()
def public_ok(monkeypatch):
    """Fixture hosts don't resolve offline; the guard itself is tested separately."""
    import engine.netguard as ng
    real = ng.assert_public_url
    monkeypatch.setattr(ng, "assert_public_url", lambda url: None)
    return real


def test_gzip_sitemaps_are_read():
    from engine.discover import is_news_sitemap, parse_sitemap
    raw = news_sitemap(WORLD).encode()
    kind, items = parse_sitemap(gzip.compress(raw))
    assert kind == "urlset" and len(items) == 3 and is_news_sitemap(gzip.compress(raw))


def test_check_reports_kinds_and_problems(engine_env, public_ok):
    f = web()
    news = run(ingest.probe_sitemap(f, "https://world.example/news-sitemap.xml"))
    assert news["ok"] and news["kind"] == "news" and news["fresh_articles"] == 3 and news["has_titles"]
    assert news["sample"][0]["title"].startswith("Floods")
    idx = run(ingest.probe_sitemap(f, "https://idx.example/sitemap_index.xml"))
    assert idx["ok"] and idx["kind"] == "index (news)" and idx["children_checked"] == ["https://idx.example/news-1.xml"]
    feed = run(ingest.probe_sitemap(f, "https://world.example/feed"))
    assert feed["ok"] and feed["kind"] == "rss" and feed["warnings"]
    old = run(ingest.probe_sitemap(f, "https://archive.example/sitemap.xml"))
    assert not old["ok"] and "archive" in old["problems"][0]
    blocked = run(ingest.probe_sitemap(f, "https://blocked.example/news.xml"))
    assert not blocked["ok"] and "robots" in blocked["problems"][0]
    page = run(ingest.probe_sitemap(f, "https://notasitemap.example/page"))
    assert not page["ok"] and "not a sitemap" in page["problems"][0]
    missing = run(ingest.probe_sitemap(f, "https://world.example/nope.xml"))
    assert not missing["ok"] and "404" in missing["problems"][0]


def test_guard_refuses_internal_urls(public_ok):
    from engine.netguard import UnsafeURL
    for url in ("http://localhost/x", "http://127.0.0.1/x", "http://10.0.0.5/x", "http://169.254.169.254/latest",
                "ftp://example.com/x", "https:///nohost"):
        with pytest.raises(UnsafeURL):
            public_ok(url)


def test_add_sitemap_creates_source_and_ingests(engine_env, public_ok):
    f = web()
    out = run(ingest.add_sitemap(f, {"sitemap_url": "https://world.example/news-sitemap.xml", "name": "World Example",
                                     "region": "Europe", "default_category": "World"}))
    src = out["source"]
    assert src["id"] == "world" and src["sitemap_url"] == "https://world.example/news-sitemap.xml"
    assert out["first_ingest"]["new_articles"] == 3 and out["first_ingest"]["new_stories"] == 3
    with pytest.raises(service.Invalid, match="already used"):
        run(ingest.add_sitemap(f, {"sitemap_url": "https://world.example/news-sitemap.xml"}))
    with pytest.raises(service.Invalid, match="archive"):
        run(ingest.add_sitemap(f, {"sitemap_url": "https://archive.example/sitemap.xml"}))
    # tagged with the general-news labels, findable by SQL filters
    run(ingest.tag_pending(50))
    sports = service.list_stories(category=["Sports"], since_hours=72)
    assert [s["headline"] for s in sports["items"]] == ["India beat Australia by six wickets to clinch Test series"]
    assert sports["items"][0]["tags"]["region"][:2] == ["India", "Australia"]
    assert service.list_stories(source_id="world", since_hours=72)["total"] == 3
    assert service.list_stories(q="interest rates", since_hours=72)["total"] == 1


def test_sitemap_routes_and_mcp(engine_env, public_ok):
    from engine import runtime
    runtime.set_fetcher(web())
    from api.app import create_app
    with TestClient(create_app()) as c:
        r = c.post("/v1/sitemaps/check", headers=A, json={"sitemap_url": "https://world.example/news-sitemap.xml"})
        assert r.status_code == 200 and r.json()["ok"]
        assert c.post("/v1/sitemaps/check", json={"sitemap_url": "x"}).status_code == 401
        bad = c.post("/v1/sitemaps", headers=A, json={"sitemap_url": "https://archive.example/sitemap.xml"})
        assert bad.status_code == 400 and "archive" in bad.json()["detail"]
        ok = c.post("/v1/sitemaps", headers=A, json={"sitemap_url": "https://idx.example/sitemap_index.xml",
                                                     "name": "Index Example", "region": "UK"})
        assert ok.status_code == 201 and ok.json()["source"]["region"] == "UK"
        h = {**A, "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        call = c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "check_sitemap", "arguments": {"sitemap_url": "https://world.example/feed"}}})
        assert json.loads(call.json()["result"]["content"][0]["text"])["kind"] == "rss"


def test_migration_upgrades_a_v1_database(tmp_path, monkeypatch):
    """A database created before the tag columns existed gets them, backfilled, on start."""
    url = f"sqlite:///{tmp_path / 'old.db'}"
    old = create_engine(url)
    with old.begin() as c:
        c.execute(text("CREATE TABLE stories (id VARCHAR(32) PRIMARY KEY, headline TEXT NOT NULL, summary TEXT, "
                       "published_at DATETIME, updated_at DATETIME, source_count INTEGER, article_count INTEGER, "
                       "tokens JSON, tag_status VARCHAR(16), tag_attempts INTEGER, tags JSON, postability INTEGER, "
                       "trending_notified BOOLEAN, created_at DATETIME)"))
        c.execute(text("INSERT INTO stories (id, headline, tags, source_count, article_count, tag_status) VALUES "
                       "('s1', 'Vegapay raises $10 Mn', :t, 1, 1, 'rules')"),
                  {"t": json.dumps({"category": ["Funding", "Fintech"], "region": ["India"], "entities": ["Vegapay"]})})
    old.dispose()
    monkeypatch.setenv("DATABASE_URL", url)
    from engine import settings
    settings.reload()
    db.reset_engine()
    eng = db.get_engine()
    cols = {c["name"] for c in inspect(eng).get_columns("stories")}
    assert {"cat_text", "region_text", "search_text", "is_roundup"} <= cols
    with eng.connect() as c:
        row = c.execute(text("SELECT cat_text, region_text, search_text FROM stories WHERE id='s1'")).first()
    assert row.cat_text == "|Funding|Fintech|" and row.region_text == "|India|" and "vegapay" in row.search_text
    db.migrate(eng)                         # idempotent: running again changes nothing and doesn't fail
    db.reset_engine()


def test_category_filter_matches_whole_values_only(engine_env):
    full_cycle(engine_env["fetcher"])
    with db.get_engine().begin() as c:     # a story labelled "AI" must not match a filter for "A"
        sid = c.execute(text("SELECT id FROM stories LIMIT 1")).scalar()
        c.execute(db.stories.update().where(db.stories.c.id == sid).values(cat_text="|AI|"))
    ids = {s["id"] for s in service.list_stories(category=["A"], since_hours=72, limit=200)["items"]}
    assert sid not in ids
    assert sid in {s["id"] for s in service.list_stories(category=["AI"], since_hours=72, limit=200)["items"]}
