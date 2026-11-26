"""Discovery: turn a source into candidate article URLs.

Priority:
  1. News sitemap  - every article from the last ~48h, exact publish times (Google News sitemap spec)
  2. RSS / Atom    - fills summaries and images for the newest items
  3. Listing page  - fallback for sites with neither
Plus auto-discovery: find RSS links in the homepage and sitemaps in robots.txt.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit
from xml.etree import ElementTree as ET

import feedparser

from .text import clean

NS_SM = "http://www.sitemaps.org/schemas/sitemap/0.9"
NS_NEWS = "http://www.google.com/schemas/sitemap-news/0.9"
NS_IMG = "http://www.google.com/schemas/sitemap-image/1.1"


@dataclass
class Candidate:
    url: str
    title: str = ""
    published: datetime | None = None    # naive UTC
    summary: str = ""
    image: str | None = None
    author: str | None = None
    keywords: str = ""
    via: str = "rss"


def parse_date(value) -> datetime | None:
    if not value:
        return None
    dt = None
    if isinstance(value, datetime):
        dt = value
    else:
        value = str(value).strip()
        try:
            dt = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            try:
                dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
    if dt.tzinfo:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


# ---- RSS / Atom ---------------------------------------------------------------
def from_rss(content: bytes) -> list[Candidate]:
    feed = feedparser.parse(content)
    out = []
    for e in feed.entries:
        link, title = e.get("link"), e.get("title")
        if not link or not title:
            continue
        image = None
        for key in ("media_content", "media_thumbnail"):
            if e.get(key):
                image = e[key][0].get("url")
                break
        if not image:
            for enc in e.get("enclosures", []):
                if str(enc.get("type", "")).startswith("image"):
                    image = enc.get("href")
                    break
        published = None
        for key in ("published_parsed", "updated_parsed"):
            if e.get(key):
                published = datetime(*e[key][:6])
                break
        out.append(Candidate(url=link, title=clean(title), published=published,
                             summary=clean(e.get("summary", ""))[:600], image=image,
                             author=e.get("author"), via="rss"))
    return out


# ---- Sitemaps -----------------------------------------------------------------
def parse_sitemap(content: bytes) -> tuple[str, list]:
    """Returns ("index", [(child_url, lastmod)]) or ("urlset", [Candidate])."""
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return "invalid", []
    tag = root.tag.split("}")[-1]
    if tag == "sitemapindex":
        children = []
        for sm in root:
            loc = sm.findtext(f"{{{NS_SM}}}loc") or sm.findtext("loc")
            if loc:
                children.append((loc.strip(), parse_date(sm.findtext(f"{{{NS_SM}}}lastmod") or sm.findtext("lastmod"))))
        return "index", children
    if tag != "urlset":
        return "invalid", []
    out = []
    for u in root:
        loc = u.findtext(f"{{{NS_SM}}}loc") or u.findtext("loc")
        if not loc:
            continue
        news = u.find(f"{{{NS_NEWS}}}news")
        title, published, keywords = "", None, ""
        if news is not None:
            title = clean(news.findtext(f"{{{NS_NEWS}}}title"))
            published = parse_date(news.findtext(f"{{{NS_NEWS}}}publication_date"))
            keywords = clean(news.findtext(f"{{{NS_NEWS}}}keywords"))
        if not published:
            published = parse_date(u.findtext(f"{{{NS_SM}}}lastmod") or u.findtext("lastmod"))
        image = u.findtext(f"{{{NS_IMG}}}image/{{{NS_IMG}}}loc")
        out.append(Candidate(url=loc.strip(), title=title, published=published, image=image,
                             keywords=keywords, via="sitemap"))
    return "urlset", out


def is_news_sitemap(content: bytes) -> bool:
    return NS_NEWS.encode() in content[:5000]


def rank_sitemap_children(children: list[tuple[str, datetime | None]], limit: int = 2) -> list[str]:
    """From a sitemap index pick the children most likely to hold fresh articles."""
    news = [u for u, _ in children if re.search(r"news", u, re.I)]
    if news:
        return news[:limit]
    posts = [(u, m) for u, m in children if not re.search(r"(tag|categor|author|page|video|image|topic|compan)", u, re.I)]
    posts.sort(key=lambda x: x[1] or datetime.min, reverse=True)
    return [u for u, _ in posts[:limit]]


def pick_news_sitemaps(robots_sitemaps: list[str]) -> list[str]:
    news = [u for u in robots_sitemaps if re.search(r"news", u, re.I)]
    return news or robots_sitemaps[:1]


# ---- Homepage auto-discovery -----------------------------------------------------
def feeds_in_html(html: bytes, base_url: str) -> list[str]:
    text = html[:300_000].decode("utf-8", "ignore")
    out = []
    for tag in re.findall(r"<link\b[^>]*>", text, re.I):
        if re.search(r'type=["\']application/(rss|atom)\+xml', tag, re.I):
            m = re.search(r'href=["\']([^"\']+)', tag, re.I)
            if m:
                url = urljoin(base_url, m.group(1))
                if "comments" not in url:
                    out.append(url)
    return list(dict.fromkeys(out))


# ---- Listing pages ---------------------------------------------------------------
def from_listing(html: bytes, base_url: str, pattern: str) -> list[Candidate]:
    rx = re.compile(pattern)
    host = urlsplit(base_url).netloc
    seen, out = set(), []
    for href, text in re.findall(r'<a\b[^>]*href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', html.decode("utf-8", "ignore"), re.S | re.I):
        url = urljoin(base_url, href)
        if urlsplit(url).netloc != host or not rx.search(url) or url in seen:
            continue
        seen.add(url)
        title = clean(text)
        out.append(Candidate(url=url, title=title if len(title) > 20 else "", via="listing"))
    return out
