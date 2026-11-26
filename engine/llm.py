"""Thin LLM wrapper. Swap the provider here without touching the rest of the engine."""
from __future__ import annotations

import json
import re

from . import settings as S


class LLMUnavailable(RuntimeError):
    pass


def available() -> bool:
    return bool(S.settings.anthropic_api_key)


def complete(prompt: str, model: str, max_tokens: int = 1500, system: str | None = None) -> str:
    if not available():
        raise LLMUnavailable("ANTHROPIC_API_KEY is not set")
    import anthropic
    client = anthropic.Anthropic(api_key=S.settings.anthropic_api_key, max_retries=3, timeout=90)
    kwargs = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
    if system:
        kwargs["system"] = system
    msg = client.messages.create(**kwargs)
    return "".join(b.text for b in msg.content if b.type == "text")


def complete_json(prompt: str, model: str, max_tokens: int = 1500, system: str | None = None):
    text = complete(prompt, model, max_tokens, system)
    m = re.search(r"\{.*\}|\[.*\]", text, re.S)
    return json.loads(m.group(0) if m else text)
