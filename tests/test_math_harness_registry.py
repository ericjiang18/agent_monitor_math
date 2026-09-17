"""Focused offline tests for the UCLA-to-Math-Harness catalog migration."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import engines_registry, jobs, settings
from agent_monitor.schema import (
    MATH_HARNESS_ARTIFACT_SCHEMA,
    MATH_HARNESS_ENGINE_IMPL,
    MATH_HARNESS_ENGINE_IMPL_VERSION,
    MATH_HARNESS_GRAPH_SCHEMA,
    normalize_run,
)


class _ThreadWithoutAlive:
    """Thread double that prevents start_job from launching either worker."""

    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs
        self.started = False

    def start(self) -> None:
        self.started = True


class MathHarnessCatalogTests(unittest.TestCase):
    def test_math_harness_replaces_ucla_only_in_the_new_run_catalog(self) -> None:
        with patch.object(
            engines_registry, "_cli_available", return_value=(True, "runner")
        ):
            catalog = engines_registry.list_engines()

        ids = [item["id"] for item in catalog]
        self.assertEqual(ids[2], "math_harness")
        self.assertIn("math_harness", engines_registry.all_engine_ids())
        self.assertNotIn("ucla", engines_registry.all_engine_ids())
        self.assertIn("ucla", engines_registry.LEGACY_ENGINE_IDS)
        self.assertIn("ucla", engines_registry.known_engine_ids())
        self.assertNotIn("ucla", ids)

        engine = next(item for item in catalog if item["id"] == "math_harness")
        self.assertEqual(engine["label"], "Math Agent Harness")
        self.assertEqual(engine["kind"], "cli")
        self.assertEqual(
            engine["auth_modes"],
            ["codex_subscription", "claude_subscription", "api_key"],
        )
        self.assertTrue(engine["subscription_supported"])
        self.assertTrue(engine["claude_subscription_supported"])
        self.assertFalse(engine["formal_lean_supported"])
        self.assertEqual(
            engines_registry.CLI_ENGINES["math_harness"]["check_file"],
            "agent_monitor/runners/math_harness_runner.py",
        )

    def test_legacy_ucla_cannot_start_a_new_run(self) -> None:
        with patch.object(jobs, "_user_extra_env") as user_env:
            with self.assertRaisesRegex(ValueError, "Unsupported engine: ucla"):
                jobs.start_job(
                    engine="ucla",
                    problem_text="Prove one equals one.",
                    user=None,
                )
        user_env.assert_not_called()

    def test_math_harness_initial_record_has_identity_and_iteration_limit(self) -> None:
        written: list[dict] = []
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with (
                patch.object(
                    engines_registry, "_cli_available", return_value=(True, "runner")
                ),
                patch.object(
                    jobs,
                    "resolve_problem",
                    return_value=("problem", "Prove one equals one.", None),
                ),
                patch.object(jobs, "workspace_dir", return_value=workspace),
                patch.object(
                    jobs, "_selected_auth_route", return_value=("api_key", True)
                ),
                patch.object(
                    jobs, "_write_run", side_effect=lambda run: written.append(dict(run))
                ),
                patch.object(jobs.threading, "Thread", _ThreadWithoutAlive),
            ):
                result = jobs.start_job(
                    engine="math_harness",
                    problem_text="Prove one equals one.",
                    max_iterations=37,
                    user=None,
                )

        self.assertTrue(result["run_id"].startswith("math_harness_problem_"))
        self.assertEqual(len(written), 1)
        initial = written[0]
        self.assertEqual(initial["engine"], "math_harness")
        self.assertEqual(initial["max_iterations"], 37)
        self.assertEqual(initial["engine_impl"], MATH_HARNESS_ENGINE_IMPL)
        self.assertEqual(
            initial["engine_impl_version"], MATH_HARNESS_ENGINE_IMPL_VERSION
        )
        self.assertEqual(initial["artifact_schema"], MATH_HARNESS_ARTIFACT_SCHEMA)
        self.assertEqual(initial["graph_schema"], MATH_HARNESS_GRAPH_SCHEMA)
        self.assertEqual(
            [stage["id"] for stage in initial["pipeline"]],
            ["scaffold", "work", "verify", "curate", "finalize"],
        )


class MathHarnessProvenanceTests(unittest.TestCase):
    def test_normalization_pins_math_harness_provenance(self) -> None:
        run = normalize_run(
            {
                "run_id": "math_harness_example",
                "engine_impl": "untrusted-runner-value",
                "engine_impl_version": "untrusted-version",
            },
            engine="math_harness",
        )

        self.assertEqual(run["engine_impl"], MATH_HARNESS_ENGINE_IMPL)
        self.assertEqual(
            run["engine_impl_version"], MATH_HARNESS_ENGINE_IMPL_VERSION
        )
        self.assertEqual(run["artifact_schema"], MATH_HARNESS_ARTIFACT_SCHEMA)
        self.assertEqual(run["graph_schema"], MATH_HARNESS_GRAPH_SCHEMA)

        legacy = normalize_run({"run_id": "ucla_old"}, engine="ucla")
        self.assertEqual(legacy["engine"], "ucla")
        self.assertNotIn("engine_impl", legacy)
        self.assertNotIn("artifact_schema", legacy)

    def test_subprocess_final_record_keeps_math_harness_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "proof.md").write_text(
                "# Proof\n\nA complete proof artifact for the final record.\n",
                encoding="utf-8",
            )
            with (
                patch.object(jobs, "_artifact_agents", return_value=None),
                patch.object(jobs, "_attach_analysis"),
            ):
                final = jobs._wrap_subprocess_result(
                    run_id="math_harness_final",
                    engine="math_harness",
                    problem_id="example",
                    problem_text="Prove the claim.",
                    result={"status": "finished", "returncode": 0},
                    started=0.0,
                    workspace=workspace,
                )

        self.assertEqual(final["status"], "finished")
        self.assertEqual(final["engine_impl"], MATH_HARNESS_ENGINE_IMPL)
        self.assertEqual(
            final["engine_impl_version"], MATH_HARNESS_ENGINE_IMPL_VERSION
        )
        self.assertEqual(final["artifact_schema"], MATH_HARNESS_ARTIFACT_SCHEMA)
        self.assertEqual(final["graph_schema"], MATH_HARNESS_GRAPH_SCHEMA)

    def test_math_harness_iteration_environment_is_bounded(self) -> None:
        env: dict[str, str] = {}
        jobs._apply_math_harness_iteration_env(
            env, engine="math_harness", max_iterations=500
        )
        self.assertEqual(env["AGENT_MONITOR_MAX_ITERATIONS"], "200")
        self.assertEqual(env["MATH_HARNESS_MAX_ITERATIONS"], "200")

        untouched: dict[str, str] = {}
        jobs._apply_math_harness_iteration_env(
            untouched, engine="ucla", max_iterations=20
        )
        self.assertEqual(untouched, {})

    def test_old_ucla_continuation_stays_on_legacy_path(self) -> None:
        previous = normalize_run(
            {
                "run_id": "ucla_old",
                "problem_id": "old",
                "problem_text": "Original theorem.",
                "status": "finished",
                "problems": [{"text": "Original theorem.", "source": "initial"}],
            },
            engine="ucla",
        )
        legacy_result = normalize_run(
            {
                "run_id": "ucla_old",
                "problem_id": "old",
                "status": "finished",
                "agents": [{"trace_id": "legacy", "output": "Updated proof."}],
            },
            engine="hermes",
        )
        writes: list[dict] = []

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with (
                patch.object(jobs, "_load_run_record", return_value=previous),
                patch.object(jobs, "_write_session_problems"),
                patch.object(
                    jobs, "_write_run", side_effect=lambda run: writes.append(dict(run))
                ),
                patch.object(jobs, "_run_hermes", return_value=legacy_result) as hermes,
                patch.object(jobs, "_run_cli_engine_with_openclaude_retry") as cli,
                patch.object(jobs, "_update_job"),
                patch.object(jobs, "_append_chat"),
                patch.object(jobs, "_root_proof_artifact", return_value=None),
                patch.object(jobs, "_launch_pending_feedback"),
            ):
                jobs._execute_continue(
                    job_id="continue-job",
                    run_id="ucla_old",
                    engine="ucla",
                    message="Tighten the last step.",
                    problem_text="Original theorem.",
                    model=None,
                    max_iterations=5,
                    workspace=str(workspace),
                )

        hermes.assert_called_once()
        cli.assert_not_called()
        self.assertEqual(writes[-1]["engine"], "ucla")
        self.assertNotIn("engine_impl", writes[-1])


class MathHarnessSettingsTests(unittest.TestCase):
    def test_ucla_saved_default_is_migrated_only_in_preferences(self) -> None:
        file_env = {
            "AGENT_MONITOR_ENGINE": "ucla",
            "AGENT_MONITOR_ACCOUNT_RUNTIME": "api",
        }
        with (
            patch.dict(settings.os.environ, {}, clear=True),
            patch.object(settings, "_read_env_file", return_value=file_env),
        ):
            result = settings.get_settings()

        self.assertEqual(result["saved_default_engine"], "ucla")
        self.assertEqual(result["default_engine"], "math_harness")
        self.assertEqual(
            result["general"]["AGENT_MONITOR_ENGINE"]["value"], "math_harness"
        )

        legacy_run = normalize_run({"run_id": "ucla_old"}, engine="ucla")
        self.assertEqual(legacy_run["engine"], "ucla")

    def test_saving_legacy_default_persists_canonical_preference(self) -> None:
        with (
            patch("agent_monitor.auth.set_user_env") as set_user_env,
            patch.object(settings, "get_settings", return_value={"default_engine": "math_harness"}),
        ):
            settings.save_settings(
                {"updates": {"AGENT_MONITOR_ENGINE": "ucla"}},
                user={"id": 17},
            )

        updates = set_user_env.call_args.args[1]
        self.assertEqual(updates["AGENT_MONITOR_ENGINE"], "math_harness")


if __name__ == "__main__":
    unittest.main()
