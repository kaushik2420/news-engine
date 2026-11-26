"""Outbound webhooks: the engine pushes events to the app instead of the app polling.

Events: story.created, story.tagged, story.trending
Each POST carries header X-Engine-Signature: sha256=<hex HMAC of the raw body with the webhook secret>.
Deliveries are queued (outbox) and retried with exponential backoff, up to 8 attempts.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import timedelta

import httpx
from sqlalchemy import select

from . import db

log = logging.getLogger("engine.webhooks")
EVENTS = ["story.created", "story.tagged", "story.trending"]
MAX_ATTEMPTS = 8


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def enqueue(conn, event: str, payload: dict) -> int:
    hooks = conn.execute(select(db.webhooks).where(db.webhooks.c.active.is_(True))).all()
    n = 0
    for h in hooks:
        if event in (h.events or EVENTS):
            conn.execute(db.deliveries.insert().values(webhook_id=h.id, event=event, payload=payload,
                                                      status="pending", attempts=0, next_attempt_at=db.utcnow()))
            n += 1
    return n


async def dispatch(client: httpx.AsyncClient, limit: int = 50) -> dict:
    now = db.utcnow()
    with db.get_engine().connect() as c:
        rows = c.execute(select(db.deliveries, db.webhooks.c.url, db.webhooks.c.secret)
                         .join(db.webhooks, db.webhooks.c.id == db.deliveries.c.webhook_id)
                         .where(db.deliveries.c.status == "pending", db.deliveries.c.next_attempt_at <= now)
                         .order_by(db.deliveries.c.id).limit(limit)).all()
    sent = failed = 0
    for r in rows:
        body = json.dumps({"event": r.event, "delivery_id": r.id, "sent_at": db.iso(now), "data": r.payload},
                          default=str).encode()
        err = None
        try:
            resp = await client.post(r.url, content=body, timeout=10, headers={
                "Content-Type": "application/json", "X-Engine-Event": r.event,
                "X-Engine-Signature": sign(r.secret, body)})
            if resp.status_code >= 300:
                err = f"HTTP {resp.status_code}"
        except httpx.HTTPError as e:
            err = str(e)[:300]
        with db.get_engine().begin() as c:
            if err is None:
                c.execute(db.deliveries.update().where(db.deliveries.c.id == r.id).values(status="sent", attempts=r.attempts + 1, last_error=None))
                c.execute(db.webhooks.update().where(db.webhooks.c.id == r.webhook_id).values(failures=0))
                sent += 1
            else:
                attempts = r.attempts + 1
                done = attempts >= MAX_ATTEMPTS
                c.execute(db.deliveries.update().where(db.deliveries.c.id == r.id).values(
                    status="failed" if done else "pending", attempts=attempts, last_error=err,
                    next_attempt_at=now + timedelta(seconds=min(3600, 15 * 2 ** attempts))))
                c.execute(db.webhooks.update().where(db.webhooks.c.id == r.webhook_id).values(failures=db.webhooks.c.failures + 1))
                failed += 1
    return {"sent": sent, "failed": failed}
