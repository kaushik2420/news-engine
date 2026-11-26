"""An offline copy of the web for tests and demos.

Serves robots.txt, news sitemaps, a sitemap index, RSS feeds, a homepage and article pages built from
tests/fixtures/raw_items.json (real headlines captured 2026-11-26). Lets the whole engine run without network.
"""
from __future__ import annotations

import json
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.sax.saxutils import escape

import httpx

FIX = Path(__file__).resolve().parent / "fixtures"
RAW = {k: v for k, v in json.loads((FIX / "raw_items.json").read_text()).items() if not k.startswith("_")}

# Articles that appear ONLY in the news sitemap (not in RSS): shows why the sitemap is the primary channel.
SITEMAP_ONLY = {
    "inc42": [{"title": "PayU India Turns Adjusted EBITDA Positive Ahead Of Listing Plans",
               "link": "https://inc42.com/buzz/payu-india-turns-adjusted-ebitda-positive/",
               "date": "Thu, 26 Nov 2026 01:10:00 +0000",
               "summary": "PayU's India business reported positive adjusted EBITDA for the first time."}],
}


def _iso(rfc: str) -> str:
    return parsedate_to_datetime(rfc).isoformat()


def news_sitemap(items) -> str:
    urls = "".join(f"""<url><loc>{escape(i['link'])}</loc><news:news><news:publication><news:name>X</news:name>
<news:language>en</news:language></news:publication><news:publication_date>{_iso(i['date'])}</news:publication_date>
<news:title>{escape(i['title'])}</news:title></news:news></url>""" for i in items)
    return ('<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
            f'xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">{urls}</urlset>')


def plain_sitemap(items) -> str:
    urls = "".join(f"<url><loc>{escape(i['link'])}</loc><lastmod>{_iso(i['date'])}</lastmod></url>" for i in items)
    return f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'


def article_page(i) -> str:
    return f"""<html><head><title>{escape(i['title'])}</title>
<meta property="og:title" content="{escape(i['title'])}">
<meta property="og:description" content="{escape(i['summary'])}">
<meta property="og:image" content="https://img.example.com/{abs(hash(i['link'])) % 10000}.jpg">
<meta property="article:published_time" content="{_iso(i['date'])}">
</head><body><article><h1>{escape(i['title'])}</h1><p>{escape(i['summary'])}</p>
<p>{escape(i['summary'])} This paragraph stands in for the full article body used for tagging.</p></article></body></html>"""


def build_routes() -> dict[str, tuple[str, str]]:
    from scripts.build_fixtures import rss  # noqa: E402  (shared RSS builder)
    r: dict[str, tuple[str, str]] = {}
    xml, html, txt = "application/xml", "text/html; charset=utf-8", "text/plain"
    r["https://inc42.com/robots.txt"] = (txt, "User-agent: *\nDisallow: /wp-admin/\nSitemap: https://inc42.com/sitemap_index.xml\nSitemap: https://inc42.com/news-sitemap.xml\n")
    r["https://techcrunch.com/robots.txt"] = (txt, "User-agent: *\nDisallow: /wp-admin/\nSitemap: https://techcrunch.com/sitemap.xml\nSitemap: https://techcrunch.com/news-sitemap.xml\n")
    r["https://yourstory.com/robots.txt"] = (txt, "User-agent: *\nDisallow: /api/*\nSitemap: https://yourstory.com/sitemap_index.xml\n")
    r["https://inc42.com/news-sitemap.xml"] = (xml, news_sitemap(RAW["inc42"] + SITEMAP_ONLY["inc42"]))
    r["https://techcrunch.com/news-sitemap.xml"] = (xml, news_sitemap(RAW["techcrunch"]))
    r["https://inc42.com/feed/"] = (xml, rss("inc42", RAW["inc42"]))
    r["https://techcrunch.com/feed/"] = (xml, rss("techcrunch", RAW["techcrunch"]))
    # YourStory: homepage advertises RSS; robots points at a sitemap INDEX whose newest child is a plain sitemap
    r["https://yourstory.com/"] = (html, '<html><head><link rel="alternate" type="application/rss+xml" href="/feed"></head><body>YS</body></html>')
    r["https://yourstory.com/feed"] = (xml, rss("yourstory", RAW["yourstory"][:6]))   # feed shows only 6 newest
    r["https://yourstory.com/sitemap_index.xml"] = (xml, """<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<sitemap><loc>https://yourstory.com/sitemap-tags.xml</loc><lastmod>2026-11-26T00:00:00+00:00</lastmod></sitemap>
<sitemap><loc>https://yourstory.com/sitemap-posts-2026-10.xml</loc><lastmod>2026-10-31T00:00:00+00:00</lastmod></sitemap>
<sitemap><loc>https://yourstory.com/sitemap-posts-2026-11.xml</loc><lastmod>2026-11-25T14:40:00+00:00</lastmod></sitemap>
</sitemapindex>""")
    r["https://yourstory.com/sitemap-posts-2026-11.xml"] = (xml, plain_sitemap(RAW["yourstory"]))
    r["https://yourstory.com/sitemap-posts-2026-10.xml"] = (xml, plain_sitemap([]))
    for src, items in list(RAW.items()) + list(SITEMAP_ONLY.items()):
        for i in items:
            r[i["link"].rstrip("/")] = (html, article_page(i))
    return r


def transport(extra: dict | None = None, log: list | None = None) -> httpx.MockTransport:
    routes = build_routes()
    if extra:
        routes.update(extra)

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if log is not None:
            log.append((req.method, url))
        hit = routes.get(url) or routes.get(url.rstrip("/"))
        if hit is None:
            return httpx.Response(404, text="not found")
        ctype, body = hit
        etag = f'"{abs(hash(body)) % 10**8}"'
        if req.headers.get("If-None-Match") == etag:
            return httpx.Response(304)
        return httpx.Response(200, text=body, headers={"Content-Type": ctype, "ETag": etag})
    return httpx.MockTransport(handler)
