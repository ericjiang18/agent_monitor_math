from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import agent_config
from agent_monitor.skill_seeds import STARTER_SKILL_PACKAGES


ROOT = Path(__file__).parents[1]
SKILL = ROOT / "agent_monitor" / "starter_skills" / "research-proof-audit"
PREPARE = SKILL / "scripts" / "prepare_latex_bundle.py"
VALIDATE = SKILL / "scripts" / "validate_output.py"
RECONCILE = SKILL / "scripts" / "validate_reconciliation.py"


def valid_minor_output() -> dict:
    return {
        "verdict": "minor_revision",
        "summary": "A local endpoint argument needs repair.",
        "findings": [
            {
                "finding_id": "F1",
                "class": "minor_repairable_gap",
                "location": {"line_start": 10, "line_end": 12},
                "claim_under_review": "The endpoint case follows from the induction step.",
                "problem": "The final endpoint is not instantiated.",
                "centrality": {
                    "value": "noncentral",
                    "rationale": "The main method is unaffected.",
                    "dependency_path": [],
                },
                "repairability": {
                    "value": "repairable",
                    "effort": "local",
                    "rationale": "Add the missing endpoint calculation.",
                },
                "evidence": [
                    {
                        "evidence_id": "E1",
                        "kind": "paper_internal",
                        "reference": "proof.tex:10-12",
                        "summary": "The range stops one index early.",
                        "supports": ["problem", "repair_task"],
                    }
                ],
                "confidence": {
                    "level": "high",
                    "rationale": "Direct substitution reproduces the omission.",
                },
                "unresolved_checks": [],
                "repair_tasks": [
                    {
                        "repair_task_id": "FIX1",
                        "statement": "Check and insert the last endpoint.",
                        "kind": "case_analysis",
                        "status": "open",
                        "success_criteria": "The stated range is checked in full.",
                        "evidence_refs": ["E1"],
                    }
                ],
            }
        ],
        "coverage_notes": {
            "reviewed_regions": ["proof.tex:1-20"],
            "unreviewed_or_difficult_regions": [],
            "external_checks_not_performed": [],
        },
    }


class ResearchProofAuditSkillTests(unittest.TestCase):
    def run_validator(self, payload: dict) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "verifier-output.json"
            target.write_text(json.dumps(payload), encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(VALIDATE), str(target)],
                capture_output=True,
                text=True,
                check=False,
            )

    def test_skill_package_is_seedable_and_materializes_resources(self) -> None:
        self.assertEqual(STARTER_SKILL_PACKAGES["research-proof-audit"], SKILL)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "hermes"
            skills = home / "skills"
            skills.mkdir(parents=True)
            (skills / agent_config._SEED_MARKER).write_text("old seed\n", encoding="utf-8")
            (skills / ".seeded-v2-proof-tools").write_text("v2\n", encoding="utf-8")
            workspace = Path(tmp) / "workspace"
            with patch.object(agent_config, "HERMES_HOME", home):
                agent_config.ensure_seeded()
                preamble = agent_config.persona_preamble(workspace=workspace)
            installed = skills / "research-proof-audit"
            self.assertTrue((installed / "SKILL.md").is_file())
            self.assertTrue((installed / "scripts" / "validate_output.py").is_file())
            self.assertTrue((installed / "references" / "refuter-pass.md").is_file())
            self.assertIn("research-proof-audit", preamble)
            self.assertTrue(
                (workspace / "_agent" / "skills" / "research-proof-audit" / "SKILL.md").is_file()
            )

    def test_exact_legacy_package_upgrades_but_customized_package_is_preserved(self) -> None:
        for customized in (False, True):
            with self.subTest(customized=customized), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp) / "hermes"
                skills = home / "skills"
                profile = skills / "research-proof-audit"
                skills.mkdir(parents=True)
                shutil.copytree(SKILL, profile)
                (profile / "SKILL.md").write_text(
                    "user customized\n" if customized else "legacy built-in\n",
                    encoding="utf-8",
                )
                legacy_digest = agent_config._skill_package_digest(profile)
                (skills / agent_config._SEED_MARKER).write_text(
                    "old seed\n", encoding="utf-8"
                )
                with (
                    patch.object(agent_config, "HERMES_HOME", home),
                    patch.object(
                        agent_config,
                        "_LEGACY_RESEARCH_AUDIT_PACKAGE_DIGESTS",
                        set() if customized else {legacy_digest},
                    ),
                ):
                    agent_config.ensure_seeded()
                if customized:
                    self.assertEqual(
                        (profile / "SKILL.md").read_text(encoding="utf-8"),
                        "user customized\n",
                    )
                else:
                    self.assertEqual(
                        (profile / "SKILL.md").read_bytes(),
                        (SKILL / "SKILL.md").read_bytes(),
                    )
                    self.assertTrue(
                        (profile / "scripts" / "validate_reconciliation.py").is_file()
                    )
                self.assertTrue(
                    (skills / agent_config._SEED_V2_MARKER).is_file()
                )

    def test_latex_preparer_copies_static_dependency_closure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            (source / "sections").mkdir(parents=True)
            (source / "main.tex").write_text(
                "\\documentclass{article}\n"
                "\\begin{document}\n"
                "\\input{sections/braced}\n"
                "\\input sections/plain\n"
                "\\end{document}\n",
                encoding="utf-8",
            )
            (source / "sections" / "braced.tex").write_text("Braced.\n", encoding="utf-8")
            (source / "sections" / "plain.tex").write_text("Plain.\n", encoding="utf-8")
            output = root / "prepared"
            result = subprocess.run(
                [sys.executable, str(PREPARE), str(source), "--out", str(output)],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads((output / "source-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                {item["path"] for item in manifest["files"]},
                {"main.tex", "sections/braced.tex", "sections/plain.tex"},
            )
            self.assertEqual(manifest["total_bytes"], sum(item["bytes"] for item in manifest["files"]))

    def test_latex_preparer_rejects_escape_and_resource_overrun(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            source.mkdir()
            outside = root / "outside.tex"
            outside.write_text("outside\n", encoding="utf-8")
            (source / "escape.tex").symlink_to(outside)
            (source / "main.tex").write_text(
                "\\documentclass{article}\n\\begin{document}\n"
                "\\input{escape}\n\\end{document}\n",
                encoding="utf-8",
            )
            escaped = subprocess.run(
                [sys.executable, str(PREPARE), str(source), "--out", str(root / "escaped")],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(escaped.returncode, 2)
            self.assertIn("required TeX dependencies are unresolved", escaped.stderr)

            limited = subprocess.run(
                [
                    sys.executable,
                    str(PREPARE),
                    str(source),
                    "--out",
                    str(root / "limited"),
                    "--max-bytes",
                    "1",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(limited.returncode, 0)
            self.assertIn("--max-bytes=1", limited.stderr)

    def test_validator_accepts_valid_output(self) -> None:
        result = self.run_validator(valid_minor_output())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("valid verifier-output.v1", result.stdout)

    def test_validator_rejects_broken_cross_references_and_line_range(self) -> None:
        payload = valid_minor_output()
        payload["findings"][0]["location"]["line_end"] = 9
        payload["findings"][0]["repair_tasks"][0]["evidence_refs"] = ["E99"]
        result = self.run_validator(payload)
        self.assertEqual(result.returncode, 1)
        self.assertIn("line_end precedes line_start", result.stdout)
        self.assertIn("unknown evidence refs", result.stdout)

    def test_validator_requires_high_confidence_for_central_rejection(self) -> None:
        payload = valid_minor_output()
        finding = payload["findings"][0]
        payload["verdict"] = "reject"
        finding["class"] = "central_unsalvageable_error"
        finding["centrality"]["value"] = "central_claim"
        finding["repairability"] = {
            "value": "not_repairable",
            "effort": "none",
            "rationale": "A concrete legal counterexample was found.",
        }
        finding["confidence"]["level"] = "medium"
        result = self.run_validator(payload)
        self.assertEqual(result.returncode, 1)
        self.assertIn("requires high confidence", result.stdout)

    def test_reconciliation_validator_accepts_exact_final_candidate_and_rejects_stale_proof(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            audit = workspace / "audit" / "sample"
            audit.mkdir(parents=True)
            proof = workspace / "proof.md"
            proof.write_text("Audited candidate.\n", encoding="utf-8")
            proof_sha = hashlib.sha256(proof.read_bytes()).hexdigest()
            (audit / "final-candidate.md").write_bytes(proof.read_bytes())
            for name in ("global.json", "decomposed.json", "verifier-output.json"):
                (audit / name).write_text(
                    json.dumps(valid_minor_output()), encoding="utf-8"
                )
            (audit / "merge-map.json").write_text(
                json.dumps(
                    {
                        "schema_version": "ensemble-paper-audit-app.merge-map.v1",
                        "findings": [
                            {
                                "finding_id": "F1",
                                "sources": [
                                    {"pass": "global", "finding_id": "F1"},
                                    {"pass": "decomposed", "finding_id": "F1"},
                                ],
                            }
                        ],
                        "counts": {
                            "global_only": 0,
                            "decomposed_only": 0,
                            "both": 1,
                        },
                    }
                ),
                encoding="utf-8",
            )
            run = {
                "candidate_sha256": proof_sha,
                "repair_history": [],
                "reconciliation": {
                    "schema_version": "research-proof-audit.reconciliation.v1",
                    "status": "audited_unchanged",
                    "final_artifact": {"path": "proof.md", "sha256": proof_sha},
                    "audited_candidate": {
                        "path": "final-candidate.md",
                        "sha256": proof_sha,
                    },
                    "reruns": {
                        "global": "global.json",
                        "decomposed": "decomposed.json",
                        "merged": "verifier-output.json",
                    },
                    "superseded_findings": [],
                },
            }
            (audit / "run.json").write_text(json.dumps(run), encoding="utf-8")

            valid = subprocess.run(
                [sys.executable, str(RECONCILE), "audit/sample"],
                cwd=workspace,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(valid.returncode, 0, valid.stdout + valid.stderr)
            certificate = json.loads(valid.stdout)
            self.assertTrue(certificate["valid"])
            self.assertEqual(certificate["status"], "validated")
            self.assertEqual(len(certificate["certificate_sha256"]), 64)

            proof.write_text("Changed after audit.\n", encoding="utf-8")
            stale = subprocess.run(
                [sys.executable, str(RECONCILE), "audit/sample"],
                cwd=workspace,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(stale.returncode, 1, stale.stdout + stale.stderr)
            stale_result = json.loads(stale.stdout)
            self.assertEqual(stale_result["status"], "unreconciled")
            self.assertTrue(
                any("current proof" in error for error in stale_result["errors"])
            )

    def test_reconciliation_validator_requires_explicit_reruns_after_repair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            audit = workspace / "audit" / "sample"
            audit.mkdir(parents=True)
            proof = workspace / "proof.md"
            proof.write_text("Repaired candidate.\n", encoding="utf-8")
            proof_sha = hashlib.sha256(proof.read_bytes()).hexdigest()
            (audit / "final-candidate.md").write_bytes(proof.read_bytes())
            for name in ("global.json", "decomposed.json", "verifier-output.json"):
                (audit / name).write_text(
                    json.dumps(valid_minor_output()), encoding="utf-8"
                )
            (audit / "run.json").write_text(
                json.dumps(
                    {
                        "candidate_sha256": "0" * 64,
                        "repair_history": [{"finding_id": "F1"}],
                        "reconciliation": {
                            "schema_version": "research-proof-audit.reconciliation.v1",
                            "status": "repaired_revalidated",
                            "final_artifact": {"path": "proof.md", "sha256": proof_sha},
                            "audited_candidate": {
                                "path": "final-candidate.md",
                                "sha256": proof_sha,
                            },
                            "reruns": {
                                "global": "global.json",
                                "decomposed": "decomposed.json",
                                "merged": "verifier-output.json",
                            },
                            "superseded_findings": ["F1"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [sys.executable, str(RECONCILE), "audit/sample"],
                cwd=workspace,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        errors = json.loads(result.stdout)["errors"]
        self.assertIn(
            "repaired_revalidated requires an explicit global rerun file", errors
        )
        self.assertIn(
            "repaired_revalidated requires an explicit decomposed rerun file", errors
        )


if __name__ == "__main__":
    unittest.main()
