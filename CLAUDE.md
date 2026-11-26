# Working on the News Engine

Backend-only service: a continuous worker plus a REST/MCP API over Postgres. No UI. The consuming app and its
Claude integrate via INTEGRATION.md, which is a contract: keep it accurate when routes, tools or the story shape change.

## Rules
- `engine/service.py` is the single source of behaviour. REST routes (`api/app.py`) and MCP tools
  (`api/mcp_server.py`) are thin wrappers. Add features there first, then expose them in both.
- Story IDs are stable and stored by the app. Never re-key or re-cluster existing stories.
- Never return article `body` or long publisher text from any route or tool. `summary` is engine-written;
  `excerpt` is capped at 240 chars.
- Respect robots.txt and per-domain pacing; don't add code paths that bypass `engine/fetch.py`.
- Taxonomy values come only from `config/taxonomy.yaml`; `tagger.validate()` drops anything else.
- Timestamps are naive UTC (`db.utcnow()`); API output uses `db.iso()` (Z-suffixed).
- Run `pytest -q` before committing; tests run offline against `tests/fixtures_transport.py`.
  Add fixture routes there rather than hitting the network in tests.
