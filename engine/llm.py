"""Thin LLM wrapper. Swap the provider here without touching the rest of the engine."""
from __future__ import annotations

import json
import logging
import re
import time
from datetime import timedelta

from . import settings as S

log = logging.getLogger(__name__)
PAUSE_MINUTES = 10           # after a billing or key error, wait this long before calling the AI again
_STATE_REFRESH_SEC = 30
_pause = {"until": None, "reason": None, "checked": 0.0}


class LLMUnavailable(RuntimeError):
    pass


class LLMPaused(LLMUnavailable):
    """The AI account refused the call (no credits, bad or revoked key). Not the story's fault: retry later."""


def available() -> bool:
    return bool(S.settings.anthropic_api_key)


def account_problem(e: Exception) -> str | None:
    """Plain reason if this error is about the AI account rather than one request, else None."""
    status = getattr(e, "status_code", None)
    text = str(e).lower()
    if "credit balance" in text or "billing" in text:
        return "the Anthropic account is out of API credits (top up at console.anthropic.com > Plans & Billing)"
    if status == 401:
        return "the ANTHROPIC_API_KEY was rejected (wrong, revoked or expired key)"
    if status == 403:
        return "the ANTHROPIC_API_KEY is not allowed to use this model"
    return None


def pause(reason: str) -> None:
    from . import db
    until = db.utcnow() + timedelta(minutes=PAUSE_MINUTES)
    _pause.update(until=until, reason=reason, checked=time.monotonic())
    log.warning("AI calls paused for %d min: %s", PAUSE_MINUTES, reason)
    try:                      # shared with the other process (API <-> worker) through the database
        db.set_state("llm_pause", {"until": db.iso(until), "reason": reason})
    except Exception:
        pass


def paused() -> str | None:
    """Reason the AI is paused right now, or None. Either process may have hit the error."""
    from . import db
    if time.monotonic() - _pause["checked"] > _STATE_REFRESH_SEC:
        _pause["checked"] = time.monotonic()
        try:
            shared, _ = db.get_state("llm_pause", None)
            if shared and shared.get("until"):
                until = db.parse_iso(shared["until"])
                if not _pause["until"] or until > _pause["until"]:
                    _pause.update(until=until, reason=shared.get("reason"))
        except Exception:
            pass
    if _pause["until"] and db.utcnow() < _pause["until"]:
        return _pause["reason"]
    return None


def complete(prompt: str, model: str, max_tokens: int = 1500, system: str | None = None) -> str:
    if not available():
        raise LLMUnavailable("ANTHROPIC_API_KEY is not set")
    why = paused()
    if why:
        raise LLMPaused(why)
    import anthropic
    client = anthropic.Anthropic(api_key=S.settings.anthropic_api_key, max_retries=3, timeout=90)
    kwargs = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
    if system:
        kwargs["system"] = system
    try:
        msg = client.messages.create(**kwargs)
    except Exception as e:
        why = account_problem(e)
        if why:
            pause(why)
            raise LLMPaused(why) from e
        raise
    return "".join(b.text for b in msg.content if b.type == "text")


def complete_json(prompt: str, model: str, max_tokens: int = 1500, system: str | None = None):
    text = complete(prompt, model, max_tokens, system)
    m = re.search(r"\{.*\}|\[.*\]", text, re.S)
    return json.loads(m.group(0) if m else text)
