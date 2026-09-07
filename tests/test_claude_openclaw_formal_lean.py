from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_monitor.runners import openclaw_runner


def _private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def _valid_lean(name: str = "generated") -> str:
    return (
        "/- Fresh OpenClaw formal artifact. The outer runner performs the "
        "authoritative compilation. -/\n"
        f"theorem {name} : True := by\n"
        "  trivial\n"
    )


class ClaudeOpenClawFormalLeanArtifactTests(unittest.TestCase):
    def test_validator_accepts_only_fresh_substantive_root_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            proof = workspace / "Proof.lean"
            proof.write_text(_valid_lean(), encoding="utf-8")

            accepted, detail = (
                openclaw_runner._validate_fresh_formal_lean_artifact(
                    workspace,
                    prior_sha256=None,
                )
            )
            prior = openclaw_runner._proof_sha256(proof)
            stale, stale_detail = (
                openclaw_runner._validate_fresh_formal_lean_artifact(
                    workspace,
                    prior_sha256=prior,
                )
            )

        self.assertTrue(accepted, detail)
        self.assertFalse(stale)
        self.assertIn("unchanged", stale_detail)

    def test_validator_rejects_missing_short_placeholder_and_linked_artifacts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            proof = workspace / "Proof.lean"

            cases: list[tuple[str, str]] = []
            accepted, detail = (
                openclaw_runner._validate_fresh_formal_lean_artifact(
                    workspace,
                    prior_sha256=None,
                )
            )
            cases.append(("missing", detail))
            self.assertFalse(accepted)

            proof.write_text("example : True := by trivial\n", encoding="utf-8")
            accepted, detail = (
                openclaw_runner._validate_fresh_formal_lean_artifact(
                    workspace,
                    prior_sha256=None,
                )
            )
            cases.append(("short", detail))
            self.assertFalse(accepted)

            proof.write_text(
                "/- padded formal candidate without placeholders -/\n"
                "theorem bad : True := by\n"
                "  sorry\n"
                + ("-- padding\n" * 8),
                encoding="utf-8",
            )
            accepted, detail = (
                openclaw_runner._validate_fresh_formal_lean_artifact(
                    workspace,
                    prior_sha256=None,
                )
            )
            cases.append(("placeholder", detail))
            self.assertFalse(accepted)

            proof.unlink()
            outside = Path(tmp).parent / f"{Path(tmp).name}-outside.lean"
            outside.write_text(_valid_lean("linked"), encoding="utf-8")
            self.addCleanup(outside.unlink, missing_ok=True)
            proof.symlink_to(outside)
            accepted, detail = (
                openclaw_runner._validate_fresh_formal_lean_artifact(
                    workspace,
                    prior_sha256=None,
                )
            )
            cases.append(("linked", detail))
            self.assertFalse(accepted)

        rendered = dict(cases)
        self.assertIn("did not create", rendered["missing"])
        self.assertIn("truncated", rendered["short"])
        self.assertIn("sorry/admit", rendered["placeholder"])
        self.assertIn("symbolic link", rendered["linked"])

    def _run_formal_main(
        self,
        workspace: Path,
        account: Path,
        claude_binary: Path,
        *,
        watchdog_returncode: int,
        generated_text: str | None,
        formal_flag: str = "AGENT_MONITOR_CLAUDE_LEAN_MODE",
    ) -> tuple[int, dict[str, MagicMock]]:
        def which(name: str) -> str | None:
            if name == "openclaw":
                return "/mock/openclaw"
            if name == "claude":
                return str(claude_binary)
            return None

        def watchdog(*_args, **_kwargs) -> int:
            if generated_text is not None:
                (workspace / "Proof.lean").write_text(
                    generated_text,
                    encoding="utf-8",
                )
            return watchdog_returncode

        selected_env = {
            "HOME": str(account),
            "CLAUDE_CONFIG_DIR": str(account),
            "AGENT_MONITOR_CLAUDE_SUBSCRIPTION": "1",
            "AGENT_MONITOR_CLAUDE_MODEL": "claude-haiku-4-5",
        }
        selected_env[formal_flag] = "1"
        with (
            patch.dict(os.environ, selected_env, clear=True),
            patch.object(
                openclaw_runner.sys,
                "argv",
                ["openclaw_runner.py", "formalize this", "lean-run"],
            ),
            patch.object(openclaw_runner.Path, "cwd", return_value=workspace),
            patch.object(openclaw_runner.shutil, "which", side_effect=which),
            patch.object(
                openclaw_runner,
                "_bind_workspace_tool_python",
                return_value=[],
            ),
            patch.object(
                openclaw_runner,
                "_run_with_terminal_watchdog",
                side_effect=watchdog,
            ),
            patch.object(
                openclaw_runner,
                "_attest_claude_session_model",
                return_value=(
                    ("claude-cli", "claude-haiku-4-5-20251001"),
                ),
            ),
            patch.object(openclaw_runner, "_terminal_event_since") as terminal,
            patch.object(
                openclaw_runner,
                "_persist_terminal_candidate",
            ) as persist,
            patch.object(
                openclaw_runner,
                "_recover_checkpointed_proof",
            ) as recover,
            patch.object(
                openclaw_runner,
                "_normalize_requested_outcome",
            ) as normalize,
            patch.object(
                openclaw_runner,
                "_run_supplemental_audit",
            ) as audit,
            patch("builtins.print") as printed,
        ):
            returncode = openclaw_runner.main()
        return returncode, {
            "terminal": terminal,
            "persist": persist,
            "recover": recover,
            "normalize": normalize,
            "audit": audit,
            "printed": printed,
        }

    def test_formal_main_accepts_fresh_artifact_and_skips_informal_pipeline(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = _private_dir(root / "workspace")
            account = _private_dir(root / "account")
            claude_binary = _executable(root / "claude")
            returncode, calls = self._run_formal_main(
                workspace,
                account,
                claude_binary,
                watchdog_returncode=0,
                generated_text=_valid_lean(),
            )
            self.assertFalse((workspace / "proof.md").exists())

        self.assertEqual(returncode, 0)
        for name in ("terminal", "persist", "recover", "normalize", "audit"):
            calls[name].assert_not_called()
        status = "\n".join(
            str(call.args[0])
            for call in calls["printed"].call_args_list
            if call.args
        )
        self.assertIn('"agentMonitorFormalLeanArtifactAccepted": true', status)

    def test_formal_main_accepts_generic_formal_lean_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = _private_dir(root / "workspace")
            account = _private_dir(root / "account")
            claude_binary = _executable(root / "claude")
            returncode, calls = self._run_formal_main(
                workspace,
                account,
                claude_binary,
                watchdog_returncode=0,
                generated_text=_valid_lean("generic_flag"),
                formal_flag="AGENT_MONITOR_FORMAL_LEAN_MODE",
            )
            self.assertFalse((workspace / "proof.md").exists())

        self.assertEqual(returncode, 0)
        for name in ("terminal", "persist", "recover", "normalize", "audit"):
            calls[name].assert_not_called()


    def test_formal_main_rejects_stale_artifact_and_child_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = _private_dir(root / "workspace")
            account = _private_dir(root / "account")
            claude_binary = _executable(root / "claude")
            (workspace / "Proof.lean").write_text(
                _valid_lean("stale"),
                encoding="utf-8",
            )
            stale_returncode, stale_calls = self._run_formal_main(
                workspace,
                account,
                claude_binary,
                watchdog_returncode=0,
                generated_text=None,
            )
            failed_returncode, failed_calls = self._run_formal_main(
                workspace,
                account,
                claude_binary,
                watchdog_returncode=9,
                generated_text=_valid_lean("untrusted_after_failure"),
            )

        self.assertEqual(stale_returncode, 65)
        self.assertEqual(failed_returncode, 9)
        for calls in (stale_calls, failed_calls):
            for name in ("terminal", "persist", "recover", "normalize", "audit"):
                calls[name].assert_not_called()


if __name__ == "__main__":
    unittest.main()
