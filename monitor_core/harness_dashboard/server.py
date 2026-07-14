#!/usr/bin/env python3
"""HTTP server for harness pipeline dashboard."""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from harness_dashboard.latex_provenance import (
    build_final_latex_bundle,
    compile_pdf,
    pdf_cache_path,
    resolve_latex_path,
)

PACKAGE_DIR = Path(__file__).resolve().parent
WEB_DIR = PACKAGE_DIR / "web"
CACHE_DIR = Path(os.environ.get("LLM_DASHBOARD_CACHE", "monitor/.cache")) / "harness"
MANIFEST_PATH = CACHE_DIR / "manifest.json"
LOG_PATH = os.environ.get("LLM_MONITOR_LOG", "monitor/calls.jsonl")
HARNESS_OUTPUTS_PATH = os.environ.get(
    "LLM_HARNESS_OUTPUTS",
    str(Path.cwd() / "monitor" / "harness_outputs.jsonl"),
)
HARNESS_OUTPUT_ROOT = Path(os.environ.get("HARNESS_OUTPUT_ROOT", "harness_0518_Final/output"))
PORT = int(os.environ.get("LLM_MONITOR_PORT", "4600"))

_run_cache: dict[str, tuple[float, dict]] = {}


def _load_run(run_id: str) -> dict | None:
    path = CACHE_DIR / f"{run_id}.json"
    if not path.exists():
        return None
    mtime = path.stat().st_mtime
    cached = _run_cache.get(run_id)
    if cached and cached[0] == mtime:
        return cached[1]
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not data.get("final_latex"):
        bundle = build_final_latex_bundle(data)
        if bundle:
            data["final_latex"] = bundle
    _run_cache[run_id] = (mtime, data)
    return data


def _final_latex_payload(run_id: str, run_data: dict) -> dict:
    bundle = run_data.get("final_latex") or build_final_latex_bundle(run_data)
    if not bundle:
        return {"error": "no final proof found", "run_id": run_id}

    tex_path, _ = resolve_latex_path(run_data)
    pdf_ok = False
    if tex_path and bundle.get("is_latex"):
        pdf_ok = compile_pdf(tex_path, pdf_cache_path(CACHE_DIR, run_id))

    return {
        "run_id": run_id,
        "source_label": bundle.get("source_label"),
        "source_path": bundle.get("source_path"),
        "char_count": bundle.get("char_count"),
        "line_count": bundle.get("line_count"),
        "format": bundle.get("format"),
        "is_latex": bundle.get("is_latex"),
        "pdf_available": pdf_ok,
        "pdf_url": f"/api/run/{run_id}/final.pdf" if pdf_ok else None,
        "provenance": bundle.get("provenance") or [],
        "matched_line_pct": bundle.get("matched_line_pct"),
        "unmatched_line_pct": bundle.get("unmatched_line_pct"),
        "contributor_count": bundle.get("contributor_count"),
        "selector": bundle.get("selector"),
    }


def _read_final_tex(run_data: dict) -> str | None:
    tex_path, _ = resolve_latex_path(run_data)
    if not tex_path or not tex_path.exists():
        return None
    return tex_path.read_text(encoding="utf-8", errors="replace")


def _harness_output_detail(trace_id: str) -> dict | None:
    if not os.path.exists(HARNESS_OUTPUTS_PATH):
        return None
    found = None
    with open(HARNESS_OUTPUTS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("trace_id") == trace_id:
                found = row
    return found


def _call_detail(trace_id: str, round_idx: int) -> dict | None:
    detail = None
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("trace_id") == trace_id and row.get("round") == round_idx:
                    detail = row
                    break
    ho = _harness_output_detail(trace_id)
    if ho:
        if detail is None:
            detail = {"trace_id": trace_id, "round": round_idx, "kind": "call"}
        rm = detail.setdefault("response_message", {})
        if ho.get("output") and not rm.get("content"):
            rm["content"] = ho["output"]
        if ho.get("thinking") and not rm.get("reasoning"):
            rm["reasoning"] = ho["thinking"]
        if ho.get("prompt") and not detail.get("request_messages"):
            detail["request_messages"] = [{"role": "user", "content": ho["prompt"]}]
        detail["harness_output"] = ho
    return detail


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, body: str, ctype: str = "application/json"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
            self._send(200, html, "text/html; charset=utf-8")
            return

        if path == "/api/runs":
            if MANIFEST_PATH.exists():
                self._send(200, MANIFEST_PATH.read_text(encoding="utf-8"))
            else:
                self._send(200, json.dumps({"runs": []}))
            return

        if path.startswith("/api/run/") and path.endswith("/final_latex"):
            run_id = path.removeprefix("/api/run/").removesuffix("/final_latex")
            data = _load_run(run_id)
            if not data:
                self._send(404, json.dumps({"error": "run not found"}))
                return
            payload = _final_latex_payload(run_id, data)
            code = 200 if "error" not in payload else 404
            self._send(code, json.dumps(payload, ensure_ascii=False))
            return

        if path.startswith("/api/run/") and path.endswith("/final.tex"):
            run_id = path.removeprefix("/api/run/").removesuffix("/final.tex")
            data = _load_run(run_id)
            if not data:
                self._send(404, json.dumps({"error": "run not found"}))
                return
            tex = _read_final_tex(data)
            if tex:
                self._send(200, tex, "text/plain; charset=utf-8")
            else:
                self._send(404, json.dumps({"error": "final proof not found"}))
            return

        if path.startswith("/api/run/") and path.endswith("/final.pdf"):
            run_id = path.removeprefix("/api/run/").removesuffix("/final.pdf")
            data = _load_run(run_id)
            if not data:
                self._send(404, json.dumps({"error": "run not found"}))
                return
            tex_path, _ = resolve_latex_path(data)
            if not tex_path:
                self._send(404, json.dumps({"error": "no latex source"}))
                return
            cache_pdf = pdf_cache_path(CACHE_DIR, run_id)
            if not compile_pdf(tex_path, cache_pdf):
                self._send(404, json.dumps({"error": "pdf compile failed or not latex"}))
                return
            self._send_bytes(200, cache_pdf.read_bytes(), "application/pdf")
            return

        if path.startswith("/api/run/"):
            run_id = path.removeprefix("/api/run/")
            data = _load_run(run_id)
            if data:
                self._send(200, json.dumps(data, ensure_ascii=False))
            else:
                self._send(404, json.dumps({"error": "run not found"}))
            return

        if path == "/api/call_detail":
            tid = qs.get("trace_id", [""])[0]
            rnd = int(qs.get("round", ["1"])[0])
            detail = _call_detail(tid, rnd)
            if detail:
                self._send(200, json.dumps(detail, ensure_ascii=False))
            else:
                self._send(404, json.dumps({"error": "call not found"}))
            return

        if path == "/api/rebuild":
            from harness_dashboard.build import rebuild
            rebuild()
            self._send(200, json.dumps({"ok": True}))
            return

        if path == "/health":
            self._send(200, json.dumps({"ok": True}))
            return

        self._send(404, json.dumps({"error": "not found"}))


def main():
    print(f"harness pipeline dashboard -> http://localhost:{PORT}")
    print(f"  cache: {CACHE_DIR}")
    print(f"  calls: {LOG_PATH}")
    print(f"  harness output: {HARNESS_OUTPUT_ROOT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
