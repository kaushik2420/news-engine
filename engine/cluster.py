"""Incremental clustering: attach each new article to an existing story or start a new one.

Story IDs are stable forever (the app can store them). Matching uses weighted word overlap between the
article and each recent story's token set, where rare words (names, numbers) weigh more than common ones.
"""
from __future__ import annotations

import math
import re
import uuid
from collections import Counter
from datetime import timedelta

from sqlalchemy import func, select

from . import db
from . import settings as S
from .text import tokens as tokenize

MAX_STORY_TOKENS = 250
MIN_CORPUS = 300
MIN_SHARED = 3
ROUNDUP = re.compile(r"(&|and) more\b|\bdaily brief\b|\btop (\d+ )?(stories|news)\b|\bround-?up\b|\bnewsletter\b|"
                     r"\bthis week in\b|\bweekly (wrap|recap)\b|\bfunding (galore|this week)\b|\bmorning brief\b", re.I)


def is_roundup(title: str) -> bool:
    """Multi-story digests ('X, Y & More') must not absorb or join single-event stories."""
    return bool(ROUNDUP.search(title or ""))


class Clusterer:
    """Load once per worker pass, then call assign() for each new article."""

    def __init__(self, conn):
        cfg = S.settings
        self.threshold = cfg.cluster_threshold
        self.window = timedelta(hours=cfg.cluster_window_hours)
        since = db.utcnow() - self.window - timedelta(hours=24)
        rows = conn.execute(select(db.stories.c.id, db.stories.c.tokens, db.stories.c.updated_at)
                            .where(db.stories.c.updated_at >= since)).all()
        self.stories = {r.id: (set(r.tokens or []), r.updated_at) for r in rows}
        self.df = Counter(t for toks, _ in self.stories.values() for t in toks)

    def _idf(self, t: str) -> float:
        # floor the corpus size so word weights are stable on a fresh database (cold start)
        n = max(len(self.stories) + 1, MIN_CORPUS)
        return math.log((n + 1) / (self.df.get(t, 0) + 0.5))

    def _weight(self, toks) -> float:
        return sum(self._idf(t) for t in toks)

    def best_match(self, toks: set[str], published) -> tuple[str | None, float]:
        best, best_score = None, 0.0
        wa = self._weight(toks)
        if not toks or wa == 0:
            return None, 0.0
        for sid, (stoks, updated) in self.stories.items():
            if published and updated and abs(published - updated) > self.window:
                continue
            inter = toks & stoks
            if len(inter) < MIN_SHARED:
                continue
            score = self._weight(inter) / min(wa, self._weight(stoks) or 1e-9)
            if score > best_score:
                best, best_score = sid, score
        return (best, best_score) if best_score >= self.threshold else (None, best_score)

    def assign(self, conn, article: dict) -> tuple[str, bool]:
        """article: id, source_id, title, summary, published_at. Returns (story_id, created)."""
        toks = set(tokenize(f"{article['title']} {article['title']} {article.get('summary') or ''}"))
        published = article.get("published_at") or db.utcnow()
        roundup = is_roundup(article["title"])
        sid = None if roundup else self.best_match(toks, published)[0]
        if sid is None:
            sid = uuid.uuid4().hex[:16]
            stored = set() if roundup else toks          # empty token set: nothing can join a roundup
            conn.execute(db.stories.insert().values(
                id=sid, headline=article["title"], summary=article.get("summary") or "",
                published_at=published, updated_at=published, source_count=1, article_count=1,
                tokens=sorted(stored)[:MAX_STORY_TOKENS], tag_status="pending",
                tags={"is_roundup": True} if roundup else {}, created_at=db.utcnow(),
                **db.tag_columns({"is_roundup": roundup}, article["title"], article.get("summary"))))
            conn.execute(db.articles.update().where(db.articles.c.id == article["id"]).values(story_id=sid))
            self.stories[sid] = (stored, published)
            self.df.update(toks)
            return sid, True

        conn.execute(db.articles.update().where(db.articles.c.id == article["id"]).values(story_id=sid))
        row = conn.execute(select(db.stories).where(db.stories.c.id == sid)).first()
        merged = set(row.tokens or []) | toks
        counts = conn.execute(select(func.count(func.distinct(db.articles.c.source_id)), func.count())
                              .where(db.articles.c.story_id == sid)).first()
        values = {"tokens": sorted(merged)[:MAX_STORY_TOKENS], "source_count": counts[0], "article_count": counts[1],
                  "updated_at": max(row.updated_at or published, published),
                  "published_at": min(row.published_at or published, published)}
        if not row.summary and article.get("summary"):
            values["summary"] = article["summary"]
        conn.execute(db.stories.update().where(db.stories.c.id == sid).values(**values))
        if counts[0] > (row.source_count or 1):
            from .digest import mark_stale
            mark_stale(conn, sid)                    # a new outlet: the digest should include it
        self.df.update(toks - set(row.tokens or []))
        self.stories[sid] = (merged, values["updated_at"])
        return sid, False
