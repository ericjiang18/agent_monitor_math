import json
from pathlib import Path
import shutil
import subprocess
import unittest

HTML = (
    Path(__file__).parents[1] / "agent_monitor" / "web" / "console.html"
).read_text(encoding="utf-8")
SCRIPT = HTML.rsplit("<script>", 1)[1].split("</script>", 1)[0]
NODE = shutil.which("node")
if not NODE and Path("/home/ubuntu/.local/node24/bin/node").is_file():
    NODE = "/home/ubuntu/.local/node24/bin/node"


def _js_function(name: str) -> str:
    start = SCRIPT.index(f"function {name}(")
    opening = SCRIPT.index("{", start)
    depth = 0
    for index in range(opening, len(SCRIPT)):
        char = SCRIPT[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return SCRIPT[start : index + 1]
    raise AssertionError(f"unterminated JavaScript function: {name}")


class ClaudeSettingsUiTests(unittest.TestCase):
    def test_card_uses_every_authenticated_claude_account_endpoint(self) -> None:
        for endpoint in (
            "/api/settings/claude/status",
            "/api/settings/claude/login/poll",
            "/api/settings/claude/login/start",
            "/api/settings/claude/login/code",
            "/api/settings/claude/login/cancel",
            "/api/settings/claude/logout",
        ):
            self.assertIn(endpoint, HTML)
        self.assertIn('id="claude-card"', HTML)
        self.assertIn("refreshClaudeCard();", HTML)

    def test_expired_claude_session_has_reauth_state_and_clears_models(self) -> None:
        self.assertIn("Claude authorization needs renewal", HTML)
        self.assertIn("Reconnect via Claude", HTML)
        self.assertIn("Reauthorization required", HTML)
        self.assertIn("st.reauth_required?'reauth required':'not connected'", HTML)
        self.assertIn("SETTINGS.claude_reauth_required=!!st.reauth_required", HTML)
        self.assertIn("else if(!st.connected){ SETTINGS.claude_models=[]", HTML)
        self.assertIn("claude:[]", HTML)

    def test_expired_claude_card_is_functionally_fail_closed(self) -> None:
        if not NODE:
            self.skipTest("node is unavailable")
        source = "\n".join(
            _js_function(name) for name in ("runtimeUiSpec", "claudeCardBody")
        )
        program = f"""
const LIB_ESC=value=>String(value);
let SETTINGS={{configured_providers:['anthropic'],claude_connected:false,claude_reauth_required:true}};
{source}
const body=claudeCardBody({{claude_installed:true,status:'idle',connected:false,reauth_required:true}});
console.log(JSON.stringify([runtimeUiSpec('claude').state,body.includes('Reconnect via Claude'),body.includes('will not be used as a fallback'),body.includes('Connected.') ]));
"""
        result = subprocess.run(
            [NODE, "-e", program], capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            ["Reauthorization required", True, True, False],
        )

    def test_one_new_run_runtime_selector_replaces_dual_toggles(self) -> None:
        self.assertIn("function setAccountRuntime(value)", HTML)
        self.assertIn('role="radiogroup" aria-label="Runtime for new runs"', HTML)
        self.assertIn('data-account-runtime="${runtime}"', HTML)
        self.assertIn("account_runtime:selectedAccountRuntime()", HTML)
        self.assertNotIn('id="codex-enabled"', HTML)
        self.assertNotIn('id="claude-enabled"', HTML)
        self.assertNotIn("mode-switch", HTML)
        self.assertNotIn("{updates,codex_enabled:$('codex-enabled')", HTML)

    def test_settings_explain_pinning_and_dormant_api_credentials(self) -> None:
        self.assertIn("Existing runs stay pinned.", HTML)
        self.assertIn("Connection cards do not change routing", HTML)
        self.assertIn('id="api-provider-group"', HTML)
        self.assertIn("Dormant for new runs", HTML)
        self.assertIn("SETTINGS.api_providers_active=runtime==='api'", HTML)
        self.assertNotIn("Future codex runs will fall back to your API key", HTML)

    def test_models_and_incompatible_engines_are_runtime_scoped(self) -> None:
        self.assertIn("SETTINGS?.models_by_runtime||{}", HTML)
        self.assertIn("function syncRuntimeModelControls(forceDefault=false)", HTML)
        self.assertIn("const presets=runtimeModelOptions()", HTML)
        self.assertNotIn("const m=[...new Set(SETTINGS.model_presets||[])]", HTML)
        self.assertIn("if(runtime==='claude')return SETTINGS?.claude_connected", HTML)
        self.assertIn("if(runtime==='codex')return SETTINGS?.codex_connected", HTML)
        self.assertIn("Switch runtime in Settings or choose a compatible harness.", HTML)
        self.assertIn("${usable?'':'disabled'}", HTML)

    def test_runtime_routing_behavior_never_falls_back_to_api(self) -> None:
        if not NODE:
            self.skipTest("node is unavailable")
        source = "\n".join(
            _js_function(name)
            for name in (
                "selectedAccountRuntime",
                "runtimeModelOptions",
                "engineModelMode",
            )
        )
        program = f"""
let SETTINGS=null, ENGINES=[], engine='plain';
{source}
const multi={{auth_modes:['api_key','codex_subscription','claude_subscription']}};
const apiOnly={{auth_modes:['api_key']}};
const output=[];
SETTINGS={{account_runtime:'claude',claude_connected:true,models_by_runtime:{{claude:['claude-haiku-4-5'],codex:['gpt-codex'],api:['kimi-k3']}}}};
output.push(engineModelMode(multi),engineModelMode(apiOnly),runtimeModelOptions());
SETTINGS.claude_connected=false;
output.push(engineModelMode(multi));
SETTINGS={{account_runtime:'codex',codex_connected:true,models_by_runtime:{{claude:['claude-haiku-4-5'],codex:['gpt-codex'],api:['kimi-k3']}}}};
output.push(engineModelMode(multi),runtimeModelOptions());
SETTINGS={{account_runtime:'api',configured_providers:['kimi'],models_by_runtime:{{claude:['claude-haiku-4-5'],codex:['gpt-codex'],api:['kimi-k3']}}}};
output.push(engineModelMode(multi),runtimeModelOptions());
console.log(JSON.stringify(output));
"""
        result = subprocess.run(
            [NODE, "-e", program], capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            [
                "claude_subscription",
                "unavailable",
                ["claude-haiku-4-5"],
                "unavailable",
                "codex_subscription",
                ["gpt-codex"],
                "api_key",
                ["kimi-k3"],
            ],
        )

    def test_cancel_restores_persisted_runtime_and_save_commits_once(self) -> None:
        self.assertIn("SETTINGS_RUNTIME_BASELINE=selectedAccountRuntime()", HTML)
        self.assertIn("saved!==true&&SETTINGS_RUNTIME_BASELINE&&SETTINGS", HTML)
        self.assertIn("applyRuntimeToSettings(SETTINGS_RUNTIME_BASELINE)", HTML)
        self.assertIn("closeSettings(true);syncRuntimeModelControls();renderEngines()", HTML)
        save_start = HTML.index("async function saveSettings()")
        save_end = HTML.index("$('settings-btn').onclick", save_start)
        self.assertNotIn("await loadSettings()", HTML[save_start:save_end])

    def test_cancel_and_x_restore_the_runtime_baseline_in_javascript(self) -> None:
        if not NODE:
            self.skipTest("node is unavailable")
        source = "\n".join(
            _js_function(name)
            for name in (
                "runtimeModelOptions",
                "applyRuntimeToSettings",
                "closeSettings",
            )
        )
        program = f"""
let SETTINGS={{account_runtime:'api',configured_providers:['kimi'],claude_connected:true,codex_connected:true,models_by_runtime:{{api:['kimi-k3'],claude:['claude-haiku-4-5'],codex:['gpt-codex']}}}};
let SETTINGS_RUNTIME_BASELINE='api';
const modal={{classList:{{remove:()=>{{}}}}}};
const $=id=>id==='settings-modal'?modal:null;
const stopCodexPoll=()=>{{}}, stopClaudePoll=()=>{{}};
let syncCount=0, renderCount=0;
const syncRuntimeModelControls=()=>{{syncCount+=1;}}, renderEngines=()=>{{renderCount+=1;}};
{source}
SETTINGS.account_runtime='claude';
closeSettings({{type:'click'}});
const cancelled=[SETTINGS.account_runtime,SETTINGS_RUNTIME_BASELINE,syncCount,renderCount];
SETTINGS_RUNTIME_BASELINE='api';
SETTINGS.account_runtime='claude';
closeSettings(true);
console.log(JSON.stringify([cancelled,SETTINGS.account_runtime,SETTINGS_RUNTIME_BASELINE]));
"""
        result = subprocess.run(
            [NODE, "-e", program], capture_output=True, text=True, check=False
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [["api", None, 1, 1], "claude", None])

    def test_start_run_has_one_declaration_and_one_response_decode(self) -> None:
        self.assertEqual(HTML.count("async function startRun()"), 1)
        start = HTML.index("async function startRun()")
        end = HTML.index("/* ── runs", start)
        fragment = HTML[start:end]
        self.assertEqual(fragment.count("const d=await r.json()"), 1)

    def test_main_script_is_valid_javascript(self) -> None:
        if not NODE:
            self.skipTest("node is unavailable")
        result = subprocess.run(
            [NODE, "--check", "-"],
            input=SCRIPT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_one_time_code_is_not_a_saved_settings_field(self) -> None:
        start = HTML.index('id="claude-login-code"')
        end = HTML.index('</label>', start)
        fragment = HTML[start:end]
        self.assertIn('autocomplete="off"', fragment)
        self.assertNotIn("data-key", fragment)
        self.assertIn("input.value=''; submit.disabled=true", HTML)
        self.assertLess(
            HTML.index("input.value=''; submit.disabled=true"),
            HTML.index("fetch('/api/settings/claude/login/code"),
        )
        self.assertIn("if(code)code.value='';", HTML)
        self.assertIn("stopCodexPoll(); stopClaudePoll();", HTML)

    def test_login_url_and_server_errors_are_html_escaped(self) -> None:
        self.assertIn('href="${LIB_ESC(st.url)}"', HTML)
        self.assertIn("${LIB_ESC(st.error)}", HTML)


if __name__ == "__main__":
    unittest.main()
