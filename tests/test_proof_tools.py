from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

from agent_monitor import agent_config, library
from agent_monitor.proof_tools import (
    ToolInputError,
    audit_output_validator,
    bounded_counterexample_search,
    evaluate_exact,
    exact_math_certificate,
    statement_fidelity_audit,
)


class ExactProofToolTests(unittest.TestCase):
    def test_audit_output_validator_accepts_contract_and_rejects_shortcut_schema(self) -> None:
        valid = {
            "verdict": "accept",
            "summary": "The declared scope was checked.",
            "findings": [],
            "coverage_notes": {
                "reviewed_regions": ["complete candidate"],
                "unreviewed_or_difficult_regions": [],
                "external_checks_not_performed": ["none requested"],
            },
        }
        accepted = audit_output_validator({"document": valid})
        self.assertTrue(accepted["valid"])
        self.assertEqual(accepted["verdict"], "accept")
        self.assertEqual(len(accepted["certificate_sha256"]), 64)

        raw_accepted = audit_output_validator(valid)
        self.assertTrue(raw_accepted["valid"])
        self.assertEqual(raw_accepted["verdict"], "accept")

        shortcut = {
            "schema_version": "verifier-output.v1",
            "verdict": "accept_partial_progress",
            "findings": [],
            "coverage_notes": "checked",
        }
        rejected = audit_output_validator({"document": shortcut})
        self.assertFalse(rejected["valid"])
        self.assertEqual(rejected["phase"], "schema")
        self.assertTrue(
            any("summary" in problem for problem in rejected["errors"])
        )

    def test_audit_output_validator_cli_accepts_raw_stdin_and_workspace_at_file(self) -> None:
        valid = {
            "verdict": "accept",
            "summary": "The declared scope was checked.",
            "findings": [],
            "coverage_notes": {
                "reviewed_regions": ["complete candidate"],
                "unreviewed_or_difficult_regions": [],
                "external_checks_not_performed": ["none requested"],
            },
        }
        command = [
            sys.executable,
            str(Path(__file__).parents[1] / "agent_monitor" / "proof_tools.py"),
            "audit-output-validator",
        ]
        stdin_result = subprocess.run(
            command,
            input=json.dumps(valid),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(stdin_result.returncode, 0, stdin_result.stdout)
        self.assertTrue(json.loads(stdin_result.stdout)["valid"])

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            nested = workspace / "audit" / "run-1"
            nested.mkdir(parents=True)
            verifier = nested / "verifier-output.json"
            verifier.write_text(json.dumps(valid), encoding="utf-8")

            nested_result = subprocess.run(
                [*command, "@audit/run-1/verifier-output.json"],
                input="",
                capture_output=True,
                text=True,
                cwd=workspace,
                check=False,
            )
            self.assertEqual(nested_result.returncode, 0, nested_result.stdout)
            self.assertTrue(json.loads(nested_result.stdout)["valid"])

            outside = root / "outside.json"
            outside.write_text(json.dumps(valid), encoding="utf-8")
            escaped_result = subprocess.run(
                [*command, "@../outside.json"],
                input="",
                capture_output=True,
                text=True,
                cwd=workspace,
                check=False,
            )
            self.assertEqual(escaped_result.returncode, 2)
            self.assertIn("workspace-relative", json.loads(escaped_result.stdout)["error"])

            link = workspace / "linked.json"
            link.symlink_to(outside)
            linked_result = subprocess.run(
                [*command, "@linked.json"],
                input="",
                capture_output=True,
                text=True,
                cwd=workspace,
                check=False,
            )
            self.assertEqual(linked_result.returncode, 2)
            self.assertIn("symbolic links", json.loads(linked_result.stdout)["error"])

    def test_exact_certificate_uses_integers_and_rationals(self) -> None:
        result = exact_math_certificate(
            {
                "expressions": [
                    "4455*4480 == 19958400",
                    "1/3 + 1/6",
                    "binomial(12, 4) == 495",
                    "is_prime(2305843009213693951)",
                ]
            }
        )
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["results"][1]["value"]["exact"], "1/2")
        self.assertEqual(len(result["certificate_sha256"]), 64)

    def test_exact_evaluator_rejects_python_escape_surfaces(self) -> None:
        for expression in (
            "__import__('os').system('id')",
            "(1).__class__",
            "[x for x in range(3)]",
            "open('proof.md')",
            "1.25",
        ):
            with self.subTest(expression=expression):
                with self.assertRaises(ToolInputError):
                    evaluate_exact(expression)

    def test_fractional_arithmetic_is_exact(self) -> None:
        self.assertEqual(evaluate_exact("2 / 3 + 5 / 6"), Fraction(3, 2))

    def test_counterexample_search_records_complete_scope(self) -> None:
        result = bounded_counterexample_search(
            {
                "predicate": "divides(n, binomial(n, 2))",
                "ranges": [{"name": "n", "start": 2, "end": 12}],
            }
        )
        self.assertEqual(result["status"], "counterexample_found")
        self.assertTrue(result["scope_complete"])
        self.assertEqual(result["checked_cases"], 11)
        self.assertEqual(result["counterexamples"][0]["variables"], {"n": 2})
        self.assertEqual(result["counterexample_count"], 6)

    def test_counterexample_search_never_extrapolates_scope(self) -> None:
        result = bounded_counterexample_search(
            {
                "predicate": "gcd(n, n+1) == 1",
                "ranges": [{"name": "n", "start": 1, "end": 200}],
            }
        )
        self.assertEqual(result["status"], "no_counterexample_in_scope")
        self.assertIn("declared finite", result["limitations"])

    def test_counterexample_search_enforces_case_budget(self) -> None:
        with self.assertRaisesRegex(ToolInputError, "maximum"):
            bounded_counterexample_search(
                {
                    "predicate": "x == x",
                    "ranges": [
                        {"name": "x", "start": 1, "end": 1000},
                        {"name": "y", "start": 1, "end": 1000},
                    ],
                }
            )

    def test_fidelity_preflight_flags_domain_and_admission(self) -> None:
        result = statement_fidelity_audit(
            {
                "original": "For every positive natural number n, there exists an integer k with k > n.",
                "candidate": "For every integer n, choose k = n + 1.",
                "formal": "theorem main (n : Int) : exists k : Int, k > n := by sorry",
            }
        )
        self.assertEqual(result["status"], "review_required")
        kinds = {item["kind"] for item in result["issues"]}
        self.assertIn("missing-domains-and-qualifiers", kinds)
        self.assertIn("unfinished-formalization", kinds)
        self.assertIn("not a semantic proof", result["limitations"])


class TrustedLibraryToolTests(unittest.TestCase):
    def test_broker_runs_only_unmodified_starters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library_dir = root / "library"
            with (
                patch.object(library, "LIBRARY_DIR", library_dir),
                patch.object(library, "LIBRARY_FILE", library_dir / "library.json"),
                patch.object(library, "SEED_MARKER", library_dir / ".seeded"),
                patch.object(agent_config, "HERMES_HOME", root / "hermes"),
            ):
                result = library.get_library()
                exact = next(item for item in result["items"] if item["name"] == "exact-math-certificate")
                self.assertTrue(exact["runnable"])
                run = library.run_trusted_tool(
                    exact["id"], {"expressions": ["16*5**2 + 112*5 + 120 == 1080"]}
                )
                self.assertEqual(run["status"], "passed")
                self.assertTrue(run["broker"]["trusted"])

                library.upsert_item({**exact, "content": "#!/bin/sh\necho tampered\n"})
                edited = next(
                    item for item in library.get_library()["items"] if item["id"] == exact["id"]
                )
                self.assertFalse(edited["runnable"])
                with self.assertRaisesRegex(ValueError, "unmodified trusted"):
                    library.run_trusted_tool(exact["id"], {"expressions": ["1 == 1"]})

    def test_legacy_wrapper_migrates_and_supports_help_and_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library_dir = root / "library"
            workspace = root / "workspace"
            starter = dict(library._EXACT_MATH_CERTIFICATE_TOOL)
            current_body = starter["content"].split("\n", 2)[2].rsplit("\n", 1)[0]
            starter["content"] = starter["content"].replace(
                current_body,
                'exec "$AGENT_MONITOR_PYTHON" -m agent_monitor.proof_tools exact-math-certificate "$1"',
                1,
            )
            library_dir.mkdir(parents=True)
            (library_dir / "library.json").write_text(
                json.dumps({"items": [starter], "settings": {"auto_memory": True}}),
                encoding="utf-8",
            )
            with (
                patch.object(library, "LIBRARY_DIR", library_dir),
                patch.object(library, "LIBRARY_FILE", library_dir / "library.json"),
                patch.object(library, "SEED_MARKER", library_dir / ".seeded"),
                patch.object(agent_config, "HERMES_HOME", root / "hermes"),
            ):
                data = library.get_library()
                exact = next(item for item in data["items"] if item["id"] == starter["id"])
                self.assertTrue(exact["runnable"])
                library.materialize(workspace)

            script = workspace / "_library" / "tools" / "exact-math-certificate.sh"
            env = {**os.environ, "AGENT_MONITOR_PYTHON": sys.executable}
            help_result = subprocess.run(
                ["bash", str(script), "--help"], capture_output=True, text=True, env=env, check=False
            )
            self.assertEqual(help_result.returncode, 0)
            self.assertIn("Usage:", help_result.stdout)
            stdin_result = subprocess.run(
                ["bash", str(script)],
                input=json.dumps({"expressions": ["2 + 2 == 4"]}),
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertEqual(stdin_result.returncode, 0)
            self.assertEqual(json.loads(stdin_result.stdout)["status"], "passed")

    def test_materialized_trusted_tool_binds_server_python(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            library_dir = root / "library"
            workspace = root / "workspace"
            with (
                patch.object(library, "LIBRARY_DIR", library_dir),
                patch.object(library, "LIBRARY_FILE", library_dir / "library.json"),
                patch.object(library, "SEED_MARKER", library_dir / ".seeded"),
                patch.object(agent_config, "HERMES_HOME", root / "hermes"),
            ):
                library.get_library()
                library.materialize(workspace)

            script = workspace / "_library" / "tools" / "exact-math-certificate.sh"
            content = script.read_text(encoding="utf-8")
            self.assertNotIn("AGENT_MONITOR_PYTHON", content)
            self.assertIn(str(Path(sys.executable)), content)
            self.assertIn(
                str(Path(library.__file__).with_name("proof_tools.py").resolve()),
                content,
            )
            env = {key: value for key, value in os.environ.items() if key != "AGENT_MONITOR_PYTHON"}
            result = subprocess.run(
                ["bash", str(script), json.dumps({"expressions": ["3*7 == 21"]})],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["status"], "passed")

            validator = workspace / "_library" / "tools" / "audit-output-validator.sh"
            verifier = {
                "verdict": "accept",
                "summary": "The declared scope was checked.",
                "findings": [],
                "coverage_notes": {
                    "reviewed_regions": ["complete candidate"],
                    "unreviewed_or_difficult_regions": [],
                    "external_checks_not_performed": ["none requested"],
                },
            }
            validated = subprocess.run(
                ["bash", str(validator)],
                input=json.dumps(verifier),
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            self.assertEqual(validated.returncode, 0, validated.stderr)
            self.assertTrue(json.loads(validated.stdout)["valid"])

    def test_existing_profile_receives_only_new_seed_packages(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "hermes"
            skills = home / "skills"
            existing = skills / "proof-strategies"
            existing.mkdir(parents=True)
            (existing / "SKILL.md").write_text("user-edited", encoding="utf-8")
            (skills / agent_config._SEED_MARKER).write_text("old seed\n", encoding="utf-8")
            with patch.object(agent_config, "HERMES_HOME", home):
                agent_config.ensure_seeded()
            self.assertEqual((existing / "SKILL.md").read_text(encoding="utf-8"), "user-edited")
            for name in (
                "open-problem-triage",
                "computational-certificates",
                "formalization-contract",
                "reviewer-reconciliation",
            ):
                self.assertTrue((skills / name / "SKILL.md").is_file())
                self.assertTrue((skills / name / "agents" / "openai.yaml").is_file())
            self.assertTrue((skills / agent_config._SEED_V2_MARKER).is_file())

    def test_console_contains_trusted_tool_lab(self) -> None:
        console = (
            Path(__file__).parents[1] / "agent_monitor" / "web" / "console.html"
        ).read_text(encoding="utf-8")
        self.assertIn("lib-tool-runner", console)
        self.assertIn("Run tool", console)
        self.assertIn("/api/library/tools/", console)


if __name__ == "__main__":
    unittest.main()
