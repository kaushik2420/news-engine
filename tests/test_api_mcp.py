import json

import pytest
from fastapi.testclient import TestClient

from tests.conftest import ADMIN, READ, full_cycle

A = {"Authorization": f"Bearer {ADMIN}"}
R = {"Authorization": f"Bearer {READ}"}
MCP_H = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


@pytest.fixture()
def client(engine_env):
    full_cycle(engine_env["fetcher"])
    from api.app import create_app
    with TestClient(create_app()) as c:
        yield c


def test_auth_scopes(client):
    assert client.get("/v1/stories").status_code == 401
    assert client.get("/health").status_code in (200, 503)                       # public
    assert client.get("/v1/stories", headers=R).status_code == 200
    assert client.get("/v1/whoami", headers=R).json() == {"scope": "read"}
    assert client.post("/v1/sources", headers=R, json={"id": "x1", "homepage": "https://x.com"}).status_code == 403
    assert client.post("/v1/sources", headers=A, json={"id": "x1", "homepage": "https://x.com"}).status_code == 201


def test_story_routes(client):
    r = client.get("/v1/stories", headers=R, params={"since_hours": 72, "category": ["Funding"], "limit": 50}).json()
    assert r["total"] >= 3 and all("Funding" in s["tags"]["category"] for s in r["items"])
    s = r["items"][0]
    assert "body" not in json.dumps(s) and s["links"][0]["url"].startswith("https://")
    full = client.get(f"/v1/stories/{s['id']}", headers=R).json()
    assert full["articles"] and full["id"] == s["id"]
    assert client.get("/v1/stories/nope", headers=R).status_code == 404
    t = client.get("/v1/stories/trending", headers=R, params={"hours": 48}).json()
    assert t["total"] == 2
    q = client.get("/v1/search", headers=R, params={"q": "whatsapp", "since_hours": 72}).json()
    assert q["total"] == 1


def test_profile_feed_events_and_briefs(client):
    p = {"role": "Fintech founder", "niche": "Indian payments", "goal": "credibility", "tone": "clear",
         "categories": ["Fintech"], "audiences": ["Founders"], "regions": ["India"], "platforms": ["linkedin"]}
    assert client.put("/v1/profiles/user-1", headers=R, json=p).status_code == 200
    assert client.put("/v1/profiles/user-2", headers=R, json={**p, "categories": ["Nope"]}).status_code == 400
    feed = client.get("/v1/profiles/user-1/feed", headers=R, params={"since_hours": 72}).json()["items"]
    assert "Fintech" in feed[0]["tags"]["category"] and "score" in feed[0]["rank"]
    top = feed[0]["id"]
    ev = client.post("/v1/profiles/user-1/events", headers=R, json={"story_id": top, "type": "dismiss"}).json()
    assert ev["learned_weights"]["Fintech"] < 0
    again = client.get("/v1/profiles/user-1/feed", headers=R, params={"since_hours": 72}).json()["items"]
    assert top not in {s["id"] for s in again}                                    # dismissed stories disappear
    b = client.post(f"/v1/stories/{top}/brief", headers=R, json={"platform": "linkedin", "profile_id": "user-1"}).json()
    assert "Fintech founder" in b["prompt"] and b["sources"] and "Never copy" in b["system"]
    assert client.post(f"/v1/stories/{top}/brief", headers=R, json={"platform": "myspace"}).status_code == 422
    assert client.post(f"/v1/stories/{top}/generate", headers=R, json={"platform": "x"}).status_code == 400  # no LLM key


def test_source_admin_routes(client):
    r = client.post("/v1/sources", headers=A, json={"id": "bad", "homepage": "not-a-url"})
    assert r.status_code == 400
    assert client.patch("/v1/sources/inc42", headers=A, json={"interval_min": 30}).json()["interval_min"] == 30
    got = client.post("/v1/sources/inc42/ingest", headers=A).json()
    assert got["source"] == "inc42" and "new_articles" in got
    assert client.get("/v1/sources/inc42", headers=R).json()["recent_runs"]
    assert client.get("/v1/runs", headers=R, params={"kind": "source"}).json()


def test_webhooks_queue_and_retry(client, engine_env):
    w = client.post("/v1/webhooks", headers=A, json={"url": "https://hooks.example/engine"}).json()
    assert w["secret"]
    client.post(f"/v1/webhooks/{w['id']}/test", headers=A)
    client.post("/v1/admin/cycle", headers=A)          # dispatch: the mock web returns 404 for the hook URL
    d = client.get("/v1/webhooks/deliveries", headers=A).json()
    ping = next(x for x in d if x["event"] == "ping")
    assert ping["status"] == "pending" and ping["attempts"] == 1 and "404" in ping["last_error"]


def _mcp(client, method, params, headers, rid=1, query=""):
    return client.post(f"/mcp{query}", headers={**MCP_H, **headers}, json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params})


def _tool(client, name, args, headers=A, query=""):
    r = _mcp(client, "tools/call", {"name": name, "arguments": args}, headers, query=query)
    assert r.status_code == 200, r.text
    return json.loads(r.json()["result"]["content"][0]["text"])


def test_mcp_end_to_end(client):
    assert _mcp(client, "tools/list", {}, {}).status_code == 401
    tools = {t["name"] for t in _mcp(client, "tools/list", {}, A).json()["result"]["tools"]}
    assert {"get_feed", "get_post_brief", "add_source", "engine_status", "search_stories"} <= tools
    assert _tool(client, "engine_status", {})["counts"]["stories"] == 33
    trend = _tool(client, "get_trending", {"hours": 48})
    sid = trend["items"][0]["id"]
    _tool(client, "upsert_profile", {"profile_id": "u9", "role": "Marketer", "categories": ["Social Media"]})
    feed = _tool(client, "get_feed", {"profile_id": "u9", "since_hours": 72, "limit": 3})
    assert feed["items"][0]["tags"]["category"][0] == "Social Media"
    brief = _tool(client, "get_post_brief", {"story_id": sid, "platform": "reel", "profile_id": "u9"})
    assert "HOOK" in brief["prompt"]


def test_mcp_read_key_cannot_write_and_query_key_works(client):
    denied = _tool(client, "add_source", {"id": "zz", "name": "ZZ", "homepage": "https://zz.example/"}, headers=R)
    assert "admin" in denied["error"]
    ok = _tool(client, "add_source", {"id": "zz", "name": "ZZ", "homepage": "https://zz.example/"}, headers={}, query=f"?key={ADMIN}")
    assert ok["id"] == "zz"
