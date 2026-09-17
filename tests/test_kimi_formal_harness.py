from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import lean_verify
from agent_monitor.runners import kimi_codex_runner


class KimiFormalHarnessTests(unittest.TestCase):
    def test_formal_codex_harness_selects_kimi_wrapper_only_for_kimi(self) -> None:
        original = ["codex", "exec", "formalize"]
        selected = lean_verify._selected_harness_argv(
            engine="codex",
            model="kimi-k3",
            prompt="formalize",
            default=original,
        )
        self.assertEqual(selected[0:2], [os.sys.executable, "-u"])
        self.assertTrue(selected[2].endswith("/runners/kimi_codex_runner.py"))
        self.assertEqual(selected[3], "formalize")
        native = lean_verify._selected_harness_argv(
            engine="codex",
            model="gpt-5.6-sol",
            prompt="formalize",
            default=original,
        )
        self.assertEqual(
            native,
            ["codex", "exec", "--model", "gpt-5.6-sol", "formalize"],
        )

    def test_formal_codex_harness_replaces_stale_model_and_rejects_unbound_metadata(
        self,
    ) -> None:
        selected = lean_verify._selected_harness_argv(
            engine="codex",
            model="gpt-5.6-sol",
            prompt="formalize",
            default=["codex", "exec", "--model", "stale-model", "formalize"],
        )
        self.assertEqual(selected[selected.index("--model") + 1], "gpt-5.6-sol")
        with self.assertRaisesRegex(ValueError, "explicit model"):
            lean_verify._selected_harness_argv(
                engine="codex",
                model=None,
                prompt="formalize",
                default=["codex", "exec", "formalize"],
            )
        with self.assertRaisesRegex(ValueError, "cannot bind"):
            lean_verify._selected_harness_argv(
                engine="codex",
                model="gpt-5.6-sol",
                prompt="formalize",
                default=["custom-codex-adapter"],
            )

    def test_formal_codex_rejects_unavailable_model_before_workspace_or_spawn(
        self,
    ) -> None:
        run = {
            "owner_id": 7,
            "auth_route": "codex_subscription",
            "model": "gpt-5.6-sol",
        }
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            with (
                patch.object(
                    lean_verify,
                    "_harness_auth_route",
                    return_value=("codex_subscription", {}),
                ),
                patch.object(
                    lean_verify,
                    "harness_model_options",
                    return_value={"models": ["gpt-5.6-sol"]},
                ),
                patch("agent_monitor.lean_verify.threading.Thread") as thread,
            ):
                with self.assertRaisesRegex(ValueError, "not available"):
                    lean_verify.start_harness(
                        workspace=workspace, run_record=run, user={"id": 7},
                        engine="codex", model="gpt-incompatible",
                    )
    def test_kimi_formal_env_never_requires_or_reenables_codex_auth(self) -> None:
        with (
            patch(
                "agent_monitor.settings.resolved_user_env",
                return_value={
                    "KIMI_API_KEY": "test-kimi-key",
                    "KIMI_API_BASE": "https://kimi.example/v1",
                    "AGENT_MONITOR_CODEX_SUBSCRIPTION": "1",
                },
            ),
            patch("agent_monitor.jobs._ensure_codex_auth") as ensure_codex,
            patch.object(
                lean_verify,
                "toolchain_status",
                return_value={"lean": "/bin/lean", "lake": None, "elan": None},
            ),
        ):
            env = lean_verify._harness_env(
                "codex", {"id": 7}, model="kimi-k3"
            )

        ensure_codex.assert_not_called()
        self.assertEqual(env["AGENT_MONITOR_AUTH_MODE"], "api_key")
        self.assertEqual(env["AGENT_MONITOR_KIMI_CODEX_MODE"], "lean")
        self.assertNotIn("AGENT_MONITOR_CODEX_SUBSCRIPTION", env)

    def test_kimi_codex_runner_requires_proof_lean_in_formal_mode(self) -> None:
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                with patch.dict(
                    os.environ,
                    {"AGENT_MONITOR_KIMI_CODEX_MODE": "lean"},
                    clear=False,
                ):
                    self.assertIsNone(kimi_codex_runner._artifact())
                    Path("proof.md").write_text("informal", encoding="utf-8")
                    self.assertIsNone(kimi_codex_runner._artifact())
                    Path("Proof.lean").write_text(
                        "theorem main (p : Prop) (h : p) : p := h\n",
                        encoding="utf-8",
                    )
                    self.assertEqual(
                        kimi_codex_runner._artifact(), Path(tmp) / "Proof.lean"
                    )
            finally:
                os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
