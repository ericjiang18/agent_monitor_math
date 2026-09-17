from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from agent_monitor import jobs, lean_verify, proof_graph, run_tuning
from agent_monitor.runners import api_backend


HTML = (
    Path(__file__).parents[1] / "agent_monitor" / "web" / "console.html"
).read_text(encoding="utf-8")
SCRIPT = HTML.rsplit("<script>", 1)[1].split("</script>", 1)[0]
NODE = shutil.which("node") or "/home/ubuntu/.local/node24/bin/node"


class RunTuningContractTests(unittest.TestCase):
    def test_codex_sol_exposes_real_fast_and_ultra_values(self) -> None:
        caps = run_tuning.capabilities(
            "gpt-5.6-sol", "codex_subscription", "codex"
        )
        self.assertEqual(
            [item["id"] for item in caps["reasoning"]],
            ["default", "low", "medium", "high", "xhigh", "max"],
        )
        self.assertEqual(
            [item["id"] for item in caps["speed"]], ["standard", "fast"]
        )
        self.assertEqual(caps["reasoning"][-1]["label"], "Ultra")

    def test_claude_code_and_openai_api_expose_only_real_controls(self) -> None:
        claude = run_tuning.capabilities("sonnet", "claude_subscription", "plain")
        openai = run_tuning.capabilities("gpt-5.6-sol", "api_key", "plain")
        self.assertEqual(
            [item["id"] for item in claude["reasoning"]],
            ["default", "low", "medium", "high", "xhigh", "max"],
        )
        self.assertEqual([item["id"] for item in claude["speed"]], ["standard"])
        self.assertIn("deepagents", claude["supported_engines"])
        self.assertEqual(
            [item["id"] for item in openai["speed"]], ["standard", "fast"]
        )

    def test_capabilities_fail_closed_for_unattested_harness_and_sponsor(self) -> None:
        unsupported = run_tuning.capabilities(
            "gpt-5.6-sol", "api_key", "openclaude"
        )
        sponsored = run_tuning.capabilities("kimi-k3", "sponsored_kimi", "plain")
        self.assertEqual([item["id"] for item in unsupported["reasoning"]], ["default"])
        self.assertEqual([item["id"] for item in unsupported["speed"]], ["standard"])
        self.assertEqual([item["id"] for item in sponsored["reasoning"]], ["default"])

    def test_invalid_explicit_profile_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "reasoning effort"):
            run_tuning.normalize(
                "kimi-k3", "api_key", reasoning_effort="xhigh", engine="plain"
            )
        with self.assertRaisesRegex(ValueError, "speed mode"):
            run_tuning.normalize(
                "kimi-k3", "api_key", speed_mode="fast", engine="plain"
            )

    def test_environment_projection_is_provider_specific(self) -> None:
        kimi_env: dict[str, str] = {}
        run_tuning.apply_environment(
            kimi_env,
            model="kimi-k3",
            auth_route="api_key",
            reasoning_effort="max",
            speed_mode="standard",
        )
        self.assertEqual(kimi_env["KIMI_REASONING_EFFORT"], "max")
        self.assertNotIn("service_tier", kimi_env)

        claude_env: dict[str, str] = {}
        run_tuning.apply_environment(
            claude_env,
            model="sonnet",
            auth_route="claude_subscription",
            reasoning_effort="high",
            speed_mode="standard",
        )
        self.assertEqual(claude_env["AGENT_MONITOR_CLAUDE_EFFORT"], "high")

    def test_openai_api_payload_receives_reasoning_and_fast(self) -> None:
        captured: dict = {}

        def fake_http(_url, **kwargs):
            captured.update(kwargs["payload"])
            return 200, {"output_text": "done", "usage": {}}

        env = {
            "OPENAI_API_KEY": "test-only-key",
            run_tuning.REASONING_ENV: "max",
            run_tuning.SPEED_ENV: "fast",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch("agent_monitor.settings._http_json", side_effect=fake_http),
        ):
            result = api_backend.api_chat("system", "user", model="gpt-5.6-sol")
        self.assertEqual(result.text, "done")
        self.assertEqual(captured["reasoning"], {"effort": "max"})
        self.assertEqual(captured["service_tier"], "fast")

    def test_dag_openai_payload_receives_same_profile(self) -> None:
        captured: dict = {}

        def fake_http(_url, **kwargs):
            captured.update(kwargs["payload"])
            return 200, {"output_text": "{}"}

        env = {
            "OPENAI_API_KEY": "test-only-key",
            run_tuning.REASONING_ENV: "high",
            run_tuning.SPEED_ENV: "standard",
        }
        with patch("agent_monitor.settings._http_json", side_effect=fake_http):
            model, content, provider = proof_graph._call_llm(
                env, "gpt-5.6-sol", "graph"
            )
        self.assertEqual((model, content, provider), ("gpt-5.6-sol", "{}", "openai"))
        self.assertEqual(captured["reasoning"], {"effort": "high"})
        self.assertEqual(captured["service_tier"], "default")

    def test_codex_cli_config_is_exact_and_kimi_remains_proxy_owned(self) -> None:
        env = {
            run_tuning.REASONING_ENV: "max",
            run_tuning.SPEED_ENV: "fast",
        }
        self.assertEqual(
            run_tuning.codex_config_args(env, "gpt-5.6-sol"),
            [
                "-c",
                'model_reasoning_effort="max"',
                "-c",
                'service_tier="fast"',
            ],
        )
        self.assertEqual(run_tuning.codex_config_args(env, "kimi-k3"), [])

    def test_unsupported_harness_rejects_before_problem_or_workspace(self) -> None:
        engine = {
            "id": "openclaude",
            "label": "OpenClaude",
            "available": True,
            "auth_modes": ["api_key"],
        }
        with (
            patch("agent_monitor.engines_registry.all_engine_ids", return_value={"openclaude"}),
            patch("agent_monitor.engines_registry.list_engines", return_value=[engine]),
            patch("agent_monitor.engines_registry.supported_models", return_value=[]),
            patch.object(jobs, "_user_extra_env", return_value={"OPENAI_API_KEY": "test"}),
            patch.object(jobs, "_selected_auth_route", return_value=("api_key", True)),
            patch.object(jobs, "resolve_problem") as resolve,
            patch.object(jobs, "workspace_dir") as workspace,
        ):
            with self.assertRaisesRegex(ValueError, "speed mode"):
                jobs.start_job(
                    engine="openclaude",
                    problem_text="prove True",
                    model="gpt-5.6-sol",
                    auth_route="api_key",
                    speed_mode="fast",
                )
        resolve.assert_not_called()
        workspace.assert_not_called()

    def test_new_run_persists_and_projects_validated_profile(self) -> None:
        captured_runs: list[dict] = []
        captured_threads: list[dict] = []

        class FakeThread:
            def __init__(self, *, kwargs, **_other):
                captured_threads.append(kwargs)

            def start(self) -> None:
                return None

        engine = {
            "id": "plain",
            "label": "Plain",
            "available": True,
            "auth_modes": ["api_key"],
        }
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("agent_monitor.engines_registry.all_engine_ids", return_value={"plain"}),
                patch("agent_monitor.engines_registry.list_engines", return_value=[engine]),
                patch("agent_monitor.engines_registry.supported_models", return_value=[]),
                patch.object(jobs, "_user_extra_env", return_value={"OPENAI_API_KEY": "test"}),
                patch.object(jobs, "_selected_auth_route", return_value=("api_key", True)),
                patch.object(jobs, "resolve_problem", return_value=("case", "prove True", None)),
                patch.object(jobs, "workspace_dir", return_value=Path(tmp)),
                patch.object(jobs, "_write_run", side_effect=lambda run: captured_runs.append(dict(run))),
                patch.object(jobs, "_JOBS", {}),
                patch.object(jobs.threading, "Thread", FakeThread),
            ):
                job = jobs.start_job(
                    engine="plain",
                    problem_text="prove True",
                    model="gpt-5.6-sol",
                    auth_route="api_key",
                    reasoning_effort="max",
                    speed_mode="fast",
                )
        self.assertEqual(job["reasoning_effort"], "max")
        self.assertEqual(job["speed_mode"], "fast")
        self.assertEqual(captured_runs[0]["reasoning_effort"], "max")
        worker = captured_threads[0]
        self.assertEqual(worker["reasoning_effort"], "max")
        self.assertEqual(worker["speed_mode"], "fast")
        self.assertEqual(
            worker["extra_env"][run_tuning.REASONING_ENV], "max"
        )

    def test_manual_formal_codex_argv_carries_profile(self) -> None:
        env = {
            run_tuning.REASONING_ENV: "xhigh",
            run_tuning.SPEED_ENV: "fast",
        }
        argv = lean_verify._selected_harness_argv(
            engine="codex",
            model="gpt-5.6-sol",
            prompt="formalize",
            default=["codex", "exec", "formalize"],
            env=env,
        )
        prompt_index = argv.index("formalize")
        self.assertEqual(argv[argv.index("--model") + 1], "gpt-5.6-sol")
        self.assertLess(argv.index('model_reasoning_effort="xhigh"'), prompt_index)
        self.assertLess(argv.index('service_tier="fast"'), prompt_index)

    def test_continuation_and_formal_environment_keep_recorded_profile(self) -> None:
        record = {
            "run_id": "plain_case",
            "owner_id": 7,
            "auth_route": "api_key",
            "model": "kimi-k3",
            "credential_source": "user",
            "reasoning_effort": "max",
            "speed_mode": "standard",
        }
        engine = {"id": "plain", "auth_modes": ["api_key"]}
        with patch("agent_monitor.engines_registry.list_engines", return_value=[engine]):
            continued, route = jobs._continuation_auth_environment(
                run_data=record,
                engine="plain",
                model="kimi-k3",
                user=None,
                env={"KIMI_API_KEY": "test"},
            )
        self.assertEqual(route, "api_key")
        self.assertEqual(continued["KIMI_REASONING_EFFORT"], "max")

        with patch("agent_monitor.settings.resolved_user_env", return_value={"KIMI_API_KEY": "test"}):
            formal = lean_verify._llm_environment(None, record)
        self.assertEqual(formal["KIMI_REASONING_EFFORT"], "max")
        self.assertEqual(formal[run_tuning.REASONING_ENV], "max")


class RunTuningUiTests(unittest.TestCase):
    def test_run_mode_replaces_subagents_in_the_prompt_dock(self) -> None:
        for identifier in (
            "run-profile-trigger",
            "run-profile-popover",
            "run-speed-slider",
            "run-thinking-slider",
            "run-speed-stage",
            "run-thinking-stage",
        ):
            self.assertEqual(HTML.count(f'id="{identifier}"'), 1)
        dock = HTML.split('<div class="choice-dock">', 1)[1].split(
            '<button class="start"', 1
        )[0]
        sidebar = HTML.split('<aside class="sidebar">', 1)[1].split('</aside>', 1)[0]
        self.assertIn('<details class="run-profile choice-field"', dock)
        self.assertIn('<summary class="run-profile-trigger"', dock)
        details = dock.split('<details class="run-profile choice-field"', 1)[1]
        self.assertLess(details.index("<summary"), details.index("run-profile-popover"))
        self.assertNotIn('<span class="choice-label">Speed / Thinking</span>', details)
        self.assertNotIn('id="run-profile"', sidebar)
        self.assertNotIn('id="subagents-check"', HTML)
        self.assertNotIn('id="subagent-model-select"', HTML)
        self.assertIn("SETTINGS?.run_tuning_by_runtime?.[runtime]?.[model]", HTML)
        self.assertIn("reasoning_effort:tuning.reasoning_effort", HTML)
        self.assertIn("speed_mode:tuning.speed_mode", HTML)
        self.assertIn("use_subagents:false", HTML)
        self.assertIn("subagent_model:null", HTML)
        self.assertIn("Existing runs, Formal Lean, Lean DAG, and continuations", HTML)

    def test_run_mode_motion_is_helpful_and_respects_reduced_motion(self) -> None:
        self.assertIn("runProfilePulse", HTML)
        self.assertIn("runProfileRevealHalf", HTML)
        self.assertIn("runProfileStars", HTML)
        self.assertIn("runSliderGlide", HTML)
        self.assertIn("runSliderShimmer", HTML)
        self.assertIn("runSliderPulse", HTML)
        self.assertIn("height: 13px", HTML)
        self.assertIn("--slider-progress", HTML)
        self.assertNotIn("calc(var(--slider-progress)", HTML)
        self.assertIn("width: 200%", HTML)
        self.assertIn("transform: translateY(0) scale(.5)", HTML)
        self.assertNotIn("animation: runProfileReveal .36s", HTML)
        self.assertIn(".run-profile-field-head b { font-size: 25px", HTML)
        self.assertNotIn(".run-profile-popover { position: fixed", HTML)
        self.assertNotIn('class="run-profile-head"', HTML)
        self.assertNotIn('class="run-profile-hero"', HTML)
        self.assertIn('class="run-slider-fill"', HTML)
        self.assertIn("run-mode-slider speed-slider", HTML)
        self.assertNotIn("speed-options", HTML)
        self.assertNotIn("function toggleRunProfile", HTML)
        self.assertIn("$('run-profile').ontoggle", HTML)
        self.assertIn("@media (prefers-reduced-motion: reduce)", HTML)

    @unittest.skipUnless(Path(NODE).is_file(), "node is unavailable")
    def test_full_inline_javascript_parses(self) -> None:
        result = subprocess.run(
            [NODE, "-e", "new Function(require('fs').readFileSync(0,'utf8'));"],
            input=SCRIPT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_user_secret_was_not_copied_into_touched_sources(self) -> None:
        for path in (
            Path("agent_monitor/run_tuning.py"),
            Path("agent_monitor/web/console.html"),
            Path("tests/test_run_tuning.py"),
        ):
            self.assertNotIn("sk" + "-proj-", path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
