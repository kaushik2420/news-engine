"""Digests: a 250-500 word, engine-written account of a story that people can read in the app.

How it stays defensible:
- written by the LLM in its own words from the full text of EVERY outlet covering the story (a synthesis,
  not a rewrite of one article); at most one short attributed quote; claims attributed to their outlet
- grounded: only facts present in the material; shorter rather than padded when material is thin
- always shipped with the source links; publisher text itself is never returned

Generation runs in the worker (best stories first, within DIGEST_PER_HOUR) and on demand from the API.
A digest is regenerated when more outlets join the story ("stale").
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import timedelta

from sqlalchemy import func, or_, select

from . import db, llm
from . import settings as S
from .extract import extract
from .fetch import Fetcher

log = logging.getLogger("engine.digest")
VERSION = 1
MAX_ARTICLES = 4
PER_ARTICLE_CHARS = 4000

PROMPT = """You write news digests for a social-content app. Readers will read your digest INSTEAD of opening
the articles, so it must be accurate, complete on the essentials, and entirely in your own words.

MATERIAL: {n} article(s) about one news event, from {outlets}.
{material}

WRITE a digest of {min_words}-{max_words} words.
Rules:
- Use ONLY facts stated in the material. No outside knowledge, no speculation, no invented numbers or quotes.
- Synthesise across outlets. Attribute claims ("according to Al Jazeera", "Euronews reported"), and note
  where outlets differ.
- Your own sentences only. Never copy a sentence or distinctive phrase. At most ONE direct quote, under
  15 words, attributed to the speaker.
- Neutral, plain English. Absolute dates (e.g. "on 26 November"), not "yesterday".
- Structure: an opening paragraph with the core news; then context, reactions and numbers; then what
  happens next if the material says. 3-6 short paragraphs. No headings, no bullet points in "body".
- If the material is too thin for {min_words} words, write less and set "enough_material": false. Never pad.
- Sensitive stories (deaths, crime, conflict): factual and respectful, no graphic detail.

Reply with JSON only:
{{"card_title": "max 70 characters, punchy, accurate, no clickbait",
  "dek": "one sentence, max 30 words, what happened and why it matters",
  "body": "the digest as plain paragraphs separated by blank lines",
  "key_points": ["3 to 5 short factual bullets"],
  "enough_material": true}}"""


def _words(text: str) -> int:
    return len(re.findall(r"\b\w[\w'’-]*\b", text or ""))


def eligible_query(now):
    """Stories the background loop should digest, best first."""
    st = db.stories.c
    return (select(st.id).where(
        or_(st.digest_status.is_(None), st.digest_status.in_(["pending", "stale"])),
        st.digest_attempts < 3, st.tag_status == "done",
        or_(st.is_roundup.is_(None), st.is_roundup.is_(False)),
        st.updated_at >= now - timedelta(hours=S.settings.digest_window_hours))
        .order_by(st.source_count.desc(), st.postability.desc(), st.updated_at.desc()))


def made_last_hour() -> int:
    with db.get_engine().connect() as c:
        return c.execute(select(func.count()).select_from(db.stories).where(
            db.stories.c.digest_at >= db.utcnow() - timedelta(hours=1))).scalar()


async def _material(fetcher: Fetcher, story_id: str) -> tuple[list[dict], str]:
    """Full text of up to MAX_ARTICLES articles (one per outlet first). Fetches pages missing their body."""
    with db.get_engine().connect() as c:
        rows = c.execute(select(db.articles.c.id, db.articles.c.url, db.articles.c.title, db.articles.c.summary,
                                db.articles.c.body, db.articles.c.published_at, db.sources.c.name)
                         .join(db.sources, db.sources.c.id == db.articles.c.source_id)
                         .where(db.articles.c.story_id == story_id)
                         .order_by(db.articles.c.published_at.desc())).all()
    picked, seen = [], set()
    for r in rows:                                   # one article per outlet, newest first
        if r.name not in seen:
            picked.append(r)
            seen.add(r.name)
    picked = picked[:MAX_ARTICLES]
    out = []
    for r in picked:
        body = r.body or ""
        if len(body) < 600:
            resp = await fetcher.get(r.url, retries=1)
            if resp.ok and "html" in (resp.content_type or "html"):
                body = extract(resp.content, resp.url).get("body") or body
                if body:
                    with db.get_engine().begin() as c:
                        c.execute(db.articles.update().where(db.articles.c.id == r.id).values(body=body[:20000]))
        out.append({"outlet": r.name, "url": r.url, "title": r.title, "published_at": db.iso(r.published_at),
                    "text": (body or r.summary or "")[:PER_ARTICLE_CHARS], "full": len(body) >= 600})
    kind = "full_text" if any(a["full"] for a in out) else "excerpts_only"
    return out, kind


def _prompt(arts: list[dict]) -> str:
    blocks = "\n\n".join(f"--- ARTICLE {i + 1} | {a['outlet']} | {a['published_at'] or 'date unknown'}\n"
                         f"Headline: {a['title']}\n{a['text']}" for i, a in enumerate(arts))
    return PROMPT.format(n=len(arts), outlets=", ".join(sorted({a["outlet"] for a in arts})), material=blocks,
                         min_words=S.settings.digest_min_words, max_words=S.settings.digest_max_words)


def _validate(raw: dict, arts: list[dict]) -> dict:
    body = str(raw.get("body") or "").strip()
    body = re.sub(r"\n{3,}", "\n\n", body)
    out = {
        "card_title": str(raw.get("card_title") or "").strip()[:90] or None,
        "dek": str(raw.get("dek") or "").strip()[:300] or None,
        "body": body,
        "key_points": [str(k).strip()[:200] for k in (raw.get("key_points") or []) if str(k).strip()][:5],
        "word_count": _words(body),
        "enough_material": bool(raw.get("enough_material", True)),
        "sources": [{"name": a["outlet"], "url": a["url"]} for a in arts],
        "kind": "multi_source" if len({a["outlet"] for a in arts}) > 1 else "single_source",
        "version": VERSION,
    }
    return out


def _copied(body: str, arts: list[dict]) -> bool:
    """Guard: reject a digest that reproduces any 15-word run (one quote under 15 words is allowed) from the source text."""
    def grams(t):
        w = re.findall(r"\w+", t.lower())
        return {" ".join(w[i:i + 15]) for i in range(max(0, len(w) - 14))}
    src = set()
    for a in arts:
        src |= grams(a["text"])
    return bool(grams(body) & src)


async def generate(fetcher: Fetcher, story_id: str) -> dict:
    """Build and store the digest for one story. Raises LLMUnavailable without a key."""
    if not llm.available():
        raise llm.LLMUnavailable("digests need ANTHROPIC_API_KEY on the engine")
    why = llm.paused()
    if why:
        raise llm.LLMPaused(why)
    with db.get_engine().begin() as c:                       # claim
        before = c.execute(select(db.stories.c.digest_status, db.stories.c.digest_attempts)
                           .where(db.stories.c.id == story_id)).first()
        c.execute(db.stories.update().where(db.stories.c.id == story_id).values(
            digest_attempts=func.coalesce(db.stories.c.digest_attempts, 0) + 1, digest_status="working"))
    try:
        return await _write(fetcher, story_id)
    except llm.LLMPaused:
        # the AI account refused (credits, key): put the story back exactly as it was, attempt not counted
        with db.get_engine().begin() as c:
            c.execute(db.stories.update().where(db.stories.c.id == story_id).values(
                digest_status=before.digest_status if before else None,
                digest_attempts=before.digest_attempts if before else 0))
        raise


async def _write(fetcher: Fetcher, story_id: str) -> dict:
    arts, kind = await _material(fetcher, story_id)
    if not arts or not any(a["text"] for a in arts):
        return _store(story_id, None, "skipped", "no article text available")
    prompt = _prompt(arts)
    raw = await asyncio.to_thread(llm.complete_json, prompt, S.settings.digest_model, 1800)
    dg = _validate(raw, arts)
    cfg = S.settings
    if dg["enough_material"] and kind == "full_text" and dg["word_count"] < cfg.digest_min_words * 0.8:
        raw = await asyncio.to_thread(llm.complete_json, prompt + f"\n\nYour previous draft had only "
                                      f"{dg['word_count']} words. The material supports {cfg.digest_min_words}+.",
                                      cfg.digest_model, 1800)
        dg = _validate(raw, arts)
    if _copied(dg["body"], arts):
        raw = await asyncio.to_thread(llm.complete_json, prompt + "\n\nYour previous draft repeated source sentences "
                                      "word for word. Rewrite every sentence in your own words.", cfg.digest_model, 1800)
        dg = _validate(raw, arts)
        if _copied(dg["body"], arts):
            return _store(story_id, None, "failed", "draft kept copying source text")
    if not dg["body"]:
        return _store(story_id, None, "failed", "empty draft")
    dg.update(material=kind, model=cfg.digest_model, generated_at=db.iso(db.utcnow()))
    return _store(story_id, dg, "done", None)


def _store(story_id: str, digest: dict | None, status: str, reason: str | None) -> dict:
    values = {"digest_status": status, "digest_at": db.utcnow()}
    if digest is not None:
        values["digest"] = digest
    elif reason:
        values["digest"] = {"error": reason}
    with db.get_engine().begin() as c:
        c.execute(db.stories.update().where(db.stories.c.id == story_id).values(**values))
    return {"story_id": story_id, "status": status, **({"reason": reason} if reason else {}),
            **({"word_count": digest["word_count"]} if digest else {})}


async def generate_on_demand(fetcher: Fetcher, story_id: str) -> None:
    """For the API/MCP: write the digest now if we can. Never raises for AI-side problems; the caller re-reads
    the story and get_digest() explains what happened in `reason`."""
    if not llm.available() or llm.paused():
        return
    try:
        await generate(fetcher, story_id)
    except llm.LLMPaused:
        return
    except Exception as e:
        log.warning("on-demand digest failed for %s: %s", story_id, e)
        _store(story_id, None, "pending", None)


async def digest_pending(fetcher: Fetcher, limit: int = 3) -> dict:
    """Worker step: digest the best eligible stories, within the hourly budget."""
    if not llm.available():
        return {"made": 0, "mode": "off"}
    if llm.paused():
        return {"made": 0, "mode": "paused"}
    room = S.settings.digest_per_hour - made_last_hour()
    if room <= 0:
        return {"made": 0, "mode": "budget_reached"}
    with db.get_engine().connect() as c:
        ids = [r.id for r in c.execute(eligible_query(db.utcnow()).limit(min(limit, room))).all()]
    made = []
    for sid in ids:
        try:
            made.append(await generate(fetcher, sid))
        except llm.LLMPaused:
            break                                    # account problem: stop, nothing counted against stories
        except Exception as e:                       # keep going; attempts cap retries at 3
            log.warning("digest failed for %s: %s", sid, e)
            _store(sid, None, "pending", None)
    return {"made": sum(1 for m in made if m["status"] == "done"), "results": made}


def mark_stale(conn, story_id: str) -> None:
    """Called when a new outlet joins a story that already has a digest."""
    conn.execute(db.stories.update().where(db.stories.c.id == story_id, db.stories.c.digest_status == "done")
                 .values(digest_status="stale", digest_attempts=0))


def digest_json(value) -> dict | None:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value
