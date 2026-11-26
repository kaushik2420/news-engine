# News Engine

The always-on backend brain for a social-content app. It collects news from publisher websites **without
publisher APIs**, merges coverage of the same event into one story, tags every story against a fixed taxonomy,
ranks personalised feeds, and turns any story into a brief for a LinkedIn post, X thread, Instagram carousel,
Reel or Story. Everything is reachable through a REST API and an MCP server.

**Attaching an app or Claude to it? Read [INTEGRATION.md](INTEGRATION.md).**

```
            ┌──────────────────────────── worker (runs all day) ────────────────────────────┐
sources ──► discover ──► store ──► cluster ──► enrich ──► tag ──► webhooks      retention
            news sitemap  articles  stable      page meta   LLM or   story.created   purge old
            + RSS         (dedupe   story IDs   + body      keyword  .tagged         bodies,
            + listing     by URL)               (internal)  rules    .trending       stories, logs
            + auto-find
            └────────────────────────────────────┬─────────────────────────────────────────┘
                                                Postgres
            ┌────────────────────────────────────┴─────────────────────────────────────────┐
            REST API /v1 (feeds, stories, profiles, events, briefs, sources, webhooks, admin)
            MCP /mcp     (19 tools for Claude)              /health  /docs
            └──────────────────────────────── api process ────────────────────────────────┘
```

## How discovery works

1. **News sitemap first.** Publishers keep a Google News sitemap listing every article from the last ~2 days
   with exact publish times. It catches far more than RSS (which usually shows only the newest 10–20).
2. **RSS second.** Fills summaries and images. Sitemap and RSS entries for the same URL are merged.
3. **Listing pages** as a fallback for sites with neither (`listing_url` + `link_pattern`).
4. **Auto-discovery.** Give a source just a `homepage`: the engine reads `<link rel="alternate">` for RSS and
   `robots.txt` for sitemaps, follows sitemap indexes, and prefers news sitemaps.
5. **Enrichment.** Articles missing a title, summary or image get their page fetched (og: tags + trafilatura).
   Untitled ones go first because they can't join a story until enriched.

Politeness is built in: robots.txt is obeyed, each domain is paced (`PER_DOMAIN_INTERVAL_SEC`), ETag /
Last-Modified are stored so unchanged feeds cost a 304, responses are size-capped, failing sources back off
exponentially (up to a day), and a source's health is visible in `/v1/sources`.

## Clustering

Each new article is compared with recent stories using weighted word overlap (rare words like names and
numbers count more). It joins the best match above `CLUSTER_THRESHOLD` or starts a new story. Story IDs never
change. Digest articles ("X, Y & More", "daily brief", "this week in") are kept as their own items so they can't
absorb real stories. The number of outlets on a story is the trending signal.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
pytest -q                                           # 21 tests, offline (SQLite)
TEST_DATABASE_URL=postgresql+psycopg://... pytest   # same suite on Postgres

python -m engine.worker --once --fixtures           # one cycle on captured real headlines, no network
python -m engine.worker                             # live, forever
uvicorn api.app:app --reload                        # API + MCP on :8000, docs at /docs
```
Or everything with Docker: `cp .env.example .env && docker compose up -d --build`.

## Deploy

The engine needs **two always-on processes** (API, worker) and **Postgres**. Any host that runs a Docker image
works. One image, two services:

| Service | Command |
|---|---|
| api | `uvicorn api.app:app --host 0.0.0.0 --port $PORT` (the Dockerfile default) |
| worker | `python -m engine.worker` |

Set `DATABASE_URL`, `ENGINE_API_KEY`, `ENGINE_READ_KEY` (and optionally `ANTHROPIC_API_KEY`) on both, then point
a subdomain such as `engine.backyardsaas.com` at the API service. Serverless platforms (e.g. Vercel functions)
suit the app but not the worker, which must stay running.

Health check for the host: `GET /health` (503 if the worker heartbeat is older than 5 minutes).

## Operating it

- Add a source: `POST /v1/sources {"id": "entrackr", "name": "Entrackr", "homepage": "https://entrackr.com/"}`
  then `POST /v1/sources/entrackr/ingest` to see what it finds. Or from Claude: `add_source` + `check_source_now`.
- Source health: `GET /v1/sources` (ok / degraded / failing / pending / disabled with `last_error`).
- What the engine did: `GET /v1/runs`, `GET /v1/status`.
- Change labels: edit `config/taxonomy.yaml` and redeploy. Seed sources: `config/sources.yaml`
  (only adds new ones; API edits are never overwritten).

## Layout

```
engine/   settings db text fetch discover extract cluster tagger llm rank generate webhooks ingest worker service runtime
api/      app (REST + mounts MCP)   mcp_server (tools)   auth (keys, scopes)
config/   sources.yaml  taxonomy.yaml
tests/    offline web (fixtures_transport.py) + discovery, pipeline, API and MCP tests
```
