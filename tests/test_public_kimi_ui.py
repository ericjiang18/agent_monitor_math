from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest

ROOT = Path(__file__).parents[1]
HTML = (ROOT / "agent_monitor" / "web" / "public_kimi.html").read_text(encoding="utf-8")
JS = (ROOT / "agent_monitor" / "web" / "public_kimi.js").read_text(encoding="utf-8")
CSS = (ROOT / "agent_monitor" / "web" / "public_kimi.css").read_text(encoding="utf-8")
NODE = shutil.which("node")
if not NODE and Path("/home/ubuntu/.local/node24/bin/node").is_file():
    NODE = "/home/ubuntu/.local/node24/bin/node"


def _js_function(name: str) -> str:
    function_start = JS.index(f"function {name}(")
    start = function_start - 6 if JS[function_start - 6 : function_start] == "async " else function_start
    opening = JS.index("{", function_start)
    depth = 0
    quote = ""
    escaped = False
    for index in range(opening, len(JS)):
        char = JS[index]
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
            continue
        if char in ("'", '"', chr(96)):
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return JS[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {name}")


def _run_node(program: str) -> object:
    if not NODE:
        raise unittest.SkipTest("node is unavailable")
    result = subprocess.run(
        [NODE, "-e", program],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


class PublicKimiUiTests(unittest.TestCase):
    def test_page_is_csp_safe_and_has_only_fixed_same_origin_assets(self) -> None:
        self.assertIn('href="/static/public_kimi.css"', HTML)
        self.assertIn('src="/static/public_kimi.js"', HTML)
        self.assertIn('name="pk-base"', HTML)
        self.assertNotIn("<style", HTML.lower())
        self.assertNotRegex(HTML, r"<script(?![^>]+src=)")
        self.assertNotIn("@import", CSS)

    def test_page_has_unique_ids_and_accessible_run_controls(self) -> None:
        ids = re.findall(r'\bid="([^"]+)"', HTML)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids.count("tab-proof"), 1)
        self.assertIn('role="tablist"', HTML)
        self.assertIn('aria-live="polite"', HTML)
        self.assertIn('id="stop-run"', HTML)
        self.assertIn('id="error-box"', HTML)
        self.assertIn("@media(max-width:620px)", CSS)

    def test_page_uses_the_provingconsole_workbench_visual_language(self) -> None:
        for phrase in ("ProvingConsole", "New proof", "Workspace", "Problem", "Proof"):
            self.assertIn(phrase, HTML)
        for selector in (".sidebar", ".workbench", ".problem-panel", ".result-panel"):
            self.assertIn(selector, CSS)

    def test_anonymous_sponsored_service_and_limits_are_explicit(self) -> None:
        for phrase in (
            "Server-sponsored Kimi K3",
            "No account",
            "No login",
            "No API key",
            "Shared public service",
            "rate and concurrency limits",
            "do not submit private or sensitive material",
        ):
            self.assertIn(phrase, HTML)
        self.assertNotRegex(HTML, r'type=["\']password')
        self.assertNotIn("api_key", HTML.lower())
        self.assertNotIn("OPENAI_API_KEY", JS)
        self.assertNotIn("KIMI_API_KEY", JS)
        self.assertIn("output tokens per model call", JS)

    def test_javascript_is_confined_to_public_api_surface(self) -> None:
        for forbidden in (
            "/api/auth",
            "/api/settings",
            "/api/engines",
            "/api/jobs",
            "/api/workspace",
            "/api/lean",
            "/api/library",
        ):
            self.assertNotIn(forbidden, JS)
        self.assertIn("requestJson('/api/config')", JS)
        self.assertIn("requestJson('/api/runs'", JS)
        self.assertEqual(JS.count("encodeURIComponent(runId)"), 2)
        self.assertIn("credentials:'same-origin'", JS)
        self.assertNotIn("localStorage", JS)

    def test_runtime_is_visibly_and_programmatically_plain_only(self) -> None:
        self.assertIn("const PUBLIC_ENGINE='plain'", JS)
        self.assertNotIn("metaharness", JS.lower())
        self.assertIn("Plain only", HTML)
        self.assertIn("Response-only harness", HTML)
        self.assertNotIn("Choose a harness", HTML)

    def test_model_content_uses_text_nodes_not_html_sinks(self) -> None:
        self.assertNotIn("innerHTML", JS)
        self.assertNotIn("outerHTML", JS)
        self.assertNotIn("insertAdjacentHTML", JS)
        self.assertNotIn("eval(", JS)
        self.assertIn("$('proof').textContent=proof", JS)
        self.assertIn("message.textContent=view.message", JS)

    def test_start_payload_behavior_is_exact_and_contains_no_routing_fields(self) -> None:
        source = _js_function("startRun")
        output = _run_node(
            """
const PUBLIC_ENGINE='plain';
const state={plainAvailable:true,selectedHarness:'plain',runId:'',pollGeneration:0};
const elements={problem:{value:'Prove that sqrt(3) is irrational.',focus(){} }};
const $=id=>elements[id]||{};
let captured=null;
const setValidation=()=>{},problemLimit=()=>12000,cancelPolling=()=>{},setBusy=()=>{};
const requestJson=async(path,options)=>{
  captured={path,method:options.method,body:JSON.parse(options.body)};
  return {run_id:'public-run-1',status:'queued'};
};
const sessionStorage={setItem(){}};
const renderRun=()=>{},schedulePoll=()=>{};
"""
            + source
            + """
(async()=>{
  await startRun({preventDefault(){}});
  console.log(JSON.stringify(captured));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        )
        self.assertEqual(output["path"], "/api/runs")
        self.assertEqual(output["method"], "POST")
        self.assertEqual(
            output["body"],
            {"engine": "plain", "problem": "Prove that sqrt(3) is irrational."},
        )
        for forbidden in ("model", "provider", "auth_route", "api_key", "token"):
            self.assertNotIn(forbidden, output["body"])

    def test_terminal_done_and_aliases_end_polling(self) -> None:
        source = "\n".join(
            (
                _js_function("normalizedStatus"),
                _js_function("isTerminal"),
            )
        )
        output = _run_node(
            """
const TERMINAL_STATES=new Set(['done','finished','completed','failed','stopped','cancelled','canceled']);
"""
            + source
            + """
console.log(JSON.stringify({
  done:isTerminal('done'),
  complete:isTerminal('complete'),
  success:isTerminal('success'),
  running:isTerminal('running'),
  canceled:normalizedStatus('canceled')
}));
"""
        )
        self.assertEqual(
            output,
            {
                "done": True,
                "complete": True,
                "success": True,
                "running": False,
                "canceled": "cancelled",
            },
        )

    def test_stale_404_is_terminal_and_is_not_polled_forever(self) -> None:
        source = _js_function("pollRun")
        output = _run_node(
            """
const state={pollGeneration:7,runId:'stale-run'};
let scheduled=0,cancelled=0,rendered=null;
const requestJson=async()=>{const error=new Error('gone');error.status=404;throw error;};
const forgetStoredRun=()=>{};
const renderRun=value=>{rendered=value;};
const cancelPolling=()=>{cancelled+=1;};
const schedulePoll=()=>{scheduled+=1;};
const $=()=>({textContent:''});
const isTerminal=()=>false;
"""
            + source
            + """
(async()=>{
  await pollRun('stale-run',7);
  console.log(JSON.stringify({scheduled,cancelled,status:rendered.status,error:rendered.error}));
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        )
        self.assertEqual(output["scheduled"], 0)
        self.assertEqual(output["cancelled"], 1)
        self.assertEqual(output["status"], "failed")
        self.assertIn("no longer available", output["error"])

    def test_script_parses_as_javascript(self) -> None:
        if not NODE:
            self.skipTest("node is unavailable")
        result = subprocess.run(
            [NODE, "--check", str(ROOT / "agent_monitor" / "web" / "public_kimi.js")],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
