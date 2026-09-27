"""Package the compiled report and identify its dated evidence without reruns."""
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile

HERE = Path(__file__).resolve().parent


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition, message):
    if not condition:
        raise SystemExit(message)


def main():
    original = json.loads((HERE / "evidence.json").read_text())
    old = HERE / "revisions/2026-09-15-original"
    original_checks = {}
    for name, expected in original["report_artifact_sha256"].items():
        path = old / name if (old / name).exists() else HERE / name
        original_checks[str(path.relative_to(HERE))] = digest(path) == expected
    require(all(original_checks.values()), "Original evidence/document mismatch")

    experiment = HERE / "experiments/2026-09-16-ten-runs"
    spec = importlib.util.spec_from_file_location("report_summary", experiment / "summarize.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    computed, rows = module.collect(experiment)
    recorded = json.loads((experiment / "summary.json").read_text())
    require(computed == recorded, "Recorded ten-run summary differs from raw evidence")
    require(len(rows) == 20, "Expected twenty collaboration trials")

    example = HERE / "examples/sqrt-eight"
    fresh = json.loads((example / "fresh-check.json").read_text())
    provenance = json.loads((example / "provenance.json").read_text())
    source_hash = digest(example / "Proof.lean")
    require(source_hash == fresh["source_sha256"] == provenance["copied_source_sha256"]
            == provenance["historical_verification"]["source_sha256"],
            "Solved-example source differs from recorded checked source")
    require(fresh["all_passed"] and all(c["exit_code"] == 0 for c in fresh["checks"]),
            "Solved-example check did not pass")
    for check in fresh["checks"]:
        require(digest(example / check["command"][-1]) == check["source_sha256"],
                "Solved-example audit source changed")
    require((HERE / "main.pdf").read_bytes().startswith(b"%PDF-"), "Compile main.pdf first")

    upload_note = (
        "Compile main.tex with pdfLaTeX in Overleaf.\n"
        "The entire document is self-contained; no images, .sty, or .bib files are needed.\n"
        "Title/authors: Math Framework LLM Assistant For Research; Richard C, Eric J.\n"
        "Revised 20 September 2026 to follow the supplied mentor's template.\n"
        "Section 6.4 adds a saved solved sqrt(8) irrationality example with Lean evidence.\n"
        "The original 15 September checks and the 16 September measurements retain their dates.\n"
        "Supporting evidence is in the companion repository report directory.\n"
    )
    with zipfile.ZipFile(HERE / "ansatz-overleaf.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(HERE / "main.tex", "main.tex")
        archive.writestr("README.txt", upload_note)

    files = [HERE / n for n in (
        "main.tex", "main.pdf", "main.log", "report.md", "build_latex.py",
        "package_report.py", "README.md", "evidence.json", "ansatz-overleaf.zip",
        "lean-check.txt", "collaboration-tests.xml",
    )]
    files += sorted(p for p in example.iterdir() if p.is_file())
    files += [experiment / n for n in ("summary.json", "experiment.json", "summarize.py")]
    files += sorted(experiment.glob("run-*.json"))
    if (HERE / "document-check.json").exists():
        files.append(HERE / "document-check.json")
    manifest = {
        "schema_version": 1,
        "packaged_at_utc": datetime.now(timezone.utc).isoformat(),
        "revision_date": "2026-09-20",
        "template_url": "https://www.overleaf.com/read/wszynkpnmpks#fe3644",
        "scope": "Layout revision, added solved example, and incorporation of recorded ten-run results.",
        "original_source_inspection_date": "2026-09-15",
        "original_evidence_unchanged": digest(HERE / "evidence.json") == digest(old / "evidence.json"),
        "original_document_hash_checks": original_checks,
        "repeated_measurement_date": "2026-09-16",
        "saved_summary_recomputed_and_matched": True,
        "collaboration_trials": len(rows),
        "solved_example": {
            "formal_statement": provenance["formal_statement"],
            "source_sha256": source_hash,
            "checked_at_utc": fresh["checked_at_utc"],
            "check_file": "examples/sqrt-eight/fresh-check.json",
        },
        "packaging_note": "Packaging revalidates retained evidence; it does not compile LaTeX, rerun Lean, rerun pytest, or execute collaboration workloads.",
        "artifact_sha256": {str(p.relative_to(HERE)): digest(p) for p in files},
    }
    (HERE / "revision-evidence.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print("Packaged ansatz-overleaf.zip; wrote revision-evidence.json")


if __name__ == "__main__":
    main()
