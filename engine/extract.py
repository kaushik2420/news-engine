"""Enrichment: pull title, description, image, date, author and body text from an article page.

Body text is stored only for tagging and is purged after KEEP_BODY_DAYS. It is never served.
"""
from __future__ import annotations

import json
import re

import trafilatura

from .discover import parse_date
from .text import clean


def _meta(html: str, *names: str) -> str | None:
    for n in names:
        m = re.search(rf'<meta[^>]+(?:property|name)=["\']{re.escape(n)}["\'][^>]+content=["\']([^"\']+)', html, re.I) \
            or re.search(rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']{re.escape(n)}["\']', html, re.I)
        if m:
            return clean(m.group(1))
    return None


def extract(content: bytes, url: str) -> dict:
    html = content.decode("utf-8", "ignore")
    out = {
        "title": _meta(html, "og:title", "twitter:title"),
        "summary": _meta(html, "og:description", "description", "twitter:description"),
        "image": _meta(html, "og:image", "twitter:image"),
        "published": parse_date(_meta(html, "article:published_time", "pubdate", "date")),
        "author": _meta(html, "author", "article:author"),
        "body": "",
    }
    try:
        raw = trafilatura.extract(html, url=url, output_format="json", with_metadata=True,
                                  include_comments=False, include_tables=False, favor_precision=True)
    except Exception:   # trafilatura can raise on pathological HTML; enrichment is best-effort
        raw = None
    if raw:
        data = json.loads(raw)
        out["body"] = (data.get("text") or "")[:20000]
        out["title"] = out["title"] or clean(data.get("title"))
        out["summary"] = out["summary"] or clean(data.get("description"))
        out["image"] = out["image"] or data.get("image")
        out["author"] = out["author"] or data.get("author")
        out["published"] = out["published"] or parse_date(data.get("date"))
    if not out["title"]:
        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
        out["title"] = clean(m.group(1)) if m else ""
    if out["image"] and not out["image"].startswith("https://"):
        out["image"] = None
    if out["summary"]:
        out["summary"] = out["summary"][:600]
    return out
