"""Workstream E1: the Ansatz CGM engine wiring and public-hosting hardening.

These tests never contact a provider: model calls, the sponsored broker and
subprocess launches are replaced, and HTTP checks use loopback servers only.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet

from agent_monitor import auth, jobs, kimi_k3, proof_graph, settings

ROOT = Path(__file__).resolve().parents[1]


# ── engine catalog and job environment ──────────────────────────────────────


def test_engine_labels_keep_ids_and_prefer_the_pinned_codex_path():
    from agent_monitor import engines_registry

    catalog = {item["id"]: item for item in engines_registry.list_engines()}
    assert catalog["kimi"]["label"] == "Ansatz CGM · Kimi K3"
    assert "guest-safe" in catalog["kimi"]["description"]
    assert catalog["math_harness"]["label"] == "Ansatz CGM harness"
    assert "any account route" in catalog["math_harness"]["description"]
    assert catalog["kimi"]["auth_modes"] == ["api_key"]
    assert "plain" in catalog
    assert engines_registry._DEFAULT_EXTRA_PATHS[0] == "~/.npm-global/bin"
    assert engines_registry._DEFAULT_EXTRA_PATHS.index(
        "~/.npm-global/bin"
    ) < engines_registry._DEFAULT_EXTRA_PATHS.index("~/.local/bin")


def test_kimi_runs_use_the_cgm_pipeline_stage_columns():
    assert jobs._pipeline_for("kimi") == jobs._pipeline_for("math_harness")
    assert [stage["id"] for stage in jobs._pipeline_for("kimi")] == [
        "scaffold", "work", "verify", "curate", "finalize",
    ]
    assert [stage["label"] for stage in jobs._pipeline_for("kimi")] == [
        "1 Scaffold", "2 Explore", "3 Verify", "4 Curate", "5 Synthesize",
    ]


def test_analyze_runs_reuse_the_stage_ids_with_their_own_labels():
    # The Analyze runner announces the same trusted ids (cli_events gates
    # them) with its own stages: Ingest, Segment, Check, Roll-up, Report.
    from agent_monitor import cli_events

    stages = jobs._pipeline_for("analyze")
    assert [stage["id"] for stage in stages] == [stage["id"] for stage in jobs._pipeline_for("kimi")]
    assert [stage["label"] for stage in stages] == [
        "1 Ingest", "2 Segment", "3 Check", "4 Roll-up", "5 Report",
    ]
    assert "analyze" in cli_events.PIPELINE_STAGE_ENGINES
    parser = cli_events.CLIEventParser("codex", source_engine="analyze")
    parser.feed('{"type": "pipeline.stage", "stage": "verify", "label": "3 Check"}\n')
    assert parser.pipeline_stage == "verify"
    untrusted = cli_events.CLIEventParser("codex", source_engine="codex")
    untrusted.feed('{"type": "pipeline.stage", "stage": "verify", "label": "3 Check"}\n')
    assert untrusted.pipeline_stage is None


def test_bounded_run_env_pins_guest_profile_and_cgm_output_ceiling():
    env = {"AGENT_MONITOR_MAX_OUTPUT_TOKENS": "256", "AGENT_MONITOR_GUEST": "0"}
    jobs._apply_bounded_run_env(env, engine="kimi", guest=True, sponsored=False)
    assert env == {"AGENT_MONITOR_MAX_OUTPUT_TOKENS": "8192", "AGENT_MONITOR_GUEST": "1"}
    sponsored = {"AGENT_MONITOR_MAX_OUTPUT_TOKENS": "20000"}
    jobs._apply_bounded_run_env(sponsored, engine="math_harness", guest=False, sponsored=True)
    assert sponsored == {"AGENT_MONITOR_MAX_OUTPUT_TOKENS": "8192"}
    own_key = {"AGENT_MONITOR_MAX_OUTPUT_TOKENS": "20000"}
    jobs._apply_bounded_run_env(own_key, engine="kimi", guest=False, sponsored=False)
    assert own_key == {"AGENT_MONITOR_MAX_OUTPUT_TOKENS": "20000"}
    plain_guest: dict[str, str] = {"AGENT_MONITOR_MAX_OUTPUT_TOKENS": "4096"}
    jobs._apply_bounded_run_env(plain_guest, engine="plain", guest=True, sponsored=False)
    assert plain_guest == {"AGENT_MONITOR_MAX_OUTPUT_TOKENS": "4096", "AGENT_MONITOR_GUEST": "1"}


def _start_kimi(monkeypatch, tmp_path, *, user_env, sponsored=False, user=None):
    captured: list[dict] = []

    class FakeThread:
        def __init__(self, *, kwargs=None, **_rest):
            captured.append(dict(kwargs or {}))

        def start(self):
            return None

    meta = {"id": "kimi", "label": "Ansatz CGM · Kimi K3", "available": True,
            "auth_modes": ["api_key"]}
    monkeypatch.setattr("agent_monitor.engines_registry.list_engines", lambda: [meta])
    monkeypatch.setattr(jobs, "_user_extra_env", lambda *_a, **_k: dict(user_env))
    monkeypatch.setattr(jobs, "workspace_dir", lambda _run_id: tmp_path)
    monkeypatch.setattr(jobs, "_write_run", lambda run: None)
    monkeypatch.setattr(jobs, "_JOBS", {})
    monkeypatch.setattr(jobs, "_STOP_EVENTS", {})
    monkeypatch.setattr(jobs.threading, "Thread", FakeThread)
    from agent_monitor import sponsored_kimi_client

    issued: list[dict] = []
    monkeypatch.setattr(sponsored_kimi_client, "configured", lambda: sponsored)
    monkeypatch.setattr(sponsored_kimi_client, "endpoint", lambda: "http://127.0.0.1:4610")
    monkeypatch.setattr(
        sponsored_kimi_client,
        "issue_credentials",
        lambda **kwargs: issued.append(kwargs) or {
            "KIMI_API_KEY": "t" * 64,
            "KIMI_API_BASE": "http://127.0.0.1:4610/internal/v1",
            "AGENT_MONITOR_SPONSORED_KIMI": "1",
            "AGENT_MONITOR_SPONSORED_KIMI_URL": "http://127.0.0.1:4610",
        },
    )
    jobs.start_job(
        engine="kimi",
        problem_text="Prove it.",
        model="kimi-k3",
        user=user or {"id": 41, "email": "member@example.test"},
        auth_route="api_key",
    )
    return captured[0]["extra_env"], issued


def test_own_key_kimi_runs_use_only_the_operator_endpoint(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_MONITOR_KIMI_BASE_URL", raising=False)
    user_env = {
        "KIMI_API_KEY": "user-owned-kimi-key-123456789",
        "KIMI_API_BASE": "http://169.254.169.254/latest",
        "KIMI_BASE_URL": "http://10.0.0.1/v1",
    }
    env, issued = _start_kimi(monkeypatch, tmp_path, user_env=user_env)
    assert issued == []
    assert env["KIMI_API_BASE"] == env["KIMI_BASE_URL"] == kimi_k3.DEFAULT_BASE_URL
    assert env["AGENT_MONITOR_KIMI_BASE_URL"] == kimi_k3.DEFAULT_BASE_URL
    assert "AGENT_MONITOR_GUEST" not in env

    monkeypatch.setenv("AGENT_MONITOR_KIMI_BASE_URL", "https://kimi.operator.example/v1/")
    env, _ = _start_kimi(monkeypatch, tmp_path, user_env=user_env)
    assert env["KIMI_API_BASE"] == "https://kimi.operator.example/v1"
    for bad in ("http://kimi.operator.example/v1", "https://u:p@kimi.example/v1", "file:///etc/passwd"):
        monkeypatch.setenv("AGENT_MONITOR_KIMI_BASE_URL", bad)
        env, _ = _start_kimi(monkeypatch, tmp_path, user_env=user_env)
        assert env["KIMI_API_BASE"] == kimi_k3.DEFAULT_BASE_URL


def test_signed_in_sponsored_kimi_runs_get_the_bounded_cgm_profile(tmp_path, monkeypatch):
    env, issued = _start_kimi(
        monkeypatch, tmp_path, user_env=dict(auth._NEW_USER_DEFAULTS), sponsored=True
    )
    assert issued and issued[0]["engine"] == "plain"
    # The sponsored flag alone selects the CGM guest budget profile; the TTL
    # covers the whole 2400 s pipeline plus one API timeout and the margin.
    assert issued[0]["minimum_ttl_seconds"] >= 2400 + 300 + 300
    assert env["AGENT_MONITOR_MAX_OUTPUT_TOKENS"] == "8192"
    assert env["KIMI_BASE_URL"] == "http://127.0.0.1:4610/internal/v1"
    assert "AGENT_MONITOR_GUEST" not in env


def test_new_accounts_default_to_the_kimi_cgm_engine():
    assert auth._NEW_USER_DEFAULTS["AGENT_MONITOR_ENGINE"] == "kimi"
    assert auth._NEW_USER_DEFAULTS["AGENT_MONITOR_MODEL"] == "kimi-k3"


def test_kimi_proof_is_not_rewritten_when_a_cgm_receipt_exists(tmp_path):
    proof = "# Claim\n\nA deterministic CGM closure that must keep its receipt hash.\n"
    (tmp_path / "proof.md").write_text(proof, encoding="utf-8")
    receipt = tmp_path / "math_harness" / "project" / "engine_receipt.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text("{}", encoding="utf-8")
    run = {"status": "finished", "engine": "kimi", "model": "kimi-k3",
           "credential_source": "sponsored_kimi"}
    assert jobs._normalize_terminal_kimi_outcome(run, tmp_path) is False
    assert (tmp_path / "proof.md").read_text(encoding="utf-8") == proof


def test_guest_completion_hook_is_best_effort(tmp_path, monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(jobs, "_GUEST_COMPLETION_HOOK", lambda *args: seen.append(args))
    jobs._notify_guest_completion("r1", 7, tmp_path)
    jobs._notify_guest_completion("r2", None, tmp_path)
    assert seen == [("r1", 7, tmp_path)]

    def broken(*_args):
        raise RuntimeError("capacity")

    monkeypatch.setattr(jobs, "_GUEST_COMPLETION_HOOK", broken)
    jobs._notify_guest_completion("r3", 7, tmp_path)  # never raises


def test_purge_guest_runs_selects_only_old_finished_guest_runs(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(jobs, "_cache_dir", lambda: cache)
    monkeypatch.setattr(jobs, "_JOBS", {})
    old = "2020-01-01T00:00:00+00:00"
    records = {
        "kimi_old_guest": {"guest": True, "status": "finished", "updated_at": old},
        "kimi_old_legacy": {"owner": "guest+ab@public.local", "status": "failed", "updated_at": old},
        "kimi_new_guest": {"guest": True, "status": "finished", "updated_at": jobs._now()},
        "kimi_old_member": {"guest": False, "status": "finished", "updated_at": old},
        "kimi_old_running": {"guest": True, "status": "running", "updated_at": old},
    }
    for run_id, record in records.items():
        (cache / f"{run_id}.json").write_text(json.dumps({"run_id": run_id, **record}), encoding="utf-8")
    deleted: list[str] = []
    monkeypatch.setattr(jobs, "delete_run", lambda run_id: deleted.append(run_id))
    assert jobs.purge_guest_runs(older_than_days=30) == ["kimi_old_guest", "kimi_old_legacy"]
    assert deleted == []
    assert jobs.purge_guest_runs(older_than_days=30, dry_run=False) == [
        "kimi_old_guest", "kimi_old_legacy",
    ]
    assert deleted == ["kimi_old_guest", "kimi_old_legacy"]


# ── CLI event projection ────────────────────────────────────────────────────


def _feed(parser, *events):
    for event in events:
        parser.feed(json.dumps(event) + "\n")


def test_pipeline_stage_events_place_turns_in_cgm_columns():
    from agent_monitor.cli_events import CLIEventParser

    for engine in ("kimi", "math_harness"):
        parser = CLIEventParser("codex", source_engine=engine)
        _feed(
            parser,
            {"type": "thread.started", "thread_id": "math-harness"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "header"}},
            {"type": "pipeline.stage", "stage": "work", "label": "2 Explore", "detail": "strategist"},
            {"type": "item.completed", "item": {"type": "reasoning", "text": "strategist"}},
            {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 7}},
            {"type": "pipeline.stage", "stage": "verify", "label": "3 Verify"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "verdict"}},
            {"type": "pipeline.stage", "stage": "bogus", "label": "x"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "still verify"}},
        )
        nodes = parser.agent_nodes("run", status="finished")
        assert [node["pipeline_stage"] for node in nodes] == [
            "scaffold", "work", "verify", "verify",
        ], engine
        assert nodes[1]["input_tokens"] == 5
        assert any(line.startswith("stage · 2 Explore") for line in parser.lines)

    # Other engines never trust runner-supplied stage ids.
    parser = CLIEventParser("codex", source_engine="codex")
    _feed(
        parser,
        {"type": "pipeline.stage", "stage": "verify", "label": "3 Verify"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "hi"}},
    )
    assert [node["pipeline_stage"] for node in parser.agent_nodes("run")] == ["write"]


# ── console process defaults and startup key check ──────────────────────────


def test_console_binds_loopback_by_default():
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"AGENT_MONITOR_HOST"}
    }
    env["AGENT_MONITOR_ENV_PATH"] = "/dev/null"
    completed = subprocess.run(
        [sys.executable, "-c", "from agent_monitor import console_server as c; print(c.HOST)"],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().splitlines()[-1] == "127.0.0.1"


@pytest.fixture()
def keyless(monkeypatch):
    for name in (
        "AGENT_MONITOR_DATA_KEY_FILE",
        "AGENT_MONITOR_DATA_ENCRYPTION_KEY",
        "AGENT_MONITOR_SECRET_KEY",
        "CREDENTIALS_DIRECTORY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("AGENT_MONITOR_REQUIRE_DATA_KEY", raising=False)


def test_required_data_key_fails_fast_at_startup(keyless, monkeypatch, tmp_path):
    from agent_monitor import console_server as console

    monkeypatch.setenv("AGENT_MONITOR_REQUIRE_DATA_KEY", "1")
    with pytest.raises(SystemExit, match="refusing to start"):
        console._open_account_database()
    monkeypatch.setenv("AGENT_MONITOR_DATA_ENCRYPTION_KEY", "not-a-fernet-key")
    with pytest.raises(SystemExit, match="refusing to start"):
        console._open_account_database()
    missing = tmp_path / "missing.key"
    monkeypatch.delenv("AGENT_MONITOR_DATA_ENCRYPTION_KEY")
    monkeypatch.setenv("AGENT_MONITOR_DATA_KEY_FILE", str(missing))
    with pytest.raises(SystemExit, match="refusing to start"):
        console._open_account_database()

    # A valid key opens and migrates the database before serving.
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.delenv("AGENT_MONITOR_DATA_KEY_FILE")
    monkeypatch.setenv("AGENT_MONITOR_DATA_ENCRYPTION_KEY", key)
    console._open_account_database()
    assert auth.DB_PATH.exists()
    user = auth.register("owner@example.test", "long-enough-password")
    auth.set_user_env(int(user["id"]), {"KIMI_API_KEY": "user-owned-kimi-key-123"})

    # A different (valid) key cannot authenticate the stored settings.
    monkeypatch.setattr(auth, "_INITIALIZED", False)
    monkeypatch.setenv("AGENT_MONITOR_DATA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    with pytest.raises(SystemExit, match="refusing to start"):
        console._open_account_database()

    # Without the requirement a broken key only warns (development).
    monkeypatch.delenv("AGENT_MONITOR_REQUIRE_DATA_KEY")
    monkeypatch.setattr(auth, "_INITIALIZED", False)
    console._open_account_database()


def test_keyless_admin_defaults_are_rewrapped_by_a_running_console(keyless, monkeypatch):
    # `agent-monitor create-admin` runs without the root-only data key.
    admin = auth.register("admin@example.test", "long-enough-password")
    with sqlite3.connect(auth.DB_PATH) as conn:
        stored = dict(conn.execute(
            "SELECT key, value FROM user_env WHERE user_id=?", (admin["id"],)
        ).fetchall())
    assert stored["AGENT_MONITOR_ENGINE"] == "kimi"  # plaintext seed

    # The console already initialized its database with the key.
    monkeypatch.setenv("AGENT_MONITOR_DATA_ENCRYPTION_KEY", Fernet.generate_key().decode("ascii"))
    assert auth._INITIALIZED is True
    assert auth.get_user_env(int(admin["id"])) == auth._NEW_USER_DEFAULTS
    with sqlite3.connect(auth.DB_PATH) as conn:
        values = [row[0] for row in conn.execute(
            "SELECT value FROM user_env WHERE user_id=?", (admin["id"],)
        )]
    assert values and all(value.startswith("enc:v1:") for value in values)
    assert auth.get_user_env(int(admin["id"])) == auth._NEW_USER_DEFAULTS

    # Any other plaintext value still fails closed.
    with sqlite3.connect(auth.DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO user_env (user_id, key, value) VALUES (?,?,?)",
            (admin["id"], "KIMI_API_KEY", "sk-plaintext-secret-123456"),
        )
    with pytest.raises(auth.UserEnvDecryptionError):
        auth.get_user_env(int(admin["id"]))


# ── provider verification (SSRF) ────────────────────────────────────────────


def test_non_admin_cannot_verify_against_custom_bases(monkeypatch):
    urls: list[str] = []
    monkeypatch.setattr(
        settings, "_http_json",
        lambda url, **_kwargs: urls.append(url) or (401, {"error": {"message": "bad key"}}),
    )
    member = {"id": 7, "is_admin": False}
    monkeypatch.setattr(settings, "resolved_user_env", lambda _user: {"OPENAI_API_KEY": "sk-member-123456789"})
    refused = settings.verify_provider(
        "openai", base_url="http://169.254.169.254/latest/meta-data", user=member
    )
    assert refused["ok"] is False and "operator" in refused["error"]
    monkeypatch.setattr(
        settings, "resolved_user_env",
        lambda _user: {"OPENAI_API_KEY": "sk-member-123456789", "OPENAI_BASE_URL": "http://10.0.0.5/v1"},
    )
    stored = settings.verify_provider("openai", user=member)
    assert stored["ok"] is False and "operator" in stored["error"]
    assert urls == []

    # The built-in endpoint still works, and its JSON error message is shown.
    monkeypatch.setattr(settings, "resolved_user_env", lambda _user: {"OPENAI_API_KEY": "sk-member-123456789"})
    result = settings.verify_provider(
        "openai", base_url="https://api.openai.com/v1/", user=member
    )
    assert urls == ["https://api.openai.com/v1/models"]
    assert result["error"] == "bad key"
    # Kimi checks go to the operator's endpoint, never an account base.
    monkeypatch.setattr(settings, "resolved_user_env", lambda _user: {"KIMI_API_KEY": "kimi-member-123456789"})
    settings.verify_provider("kimi", user=member)
    assert urls[-1] == kimi_k3.DEFAULT_BASE_URL + "/chat/completions"


def test_verification_never_echoes_upstream_bodies_from_custom_bases(monkeypatch):
    admin = {"id": 1, "is_admin": True}
    monkeypatch.setattr(settings, "resolved_user_env", lambda _user: {"OPENAI_API_KEY": "sk-admin-123456789"})
    replies = iter([
        (200, "<html>internal metadata secret</html>"),  # non-dict 200 on openai counts as ok
        (500, "internal metadata secret"),
        (403, {"error": {"message": "internal metadata secret"}}),
    ])
    urls: list[str] = []
    monkeypatch.setattr(settings, "_http_json", lambda url, **_k: urls.append(url) or next(replies))
    settings.verify_provider("openai", base_url="http://127.0.0.1:9/v1", user=admin)
    failed = settings.verify_provider("openai", base_url="http://127.0.0.1:9/v1", user=admin)
    rejected = settings.verify_provider("openai", base_url="http://127.0.0.1:9/v1", user=admin)
    assert urls == ["http://127.0.0.1:9/v1/models"] * 3  # the operator may use a custom base
    for result in (failed, rejected):
        assert result["ok"] is False
        assert "secret" not in json.dumps(result)
    assert "HTTP 500" in failed["error"] and "HTTP 403" in rejected["error"]


class _Redirect(BaseHTTPRequestHandler):
    target = ""

    def log_message(self, *_args):
        pass

    def do_POST(self):  # noqa: N802
        self.send_response(302)
        self.send_header("Location", self.target)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_GET = do_POST


class _Sink(BaseHTTPRequestHandler):
    hits: list[str] = []

    def log_message(self, *_args):
        pass

    def do_GET(self):  # noqa: N802
        type(self).hits.append(str(self.headers.get("Authorization")))
        body = b"{}"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET


def test_provider_http_never_follows_redirects_with_credentials():
    sink = ThreadingHTTPServer(("127.0.0.1", 0), _Sink)
    redirect = ThreadingHTTPServer(("127.0.0.1", 0), _Redirect)
    _Redirect.target = f"http://127.0.0.1:{sink.server_address[1]}/stolen"
    _Sink.hits = []
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (sink, redirect)]
    for thread in threads:
        thread.start()
    try:
        code, _body = settings._http_json(
            f"http://127.0.0.1:{redirect.server_address[1]}/v1/chat/completions",
            method="POST",
            headers={"Authorization": "Bearer secret-token"},
            payload={"x": 1},
            timeout=5,
        )
    finally:
        for server in (sink, redirect):
            server.shutdown()
            server.server_close()
    assert code == 302
    assert _Sink.hits == []


def test_operator_kimi_base_accepts_only_credential_free_https(monkeypatch):
    monkeypatch.delenv("AGENT_MONITOR_KIMI_BASE_URL", raising=False)
    assert settings.operator_kimi_base_url() == kimi_k3.DEFAULT_BASE_URL
    monkeypatch.setenv("AGENT_MONITOR_KIMI_BASE_URL", "https://kimi.example/v1/")
    assert settings.operator_kimi_base_url() == "https://kimi.example/v1"
    for bad in ("http://kimi.example/v1", "https://a:b@kimi.example/v1",
                "https://kimi.example/v1?x=1", "https://:99999/v1", "ftp://kimi.example"):
        monkeypatch.setenv("AGENT_MONITOR_KIMI_BASE_URL", bad)
        assert settings.operator_kimi_base_url() == kimi_k3.DEFAULT_BASE_URL
    # Not an account setting: users can never save it.
    assert "AGENT_MONITOR_KIMI_BASE_URL" not in settings._known_env_keys()


# ── informal DAG from CGM facts ─────────────────────────────────────────────

_FACTS = [
    {"fact_id": "a" * 16, "statement": "Lemma: every even $n$ is $2k$.", "proof": "p1",
     "predecessors": [], "is_target": False, "attested": True},
    {"fact_id": "b" * 16, "statement": "Lemma: $2a + 2b = 2(a+b)$.", "proof": "p2",
     "predecessors": [], "is_target": False, "attested": True},
    {"fact_id": "c" * 16, "statement": "The sum of two even integers is even.", "proof": "p3",
     "predecessors": ["a" * 16, "b" * 16, "a" * 16], "is_target": True, "attested": True},
]


def _with_facts(monkeypatch, facts):
    from agent_monitor import engine_graph

    monkeypatch.setattr(engine_graph, "load_fact_dag", lambda _ws: [dict(f) for f in facts] if facts else None)


def test_fact_dag_becomes_a_deterministic_informal_graph(monkeypatch, tmp_path):
    graph = proof_graph.graph_from_facts(_FACTS)
    assert [node["id"] for node in graph["nodes"]] == ["a" * 16, "b" * 16, "c" * 16]
    assert [node["kind"] for node in graph["nodes"]] == ["lemma", "lemma", "conclusion"]
    assert [node["label"] for node in graph["nodes"]] == ["Lemma 1", "Lemma 2", "Target theorem"]
    assert graph["nodes"][2]["statement"] == "The sum of two even integers is even."
    assert graph["edges"] == [
        {"from": "a" * 16, "to": "c" * 16, "label": "uses"},
        {"from": "b" * 16, "to": "c" * 16, "label": "uses"},
    ]
    assert "not a Lean certificate" in " ".join(graph["nodes"][0]["citations"])
    assert graph == proof_graph.graph_from_facts(_FACTS)  # deterministic

    cyclic = [dict(_FACTS[0], predecessors=["c" * 16]), _FACTS[1], _FACTS[2]]
    with pytest.raises(ValueError):
        proof_graph.graph_from_facts(cyclic)

    long_statement = [dict(_FACTS[2], statement="x" * 5000, predecessors=[])]
    assert len(proof_graph.graph_from_facts(long_statement)["nodes"][0]["statement"]) == 1200


def test_informal_generation_uses_facts_without_a_model_call(monkeypatch, tmp_path):
    _with_facts(monkeypatch, _FACTS)
    (tmp_path / "proof.md").write_text("# Claim\n\nproof text " * 20, encoding="utf-8")

    def no_model(*_args, **_kwargs):
        raise AssertionError("a model was called")

    monkeypatch.setattr(proof_graph, "_call_llm", no_model)
    monkeypatch.setattr(proof_graph, "_run_claude_prompt", no_model)
    monkeypatch.setattr(proof_graph, "_run_codex_prompt", no_model)
    result = proof_graph.generate(
        workspace=tmp_path, run_record={"engine": "kimi"}, user={"id": 1, "guest": True},
        model="kimi-k3", kind="informal",
    )
    assert result["model"] == "cgm-fact-graph"
    assert result["status"] == "derived" and result["attested"] is True
    assert result["certificate"] is False
    assert len(result["graph"]["nodes"]) == 3
    cached = proof_graph.load_cached(tmp_path, kind="informal")
    assert cached["model"] == "cgm-fact-graph"
    assert cached["graph"] == result["graph"]


def test_load_or_build_informal_refreshes_from_changed_facts(monkeypatch, tmp_path):
    _with_facts(monkeypatch, None)
    assert proof_graph.load_or_build_informal(tmp_path) is None

    _with_facts(monkeypatch, _FACTS)
    built = proof_graph.load_or_build_informal(tmp_path)
    assert built["model"] == "cgm-fact-graph" and len(built["graph"]["nodes"]) == 3
    assert proof_graph.load_cached(tmp_path, kind="informal")["model"] == "cgm-fact-graph"
    again = proof_graph.load_or_build_informal(tmp_path)
    assert again["generated_at"] == built["generated_at"]

    _with_facts(monkeypatch, [dict(_FACTS[2], predecessors=[])])
    changed = proof_graph.load_or_build_informal(tmp_path)
    assert [node["id"] for node in changed["graph"]["nodes"]] == ["c" * 16]


def test_reading_the_informal_dag_never_replaces_a_model_derived_graph(monkeypatch, tmp_path):
    # Lean "Informal: <node>" links point at the model graph's node ids; a GET
    # must not silently swap that graph for the fact graph.
    model_graph = {
        "model": "gpt-5-mini", "kind": "informal", "source": "proof.md",
        "graph": {"title": "t", "nodes": [{"id": "n1", "kind": "conclusion", "label": "x",
                                           "statement": "s", "citations": []}], "edges": []},
    }
    proof_graph._write_cached(tmp_path, proof_graph.GRAPH_FILENAME, model_graph)
    _with_facts(monkeypatch, _FACTS)
    assert proof_graph.load_or_build_informal(tmp_path)["model"] == "gpt-5-mini"
    assert proof_graph.load_cached(tmp_path, kind="informal")["model"] == "gpt-5-mini"
    # An explicit refresh still switches the run to the deterministic graph.
    refreshed = proof_graph.generate(
        workspace=tmp_path, run_record={"engine": "math_harness"}, user={"id": 1},
        model="kimi-k3", kind="informal",
    )
    assert refreshed["model"] == "cgm-fact-graph"
    assert proof_graph.load_or_build_informal(tmp_path)["model"] == "cgm-fact-graph"


def test_fact_graph_requires_one_target_and_labels_unattested_facts(monkeypatch, tmp_path):
    from agent_monitor import auto_pipeline

    # Verifier-partial runs have lemmas but no proved target: no fact graph.
    partial = [dict(_FACTS[0], attested=False), dict(_FACTS[1], attested=False)]
    with pytest.raises(ValueError):
        proof_graph.graph_from_facts(partial)
    _with_facts(monkeypatch, partial)
    assert proof_graph.fact_graph_result(tmp_path) is None
    assert proof_graph.load_or_build_informal(tmp_path) is None

    _with_facts(monkeypatch, _FACTS)
    attested = proof_graph.fact_graph_result(tmp_path)
    assert attested["attested"] is True
    assert attested["basis"] == proof_graph.FACT_GRAPH_ATTESTED_BASIS

    unattested_facts = [dict(fact, attested=False) for fact in _FACTS]
    _with_facts(monkeypatch, unattested_facts)
    unattested = proof_graph.fact_graph_result(tmp_path)
    assert unattested["attested"] is False and unattested["certificate"] is False
    assert unattested["basis"] == proof_graph.FACT_GRAPH_UNATTESTED_BASIS
    assert all(
        "model-verifier accepted" not in " ".join(node["citations"])
        for node in unattested["graph"]["nodes"]
    )

    # The pipeline stage detail follows the attestation, never a fixed claim.
    stages: list[dict] = []
    monkeypatch.setattr(
        auto_pipeline, "_set_stage",
        lambda _ws, stage, status, **kwargs: stages.append({"status": status, **kwargs}),
    )
    monkeypatch.setattr(proof_graph, "generate", lambda **_kwargs: unattested)
    auto_pipeline._informal_worker(tmp_path, {"engine": "kimi"}, {"id": 2}, "kimi-k3")
    done = stages[-1]
    assert done["status"] == "done"
    assert "unattested CGM facts" in done["detail"]
    assert "model-verif" not in done["detail"]
    assert done["result"]["attested"] is False

    monkeypatch.setattr(proof_graph, "generate", lambda **_kwargs: attested)
    auto_pipeline._informal_worker(tmp_path, {"engine": "kimi"}, {"id": 2}, "kimi-k3")
    assert "model-verifier accepted" in stages[-1]["detail"]
    assert stages[-1]["result"]["attested"] is True


def test_graph_prompt_keeps_the_output_contract_in_cgm_style():
    prompt = proof_graph.GRAPH_PROMPT
    for token in ("{problem}", "{proof}", '"nodes"', '"edges"', "STRICT JSON",
                  "Exactly one node", "acyclic", "self-contained", "Never invent a source"):
        assert token in prompt


def test_fact_runs_build_the_informal_dag_before_lean(monkeypatch, tmp_path):
    from agent_monitor import auto_pipeline

    order: list[str] = []
    monkeypatch.setattr(proof_graph, "fact_graph_result", lambda _ws: {"model": "cgm-fact-graph"})
    monkeypatch.setattr(proof_graph, "_proof_source", lambda _ws, _rec: ("proof.md", ""))

    def informal(*_args):
        time.sleep(0.05)
        order.append("informal")

    monkeypatch.setattr(auto_pipeline, "_informal_worker", informal)
    monkeypatch.setattr(auto_pipeline, "_lean_and_formal_worker", lambda *_a: order.append("lean"))
    # A signed-in own-key run would otherwise run both workers concurrently.
    auto_pipeline._run_batch(
        tmp_path, {"engine": "kimi", "credential_source": "user", "auth_route": "api_key"},
        {"id": 3}, "kimi-k3", "kimi",
    )
    assert order == ["informal", "lean"]


# ── broker process and ledger ───────────────────────────────────────────────


def test_broker_stops_cleanly_on_sigterm(tmp_path):
    script = tmp_path / "serve.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, {str(ROOT)!r})\n"
        "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
        "from agent_monitor import public_kimi_server as p\n"
        "server = ThreadingHTTPServer(('127.0.0.1', 0), BaseHTTPRequestHandler)\n"
        "p._install_shutdown_handlers(server)\n"
        "print('ready', flush=True)\n"
        "try:\n"
        "    server.serve_forever(poll_interval=0.1)\n"
        "finally:\n"
        "    server.server_close()\n"
        "print('stopped', flush=True)\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env={**os.environ, "AGENT_MONITOR_ENV_PATH": "/dev/null"},
    )
    try:
        assert proc.stdout.readline().strip() == "ready"
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, err
    assert "stopped" in out


def _open_ledger(path: str, start_at: float, queue) -> None:
    from agent_monitor import usage_ledger

    usage_ledger.DB_PATH = Path(path)
    usage_ledger._INITIALIZED.clear()
    while time.time() < start_at:
        pass
    try:
        with usage_ledger._connect() as conn:
            conn.execute("SELECT count(*) FROM runs_index").fetchone()
        queue.put(None)
    except Exception as exc:  # noqa: BLE001
        queue.put(repr(exc))


def test_fresh_ledger_survives_concurrent_first_opens(tmp_path):
    context = multiprocessing.get_context("fork")
    failures: list[str] = []
    for trial in range(4):
        path = str(tmp_path / f"usage-{trial}.db")
        queue = context.Queue()
        start_at = time.time() + 0.4
        workers = [
            context.Process(target=_open_ledger, args=(path, start_at, queue))
            for _ in range(6)
        ]
        for worker in workers:
            worker.start()
        results = [queue.get(timeout=60) for _ in workers]
        for worker in workers:
            worker.join(timeout=30)
        failures.extend(result for result in results if result)
    assert failures == []
    with sqlite3.connect(tmp_path / "usage-0.db") as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


# ── DeepAgents formal checker ───────────────────────────────────────────────


def _langchain_tool_modules():
    class StructuredTool:
        def __init__(self, func):
            self.func = func

        @classmethod
        def from_function(cls, *, func, **_kwargs):
            return cls(func)

    tools = SimpleNamespace(StructuredTool=StructuredTool)
    return {"langchain_core": SimpleNamespace(tools=tools), "langchain_core.tools": tools}


def test_deepagents_lean_check_runs_the_trusted_check_script(tmp_path, monkeypatch):
    from agent_monitor.runners import deepagents_runner

    (tmp_path / "Proof.lean").write_text("theorem main : True := trivial\n", encoding="utf-8")
    (tmp_path / "lakefile.lean").write_text("package p\n", encoding="utf-8")
    script = "#!/bin/sh\necho checked \"$1\" from \"$0\"\n"
    (tmp_path / "check.sh").write_text(script, encoding="utf-8")
    for name, module in _langchain_tool_modules().items():
        monkeypatch.setitem(sys.modules, name, module)
    captured: list[list[str]] = []
    real_run = subprocess.run

    def run(argv, **kwargs):
        captured.append(list(argv))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(deepagents_runner.subprocess, "run", run)
    state: dict = {}
    tool = deepagents_runner._create_lean_check_tool(tmp_path, state)
    output = tool.func()
    assert captured[-1][:2] == ["/bin/sh", "-c"]
    assert captured[-1][2] == script
    assert captured[-1][3:] == [str(tmp_path / "check.sh"), "Proof.lean"]
    assert output.startswith("exit 0") and "checked Proof.lean" in output
    assert state["exit_code"] == 0 and state["command"] == ["check.sh", "Proof.lean"]

    # The agent's file tools can edit check.sh; a changed wrapper never runs.
    (tmp_path / "check.sh").write_text("#!/bin/sh\ntouch pwned\n", encoding="utf-8")
    calls = len(captured)
    output = tool.func()
    assert output.startswith("exit 2") and "refusing" in output
    assert len(captured) == calls and not (tmp_path / "pwned").exists()
    (tmp_path / "check.sh").unlink()
    (tmp_path / "check.sh").symlink_to(tmp_path / "Proof.lean")
    assert tool.func().startswith("exit 2")


# ── deploy bundle ───────────────────────────────────────────────────────────


def test_console_unit_prefers_the_pinned_codex_and_hides_the_user_manager():
    unit = (ROOT / "deploy" / "ansatze" / "agent-monitor.service").read_text(encoding="utf-8")
    path_line = next(line for line in unit.splitlines() if line.startswith("Environment=PATH="))
    entries = path_line.removeprefix("Environment=PATH=").split(":")
    assert entries[0] == "/home/ubuntu/.npm-global/bin"
    assert entries.index("/home/ubuntu/.npm-global/bin") < entries.index("/home/ubuntu/.local/bin")
    inaccessible = [line for line in unit.splitlines() if line.startswith("InaccessiblePaths=")]
    assert len(inaccessible) == 1 and "-/run/user" in inaccessible[0].split()


def test_setup_prefers_the_uv_interpreter_path_install_sh_requires():
    setup = (ROOT / "setup.sh").read_text(encoding="utf-8")
    assert setup.index("uv python find 3.13") < setup.index("for cand in python3 python3.13")


def test_guest_policy_advertises_the_enforced_cgm_bounds():
    from agent_monitor import console_server as console
    from agent_monitor import lean_verify

    policy = console.guest_policy(None)
    assert policy["default_engine"] == "kimi"
    assert policy["proof_graph_generation"] is True and policy["lean_actions"] is True
    assert policy["formal_harness"] is False
    assert policy["cgm_max_output_tokens"] == 8192
    assert policy["max_lean_repairs"] == lean_verify.GUEST_MAX_REPAIRS == 4
    assert policy["max_feedback_repairs"] == console._GUEST_REVISE_MAX_REPAIRS == 1


def test_guest_feedback_revision_cap_matches_the_lean_pipeline(tmp_path, monkeypatch):
    """The advertised guest revise cap is what revise_with_feedback enforces."""
    from agent_monitor import console_server as console
    from agent_monitor import lean_verify

    seen: list[int] = []
    monkeypatch.setattr(lean_verify, "harness_job", lambda _ws: None)
    monkeypatch.setattr(
        lean_verify, "_revise_with_feedback_impl",
        lambda **kwargs: seen.append(kwargs["max_repairs"]) or {"status": "running"},
    )
    lean_verify.revise_with_feedback(
        workspace=tmp_path, user={"id": 9, "guest": True}, message="fix", max_repairs=3,
    )
    assert seen == [console._GUEST_REVISE_MAX_REPAIRS]
    assert lean_verify.GUEST_REVISE_MAX_REPAIRS == console._GUEST_REVISE_MAX_REPAIRS == 1
    assert console._bounded_repairs(
        9, default=1, cap=console._REVISE_MAX_REPAIRS, guest=True,
        guest_cap=console._GUEST_REVISE_MAX_REPAIRS,
    ) == 1


# ── derived calls: operator-chosen endpoints only ──────────────────────────


def test_non_admin_derived_env_keeps_only_operator_bases(monkeypatch):
    monkeypatch.setattr(settings, "_read_env_file", lambda: {"OPENROUTER_BASE_URL": "https://gw.operator.example/v1"})
    monkeypatch.delenv("AGENT_MONITOR_KIMI_BASE_URL", raising=False)
    for name in ("OPENAI_BASE_URL", "OPENROUTER_BASE_URL", "KIMI_API_BASE", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    stored = {
        "OPENAI_API_KEY": "sk-member-123456789",
        "OPENAI_BASE_URL": "http://10.0.0.5/v1?",
        "ANTHROPIC_BASE_URL": "http://169.254.169.254/latest",
        "KIMI_API_BASE": "http://127.0.0.1:9/v1",
        "KIMI_BASE_URL": "http://127.0.0.1:9/v1",
        "AGENT_MONITOR_BASE_URL": "http://10.0.0.6",
        "OPENROUTER_BASE_URL": "https://gw.operator.example/v1/",
    }
    member = settings.restrict_account_base_urls(dict(stored), {"id": 5, "is_admin": False})
    assert member == {
        "OPENAI_API_KEY": "sk-member-123456789",
        "OPENROUTER_BASE_URL": "https://gw.operator.example/v1/",
        "KIMI_API_BASE": kimi_k3.DEFAULT_BASE_URL,
    }
    guest = settings.restrict_account_base_urls(dict(stored), {"id": 6, "is_admin": True, "guest": True})
    assert "OPENAI_BASE_URL" not in guest
    assert settings.restrict_account_base_urls(dict(stored), {"id": 1, "is_admin": True}) == stored
    assert settings.restrict_account_base_urls(dict(stored), None) == stored


def test_member_dag_generation_never_uses_a_stored_custom_base(monkeypatch, tmp_path):
    _with_facts(monkeypatch, None)
    (tmp_path / "problem.txt").write_text("Prove it.", encoding="utf-8")
    (tmp_path / "proof.md").write_text("# Claim\n\n" + "A proof step. " * 40, encoding="utf-8")
    monkeypatch.setattr(settings, "_read_env_file", lambda: {})
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setattr(
        settings, "resolved_user_env",
        lambda _user: {"OPENAI_API_KEY": "sk-member-123456789", "OPENAI_BASE_URL": "http://10.0.0.5/v1?"},
    )
    seen: list[str] = []

    def fake_http(url, **_kwargs):
        seen.append(url)
        return 503, {"error": {"message": "unavailable"}}

    monkeypatch.setattr(settings, "_http_json", fake_http)
    result = proof_graph.generate(
        workspace=tmp_path, run_record={"engine": "plain", "auth_route": "api_key"},
        user={"id": 5, "is_admin": False}, model="gpt-5-mini-2025-08-07",
        kind="informal", allow_codex_fallback=False,
    )
    # The stored private base was replaced by the built-in endpoint; the
    # failed call then fell back to the structural parse.
    assert seen == ["https://api.openai.com/v1/responses"]
    assert result["model"] == "structural-parse"


def test_member_lean_calls_never_use_a_stored_custom_base(monkeypatch):
    from agent_monitor import lean_verify, sponsored_kimi_client

    monkeypatch.setattr(settings, "_read_env_file", lambda: {})
    for name in ("OPENAI_BASE_URL", "ANTHROPIC_BASE_URL", "KIMI_API_BASE", "AGENT_MONITOR_KIMI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    stored = {
        "OPENAI_API_KEY": "sk-member-123456789",
        "OPENAI_BASE_URL": "http://10.0.0.5/v1?",
        "ANTHROPIC_BASE_URL": "http://169.254.169.254/latest",
        "KIMI_API_BASE": "http://127.0.0.1:9/v1",
    }
    monkeypatch.setattr(settings, "resolved_user_env", lambda _user: dict(stored))
    record = {"engine": "plain", "auth_route": "api_key", "run_id": "r1", "owner_id": 5}
    member = {"id": 5, "is_admin": False}

    env = lean_verify._llm_environment(member, record)
    for name in ("OPENAI_BASE_URL", "ANTHROPIC_BASE_URL"):
        assert name not in env
    assert env["KIMI_API_BASE"] == kimi_k3.DEFAULT_BASE_URL
    assert env["OPENAI_API_KEY"] == "sk-member-123456789"

    # The translation request itself goes to the provider's built-in endpoint.
    seen: list[str] = []

    def fake_http(url, **_kwargs):
        seen.append(url)
        return 503, {"error": {"message": "unavailable"}}

    monkeypatch.setattr(settings, "_http_json", fake_http)
    with pytest.raises(ValueError):
        lean_verify._chat(env, "gpt-5-mini-2025-08-07", "Translate this proof.")
    assert seen == ["https://api.openai.com/v1/responses"]

    # A sponsored run still gets its attested loopback credential afterwards.
    monkeypatch.setattr(
        sponsored_kimi_client, "issue_credentials",
        lambda **_kwargs: {
            "KIMI_API_KEY": "loopback-token-123456789",
            "KIMI_API_BASE": "http://127.0.0.1:4610/internal/v1",
        },
    )
    sponsored = lean_verify._llm_environment(member, {**record, "credential_source": "sponsored_kimi"})
    assert sponsored["KIMI_API_BASE"] == "http://127.0.0.1:4610/internal/v1"
    assert "OPENAI_BASE_URL" not in sponsored

    # The operator and the admin keep their configured endpoints.
    for operator in (None, {"id": 1, "is_admin": True}):
        kept = lean_verify._llm_environment(operator, {**record, "owner_id": 1})
        assert kept["OPENAI_BASE_URL"] == "http://10.0.0.5/v1?"
        assert kept["KIMI_API_BASE"] == "http://127.0.0.1:9/v1"


def test_derived_calls_never_echo_non_public_endpoint_bodies(monkeypatch):
    monkeypatch.setattr(
        settings, "_http_json",
        lambda url, **_kwargs: (404, {"error": {"message": "metadata: iam/credentials"}}),
    )
    for base in ("http://10.0.0.5/v1?", "https://169.254.169.254/latest", "http://127.0.0.1:8080/v1"):
        with pytest.raises(ValueError) as raised:
            proof_graph._call_llm(
                {"OPENAI_API_KEY": "sk-admin-123456789", "OPENAI_BASE_URL": base},
                "gpt-5-mini-2025-08-07", "prompt",
            )
        assert "metadata" not in str(raised.value) and "HTTP 404" in str(raised.value)
    # The attested sponsored broker and public providers keep their messages.
    for base in ("http://127.0.0.1:4610/internal/v1", "https://api.openai.com/v1"):
        with pytest.raises(ValueError) as raised:
            proof_graph._call_llm(
                {"OPENAI_API_KEY": "sk-admin-123456789", "OPENAI_BASE_URL": base},
                "gpt-5-mini-2025-08-07", "prompt",
            )
        assert "metadata: iam/credentials" in str(raised.value)


# ── kimi engine end to end down to the HTTP layer (no provider contacted) ──

_CGM_STRATEGY = json.dumps(
    {
        "immediate_conclusions": [],
        "obstacles": ["None."],
        "plans": [
            {
                "label": "Direct",
                "technique": "plain",
                "approach": "Write both integers as multiples of 2.",
                "subgoals": [{"id": "S1", "statement": "2a + 2b = 2(a + b)."}],
                "risks": "None.",
            }
        ],
    }
)
_CGM_WORKER = """=== LEMMA L1 ===
Subgoal: S1
Uses: none
Statement:
For all integers a and b, 2a + 2b = 2(a + b).
Proof:
By distributivity of multiplication over addition.
=== END ===

=== THEOREM ===
Uses: L1
Statement:
(the problem)
Proof:
Write the even integers as 2a and 2b; by L1 their sum is 2(a + b), which is even.
=== END ===

=== STATUS ===
solved
=== END ==="""
_CGM_CORRECT = json.dumps(
    {
        "verification_report": {"summary": "Sound.", "critical_errors": [], "gaps": []},
        "verdict": "correct",
        "repair_hints": "",
    }
)
_CGM_CURATOR = json.dumps({"annotations": []})


def _scripted_kimi_http(requests: list[dict]):
    from agent_monitor.cgm import prompts

    lock = threading.Lock()

    def fake_http(url, *, method="GET", headers=None, payload=None, timeout=None, **_kwargs):
        with lock:
            requests.append({"url": url, "method": method, "headers": dict(headers or {}),
                             "payload": json.loads(json.dumps(payload))})
        system = next(
            (m.get("content") for m in (payload or {}).get("messages") or [] if m.get("role") == "system"),
            "",
        )
        if system == prompts.STRATEGIST_SYSTEM:
            text = _CGM_STRATEGY
        elif system.startswith(prompts.WORKER_SYSTEM):
            text = _CGM_WORKER
        elif system == prompts.VERIFIER_SYSTEM:
            text = _CGM_CORRECT
        elif system == prompts.CURATOR_SYSTEM:
            text = _CGM_CURATOR
        else:
            raise AssertionError("unexpected system prompt")
        return 200, {
            "model": "kimi-k3",
            "choices": [{"message": {"role": "assistant", "content": text}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
        }

    return fake_http


@pytest.mark.parametrize("sponsored", [True, False])
def test_kimi_runner_sends_only_bounded_tool_free_requests_to_the_pinned_base(
    sponsored, tmp_path, monkeypatch, capsys
):
    from agent_monitor.runners import kimi_runner

    (tmp_path / "problem.txt").write_text(
        "Prove that the sum of two even integers is even.", encoding="utf-8"
    )
    for name in list(os.environ):
        if name.startswith(("AGENT_MONITOR_", "KIMI_", "OPENAI_")) or name.lower().endswith("_proxy"):
            monkeypatch.delenv(name, raising=False)
    hostile = {
        "AGENT_MONITOR_GUEST": "1",
        "AGENT_MONITOR_MAX_OUTPUT_TOKENS": "8192",
        "AGENT_MONITOR_API_MAX_TOKENS": "999999",
        "AGENT_MONITOR_CGM_CONCURRENCY": "1",
        "AGENT_MONITOR_CODEX_SUBSCRIPTION": "1",
        "KIMI_API_KEY": "guest-loopback-token-123456789",
        "KIMI_BASE_URL": "http://169.254.169.254/latest",
        "HTTPS_PROXY": "http://10.0.0.9:3128",
    }
    if sponsored:
        hostile["AGENT_MONITOR_SPONSORED_KIMI"] = "1"
        hostile["KIMI_API_BASE"] = "http://127.0.0.1:4610/internal/v1"
        expected_base = "http://127.0.0.1:4610/internal/v1"
    else:
        hostile["KIMI_API_BASE"] = "http://10.0.0.5/v1"  # user-settable: ignored
        expected_base = kimi_k3.DEFAULT_BASE_URL
    for name, value in hostile.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["kimi_runner.py", "Prove the problem in problem.txt."])
    requests: list[dict] = []
    monkeypatch.setattr(settings, "_http_json", _scripted_kimi_http(requests))

    assert kimi_runner.main() == 0

    assert requests, "the CGM pipeline made no provider call"
    roles = set()
    for call in requests:
        assert call["url"] == f"{expected_base}/chat/completions"
        assert call["method"] == "POST"
        assert call["headers"]["Authorization"] == "Bearer guest-loopback-token-123456789"
        if sponsored:
            # The broker learns the client's whole timeout so it answers first.
            assert call["headers"][kimi_k3.TIMEOUT_HEADER] == "630"
        else:
            assert kimi_k3.TIMEOUT_HEADER not in call["headers"]
        body = call["payload"]
        assert body["model"] == "kimi-k3"
        assert body["max_tokens"] <= 8192
        for field in ("tools", "functions", "tool_choice", "function_call"):
            assert field not in body
        roles.add(body["messages"][0]["content"][:40])
    assert len(roles) >= 3  # strategist, worker, verifier (and curator)
    receipt = json.loads(
        (tmp_path / "math_harness" / "project" / "engine_receipt.json").read_text()
    )
    assert receipt["verdict"] == "accepted"
    assert len(requests) <= 12  # the guest CGM call budget
    out = capsys.readouterr().out
    assert "not a Lean certificate" in out
    assert "guest-loopback-token-123456789" not in out


# ── kimi engine stop semantics (SIGTERM → 143, nothing published) ──────────


def test_kimi_runner_maps_a_stopped_pipeline_to_143_on_the_main_thread(
    tmp_path, monkeypatch, capsys
):
    from agent_monitor.runners import kimi_runner
    from agent_monitor.runners import math_harness_runner as harness

    seen_threads: list[bool] = []

    def cancelled(*_args, **_kwargs):
        # run_pipeline intercepts SIGTERM only when it runs on the main thread.
        seen_threads.append(threading.current_thread() is threading.main_thread())
        raise harness.RunCancelled("the Math Harness run was stopped")

    monkeypatch.setattr(harness, "run_pipeline", cancelled)
    monkeypatch.setenv("KIMI_API_KEY", "sk-test-kimi-123456789")
    monkeypatch.delenv("AGENT_MONITOR_SPONSORED_KIMI", raising=False)
    monkeypatch.setattr(sys, "argv", ["kimi_runner.py", "Prove it."])
    monkeypatch.chdir(tmp_path)
    assert kimi_runner.main() == 128 + int(signal.SIGTERM) == 143
    assert seen_threads == [True]
    items = [
        json.loads(line).get("item") or {}
        for line in capsys.readouterr().out.splitlines() if line.strip()
    ]
    assert items[-1]["type"] == "error" and items[-1]["terminal"] is True
    assert "stopped" in items[-1]["message"] and "nothing was published" in items[-1]["message"]
    assert not any(item.get("type") == "file_change" for item in items)

    # Any other pipeline failure (a MathHarnessError that is not a stop) is 1.
    monkeypatch.setattr(
        harness, "run_pipeline",
        lambda *_a, **_k: (_ for _ in ()).throw(harness.MathHarnessError("no usable worker candidate")),
    )
    assert kimi_runner.main() == 1
    assert "Kimi could not finish this proof" in capsys.readouterr().out


# ── sponsored provider failures: clear messages, bounded retries ───────────

_FAST_503 = {
    "message": "Kimi request was rejected", "type": "upstream_rejected", "forwarded": True,
    "upstream_status": 503, "upstream_elapsed_ms": 250, "retry_after": None,
}


def _sponsored_runner_env(monkeypatch, tmp_path) -> None:
    for name in list(os.environ):
        if name.startswith(("AGENT_MONITOR_", "KIMI_", "OPENAI_")) or name.lower().endswith("_proxy"):
            monkeypatch.delenv(name, raising=False)
    for name, value in {
        "AGENT_MONITOR_SPONSORED_KIMI": "1",
        "AGENT_MONITOR_GUEST": "1",
        "AGENT_MONITOR_MAX_OUTPUT_TOKENS": "8192",
        "AGENT_MONITOR_CGM_CONCURRENCY": "1",
        "KIMI_API_KEY": "sponsored-loopback-token-123456789",
        "KIMI_API_BASE": "http://127.0.0.1:4610/internal/v1",
        # kimi_runner sets these; registering them restores them afterwards.
        "NO_PROXY": "*",
        "no_proxy": "*",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["kimi_runner.py", "Prove the problem in problem.txt."])


@pytest.mark.parametrize(
    ("code", "error", "elapsed", "expected"),
    [
        (503, _FAST_503, 0.3,
         "Kimi's provider is temporarily overloaded and refused the request (HTTP 503). "
         "No tokens were used. Please try again in a few minutes."),
        (504, {"type": "upstream_timeout", "forwarded": True}, 600.2,
         "Kimi took longer than 10 minutes to answer, so the call was stopped (HTTP 504). "
         "Please try again later."),
        (504, {"type": "gateway_timeout", "forwarded": True}, 615.0,
         "Kimi took longer than 10 minutes to answer, so the call was stopped (HTTP 504). "
         "Please try again later."),
        (429, {"type": "gateway_busy", "forwarded": False, "retry_after": 2}, 0.1,
         "Kimi is busy on this site right now (HTTP 429). Please try again in a few minutes."),
        (429, {"type": "gateway_quota", "forwarded": False, "retry_after": 20}, 0.1,
         "Kimi is busy on this site right now (HTTP 429). Please try again in a few minutes."),
        (429, {"type": "gateway_quota", "forwarded": False, "retry_after": 900}, 0.1,
         "This site's hourly Kimi limit is reached (HTTP 429). "
         "Please try again in about 15 minutes."),
        (502, {"type": "gateway_unavailable", "forwarded": True}, 3.0,
         "The connection to the included Kimi service failed while it was answering "
         "(HTTP 502). Please try again later."),
        (401, {"type": "unauthorized", "forwarded": False}, 0.1,
         "The included Kimi access has expired (HTTP 401). Please try again."),
        (429, {"type": "sponsor_quota", "forwarded": False, "retry_after": 60}, 0.1,
         "This run used up its Kimi call budget (HTTP 429)."),
    ],
)
def test_kimi_runner_explains_sponsored_provider_failures(
    code, error, elapsed, expected, tmp_path, monkeypatch, capsys
):
    from agent_monitor.runners import kimi_runner
    from agent_monitor.runners import math_harness_runner as harness

    failure = kimi_k3.call_failure(code, {"error": dict(error)}, elapsed=elapsed)

    def fail(*_args, **_kwargs):
        # The first call of the run failed, so nothing was billed.
        raise harness.ProviderCallError(
            "strategist provider call failed: detail", failure=failure, billed=False
        )

    _sponsored_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(harness, "run_pipeline", fail)
    assert kimi_runner.main() == 1
    items = [
        json.loads(line).get("item") or {}
        for line in capsys.readouterr().out.splitlines() if line.strip()
    ]
    assert items[-1]["terminal"] is True
    assert items[-1]["message"] == f"Kimi could not finish this proof: {expected}"


def test_kimi_runner_never_claims_no_tokens_for_a_billed_run(
    tmp_path, monkeypatch, capsys
):
    from agent_monitor.runners import kimi_runner
    from agent_monitor.runners import math_harness_runner as harness

    failure = kimi_k3.call_failure(503, {"error": dict(_FAST_503)}, elapsed=0.3)
    assert failure.retryable

    def fail(*_args, **_kwargs):
        # The last call was refused unprocessed, but earlier calls were billed.
        raise harness.MathHarnessError(
            "no worker produced a usable candidate", failure=failure, billed=True
        )

    _sponsored_runner_env(monkeypatch, tmp_path)
    monkeypatch.setattr(harness, "run_pipeline", fail)
    assert kimi_runner.main() == 1
    items = [
        json.loads(line).get("item") or {}
        for line in capsys.readouterr().out.splitlines() if line.strip()
    ]
    assert items[-1]["message"] == (
        "Kimi could not finish this proof: Kimi's provider is temporarily overloaded "
        "and refused the request (HTTP 503). Please try again in a few minutes."
    )


def test_a_billed_strategist_then_refused_workers_never_claim_no_tokens(
    tmp_path, monkeypatch, capsys
):
    from agent_monitor.runners import kimi_runner
    from agent_monitor.runners import math_harness_runner as harness

    (tmp_path / "problem.txt").write_text(
        "Prove that the sum of two even integers is even.", encoding="utf-8"
    )
    _sponsored_runner_env(monkeypatch, tmp_path)
    requests: list[dict] = []

    def broker(url, *, method="GET", headers=None, payload=None, timeout=None, **_kwargs):
        requests.append(payload)
        if len(requests) == 1:
            # The strategist is answered and billed.
            return 200, {
                "model": "kimi-k3",
                "choices": [{"message": {"role": "assistant", "content": _cgm_reply_for(payload)}}],
                "usage": {"prompt_tokens": 3000, "completion_tokens": 2000},
            }
        return 503, {"error": dict(_FAST_503)}

    waits: list[float] = []
    monkeypatch.setattr(settings, "_http_json", broker)
    monkeypatch.setattr(kimi_k3.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        harness, "_wait_for_provider", lambda _ctx, seconds: waits.append(seconds) or True
    )

    assert kimi_runner.main() == 1

    # Every worker call was refused unprocessed, so one rescue worker ran.
    assert waits == [60.0]
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    notes = [
        (event.get("item") or {}).get("text") or ""
        for event in events
        if (event.get("item") or {}).get("type") == "reasoning"
    ]
    assert "Math Harness · 2 Explore · Kimi's provider is busy; retrying in 60 s" in notes
    assert not any("no tokens used" in note for note in notes)
    usage = [event for event in events if event.get("type") == "turn.completed"]
    assert [event["stage"] for event in usage] == ["strategist"]
    items = [event.get("item") or {} for event in events]
    assert items[-1]["terminal"] is True
    # The run's strategist call was billed: the run-level message makes no
    # claim about tokens, although its last call was refused unprocessed.
    assert items[-1]["message"] == (
        "Kimi could not finish this proof: Kimi's provider is temporarily overloaded "
        "and refused the request (HTTP 503). Please try again in a few minutes."
    )


def test_a_busy_sponsored_provider_ends_a_kimi_run_with_bounded_retries(
    tmp_path, monkeypatch, capsys
):
    from agent_monitor.runners import kimi_runner
    from agent_monitor.runners import math_harness_runner as harness

    (tmp_path / "problem.txt").write_text("Prove that 1 + 1 = 2.", encoding="utf-8")
    _sponsored_runner_env(monkeypatch, tmp_path)
    requests: list[dict] = []

    def refused(url, *, method="GET", headers=None, payload=None, timeout=None, **_kwargs):
        requests.append({"headers": dict(headers or {}), "timeout": timeout})
        return 503, {"error": dict(_FAST_503)}

    sleeps: list[float] = []
    waits: list[float] = []
    monkeypatch.setattr(settings, "_http_json", refused)
    monkeypatch.setattr(kimi_k3.time, "sleep", sleeps.append)
    monkeypatch.setattr(
        harness, "_wait_for_provider", lambda _ctx, seconds: waits.append(seconds) or True
    )

    assert kimi_runner.main() == 1

    # The strategist: four calls of three attempts each, with its 30/60/120 s
    # waits between them; then the direct plan's worker and, after 60 s, one
    # rescue worker, three attempts each (12 of the run's 24 transport retries).
    assert len(requests) == 18
    assert sleeps == [5.0, 15.0] * 6
    assert waits == [30.0, 60.0, 120.0, 60.0]
    assert requests[0]["headers"][kimi_k3.TIMEOUT_HEADER] == "630"
    assert requests[0]["timeout"] == 630.0
    for request in requests:
        assert 1 <= int(request["headers"][kimi_k3.TIMEOUT_HEADER]) <= 630
    items = [
        json.loads(line).get("item") or {}
        for line in capsys.readouterr().out.splitlines() if line.strip()
    ]
    assert items[-1]["terminal"] is True
    assert items[-1]["message"] == (
        "Kimi could not finish this proof: Kimi's provider is temporarily overloaded "
        "and refused the request (HTTP 503). No tokens were used. "
        "Please try again in a few minutes."
    )
    notes = [item.get("text") or "" for item in items if item.get("type") == "reasoning"]
    assert any("retrying in 30 s (no tokens used)" in note for note in notes)
    assert "sponsored-loopback-token-123456789" not in json.dumps(items)
    assert not (tmp_path / "proof.md").exists()
    assert not (tmp_path / "math_harness" / "project" / "engine_receipt.json").exists()


class _OnceBusyBroker(BaseHTTPRequestHandler):
    """A loopback broker whose first call is refused fast upstream (a typed
    503), then answers every CGM call."""

    def log_message(self, *_args):  # pragma: no cover - silence
        return

    def do_POST(self):  # noqa: N802
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        with self.server.lock:
            self.server.seen.append(
                {"timeout": self.headers.get("X-Ansatze-Timeout"), "payload": payload}
            )
            first = len(self.server.seen) == 1
        if first:
            status, body = 503, {"error": dict(_FAST_503)}
        else:
            status, body = 200, {
                "model": "kimi-k3",
                "choices": [{"message": {"role": "assistant", "content": _cgm_reply_for(payload)}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
            }
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def test_a_typed_refusal_from_a_real_loopback_broker_is_retried_once(
    tmp_path, monkeypatch, capsys
):
    from agent_monitor.runners import kimi_runner

    server = ThreadingHTTPServer(("127.0.0.1", 0), _OnceBusyBroker)
    server.daemon_threads = True
    server.seen, server.lock = [], threading.Lock()
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    (tmp_path / "problem.txt").write_text(
        "Prove that the sum of two even integers is even.", encoding="utf-8"
    )
    _sponsored_runner_env(monkeypatch, tmp_path)
    monkeypatch.setenv("KIMI_API_BASE", f"http://127.0.0.1:{server.server_address[1]}/internal/v1")
    sleeps: list[float] = []
    monkeypatch.setattr(kimi_k3.time, "sleep", sleeps.append)
    try:
        code = kimi_runner.main()
    finally:
        server.shutdown()
        server.server_close()
    out = capsys.readouterr().out
    assert code == 0, out
    # The typed error body reached the client through urllib's HTTPError: the
    # refused strategist call was sent again once, after 5 s, unchanged.
    assert sleeps == [5.0]
    assert server.seen[0]["payload"] == server.seen[1]["payload"]
    assert server.seen[0]["timeout"] == "630"
    assert all(1 <= int(seen["timeout"]) <= 630 for seen in server.seen)
    receipt = json.loads(
        (tmp_path / "math_harness" / "project" / "engine_receipt.json").read_text()
    )
    assert receipt["verdict"] == "accepted"
    assert receipt["calls"][0]["stage"] == "strategist"
    assert len(receipt["calls"]) == len(server.seen) - 1


class _StallingBroker(BaseHTTPRequestHandler):
    """A loopback stand-in for the sponsored broker that never answers."""

    def log_message(self, *_args):  # pragma: no cover - silence
        return

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.server.requests.append(self.path)
        self.server.arrived.set()
        self.server.release.wait(30)
        body = b'{"error":{"message":"stalled"}}'
        try:
            self.send_response(503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass


def test_sigterm_stops_a_real_kimi_run_with_143_and_publishes_nothing(tmp_path):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StallingBroker)
    server.daemon_threads = True
    server.requests, server.arrived, server.release = [], threading.Event(), threading.Event()
    serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    serving.start()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "problem.txt").write_text("Prove that 1 + 1 = 2.", encoding="utf-8")
    env_file = tmp_path / "server.env"
    env_file.touch()
    token = "sponsored-loopback-token-" + "x" * 24
    env = {
        name: value for name, value in os.environ.items()
        if not name.startswith(("AGENT_MONITOR_", "KIMI_", "OPENAI_")) and not name.lower().endswith("_proxy")
    }
    env.update({
        "AGENT_MONITOR_DATA_DIR": str(tmp_path / "data"),
        "AGENT_MONITOR_ENV_PATH": str(env_file),
        "AGENT_MONITOR_SPONSORED_KIMI": "1",
        "AGENT_MONITOR_GUEST": "1",
        "AGENT_MONITOR_MAX_OUTPUT_TOKENS": "8192",
        "KIMI_API_KEY": token,
        "KIMI_API_BASE": f"http://127.0.0.1:{server.server_address[1]}/internal/v1",
    })
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "agent_monitor" / "runners" / "kimi_runner.py"), "Prove it."],
        cwd=workspace, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        start_new_session=True,
    )
    try:
        assert server.arrived.wait(60), "the runner never reached the broker"
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=30)
        elapsed = time.monotonic() - started
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=10)
        server.release.set()
        server.shutdown()
        server.server_close()
    assert proc.returncode == 143, out
    assert elapsed < 20  # the in-flight call is abandoned, not waited for
    assert server.requests and all(path == "/internal/v1/chat/completions" for path in server.requests)
    items = [json.loads(line).get("item") or {} for line in out.splitlines() if line.startswith("{")]
    assert items[-1]["type"] == "error" and items[-1]["terminal"] is True
    assert "nothing was published" in items[-1]["message"]
    assert not any(item.get("type") == "file_change" for item in items)
    assert token not in out
    assert not (workspace / "proof.md").exists()
    assert not (workspace / "math_harness" / "project" / "engine_receipt.json").exists()


# ── sponsored capacity: a briefly saturated gateway queues, never fails ────


def _cgm_reply_for(payload: dict) -> str:
    from agent_monitor.cgm import prompts

    system = next(
        (m.get("content") for m in payload.get("messages") or [] if m.get("role") == "system"), ""
    )
    if system == prompts.STRATEGIST_SYSTEM:
        return _CGM_STRATEGY
    if system.startswith(prompts.WORKER_SYSTEM):
        return _CGM_WORKER
    if system == prompts.VERIFIER_SYSTEM:
        return _CGM_CORRECT
    if system == prompts.CURATOR_SYSTEM:
        return _CGM_CURATOR
    return "ok"


class _ScriptedKimiUpstream(BaseHTTPRequestHandler):
    """Loopback stand-in for the Kimi provider behind the credential gateway."""

    def log_message(self, *_args):  # pragma: no cover - silence
        return

    def do_POST(self):  # noqa: N802
        payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        user = str(payload["messages"][-1].get("content") or "")
        self.server.seen.append(("other-guest" if user == "other guest" else "cgm", time.monotonic()))
        if user == "other guest":
            # Another guest's call holds the only gateway slot for a while.
            self.server.holding.set()
            time.sleep(self.server.hold_seconds)
        body = json.dumps({
            "model": "kimi-k3",
            "choices": [{"message": {"role": "assistant", "content": _cgm_reply_for(payload)}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7, "total_tokens": 12},
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.mark.parametrize("queue_seconds", [0, 30])
def test_guest_cgm_run_survives_a_briefly_saturated_sponsored_gateway(
    queue_seconds, tmp_path, monkeypatch, capsys
):
    from agent_monitor.public_kimi_gateway import PublicKimiGateway
    from agent_monitor.public_kimi_server import PublicKimiApplication, PublicKimiHTTPServer
    from agent_monitor.runners import kimi_runner
    from agent_monitor.runners import math_harness_runner as harness

    if queue_seconds == 0:
        # The control: a refusal never reached the provider, so the client
        # may send it again (a typed ``gateway_busy``), but here every retry
        # and wait is immediate, so all of them land while the slot is held.
        monkeypatch.setattr(kimi_k3, "_RETRY_AFTER_BOUNDS_S", (0, 0))
        monkeypatch.setattr(kimi_k3, "_SPONSORED_RETRY_DELAYS_S", (0.0, 0.0))
        monkeypatch.setattr(harness, "_wait_for_provider", lambda _ctx, _seconds: True)
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _ScriptedKimiUpstream)
    upstream.daemon_threads = True
    upstream.seen, upstream.holding, upstream.hold_seconds = [], threading.Event(), 1.5
    threading.Thread(target=upstream.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    gateway = PublicKimiGateway(
        upstream_base=f"http://127.0.0.1:{upstream.server_address[1]}/v1",
        upstream_api_key="upstream-provider-secret",
        local_token="gateway-local-token",
        reasoning_effort="high",
        max_concurrency=1,  # one slot, so a single other call saturates it
        calls_per_hour=100,
        upstream_timeout=225,
        queue_seconds=queue_seconds,
        queue_depth=8,
    )
    app = PublicKimiApplication(
        jobs=SimpleNamespace(), data_dir=tmp_path / "broker", redactions=("upstream-provider-secret",)
    )
    token, _ttl = app.sponsor_sessions.issue(engine="math_harness", ip="127.0.0.1")
    other_token, _ttl = app.sponsor_sessions.issue(engine="plain", ip="198.18.0.9")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "problem.txt").write_text(
        "Prove that the sum of two even integers is even.", encoding="utf-8"
    )
    for name in list(os.environ):
        if name.startswith(("AGENT_MONITOR_", "KIMI_", "OPENAI_")) or name.lower().endswith("_proxy"):
            monkeypatch.delenv(name, raising=False)
    with gateway:
        broker = PublicKimiHTTPServer(
            ("127.0.0.1", 0), app, "",
            gateway_base_url=gateway.base_url, gateway_token="gateway-local-token",
        )
        threading.Thread(target=broker.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
        base = broker.base_url_for_sponsored
        try:
            other = threading.Thread(
                target=lambda: settings._http_json(
                    f"{base}/chat/completions",
                    method="POST",
                    headers={"Authorization": f"Bearer {other_token}"},
                    payload={"model": "kimi-k3", "messages": [{"role": "user", "content": "other guest"}]},
                    timeout=30,
                ),
                daemon=True,
            )
            other.start()
            assert upstream.holding.wait(10)
            for name, value in {
                "AGENT_MONITOR_SPONSORED_KIMI": "1",
                "AGENT_MONITOR_GUEST": "1",
                "AGENT_MONITOR_MAX_OUTPUT_TOKENS": "8192",
                "KIMI_API_KEY": token,
                "KIMI_API_BASE": base,
                "NO_PROXY": "*",
                "no_proxy": "*",
            }.items():
                monkeypatch.setenv(name, value)
            monkeypatch.chdir(workspace)
            monkeypatch.setattr(sys, "argv", ["kimi_runner.py", "Prove the problem in problem.txt."])
            code = kimi_runner.main()
            other.join(timeout=10)
        finally:
            broker.shutdown()
            broker.server_close()
            upstream.shutdown()
            upstream.server_close()
    out = capsys.readouterr().out
    assert token not in out and "upstream-provider-secret" not in out
    receipt = workspace / "math_harness" / "project" / "engine_receipt.json"
    if queue_seconds == 0:
        # Control: without the queue every call is refused at once.  An older
        # gateway's untyped 429 is reported as is; a typed one as busy.
        assert code == 1
        assert "HTTP 429" in out
        assert "concurrency reached" in out or "busy on this site right now" in out
        assert not receipt.exists()
        return
    assert code == 0, out
    assert json.loads(receipt.read_text())["verdict"] == "accepted"
    held = [stamp for kind, stamp in upstream.seen if kind == "other-guest"]
    cgm = [stamp for kind, stamp in upstream.seen if kind == "cgm"]
    # The run's first call waited for the other guest's slot, then went on.
    assert len(held) == 1 and cgm and min(cgm) >= held[0] + 1.0
