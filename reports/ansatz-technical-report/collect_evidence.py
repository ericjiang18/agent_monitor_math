#!/usr/bin/env python3
"""Collect report evidence from source and public assets without running the app.

Usage: python3 reports/ansatz-technical-report/collect_evidence.py --output /tmp/evidence.json

Uses only the standard library. The sole write is a newly created output file;
private run data, credentials, application imports, model calls, and services
are outside this collector's scope. Lean verification is a separate command.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter, defaultdict
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
REGISTRY = "agent_monitor/engines_registry.py"
STACKS = "docs/public/research/informal-atlas.json"
BRIDGE = "docs/public/research/bipartite-atlas.json"
DEPENDENCIES = "docs/public/research/bipartite-dependencies.json"
CATALOGUE = "docs/public/open-problems.json"
EXAMPLE = "agent_monitor/examples/erdos-straus.json"
RELIABILITY = "docs/public/reliability/2026-09-09.json"
SOURCE_FILES = (
    REGISTRY, STACKS, BRIDGE, DEPENDENCIES, CATALOGUE, EXAMPLE, RELIABILITY,
    "agent_monitor/examples/ErdosStraus.lean",
    "agent_monitor/console_server.py", "agent_monitor/jobs.py",
    "agent_monitor/schema.py", "agent_monitor/cli_events.py",
    "agent_monitor/proof_graph.py", "agent_monitor/proof_bridge.py",
    "agent_monitor/lean_verify.py", "agent_monitor/proof_tools.py",
    "agent_monitor/research.py", "agent_monitor/projects.py",
    "agent_monitor/run_sharing.py", "agent_monitor/subprocess_env.py",
    "agent_monitor/library.py", "agent_monitor/web/projects.js",
    "agent_monitor/runners/ucla.py", "agent_monitor/runners/metaharness_runner.py",
    "agent_monitor/runners/kimi_runner.py", "agent_monitor/runners/plain_runner.py",
    "scripts/check_collaboration_reliability.py", "scripts/build_erdos_example.py",
    "scripts/import_informal_atlas.py", "scripts/import_bipartite_atlas.py",
    "scripts/import_open_problems.py", "scripts/problem_expansion.py",
    "docs/example-run.md", "docs/engineering.md", "docs/dag-sources.md",
    "docs/problem-sources.md", "README.md", "pyproject.toml",
    "docs/index.md", "OPERATIONS.md", "ATTRIBUTION.md",
    "tests/test_research_collaboration.py",
    "reports/ansatz-technical-report/collect_evidence.py",
)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def sha256(path: str) -> str:
    return hashlib.sha256((ROOT / path).read_bytes()).hexdigest()


def read_json(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def registry_evidence() -> dict:
    assignments = {}
    tree = ast.parse((ROOT / REGISTRY).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name, value = node.targets[0].id, node.value
        else:
            continue
        if name in ("BUILTIN_ENGINES", "CLI_ENGINES"):
            assignments[name] = ast.literal_eval(value)
    builtin, cli = assignments["BUILTIN_ENGINES"], assignments["CLI_ENGINES"]
    ids = [entry["id"] for entry in builtin] + list(cli)
    require(len(ids) == len(set(ids)), "Engine identifiers are not unique")
    require(all(key == entry["id"] for key, entry in cli.items()), "CLI engine keys differ from identifiers")
    return {"builtin_count": len(builtin), "cli_registry_count": len(cli),
            "total": len(ids), "builtin_ids": [entry["id"] for entry in builtin],
            "cli_registry_ids": list(cli),
            "interpretation": "Static registrations; availability and successful execution are not tested."}


def stacks_evidence() -> dict:
    data = read_json(STACKS)
    nodes, edges, stats = data["nodes"], data["edges"], data["stats"]
    by_id = {node["id"]: node for node in nodes}
    require(len(by_id) == len(nodes), "Duplicate Stacks node IDs")
    require(len({node["tag"] for node in nodes}) == len(nodes), "Duplicate Stacks tags")
    for node in nodes:
        require(node["id"] == "stacks:" + node["tag"], "Stacks ID/tag mismatch")
        require(node["layer"] == "informal" and node["memory_eligible"] is False,
                "Unexpected Stacks node layer or memory eligibility")
        require(hashlib.sha256(node["statement_latex"].encode()).hexdigest() == node["statement_sha256"],
                "Stacks statement hash mismatch: " + node["id"])
    pairs, adjacent = set(), defaultdict(list)
    for edge in edges:
        source, target = edge["source"], edge["target"]
        require(source in by_id and target in by_id, "Missing Stacks edge endpoint")
        require(by_id[source]["source_rank"] < by_id[target]["source_rank"], "Stacks edge violates source order")
        require((source, target) not in pairs, "Duplicate Stacks edge")
        pairs.add((source, target))
        adjacent[source].append(target)
        adjacent[target].append(source)
    unseen, sizes = set(by_id), []
    while unseen:
        pending, count = [unseen.pop()], 0
        while pending:
            current = pending.pop()
            count += 1
            for target in adjacent[current]:
                if target in unseen:
                    unseen.remove(target)
                    pending.append(target)
        sizes.append(count)
    bodies = Counter(node["statement_latex"] for node in nodes)
    measured = {
        "catalogue_nodes": len(nodes), "catalogue_edges": len(edges),
        "distinct_statement_bodies": len(bodies),
        "identical_statement_group_count": sum(count > 1 for count in bodies.values()),
        "kind_counts": dict(Counter(node["kind"] for node in nodes)),
        "chapter_count": len({node["chapter_id"] for node in nodes}),
        "weakly_connected_components": len(sizes), "isolated_nodes": sizes.count(1),
        "largest_source_component_nodes": max(sizes),
    }
    for name, value in measured.items():
        require(stats[name] == value, "Stacks declared count mismatch: " + name)
    return {**measured, "fields": len(data["clusters"]), "source": data["source"],
            "all_statement_hashes_match": True, "unique_edges": True,
            "all_edges_follow_strict_source_order": True,
            "interpretation": "Source tags and references; distinct bodies are distinct exact texts, not semantic equivalence classes."}


def bridge_evidence() -> dict:
    data, dependency_data = read_json(BRIDGE), read_json(DEPENDENCIES)
    nodes, edges = data["nodes"], data["edges"]
    by_id = {node["id"]: node for node in nodes}
    require(len(by_id) == len(nodes), "Duplicate bridge node IDs")
    for edge in edges:
        require(edge["source"] in by_id and edge["target"] in by_id, "Missing bridge endpoint")
        require(by_id[edge["source"]]["layer"] == "informal" and
                by_id[edge["target"]]["layer"] in ("formal", "unresolved"), "Invalid bridge partitions")
    for edge in dependency_data["edges"]:
        require(edge["source"] in by_id and edge["target"] in by_id, "Missing dependency endpoint")
        require(by_id[edge["source"]]["layer"] == by_id[edge["target"]]["layer"] == "informal",
                "Dependency has a non-informal endpoint")
        require(edge["type"] in ("USES", "PROVES"), "Unknown dependency relationship")
    layers = Counter(node["layer"] for node in nodes)
    statuses = Counter(edge["status"] for edge in edges)
    dependency_types = Counter(edge["type"] for edge in dependency_data["edges"])
    linked = {edge["source"] for edge in edges}
    resolved = {edge["source"] for edge in edges if edge["status"] in ("resolved", "resolved_mathlib")}
    measured = {
        "projects": len(data["projects"]), "nodes": len(nodes),
        "informal": layers["informal"], "formal": layers["formal"], "unresolved": layers["unresolved"],
        "links": len(edges), "status_counts": dict(statuses),
        "resolved_links": statuses["resolved"] + statuses["resolved_mathlib"],
        "unresolved_links": statuses["unresolved"] + statuses["ambiguous"],
        "linked_informal": len(linked), "resolved_informal": len(resolved),
        "unlinked_informal": layers["informal"] - len(linked),
        "dependencies": len(dependency_data["edges"]),
        "uses": dependency_types["USES"], "proves": dependency_types["PROVES"],
    }
    for name, value in measured.items():
        require(data["stats"][name] == value, "Bridge declared count mismatch: " + name)
    require(dependency_data["stats"]["total"] == measured["dependencies"], "Dependency total mismatch")
    return {**measured, "sources": data["sources"], "all_endpoints_valid": True,
            "interpretation": "Imported correspondences and reference resolution; no Lean compilation or semantic equivalence check."}


def catalogue_evidence() -> dict:
    data = read_json(CATALOGUE)
    records = data["problems"]
    counts = dict(Counter(record["source"] for record in records))
    kinds = dict(Counter(record["kind"] for record in records))
    require(len({record["id"] for record in records}) == len(records), "Duplicate catalogue IDs")
    require(len(records) == data["total"] and not data["compact_families"], "Catalogue total mismatch")
    require(counts == data["counts"] and kinds == data["kind_counts"], "Catalogue breakdown mismatch")
    return {"entries": len(records), "collections": len(counts), "counts": counts,
            "kind_counts": kinds, "snapshot_date": data["snapshot_date"],
            "interpretation": data["status_note"]}


def witness_evidence() -> dict:
    data = read_json(EXAMPLE)
    witnesses = data["witnesses"]
    require([row[0] for row in witnesses] == list(range(2, 501)), "Witness denominators do not cover 2 through 500 exactly")
    for n, x, y, z in witnesses:
        require(all(type(value) is int and value > 0 for value in (n, x, y, z)), "Invalid witness integer")
        require(sum((Fraction(1, value) for value in (x, y, z)), Fraction()) == Fraction(4, n),
                "Incorrect exact witness for n=" + str(n))
    require(data["lean_source"] == (ROOT / "agent_monitor/examples/ErdosStraus.lean").read_text(encoding="utf-8"),
            "Stored example Lean source differs from source file")
    return {"stored_witness_count": len(witnesses), "denominators": [2, 500],
            "all_positive_integer_witnesses": True, "all_exact_fraction_checks_passed": True,
            "recorded_lean_verification": data["verification"],
            "lean_invoked_by_collector": False, "interpretation": data["notice"]}


def reliability_evidence() -> dict:
    data = read_json(RELIABILITY)
    allowed = {"agent_monitor/projects.py", "scripts/check_collaboration_reliability.py"}
    require(set(data["source_sha256"]) == allowed, "Unexpected archived reliability source paths")
    matches = {path: sha256(path) == expected for path, expected in data["source_sha256"].items()}
    require(all(matches.values()), "Current reliability source differs from the archived measurement")
    return {"recorded_at": data["recorded_at"], "scope": data["scope"],
            "environment": data["environment"], "source_sha256": data["source_sha256"],
            "current_source_matches_archived": matches, "archived_report_passed": data["passed"],
            "trials": data["trials"], "trials_rerun_by_collector": False}


def collect() -> dict:
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                          capture_output=True, text=True).stdout.strip()
    return {
        "schema_version": 1, "collected_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Working-tree source and public artifacts; no private run data, model calls, or production traffic.",
        "git_base": base,
        "git_note": "HEAD identifies the base only; source hashes identify the inspected working-tree files.",
        "environment": {"python": platform.python_version(), "implementation": platform.python_implementation(),
                        "os": platform.system(), "release": platform.release(), "architecture": platform.machine()},
        "source_sha256": {path: sha256(path) for path in SOURCE_FILES},
        "registry": registry_evidence(), "stacks": stacks_evidence(), "bridge": bridge_evidence(),
        "catalogue": catalogue_evidence(), "erdos_straus": witness_evidence(),
        "reliability": reliability_evidence(), "all_collector_checks_passed": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New JSON output path; existing files are never overwritten")
    args = parser.parse_args()
    try:
        evidence = collect()
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(evidence, output, ensure_ascii=False, indent=2)
            output.write("\n")
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print("Evidence collection failed: " + str(exc), file=sys.stderr)
        return 1
    print("All source/public-asset checks passed; wrote " + str(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
