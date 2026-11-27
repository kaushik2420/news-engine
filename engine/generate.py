"""Post briefs and drafts.

A *brief* is everything an LLM needs to write the post (system rules, user profile, story facts, angle,
format spec). Her app's own Claude can take the brief and write the post itself, so the engine does not
need an LLM key for this. If the engine has a key, generate() writes the draft directly.
"""
from __future__ import annotations

from . import llm
from . import settings as S

FORMATS = {
    "linkedin": {"label": "LinkedIn post", "spec": """A LinkedIn post, 120-220 words. Line 1 is a scroll-stopping hook (no emoji at the start).
Short paragraphs of 1-2 lines. Give the user's own insight or lesson, not a recap of the news.
End with one genuine question to invite comments. 3 relevant hashtags at the very end."""},
    "x": {"label": "X post + thread", "spec": """Two options for X: (1) a single post under 270 characters, (2) a thread of 4-6 posts,
each under 270 characters, numbered 1/, 2/ ... The first post must work on its own."""},
    "instagram_carousel": {"label": "Instagram carousel", "spec": """An Instagram carousel of 6-8 slides. For each slide: a short headline (max 8 words)
and one supporting line. Slide 1 is the hook, the last slide is a call to action. Then a caption (max 80 words) + 5 hashtags."""},
    "reel": {"label": "Reel script", "spec": """A 30-45 second Reel script. Give: HOOK (first 3 seconds, spoken + on-screen text), 3 BEATS
(each with spoken line + on-screen text + a simple b-roll idea), CTA. Talking-head friendly, no expensive production."""},
    "story": {"label": "Instagram Story", "spec": """3 Instagram Story frames. Frame 1: the news in one line. Frame 2: an interactive sticker
(poll or quiz) with exact question and options. Frame 3: the user's take + a link sticker label."""},
}

SYSTEM = """You write social posts for professionals who want to grow their personal brand by commenting on news.
Rules: write in the user's voice from their profile. Add a point of view; never just summarise the article.
Never copy sentences from the source. Attribute facts to the source by name. Do not invent numbers or quotes.
No hype words like "game-changer", "revolutionary", "delve". Output only the draft, no preamble."""


def brief(story: dict, platform: str, profile: dict | None = None, angle: str | None = None) -> dict:
    if platform not in FORMATS:
        raise ValueError(f"unknown platform '{platform}'; choose from {sorted(FORMATS)}")
    p = (profile or {}).get("data") or profile or {}
    tags = story.get("tags") or {}
    sources = sorted({a["source_name"] for a in story.get("articles", [])})
    chosen = angle or (tags.get("angles") or [None])[0]
    prompt = f"""USER PROFILE
Role: {p.get('role') or 'professional'}
Industry / niche: {p.get('niche') or 'startups and technology'}
Goal on social: {p.get('goal') or 'build credibility and grow followers'}
Tone: {p.get('tone') or 'clear, thoughtful, a little bold'}

NEWS STORY
Headline: {story['headline']}
Facts: {((story.get('digest') or {}).get('body') or tags.get('summary') or story.get('summary') or '')[:3500]}
Reported by: {', '.join(sources)}

ANGLE: {chosen or 'pick the strongest angle for this user'}

WRITE
{FORMATS[platform]['spec']}"""
    return {"story_id": story["id"], "platform": platform, "format": FORMATS[platform]["label"], "angle": chosen,
            "suggested_angles": tags.get("angles") or [], "system": SYSTEM, "prompt": prompt,
            "sources": [{"name": a["source_name"], "url": a["url"]} for a in story.get("articles", [])]}


def generate(story: dict, platform: str, profile: dict | None = None, angle: str | None = None) -> dict:
    b = brief(story, platform, profile, angle)
    draft = llm.complete(b["prompt"], S.settings.write_model, max_tokens=1400, system=b["system"])
    return {**{k: b[k] for k in ("story_id", "platform", "format", "angle", "sources")}, "draft": draft.strip()}
