"""Tagging against the fixed taxonomy in config/taxonomy.yaml.

LLM tagger when ANTHROPIC_API_KEY is set; keyword-rule tagger otherwise (status "rules"), so the engine
always produces filterable stories. Rule-tagged stories are re-tagged by the LLM once a key is added.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache

import yaml

from . import llm
from . import settings as S

TAG_PROMPT = """You label news stories for a feed used by professionals who post on LinkedIn, X and Instagram.

Use ONLY values from this taxonomy (JSON). Pick 1-3 categories, 1 news_type, 1-3 audiences, 1-2 regions, 1 tone, 1 shelf_life.
{taxonomy}

Also return:
- entities: up to 6 companies/people/products named in the story
- summary: 1-2 sentences in YOUR OWN words, factual, max 40 words. Never copy publisher sentences.
- postability: 0-100. High = timely, broadly relevant, invites an opinion or lesson. Low = niche, dry, or legally sensitive (crime, tragedy, ongoing litigation about private people).
- angles: 3 short post angles (max 10 words each) a professional could take on this story.

Headline: {headline}
Reported by: {sources}
Text: {text}

Reply with JSON only:
{{"category": [], "news_type": "", "audience": [], "region": [], "tone": "", "shelf_life": "",
  "entities": [], "summary": "", "postability": 0, "angles": []}}"""


@lru_cache(maxsize=1)
def taxonomy() -> dict:
    return yaml.safe_load((S.settings.config_dir / "taxonomy.yaml").read_text())


def validate(tags: dict, fallback_category: str | None = None) -> dict:
    tax = taxonomy()
    out = {}
    for key in ("category", "audience", "region"):
        vals = tags.get(key) or []
        vals = [vals] if isinstance(vals, str) else vals
        out[key] = list(dict.fromkeys(v for v in vals if v in tax[key]))[:3]
    for key in ("news_type", "tone", "shelf_life"):
        out[key] = tags.get(key) if tags.get(key) in tax[key] else None
    if not out["category"]:
        out["category"] = [fallback_category if fallback_category in tax["category"] else tax["category"][0]]
    out["news_type"] = out["news_type"] or "Analysis / opinion"
    out["tone"] = out["tone"] or "Neutral"
    out["shelf_life"] = out["shelf_life"] or "This week"
    out["audience"] = out["audience"] or ["Founders"]
    out["entities"] = [str(e)[:80] for e in (tags.get("entities") or [])][:6]
    out["summary"] = str(tags.get("summary") or "")[:400]
    try:
        out["postability"] = max(0, min(100, int(tags.get("postability") or 0)))
    except (TypeError, ValueError):
        out["postability"] = 50
    out["angles"] = [str(a)[:120] for a in (tags.get("angles") or [])][:3]
    return out


def llm_tag(headline: str, summary: str, sources: list[str], text: str, fallback_category: str | None) -> dict:
    prompt = TAG_PROMPT.format(taxonomy=json.dumps(taxonomy()), headline=headline, sources=", ".join(sources),
                               text=(text or summary)[:4000])
    raw = llm.complete_json(prompt, S.settings.tag_model, max_tokens=800)
    return validate(raw, fallback_category)


RULES_CAT = {
    "Funding": r"\b(raises?|raised|bags?|funding|series [a-e]|seed round|pre-seed|invests?|investment)\b",
    "AI": r"\b(ai|genai|llms?|chatgpt|openai|anthropic|claude|gemini|model|agents?|nvidia|machine learning)\b",
    "Fintech": r"\b(upi|fintech|credit|lending|payments?|phonepe|paytm|bank(ing)?|nbfc|insurtech|navi|rbi)\b",
    "Ecommerce": r"\b(e-?commerce|flipkart|amazon|meesho|seller|d2c|quick commerce|zepto|blinkit)\b",
    "Cybersecurity": r"\b(breach|hack(ers|ed)?|stolen|ransomware|password|cyber ?attack|vulnerabilit)\w*",
    "Deeptech": r"\b(satellite|drones?|jet engine|space ?tech|deeptech|semiconductor|chips?|quantum|robotics)\b",
    "Policy": r"\b(government|ministry|meity|sebi|fcc|ban(ned)?|policy|regulat\w+|attorneys? general|secy|bill|law)\b",
    "Leadership": r"\b(ceo|cfo|cto|appoints?|steps? down|ousted|resigns?|president|succession|hires?)\b",
    "Social Media": r"\b(whatsapp|instagram|linkedin|creators?|youtube|tiktok|x's|threads)\b",
    "Education": r"\b(education|edtech|skills?|university|students?)\b",
    "Markets": r"\b(ipo|listing|valuation|stake|block deal|market share|shares|stock)\b",
    "Mobility": r"\b(ev|electric vehicles?|uber|ola|rapido|cars?|trucks?|mobility)\b",
    "Startups": r"\b(startups?|founders?|accelerator|incubator|unicorn)\b",
}
RULES_TYPE = [
    ("Funding round", r"\b(raises?|bags?|secures?|invests?)\b"),
    ("IPO / markets", r"\b(ipo|stake|block deal|listing)\b"),
    ("Leadership change", r"\b(appoints?|steps? down|ousted|resigns?|names .* (ceo|president))\b"),
    ("Data breach", r"\b(breach|hack|stolen)\b"),
    ("Policy / regulation", r"\b(fcc|ban|attorneys general|meity|sebi|rbi|regulat)"),
    ("Government programme", r"\b(government|state|ministry|scheme)\b"),
    ("Market data", r"\b(market share|data shows|report|survey|fastest)\b"),
    ("Product launch", r"\b(launch(es)?|rolls out|introduces|unveils|lets you|update)\b"),
    ("Partnership / deal", r"\b(partners?|acquires?|acquisition|deal|merger|talks)\b"),
]


def rules_tag(headline: str, summary: str, regions: list[str], source_count: int, fallback_category: str | None) -> dict:
    text = f"{headline} {summary}".lower()
    cats = [c for c, rx in RULES_CAT.items() if re.search(rx, text)][:3]
    ntype = next((t for t, rx in RULES_TYPE if re.search(rx, text)), "Analysis / opinion")
    return validate({"category": cats or [fallback_category], "news_type": ntype, "audience": ["Founders"],
                     "region": regions, "tone": "Neutral", "shelf_life": "This week", "summary": summary,
                     "postability": min(95, 45 + 12 * (source_count - 1)), "angles": []}, fallback_category)
