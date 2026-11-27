import io
import json

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from engine import db, digest, llm, service
from tests.conftest import ADMIN, READ, full_cycle, run

A = {"Authorization": f"Bearer {ADMIN}"}
R = {"Authorization": f"Bearer {READ}"}
FILLER = ("Officials outlined the plan in detail while analysts weighed what the change could mean for users "
          "businesses and regulators across several markets over the coming months ").split()


def body_of(n_words: int) -> str:
    words = (FILLER * (n_words // len(FILLER) + 1))[:n_words]
    paras = [" ".join(words[i:i + 60]) + "." for i in range(0, len(words), 60)]
    return "\n\n".join(paras)


@pytest.fixture()
def fake_llm(monkeypatch):
    calls = []

    def complete_json(prompt, model, max_tokens=1500, system=None):
        calls.append(prompt)
        return {"card_title": "WhatsApp adds scheduled messages", "dek": "Users can now queue messages up to 14 days ahead.",
                "body": body_of(320), "key_points": ["Scheduling up to 14 days", "Works in groups"],
                "enough_material": True}
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "complete_json", complete_json)
    return calls


def whatsapp_id():
    return service.list_stories(q="whatsapp", since_hours=72)["items"][0]["id"]


def test_digest_is_generated_from_all_outlets_and_stored(engine_env, fake_llm):
    full_cycle(engine_env["fetcher"])
    sid = whatsapp_id()
    out = run(digest.generate(engine_env["fetcher"], sid))
    assert out["status"] == "done" and 300 <= out["word_count"] <= 340
    prompt = fake_llm[-1]
    assert "Inc42" in prompt and "TechCrunch" in prompt and "250-500 words" in prompt   # both outlets, length rule
    s = service.get_story(sid)
    assert s["digest_status"] == "ready" and s["card_title"] == "WhatsApp adds scheduled messages"
    dg = s["digest"]
    assert dg["kind"] == "multi_source" and {x["name"] for x in dg["sources"]} == {"Inc42", "TechCrunch"}
    assert dg["model"] and dg["generated_at"] and len(dg["key_points"]) == 2
    listed = service.list_stories(q="whatsapp", since_hours=72)["items"][0]
    assert "digest" not in listed and listed["dek"]                     # lists stay light unless asked
    assert service.list_stories(q="whatsapp", since_hours=72, include_digest=True)["items"][0]["digest"]["body"]


def test_copy_guard_rejects_drafts_that_reuse_source_sentences(engine_env, monkeypatch):
    full_cycle(engine_env["fetcher"])
    sid = whatsapp_id()
    with db.get_engine().connect() as c:
        src = c.execute(select(db.articles.c.summary).where(db.articles.c.story_id == sid)).first().summary
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: {"card_title": "x", "dek": "y", "enough_material": True,
                                                                "body": src + " " + body_of(250), "key_points": []})
    out = run(digest.generate(engine_env["fetcher"], sid))
    assert out["status"] == "failed" and "copying" in out["reason"]
    assert service.get_story(sid)["digest_status"] == "unavailable"


def test_new_outlet_marks_digest_for_update(engine_env, fake_llm):
    full_cycle(engine_env["fetcher"])
    sid = whatsapp_id()
    run(digest.generate(engine_env["fetcher"], sid))
    from engine.cluster import Clusterer
    with db.get_engine().begin() as c:          # a third outlet reports the same event
        c.execute(db.sources.insert().values(id="third", name="Third", enabled=False))
        c.execute(db.articles.insert().values(id="x3", source_id="third", url="https://third.example/wa",
                                              title="WhatsApp rolls out message scheduling for chats, up to 14 days ahead",
                                              summary="WhatsApp users can schedule messages 14 days in advance.",
                                              published_at=db.utcnow(), created_at=db.utcnow()))
        sid2, created = Clusterer(c).assign(c, {"id": "x3", "source_id": "third", "title":
                                               "WhatsApp rolls out message scheduling for chats, up to 14 days ahead",
                                               "summary": "WhatsApp users can schedule messages 14 days in advance.",
                                               "published_at": db.utcnow()})
    assert sid2 == sid and not created
    assert service.get_story(sid)["digest_status"] == "updating"      # old digest still served meanwhile
    assert service.get_story(sid)["digest"]["body"]


def test_background_digests_respect_budget_and_need_llm_tags(engine_env, fake_llm, monkeypatch):
    full_cycle(engine_env["fetcher"])
    with db.get_engine().begin() as c:        # full_cycle already digested some (LLM mocked): start clean
        c.execute(db.stories.update().values(tag_status="rules", digest=None, digest_status=None, digest_at=None,
                                             digest_attempts=0))
    assert run(digest.digest_pending(engine_env["fetcher"]))["made"] == 0          # rule-tagged: not eligible
    with db.get_engine().begin() as c:
        c.execute(db.stories.update().values(tag_status="done"))
    from engine import settings
    monkeypatch.setattr(settings.settings, "digest_per_hour", 2)
    assert run(digest.digest_pending(engine_env["fetcher"], limit=5))["made"] == 2
    assert run(digest.digest_pending(engine_env["fetcher"], limit=5))["mode"] == "budget_reached"
    with db.get_engine().connect() as c:                                            # best stories first
        done = c.execute(select(db.stories.c.source_count).where(db.stories.c.digest_status == "done")).all()
    assert all(r.source_count == 2 for r in done)


def test_without_llm_key_digests_are_off(engine_env):
    full_cycle(engine_env["fetcher"])
    assert run(digest.digest_pending(engine_env["fetcher"]))["mode"] == "off"
    from api.app import create_app
    with TestClient(create_app()) as c:
        r = c.get(f"/v1/stories/{whatsapp_id()}/digest", headers=R).json()
        assert r["status"] == "not_ready" and r["digest"] is None


def test_digest_routes_mcp_and_briefs(engine_env, fake_llm):
    full_cycle(engine_env["fetcher"])
    sid = whatsapp_id()
    from api.app import create_app
    with TestClient(create_app()) as c:
        r = c.get(f"/v1/stories/{sid}/digest", headers=R).json()                 # generated on demand
        assert r["status"] == "ready" and r["digest"]["word_count"] > 250
        assert c.post(f"/v1/stories/{sid}/digest", headers=R).status_code == 403 # regenerate needs admin
        assert c.post(f"/v1/stories/{sid}/digest", headers=A).json()["status"] == "ready"
        b = c.post(f"/v1/stories/{sid}/brief", headers=R, json={"platform": "linkedin"}).json()
        assert "Officials outlined the plan" in b["prompt"]                         # briefs use the digest
        h = {**A, "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        m = c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "get_story_digest", "arguments": {"story_id": sid}}})
        assert json.loads(m.json()["result"]["content"][0]["text"])["digest"]["card_title"]


def test_thumbnails_are_public_cached_png(engine_env, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://engine.backyardsaas.com")
    from engine import settings
    settings.reload()
    full_cycle(engine_env["fetcher"])
    s = service.list_stories(q="whatsapp", since_hours=72)["items"][0]
    assert s["thumbnail"]["wide"] == f"https://engine.backyardsaas.com/thumbnails/{s['id']}.png"
    from api.app import create_app
    with TestClient(create_app()) as c:
        r = c.get(f"/thumbnails/{s['id']}.png")                                     # no key needed
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        assert Image.open(io.BytesIO(r.content)).size == (1200, 630)
        sq = c.get(f"/thumbnails/{s['id']}.png", params={"size": "square"})
        assert Image.open(io.BytesIO(sq.content)).size == (1080, 1080)
        again = c.get(f"/thumbnails/{s['id']}.png", headers={"If-None-Match": r.headers["etag"]})
        assert again.status_code == 304
        assert c.get("/thumbnails/nope.png").status_code == 404
        assert c.get(f"/thumbnails/{s['id']}.png", params={"size": "huge"}).status_code == 422


def test_every_category_renders(engine_env):
    from engine import tagger, thumbnail
    for cat in tagger.taxonomy()["category"]:
        png = thumbnail.render({"id": cat, "headline": "A headline long enough to wrap onto a few lines of the card "
                                "so that fitting and ellipsis are exercised properly", "tags": {"category": [cat],
                                "region": ["India"]}, "source_count": 3, "sources": ["A", "B", "C", "D"]}, "square")
        assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_sensitive_stories_get_a_sober_card():
    from engine import thumbnail
    assert thumbnail.is_sensitive({"category": ["Entertainment", "Crime & Justice"]})
    assert thumbnail.is_sensitive({"category": ["World"], "news_type": "Disaster / emergency"})
    assert thumbnail.is_sensitive({"category": ["World"], "tone": "Negative"})
    assert not thumbnail.is_sensitive({"category": ["Sports"], "tone": "Positive"})
    a = thumbnail.render({"id": "x", "headline": "Actor to stand trial", "source_count": 6, "sources": ["A"],
                          "tags": {"category": ["Entertainment", "Crime & Justice"]}}, "wide")
    b = thumbnail.render({"id": "x", "headline": "Actor to stand trial", "source_count": 6, "sources": ["A"],
                          "tags": {"category": ["Entertainment"]}}, "wide")
    assert a != b
