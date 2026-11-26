from engine.cluster import is_roundup
from engine.discover import feeds_in_html, from_listing, from_rss, parse_sitemap, rank_sitemap_children
from engine.text import canonical_url


def test_canonical_url():
    assert canonical_url("http://WWW.X.com/a/?utm_source=t&id=3#top") == "https://x.com/a?id=3"


def test_news_sitemap_parsing():
    xml = b"""<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"
      xmlns:news="http://www.google.com/schemas/sitemap-news/0.9"><url><loc>https://n.com/a</loc>
      <news:news><news:publication_date>2026-11-25T10:00:00+05:30</news:publication_date>
      <news:title>Startup raises $5 Mn</news:title></news:news></url></urlset>"""
    kind, items = parse_sitemap(xml)
    assert kind == "urlset" and items[0].title == "Startup raises $5 Mn"
    assert items[0].published.hour == 4 and items[0].published.tzinfo is None    # normalised to naive UTC


def test_sitemap_index_prefers_news_then_newest_posts():
    kids = [("https://y.com/sitemap-tags.xml", None), ("https://y.com/posts-2026-10.xml", None),
            ("https://y.com/posts-2026-11.xml", None)]
    from datetime import datetime
    kids = [(u, datetime(2026, 11, 25) if "11" in u else datetime(2026, 10, 1)) for u, _ in kids]
    assert rank_sitemap_children(kids, 1) == ["https://y.com/posts-2026-11.xml"]
    assert rank_sitemap_children(kids + [("https://y.com/news-sitemap.xml", None)], 1) == ["https://y.com/news-sitemap.xml"]


def test_invalid_xml_is_tolerated():
    assert parse_sitemap(b"<html>not xml")[0] == "invalid"


def test_feed_autodiscovery():
    html = b'<link rel="alternate" type="application/rss+xml" href="/feed"><link rel="alternate" type="application/rss+xml" href="/comments/feed">'
    assert feeds_in_html(html, "https://y.com/") == ["https://y.com/feed"]


def test_rss_parsing():
    xml = b"""<rss version="2.0"><channel><item><title>A &amp; B</title><link>https://n.com/a</link>
    <pubDate>Wed, 25 Nov 2026 14:00:00 +0530</pubDate><description>&lt;p&gt;Hi&lt;/p&gt;</description></item></channel></rss>"""
    c = from_rss(xml)[0]
    assert c.title == "A & B" and c.summary == "Hi" and c.published.hour == 8


def test_listing_same_host_and_pattern_only():
    html = b'<a href="/news/big-story-about-something-long">A long enough headline for a news story</a><a href="https://other.com/news/x">X</a><a href="/about">About</a>'
    got = from_listing(html, "https://n.com/news/", r"/news/[a-z0-9-]+$")
    assert [c.url for c in got] == ["https://n.com/news/big-story-about-something-long"]


def test_roundup_detection():
    assert is_roundup("Flipkart CEO Ousted, PhonePe Revives IPO & More")
    assert is_roundup("Funding Galore: Indian Startups Raised $200 Mn This Week")
    assert not is_roundup("Vegapay Bags $10 Mn To Scale Credit Line On UPI Infrastructure")
