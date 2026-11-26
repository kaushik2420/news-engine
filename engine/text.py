"""Small text helpers: canonical URLs, cleaning, tokenising."""
from __future__ import annotations

import hashlib
import html
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING = ("utm_", "fbclid", "gclid", "ref", "mc_", "igshid", "cmpid", "ito")

STOP = set("""a an the and or but of to in on for with at by from as is are was were be been it its this that
these those into over after amid up down new now says said will would can could may might more most than
about out off via per vs your you we our their they his her he she not no yes how why what who when where
which week next last first also just still mn cr crore lakh rs billion million""".split())


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith(TRACKING)]
    path = parts.path.rstrip("/") or "/"
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return urlunsplit(("https", netloc, path, urlencode(query), ""))


def url_id(url: str) -> str:
    return hashlib.sha1(canonical_url(url).encode()).hexdigest()[:20]


def clean(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _stem(w: str) -> str:
    for suf in ("ing", "ed", "es", "s"):
        if len(w) > 4 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def tokens(text: str) -> list[str]:
    out = set()
    for w in re.findall(r"[a-z0-9$%.]+", text.lower()):
        w = w.strip(".")
        if w and w not in STOP and len(w) > 1:
            out.add(_stem(w))
    return sorted(out)
