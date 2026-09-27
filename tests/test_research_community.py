import hashlib
import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from agent_monitor import console_server as console, jobs, lean_verify, research, run_sharing


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(research, "DB_PATH", tmp_path / "research.sqlite3")
    monkeypatch.setattr(run_sharing, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(run_sharing, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.delenv("LLM_DASHBOARD_CACHE", raising=False)
    (tmp_path / "runs").mkdir()


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(console, "BASE_PATH", "/test")
    monkeypatch.setattr(console.Handler, "_current_user", lambda self: {"id": int(self.headers["X-User"]), "name": "Researcher", "guest": self.headers.get("X-Guest") == "yes"} if self.headers.get("X-User") else None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), console.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(method, path, body=None, user=None, headers=None):
        conn = http.client.HTTPConnection(*server.server_address, timeout=5)
        h = {"Content-Type": "application/json", **(headers or {})}
        if user is not None:
            h["X-User"] = str(user)
        conn.request(method, "/test" + path, body=json.dumps(body) if body is not None else None, headers=h)
        response = conn.getresponse()
        data = json.loads(response.read())
        conn.close()
        return response.status, data
    yield request
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def test_forum_persistence_permissions_and_replies(api):
    endpoint = "/api/research/forum"
    assert api("GET", endpoint)[1]["total"] == 0
    post = {"title": "An arithmetic question", "body": "Can this argument be extended?", "kind": "problem", "source_url": "https://example.org/problem"}
    assert api("POST", endpoint, post)[0] == 401
    assert api("POST", endpoint, post, user=1, headers={"X-Guest": "yes"})[0] == 401
    assert api("POST", endpoint, post, user=1, headers={"Origin": "https://elsewhere.test"})[0] == 403
    assert api("POST", endpoint, {**post, "source_url": "javascript:alert(1)"}, user=1)[0] == 400
    status, item = api("POST", endpoint, post, user=1)
    assert status == 200 and "owner" not in item
    key = item["id"]
    assert api("GET", endpoint + "?q=arithmetic")[1]["total"] == 1
    assert api("GET", endpoint + "?q=absent")[1]["total"] == 0
    assert api("POST", endpoint + f"/{key}/replies", {"body": "Here is a useful reference."}, user=2)[0] == 200
    result = api("GET", endpoint + "/" + key)[1]
    assert result["replies"][0]["body"] == "Here is a useful reference."
    assert result["thread"]["kind"] == "problem"
    assert api("GET", endpoint)[1]["threads"][0]["replies"] == 1
    # A fresh connection still sees the saved post, and forum text is never memory.
    assert research.get_thread(key)["thread"]["title"] == post["title"]
    assert research.compose_context("arithmetic argument extended") == ""


@pytest.fixture
def run(monkeypatch):
    rid = "test-run"
    monkeypatch.setattr(jobs, "run_owner", lambda candidate: 1 if candidate == rid else None)
    row = {"run_id": rid, "status": "finished", "title": "Automorphic representations", "agents": [{"role": "assistant", "status": "finished", "model": "gpt-6", "model_source": "response", "output": "Automorphic representations have arithmetic structure. Check every assumption."}]}
    path = run_sharing.RUNS_DIR / (rid + ".json")
    path.write_text(json.dumps(row))
    ws = run_sharing.RUNS_DIR / "workspaces" / rid
    ws.mkdir(parents=True)
    return rid, row, path, ws


def test_dag_rejects_spoofed_model_and_requires_owner_publication(api, run):
    rid, row, path, _ = run
    endpoint = "/api/research/dag"
    payload = {"source_run_id": rid, "node_id": "automorphic", "layer": "informal", "publish": True}
    assert api("POST", endpoint, payload, user=2)[0] == 404
    assert api("POST", endpoint, {**payload, "publish": False}, user=1)[0] == 400
    row["agents"][0]["model"] = "gpt-5-mini"
    path.write_text(json.dumps(row))
    assert api("POST", endpoint, {**payload, "model": "gpt-6"}, user=1)[0] == 400
    row["agents"][0]["model"] = "gpt-6"
    path.write_text(json.dumps(row))
    status, item = api("POST", endpoint, payload, user=1)
    assert status == 200 and item["model"] == "gpt-6"
    assert "arithmetic structure" in research.compose_context("automorphic arithmetic representations")
    assert api("POST", endpoint, payload, user=1)[1]["id"] == item["id"]
    assert len(api("GET", endpoint)[1]["memories"]) == 1
    assert api("DELETE", endpoint + "/" + item["id"], user=2)[0] == 404
    assert api("DELETE", endpoint + "/" + item["id"], user=1)[0] == 200
    assert research.compose_context("automorphic arithmetic representations") == ""


def test_formal_memory_requires_matching_verified_audited_model_source(api, run, monkeypatch):
    rid, _, _, ws = run
    (ws / "lean").mkdir()
    source = "theorem example : True := by trivial"
    (ws / "lean/Proof.lean").write_text(source)
    payload = {"source_run_id": rid, "node_id": "formal-example", "layer": "formal", "publish": True}
    cached = {"status": "verified", "sorry_count": 0, "lean": source, "model": "gpt-6", "attempts": [{"check": {"ok": True}}], "fidelity": {"severity": "ok", "audit": {"ok": True, "faithful": True}}, "source_provenance": {"model": "gpt-6", "sha256": hashlib.sha256(source.encode()).hexdigest()}}
    # Exercise the hosted receipt binding as well as the local checker path.
    # Receipt signature/release validation is covered by the checker suite.
    monkeypatch.setattr(lean_verify, "_trusted_kernel_receipt", lambda value: value, raising=False)
    (ws / "lean_verify.json").write_text(json.dumps(cached))
    assert api("POST", "/api/research/dag", payload, user=1)[0] == 400
    cached["verified_source_sha256"] = hashlib.sha256(source.encode()).hexdigest()
    cached["kernel_receipt"] = {"source_sha256": cached["verified_source_sha256"]}
    (ws / "lean_verify.json").write_text(json.dumps(cached))
    assert api("POST", "/api/research/dag", payload, user=1)[0] == 200
    (ws / "lean/Proof.lean").write_text(source + "\n-- edited")
    assert api("POST", "/api/research/dag", payload, user=1)[0] == 400
    (ws / "lean/Proof.lean").write_text(source)
    cached["source_provenance"]["model"] = "gpt-5-mini"
    (ws / "lean_verify.json").write_text(json.dumps(cached))
    assert api("POST", "/api/research/dag", payload, user=1)[0] == 400


def test_manual_lean_edits_lose_model_authorship(tmp_path, monkeypatch):
    monkeypatch.setattr(lean_verify, "toolchain_status", lambda: {})
    common = {"workspace": tmp_path, "status": "verified", "uses_mathlib": False, "attempts": []}
    source = "theorem example : True := by trivial"
    generated = lean_verify._persist(**common, lean_src=source, model=lean_verify._ObservedModel("gpt-6", "gpt-6"), action="verify")
    assert generated["source_provenance"]["model"] == "gpt-6"
    assert generated["lean"] == source + "\n"
    assert generated["source_provenance"]["sha256"] == hashlib.sha256((source + "\n").encode()).hexdigest()
    checked = lean_verify._persist(**common, lean_src=source, action="check")
    assert checked["source_provenance"] == generated["source_provenance"]
    edited = lean_verify._persist(**common, lean_src=source + "\n-- manual edit", action="check")
    assert edited["source_provenance"] == {}


def test_real_fable_ids_are_eligible_but_unrecognized_suffixes_are_not():
    assert research.approved_model("claude-fable-5")
    assert research.approved_model("claude-fable-5-1")
    assert not research.approved_model("claude-fable-5-mini")


def test_memory_graph_paginates_previews_and_loads_full_node(api, run):
    rid, row, path, _ = run
    row["agents"][0]["output"] = "An arithmetic observation. " * 100
    path.write_text(json.dumps(row))
    for index in range(3):
        response = api("POST", "/api/research/dag", {"source_run_id": rid, "node_id": f"stacks:00A{index}", "layer": "informal", "publish": True}, user=1)
        assert response[0] == 200
    first = api("GET", "/api/research/dag?limit=2&layer=informal", user=1)[1]
    assert first["total"] == 3 and first["has_more"] is True
    assert len(first["memories"]) == 2
    assert first["memories"][0]["content_truncated"] is True
    assert len(first["memories"][0]["content"]) == 1200
    second = api("GET", f"/api/research/dag?limit=2&layer=informal&offset={first['next_offset']}")[1]
    assert len(second["memories"]) == 1 and second["has_more"] is False
    ids = {item["id"] for item in first["memories"] + second["memories"]}
    assert len(ids) == 3
    item = first["memories"][0]
    full = api("GET", "/api/research/dag/" + item["id"], user=1)[1]
    assert len(full["content"]) > 1200 and full["is_owner"] is True
    assert api("GET", "/api/research/dag?layer=invalid")[0] == 400
    assert api("GET", "/api/research/dag?offset=9223372036854775808")[0] == 400


def test_harness_retrieves_matching_memory_older_than_latest_500():
    with research.connect() as db:
        for index in range(502):
            content = "Automorphic representations have arithmetic structure." if index in (0, 501) else "An unrelated example."
            model = "gpt-5-mini" if index == 501 else "gpt-6"
            db.execute("INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?)", (str(index), "stacks:0001", "Observation", content, "informal", model, "Model result", f"run-{index}", 1, index, "digest"))
    result = research.compose_context("automorphic arithmetic representations")
    assert "Source run: run-0 |" in result
    assert "run-501" not in result
    assert "run-500" not in result


def test_research_api_rejects_non_object_payloads(api):
    assert api("POST", "/api/research/forum", ["unexpected"], user=1)[0] == 400


def test_formal_authorship_uses_response_model_not_requested_model(tmp_path, monkeypatch):
    from agent_monitor import settings
    monkeypatch.setattr(lean_verify, "toolchain_status", lambda: {})
    if hasattr(lean_verify, "_resolve_llm"):
        monkeypatch.setattr(lean_verify, "_resolve_llm", lambda env, model: ("https://api.openai.com/v1", "key", "gpt-6-astra"))
    else:
        from agent_monitor import proof_graph
        monkeypatch.setattr(proof_graph, "_resolve_llm_provider", lambda env, model: ("compatible", "https://api.openai.com/v1", "key", "gpt-6-astra"))
    source = "theorem example : True := by trivial"
    common = {"workspace": tmp_path, "lean_src": source, "status": "verified", "uses_mathlib": False, "attempts": [], "action": "verify"}
    for actual in ("gpt-5-mini", "gpt-6-astra", None):
        response = {"choices": [{"message": {"content": source}}]}
        if actual:
            response["model"] = actual
        monkeypatch.setattr(settings, "_http_json", lambda *args, **kwargs: (200, response))
        model, content = lean_verify._chat({}, "gpt-6-astra", "prove it")
        assert str(model) == "gpt-6-astra"  # Preserve routing for repairs.
        persisted = lean_verify._persist(**common, model=model)
        assert persisted["source_provenance"].get("model") == actual
    # An arbitrary caller-selected model name is never authorship evidence.
    assert lean_verify._persist(**common, model="gpt-6-astra")["source_provenance"] == {}
