# Coupling guide: attaching the app to the News Engine

The app (the coaches) carries users, onboarding, UI and posting. The **News Engine** (the locomotive) runs
continuously in the background: it collects news, merges duplicates into stories, tags them, ranks feeds per
user and prepares post briefs. The app never crawls anything itself; it asks the engine.

There are three couplings. Use any or all:

| Coupling | Use it for | How |
|---|---|---|
| **REST API** | The app's backend fetching feeds, stories, briefs | `https://<engine-host>/v1/...` with a bearer key |
| **MCP** | Claude (Claude Code, the Claude app, Agent SDK) talking to the engine directly | `https://<engine-host>/mcp` |
| **Webhooks** | The engine pushing new/trending stories to the app | Register a URL; verify the HMAC signature |

Live, always-current docs for every route: `https://<engine-host>/docs` (OpenAPI at `/openapi.json`).

---

## 1. Keys

| Key | Env var | Can do |
|---|---|---|
| Read key | `ENGINE_READ_KEY` | stories, search, trending, feeds, profiles, events, briefs, drafts, status |
| Admin key | `ENGINE_API_KEY` | everything above + sources, webhooks, admin actions |

Send as `Authorization: Bearer <key>`. Give the app's backend and Claude the **read key**; keep the admin key for
operating the engine. Never ship either key to a browser or mobile client: call the engine from the app's backend.

## 2. Connect Claude via MCP

**Claude Code** (in the app's repo):
```bash
claude mcp add --transport http news-engine https://<engine-host>/mcp \
  --header "Authorization: Bearer <ENGINE_READ_KEY>"
```
Or commit a `.mcp.json` (keep the key in an env var, not in git):
```json
{ "mcpServers": { "news-engine": {
    "type": "http", "url": "https://<engine-host>/mcp",
    "headers": { "Authorization": "Bearer ${NEWS_ENGINE_KEY}" } } } }
```
**Claude app (custom connector)**: if the connector form only takes a URL, use
`https://<engine-host>/mcp?key=<ENGINE_READ_KEY>`. Treat that URL as a secret.

### MCP tools
| Tool | What it returns |
|---|---|
| `engine_status` | worker heartbeat, LLM on/off, counts, queues |
| `get_taxonomy` | every valid filter value + platforms + event types |
| `list_stories` | filtered stories (`category`, `news_type`, `audience`, `region`, `tone`, `source_id`, `sort`) |
| `search_stories` | keyword search over headlines, summaries, entities |
| `get_story` | one story with all source articles |
| `get_trending` | stories covered by 2+ outlets, most-covered first |
| `get_story_digest` | the 250-500 word digest + card title + thumbnail URLs (writes it if missing) |
| `upsert_profile` / `get_profile` | store a user's interests (id = the app's user id) |
| `get_feed` | personalised ranked feed (with score breakdown) |
| `record_event` | click / save / posted / dismiss / view: the feed learns |
| `get_post_brief` | everything needed to write a LinkedIn / X / carousel / Reel / Story post |
| `generate_post` | the engine writes the draft (only if it has an LLM key) |
| `list_sources` / `get_source` | sources and their health |
| `check_sitemap` | dry run: can the engine use this sitemap? (admin key) |
| `add_sitemap` | add a news site from its sitemap URL and ingest it immediately (admin key) |
| `add_source` / `update_source` / `check_source_now` | manage sources (admin key) |
| `list_recent_articles` / `list_runs` | raw discoveries and the engine's work log |

## 3. REST quick reference

All under `/v1`. Lists take repeated query params: `?category=AI&category=Fintech`.

```
GET    /v1/status                        engine health and counts
GET    /v1/taxonomy                      valid filter values, platforms, event types
GET    /v1/stories                       filters: category news_type audience region tone shelf_life
                                         source_id q since_hours min_sources min_postability
                                         sort=latest|trending|postability limit offset
GET    /v1/stories/trending              ?hours=24
GET    /v1/search?q=...                  keyword search
GET    /v1/stories/{id}                  one story, all articles, digest
GET    /v1/stories/{id}/digest           the digest (written now if missing; ?generate=false to only read)
POST   /v1/stories/{id}/digest           rewrite the digest (admin)
GET    /thumbnails/{id}.png              card image, public; ?size=wide (1200x630, default) | square (1080x1080)
POST   /v1/stories/{id}/brief            {platform, profile_id | profile, angle?}
POST   /v1/stories/{id}/generate         same body; needs engine LLM key
PUT    /v1/profiles/{user_id}            {role, niche, goal, tone, categories[], audiences[], regions[], platforms[]}
GET    /v1/profiles/{user_id}
DELETE /v1/profiles/{user_id}
GET    /v1/profiles/{user_id}/feed       ?category=&region=&since_hours=&limit=
POST   /v1/feed                          {profile_id | profile, filters, since_hours, limit}
POST   /v1/profiles/{user_id}/events     {story_id, type: view|click|save|posted|dismiss, platform?}
GET    /v1/sources                       (admin for writes below)
POST   /v1/sources                       {id, name, homepage | sitemap_url | rss_url | listing_url, ...}
PATCH  /v1/sources/{id}
DELETE /v1/sources/{id}
POST   /v1/sitemaps/check                {sitemap_url}  dry run: robots, kind, fresh articles, sample, problems
POST   /v1/sitemaps                      {sitemap_url, name?, region?, default_category?, rss_url?, interval_min?}
                                         check + create the source + first ingest (201), or 400 with the problems
POST   /v1/sources/{id}/ingest           fetch now, return what was found
POST   /v1/sources/{id}/schedule         let the worker fetch it on the next tick
GET    /v1/articles                      raw discovered articles
GET    /v1/runs                          work log
POST   /v1/webhooks                      {url, events[]}  -> returns secret once
GET    /v1/webhooks | DELETE /v1/webhooks/{id} | POST /v1/webhooks/{id}/test | GET /v1/webhooks/deliveries
POST   /v1/admin/cycle | /v1/admin/enrich | /v1/admin/tag | /v1/admin/retention
GET    /health                           public; 200 if the worker heartbeat is fresh, else 503
```

## 3b. Adding a news site by its sitemap

The fastest way to grow coverage. Needs the **admin** key.

1. Find the site's sitemap: open `https://<site>/robots.txt` and copy a `Sitemap:` line, preferring one with
   "news" in it (a Google News sitemap lists the last ~48h of articles with titles). A sitemap index or an RSS
   feed also works.
2. Dry run: `POST /v1/sitemaps/check {"sitemap_url": "..."}`. It reports `ok`, `kind` (news / standard /
   index / rss), `fresh_articles`, a `sample` of headlines, `problems` (blocking) and `warnings`.
3. Add it: `POST /v1/sitemaps {"sitemap_url": "...", "name": "Al Jazeera", "region": "Global",
   "default_category": "World"}`. `region` and `default_category` must be values from `GET /v1/taxonomy`.
   The response holds the new source, the check and the first ingest's counts.

Refused (400) when robots.txt blocks our crawler, the URL is not a sitemap or feed, it has no articles from
the last 72h (an archive), it is already a source, or it points at a private/internal address.

## 3c. Digests and thumbnails (what makes a card worth tapping)

**Thumbnail.** Every story has an original card image drawn by the engine: category colours and motif,
the card title, region, outlet count and source names. No publisher photos, so no image-rights exposure.
- `story.thumbnail.wide`: 1200x630 PNG, for feed cards and link previews
- `story.thumbnail.square`: 1080x1080 PNG, for grids and Instagram
- Public URLs (no key), cacheable (`ETag`, `Cache-Control: public, max-age=3600`): use them directly in `<img>`.
- Set `BRAND_NAME` on the API service to print the app's name in the corner.

**Digest.** A 250-500 word account of the story that people read in the app, written by Claude in its own
words from the full text of every outlet covering it, with each claim attributed and the sources listed.
- `story.digest_status`: `ready` | `updating` (a new outlet joined; the old digest is still served) |
  `not_ready` | `unavailable` (no usable text, or it kept quoting the source)
- `story.card_title` and `story.dek`: a punchy title and one-line standfirst for the card (once ready)
- `GET /v1/stories/{id}/digest` returns `{status, card_title, digest: {card_title, dek, body, key_points,
  word_count, sources, kind, material, model, generated_at}, links, thumbnail}`. If it is not ready yet
  and the engine has an LLM key, it is written on the spot (10-30 s). `?generate=false` only reads.
- Lists and feeds: add `include_digest=true` to get `digest` on every item (heavier responses).
- The worker writes digests in the background for the best fresh stories first (most outlets, then
  postability), up to `DIGEST_PER_HOUR` (default 60). Needs `ANTHROPIC_API_KEY` on the worker.

**Reading view rule:** show the digest `body`, then "Reported by" with each `sources[].name` linked to
its `url`. Keep the link to the originals visible.

## 4. The story object (what the app renders)

```json
{
  "id": "a643f883d73f4785",            // stable forever: safe to store in the app's DB
  "headline": "WhatsApp Rolls Out Message Scheduling Up To 14 Days In Advance",
  "summary": "Engine-written summary (null until LLM tagging is on)",
  "excerpt": "Publisher's own description, max 240 chars",
  "published_at": "2026-11-25T16:57:51Z", "updated_at": "2026-11-26T05:45:02Z",
  "source_count": 2, "article_count": 2, "sources": ["Inc42", "TechCrunch"],
  "image": "https://...", "is_trending": false, "is_roundup": false,
  "tag_status": "done",                // pending | rules | done | failed
  "tags": {
    "category": ["Social Media", "Big Tech"], "news_type": "Product launch",
    "audience": ["Marketers", "Creators"], "region": ["Global"], "tone": "Positive",
    "shelf_life": "This week", "entities": ["WhatsApp", "Meta"], "postability": 82,
    "angles": ["Scheduling turns WhatsApp into a CRM-lite", "..."]
  },
  "card_title": "WhatsApp adds scheduled messages",   // from the digest, else null: fall back to headline
  "dek": "Users can now queue messages up to 14 days ahead.",
  "thumbnail": {"wide": "https://<engine-host>/thumbnails/a643f883d73f4785.png",
                "square": "https://<engine-host>/thumbnails/a643f883d73f4785.png?size=square"},
  "image_credit": "TechCrunch",        // owner of `image` (publisher photo): show only with credit/permission
  "digest_status": "ready",            // ready | updating | not_ready | unavailable
  "digest": {...},                     // only in get_story / ?include_digest=true (see 3c)
  "links": [{"source": "TechCrunch", "source_id": "techcrunch", "url": "https://..."}],
  "rank": {"relevance": 0.9, "freshness": 0.95, "trending": 1.0, "postability": 0.91, "score": 1.16}  // feeds only
}
```

**Display rules (copyright):** cards use `thumbnail`, `card_title` (or `headline`) and `dek`; the reading view
uses the `digest` with its sources credited and linked. `excerpt` (publisher text) and `image` (publisher
photo) are optional and must be attributed. The engine never returns article bodies.
Posts written from briefs must add the user's own view and credit the sources listed in the brief.

## 5. Webhooks

Events: `story.created`, `story.tagged`, `story.trending` (fires once when a story reaches `TRENDING_SOURCE_COUNT` outlets).

```
POST <your url>
X-Engine-Event: story.trending
X-Engine-Signature: sha256=<hex HMAC-SHA256 of the raw body using the webhook secret>
{"event": "story.trending", "delivery_id": 42, "sent_at": "...", "data": {"story_id": "...", "headline": "...", "source_count": 3}}
```
Verify the signature, return 2xx quickly. Failures retry with backoff up to 8 times.

## 6. Recommended app flows

1. **Onboarding:** `GET /v1/taxonomy` to build pickers, then `PUT /v1/profiles/{user_id}`.
2. **Home feed:** `GET /v1/profiles/{user_id}/feed`. Show `rank` as "why you're seeing this" if useful.
3. **Browse/filter:** `GET /v1/stories` with the user's chosen filters.
4. **Create post:** `POST /v1/stories/{id}/brief` and send `system` + `prompt` to the app's LLM,
   or `POST /v1/stories/{id}/generate` if the engine holds the LLM key.
5. **Learn:** on click/save/post/dismiss call `POST /v1/profiles/{user_id}/events`.
6. **Real-time:** subscribe a webhook to `story.trending` for "post about this now" nudges.

## 7. Limits and behaviour to know

- Labels cover general news (World, Politics, Business, Economy, Markets, Technology, Science, Health,
  Environment, Sports, Entertainment, Culture, Lifestyle, Travel, Education, Crime & Justice) plus the
  startup/tech depth labels. Regions: Global, India, Karnataka, US, UK, Europe, Middle East, Asia-Pacific,
  China, Africa, Latin America, Australia. Always read them from `GET /v1/taxonomy`.

- Stories cover the last ~72h by default (`since_hours` up to 720 for lists; `KEEP_STORY_DAYS`=30 retention).
- Without an LLM key, tags come from keyword rules (`tag_status: "rules"`): good for filters, weaker on
  audience/tone, and `summary`/`angles` are empty. Adding `ANTHROPIC_API_KEY` re-tags them automatically.
- New sources: a homepage is enough; the engine finds the RSS feed and news sitemap itself.
- One worker process per deployment. The API can scale horizontally.
