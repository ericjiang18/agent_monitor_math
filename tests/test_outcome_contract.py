from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from agent_monitor.runners import (
    deepagents_runner,
    metaharness_runner,
    openhands_subscription_runner,
    plain_runner,
)
from agent_monitor.runners.deepseek_harness_runner import ensure_requested_outcome
from agent_monitor.runners.outcome_contract import (
    canonicalize_outcome_text,
    ensure_requested_outcome as ensure_shared_outcome,
)


REQUEST = "End with ProvingConsole outcome: <label>."


class SharedOutcomeContractTests(unittest.TestCase):
    def test_sentence_punctuation_and_markdown_are_canonicalized(self) -> None:
        variants = (
            "ProvingConsole outcome: Solved.",
            "**ProvingConsole outcome: Solved!**",
            "**ProvingConsole outcome: Solved**.",
            "## ProvingConsole outcome\n\n**Solved?**",
            "## ProvingConsole outcome\r\n\r\n**Solved.**",
        )
        for footer in variants:
            with self.subTest(footer=footer):
                rendered = canonicalize_outcome_text(
                    "# Result\n\nA complete proof.\n\n" + footer + "\n"
                )
                self.assertEqual(rendered.count("ProvingConsole outcome:"), 1)
                self.assertTrue(
                    rendered.endswith("ProvingConsole outcome: Solved\n")
                )

    def test_identical_duplicates_collapse_without_downgrade(self) -> None:
        rendered = canonicalize_outcome_text(
            "# Result\n\nProof.\n\n"
            "ProvingConsole outcome: Known/Open Status.\n\n"
            "**ProvingConsole outcome: Known/Open Status**\n"
        )
        self.assertEqual(rendered.count("ProvingConsole outcome:"), 1)
        self.assertTrue(
            rendered.endswith("ProvingConsole outcome: Known/Open Status\n")
        )

    def test_conflicting_missing_and_malformed_labels_fail_closed(self) -> None:
        cases = (
            "# Result\n\nProvingConsole outcome: Solved.\n"
            "ProvingConsole outcome: Counterexample!\n",
            "# Result\n\nNo footer was supplied.\n",
            "# Result\n\nProvingConsole outcome: Probably Solved\n",
            "# Result\n\nProvingConsole outcome: Solved.\n"
            "ProvingConsole outcome: Unreviewed\n",
        )
        for text in cases:
            with self.subTest(text=text):
                rendered = canonicalize_outcome_text(text)
                self.assertEqual(rendered.count("ProvingConsole outcome:"), 1)
                self.assertTrue(
                    rendered.endswith(
                        "ProvingConsole outcome: Partial Progress\n"
                    )
                )

    def test_unrequested_text_is_untouched(self) -> None:
        original = "# Result\n\nProvingConsole outcome: Solved.\n"
        self.assertEqual(ensure_shared_outcome("ordinary task", original), original)


class RunnerOutcomeIntegrationTests(unittest.TestCase):
    def test_plain_preserves_legacy_marker_but_canonicalizes_punctuation(self) -> None:
        rendered = plain_runner._ensure_outcome_contract(
            "# Result\n\nProvingConsole: Counterexample.\n"
            "ProvingConsole: Counterexample\n"
        )
        self.assertEqual(rendered.count("ProvingConsole outcome:"), 1)
        self.assertNotIn("ProvingConsole:", rendered)
        self.assertTrue(
            rendered.endswith("ProvingConsole outcome: Counterexample\n")
        )

    def test_deepseek_and_metaharness_use_shared_fail_closed_policy(self) -> None:
        conflicting = (
            "# Candidate\n\nProvingConsole outcome: Solved.\n"
            "ProvingConsole outcome: Partial Progress\n"
        )
        for normalizer in (
            ensure_requested_outcome,
            metaharness_runner.ensure_requested_outcome,
        ):
            with self.subTest(normalizer=normalizer.__module__):
                rendered = normalizer(REQUEST, conflicting)
                self.assertEqual(rendered.count("ProvingConsole outcome:"), 1)
                self.assertTrue(
                    rendered.endswith(
                        "ProvingConsole outcome: Partial Progress\n"
                    )
                )

    def test_deepagents_normalizes_only_current_run_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            proof = workspace / "proof.md"
            proof.write_text(
                "# Result\n\nA complete proof. " * 8
                + "\n\nProvingConsole outcome: Solved.\n",
                encoding="utf-8",
            )
            stale_hash = hashlib.sha256(proof.read_bytes()).hexdigest()
            self.assertFalse(
                deepagents_runner._normalize_changed_proof_outcome(
                    workspace,
                    prompt=REQUEST,
                    initial_proof_sha256=stale_hash,
                )
            )
            self.assertTrue(
                deepagents_runner._normalize_changed_proof_outcome(
                    workspace,
                    prompt=REQUEST,
                    initial_proof_sha256=None,
                )
            )
            self.assertTrue(
                proof.read_text(encoding="utf-8").endswith(
                    "ProvingConsole outcome: Solved\n"
                )
            )

    def test_openhands_normalizes_root_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            proof = workspace / "proof.md"
            proof.write_text(
                "# Result\n\nA conservative result. " * 8
                + "\n\nProvingConsole outcome: Known/Open Status!\n",
                encoding="utf-8",
            )
            self.assertTrue(
                openhands_subscription_runner._normalize_requested_proof(
                    workspace, REQUEST
                )
            )
            self.assertTrue(
                proof.read_text(encoding="utf-8").endswith(
                    "ProvingConsole outcome: Known/Open Status\n"
                )
            )

    def test_openhands_does_not_reformat_a_stale_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td)
            proof = workspace / "proof.md"
            proof.write_text(
                "# Result\n\nA conservative result. " * 8
                + "\n\nProvingConsole outcome: Partial Progress.\n",
                encoding="utf-8",
            )
            initial = openhands_subscription_runner._artifact_fingerprint(proof)
            self.assertFalse(
                openhands_subscription_runner._normalize_requested_proof(
                    workspace,
                    REQUEST,
                    initial_fingerprint=initial,
                    require_changed=True,
                )
            )
            self.assertTrue(
                proof.read_text(encoding="utf-8").endswith(
                    "ProvingConsole outcome: Partial Progress.\n"
                )
            )


if __name__ == "__main__":
    unittest.main()
