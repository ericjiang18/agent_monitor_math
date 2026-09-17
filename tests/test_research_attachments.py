"""Research uploads must reach harnesses without crossing run boundaries."""
import base64
import http.client
import json
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from agent_monitor import attachments, console_server as console, jobs, engines_registry, settings


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aO1cAAAAASUVORK5CYII=")


def upload(name="notes.tex", data=b"Lemma: every even integer is divisible by two."):
    return {"name": name, "data": base64.b64encode(data).decode("ascii")}


@pytest.fixture
def isolated_jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(console, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(jobs, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(jobs, "_JOBS", {})
    monkeypatch.setattr(jobs, "_STOP_EVENTS", {})
    monkeypatch.setattr(jobs, "_user_extra_env", lambda *args: {"OPENAI_API_KEY": "test-only", "AGENT_MONITOR_ACCOUNT_RUNTIME": "api"})
    monkeypatch.setattr(settings, "codex_enabled", lambda *args, **kwargs: False)
    monkeypatch.setattr(engines_registry, "list_engines", lambda: [{"id": "plain", "available": True, "auth_modes": ["api_key"]}])
    workers = []

    class Worker:
        def __init__(self, *, target, kwargs, **rest):
            workers.append((target, kwargs))

        def start(self):
            pass

    monkeypatch.setattr(jobs, "threading", SimpleNamespace(Thread=Worker, get_ident=threading.get_ident, Event=threading.Event))
    return workers


def test_files_keep_original_bytes_and_bounded_prompt_excerpts(tmp_path):
    records = attachments.save(tmp_path, attachments.validate([
        upload("../../notes.tex"), upload('figure.png', PNG),
        upload("long.md", b"x" * 30_000),
    ]))
    assert records[0]["name"] == "notes.tex"
    assert attachments.file_path(tmp_path, records[1]).read_bytes() == PNG
    assert all(attachments.file_path(tmp_path, x).is_relative_to(tmp_path / "attachments") for x in records)
    context = attachments.context(tmp_path)
    assert "Lemma: every even integer" in context
    assert records[1]["path"] in context
    assert "vision_analyze" in context
    assert "Excerpt truncated" in context
    assert len(context) < 42_000


@pytest.mark.parametrize("items", [
    {}, [upload()] * 9, [upload("program.exe")], [upload("fake.png")],
    [upload("empty.md", b"")], [upload("binary.txt", b"\x00")],
    [upload("bad.txt", b"\xff")], [{"name": "notes.md", "data": "bad base64!"}],
])
def test_rejects_invalid_batches_without_files(tmp_path, items):
    with pytest.raises(ValueError):
        attachments.save(tmp_path, attachments.validate(items))
    assert not list(tmp_path.iterdir())


def test_file_batch_and_run_limits(tmp_path, monkeypatch):
    monkeypatch.setattr(attachments, "MAX_FILE_BYTES", 12)
    monkeypatch.setattr(attachments, "MAX_BATCH_BYTES", 20)
    monkeypatch.setattr(attachments, "MAX_RUN_BYTES", 24)
    with pytest.raises(ValueError, match="file size|file must"):
        attachments.validate([upload(data=b"a" * 13)])
    with pytest.raises(ValueError, match="total"):
        attachments.validate([upload(data=b"a" * 12)] * 2)
    batch = attachments.validate([upload(data=b"a" * 12)])
    attachments.save(tmp_path, batch)
    attachments.save(tmp_path, attachments.validate([upload(data=b"b" * 12)]))
    with pytest.raises(ValueError, match="run has reached"):
        attachments.save(tmp_path, attachments.validate([upload(data=b"c")]))
    assert len(attachments.list_files(tmp_path)) == 2


def test_attachment_paths_reject_traversal_and_symlinks(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "private.txt"
    outside.write_text("private data")
    folder = workspace / "attachments"
    folder.mkdir()
    (folder / "linked.txt").symlink_to(outside)
    for path in ("../private.txt", "attachments/linked.txt"):
        with pytest.raises(ValueError, match="Invalid attachment path"):
            attachments.file_path(workspace, {"path": path})


@pytest.mark.parametrize("engine", ["improof", "ucla"])
def test_pipeline_engines_receive_files_without_library_context(isolated_jobs, monkeypatch, engine):
    from agent_monitor.runners import improof, ucla

    job = jobs.start_job(engine="plain", user={"id": 7}, problem_text="Prove the lemma", attachments=[upload()])
    worker, kwargs = isolated_jobs[0]
    captured = []
    monkeypatch.setattr(improof if engine == "improof" else ucla, "run_problem", lambda path, **kw: captured.append(path.read_text()) or {})
    monkeypatch.setattr(jobs, "_wrap_subprocess_result", lambda **kw: {"run_id": job["run_id"], "status": "finished", "agents": []})
    monkeypatch.setattr(jobs, "_launch_pending_feedback", lambda *args, **kw: None)
    monkeypatch.setattr(jobs, "_record_run_memory", lambda **kw: None)
    worker(**{**kwargs, "engine": engine, "guest": True})
    assert "Lemma: every even integer" in captured[0]
    assert "RESEARCH ATTACHMENTS" in captured[0]


def test_initial_and_queued_followup_files_reach_engine(isolated_jobs, monkeypatch):
    job = jobs.start_job(engine="plain", user={"id": 7}, attachments=[upload(), upload("figure.png", PNG)])
    worker, kwargs = isolated_jobs[0]
    workspace = jobs.workspace_dir(job["run_id"])
    assert len(attachments.list_files(workspace)) == 2
    assert jobs.list_chat(job["run_id"])[0]["attachments"][1]["name"] == "figure.png"
    queued = jobs.send_human_message(job["run_id"], "", user={"id": 7}, attachments=[upload("new.md", b"Try strong induction.")])
    assert queued["status"] == "queued"
    pending = (workspace / jobs._PENDING_FEEDBACK_FILENAME).read_text()
    assert "new.md" in pending
    captured = []

    def runner(**args):
        captured.append(args["problem_text"])
        return {"run_id": job["run_id"], "status": "finished", "agents": []}

    monkeypatch.setattr(jobs, "_run_cli_engine", runner)
    monkeypatch.setattr(jobs, "_launch_pending_feedback", lambda *args, **kw: None)
    monkeypatch.setattr(jobs, "_record_run_memory", lambda **kw: None)
    worker(**{**kwargs, "guest": True})
    assert "Lemma: every even integer" in captured[0]
    assert "figure.png" in captured[0]
    assert "Try strong induction." in captured[0]


def test_finished_run_followup_preserves_existing_files(isolated_jobs, monkeypatch):
    job = jobs.start_job(engine="plain", user={"id": 7}, problem_text="Prove the lemma", attachments=[upload()])
    jobs._JOBS[job["job_id"]]["status"] = "finished"
    response = jobs.send_human_message(job["run_id"], "Check this diagram", user={"id": 7}, attachments=[upload("diagram.png", PNG)])
    assert response["status"] == "continuing"
    worker, kwargs = isolated_jobs[-1]
    captured = []
    monkeypatch.setattr(jobs, "_run_cli_engine", lambda **kw: captured.append(kw["problem_text"]) or {"run_id": job["run_id"], "status": "finished", "agents": []})
    monkeypatch.setattr(jobs, "_launch_pending_feedback", lambda *args, **kw: None)
    monkeypatch.setattr(jobs, "_record_run_memory", lambda **kw: None)
    worker(**kwargs)
    assert "Lemma: every even integer" in captured[0]
    assert "diagram.png" in captured[0]


def test_http_upload_download_ownership_and_limits(isolated_jobs, monkeypatch):
    monkeypatch.setattr(console, "BASE_PATH", "/proof-deadbeef")
    monkeypatch.setattr(console, "PUBLIC_MODE", False)
    monkeypatch.setattr(console.Handler, "_current_user", lambda self: {"id": int(self.headers["X-Test-User"])} if self.headers.get("X-Test-User") else None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), console.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(method, path, body=None, user=7, headers=None):
        conn = http.client.HTTPConnection(*server.server_address, timeout=5)
        hdr = {"Content-Type": "application/json", **(headers or {})}
        if user:
            hdr["X-Test-User"] = str(user)
        conn.request(method, "/proof-deadbeef" + path, body=json.dumps(body) if body else None, headers=hdr)
        result = conn.getresponse()
        data = result.read()
        conn.close()
        return result.status, dict(result.getheaders()), data

    try:
        assert request("POST", "/api/runs", {"engine": "plain"}, user=None)[0] == 401
        assert request("POST", "/api/runs", headers={"Content-Length": str(attachments.MAX_REQUEST_BYTES + 1)})[0] == 413
        # Exceed the old 1 MB JSON limit with real research text.
        status, _, raw = request("POST", "/api/runs", {"engine": "plain", "attachments": [upload("large.md", b"x" * 1_100_000)]})
        assert status == 200, raw
        rid = json.loads(raw)["run_id"]
        item = attachments.list_files(jobs.workspace_dir(rid))[0]
        path = f"/api/runs/{rid}/attachments/{item['id']}"
        status, headers, raw = request("GET", path)
        assert status == 200
        assert raw == b"x" * 1_100_000
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Content-Disposition"].startswith("attachment;")
        assert request("GET", path, user=8)[0] == 404
        assert request("GET", path, user=None)[0] == 401
        assert request("POST", f"/api/runs/{rid}/chat", {"attachments": [upload()]}, user=8)[0] == 404
        status, _, raw = request("POST", f"/api/runs/{rid}/chat", {"attachments": [upload("figure.png", PNG)]})
        assert status == 200, raw
        assert json.loads(raw)["messages"][-2]["attachments"][0]["name"] == "figure.png"
        status, _, raw = request("POST", "/api/runs", {"engine": "plain", "attachments": [upload("fake.png")]})
        assert status == 400, raw
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
