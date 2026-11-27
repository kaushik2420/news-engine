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
    # general news
    "World": r"\b(war|ceasefire|united nations|un |nato|summit|foreign minister|diplomat\w*|refugees?|sanctions?|gaza|ukraine|russia|israel|iran|taliban)\b",
    "Politics": r"\b(election|elections|vote|voters|parliament|congress|senate|lok sabha|rajya sabha|minister|prime minister|president|bjp|opposition|campaign|mp|mla|governor|chief minister)\b",
    "Business": r"\b(company|companies|revenue|profit|ceo|merger|acquisition|deal|industry|corporate|retail|manufactur\w+)\b",
    "Economy": r"\b(inflation|gdp|economy|economic|interest rates?|recession|central bank|rbi|federal reserve|fed|tariffs?|trade deficit|jobs report|unemployment)\b",
    "Markets": r"\b(ipo|listing|valuation|stake|block deal|market share|shares|stocks?|sensex|nifty|nasdaq|dow|s&p|bonds?|crypto|bitcoin)\b",
    "Technology": r"\b(tech|technology|software|app|apps|smartphone|iphone|android|gadget|chip|internet|online)\b",
    "Science": r"\b(scientists?|study|research(ers)?|discovery|space|nasa|isro|planet|species|physics|astronom\w+|fossil|telescope)\b",
    "Health": r"\b(health|hospital|doctors?|patients?|disease|virus|vaccine|cancer|medical|medicine|drug|who |outbreak|mental health)\b",
    "Environment": r"\b(climate|emissions?|carbon|pollution|heatwave|flood\w*|wildfire|cyclone|hurricane|monsoon|drought|renewable|solar|wildlife)\b",
    "Sports": r"\b(cricket|football|soccer|tennis|ipl|fifa|olympic\w*|nba|nfl|f1|formula 1|match|cup|league|tournament|goal|wicket|coach|squad|premier league|test series|grand prix)\b",
    "Entertainment": r"\b(film|movie|box office|bollywood|hollywood|actor|actress|singer|album|music|netflix|web series|tv series|tv show|celebrity|oscars?|grammys?|trailer)\b",
    "Culture": r"\b(art|artist|museum|book|author|festival|heritage|culture|literature)\b",
    "Lifestyle": r"\b(food|fashion|recipe|wellness|fitness|travel tips|relationships?|parenting)\b",
    "Travel": r"\b(travel|tourism|tourists?|airline|flights?|airport|visa|hotel)\b",
    "Education": r"\b(education|edtech|skills?|university|universities|students?|school|exam|neet|jee|cbse)\b",
    "Crime & Justice": r"\b(police|arrest\w*|court|judge|verdict|trial|murder|lawsuit|sued|charged|supreme court|high court|cbi|fraud|scam)\b",
    # startup / tech depth
    "Funding": r"\b(raises?|raised|bags?|funding|series [a-e]|seed round|pre-seed|invests?|investment)\b",
    "AI": r"\b(ai|genai|llms?|chatgpt|openai|anthropic|claude|gemini|model|agents?|nvidia|machine learning)\b",
    "Fintech": r"\b(upi|fintech|credit|lending|payments?|phonepe|paytm|bank(ing)?|nbfc|insurtech|navi)\b",
    "Ecommerce": r"\b(e-?commerce|flipkart|meesho|sellers?|d2c|quick commerce|zepto|blinkit|online retail)\b",
    "Big Tech": r"\b(google|alphabet|apple|microsoft|meta|amazon\.com|amazon's|aws|openai|nvidia|tesla|samsung)\b",
    "Cybersecurity": r"\b(breach|hack(ers|ed)?|stolen data|ransomware|password|cyber ?attack|vulnerabilit\w*)",
    "Deeptech": r"\b(satellite|drones?|jet engine|space ?tech|deeptech|semiconductor|quantum|robotics)\b",
    "Policy": r"\b(ministry|meity|sebi|fcc|ban(ned)?|policy|regulat\w+|attorneys? general|secy|bill|law)\b",
    "Leadership": r"\b(ceo|cfo|cto|appoints?|steps? down|ousted|resigns?|succession|hires?)\b",
    "Social Media": r"\b(whatsapp|instagram|linkedin|creators?|youtube|tiktok|x's|threads|influencers?)\b",
    "Mobility": r"\b(ev|electric vehicles?|uber|ola|rapido|cars?|trucks?|mobility|automaker)\b",
    "Startups": r"\b(startups?|founders?|accelerator|incubator|unicorn)\b",
}
# Order matters: the first match wins.
RULES_TYPE = [
    ("Funding round", r"\b(raises?|bags?|secures? \$|invests?)\b"),
    ("Election", r"\b(election|polls?|voters?|ballot|by-election)\b"),
    ("Match / event result", r"\b(beat|beats|win over|wins|thrash\w*|draw with|semi-?final|final|knocked out|clinch\w*)\b"),
    ("Disaster / emergency", r"\b(earthquake|flood\w*|cyclone|hurricane|wildfire|landslide|explosion|crash|killed|dead|death toll)\b"),
    ("Conflict", r"\b(war|strike[sd]?|missile|troops|attack|ceasefire|shelling|militants?)\b"),
    ("Earnings / results", r"\b(q[1-4]|quarterly|results|earnings|net profit|revenue (rose|fell|grew))\b"),
    ("IPO / markets", r"\b(ipo|stake|block deal|listing)\b"),
    ("Leadership change", r"\b(appoints?|steps? down|ousted|resigns?|names .* (ceo|president|chief))\b"),
    ("Data breach", r"\b(breach|hack|stolen data)\b"),
    ("Research / study", r"\b(study|research|scientists|researchers|survey finds|report finds)\b"),
    ("Policy / regulation", r"\b(fcc|ban|attorneys general|meity|sebi|rbi|regulat\w*|new rules|bill passed)\b"),
    ("Government programme", r"\b(government|ministry|scheme|programme|program)\b"),
    ("Review", r"\b(review|reviewed|hands-on)\b"),
    ("Interview", r"\b(interview|in conversation|q&a)\b"),
    ("Market data", r"\b(market share|data shows|fastest|record high|record low)\b"),
    ("Product launch", r"\b(launch(es)?|rolls out|introduces|unveils|lets you|update)\b"),
    ("Partnership / deal", r"\b(partners?|acquires?|acquisition|deal|merger|talks)\b"),
]
# Region from the story's own words, on top of the source's home region.
RULES_REGION = {
    "India": r"\b(india|indian|delhi|mumbai|bengaluru|bangalore|chennai|kolkata|hyderabad|modi|lok sabha|rupees?|₹)\b",
    "Karnataka": r"\b(karnataka|bengaluru|bangalore|mangaluru|mysuru)\b",
    "US": r"\b(us|u\.s\.|united states|america|american|washington|trump|biden|white house|new york|california)\b",
    "UK": r"\b(uk|britain|british|england|london|starmer|scotland|wales)\b",
    "Europe": r"\b(europe|european|eu|brussels|germany|france|italy|spain|ukraine|russia|poland|nato)\b",
    "Middle East": r"\b(israel|gaza|iran|iraq|syria|saudi|uae|dubai|qatar|lebanon|yemen|middle east)\b",
    "China": r"\b(china|chinese|beijing|shanghai|xi jinping|hong kong|taiwan)\b",
    "Asia-Pacific": r"\b(japan|korea|singapore|indonesia|philippines|vietnam|thailand|malaysia|asia)\b",
    "Africa": r"\b(africa|african|nigeria|kenya|egypt|ethiopia|ghana|sudan)\b",
    "Latin America": r"\b(brazil|mexico|argentina|chile|colombia|venezuela|latin america)\b",
    "Australia": r"\b(australia|australian|sydney|melbourne|canberra)\b",
}
SHELF = {"Breaking news": "Breaking", "Match / event result": "Breaking", "Disaster / emergency": "Breaking",
         "Election": "This week", "Feature": "Evergreen", "Analysis / opinion": "This week",
         "Research / study": "Evergreen", "Interview": "Evergreen", "Review": "Evergreen"}
AUDIENCE = {"Sports": "Sports fans", "Politics": "Policy watchers", "Policy": "Policy watchers",
            "Business": "Business leaders", "Economy": "Business leaders", "Markets": "Investors",
            "Funding": "Investors", "Startups": "Founders", "Fintech": "Founders", "AI": "Product & tech",
            "Technology": "Product & tech", "Cybersecurity": "Product & tech", "Social Media": "Creators",
            "Entertainment": "Creators", "Education": "Students"}
NEGATIVE = r"\b(killed|dead|death|crash|fraud|scam|arrest\w*|breach|layoffs?|loss|falls?|plunge\w*|war|attack)\b"
POSITIVE = r"\b(wins?|record|raises?|launch\w*|growth|profit|surge\w*|award|breakthrough)\b"


def rules_tag(headline: str, summary: str, regions: list[str], source_count: int, fallback_category: str | None) -> dict:
    text = f"{headline} {summary}".lower()
    cats = [c for c, rx in RULES_CAT.items() if re.search(rx, text)]
    # put the specific startup/tech labels after the general ones only when a general one matched first
    cats = cats[:3]
    ntype = next((t for t, rx in RULES_TYPE if re.search(rx, text)), "Analysis / opinion")
    found_regions = [r for r, rx in RULES_REGION.items() if re.search(rx, text)]
    region = found_regions[:2] or [r for r in regions if r][:2] or ["Global"]   # the story's own words beat the source's home
    audience = list(dict.fromkeys(AUDIENCE[c] for c in cats if c in AUDIENCE))[:3] or ["General audience"]
    tone = "Negative" if re.search(NEGATIVE, text) else "Positive" if re.search(POSITIVE, text) else "Neutral"
    if cats and cats[0] in ("Crime & Justice", "World") and ntype in ("Disaster / emergency", "Conflict"):
        postability = 30          # tragedies and conflict: low default, an LLM judges better
    else:
        postability = min(95, 45 + 12 * (source_count - 1))
    return validate({"category": cats or [fallback_category], "news_type": ntype, "audience": audience,
                     "region": region, "tone": tone, "shelf_life": SHELF.get(ntype, "This week"), "summary": summary,
                     "postability": postability, "angles": []}, fallback_category)
