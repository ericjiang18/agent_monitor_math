"""Build the informal/formal atlas from the supplied knowledge graph.

Source FORMALIZES relationships connect the informal and formal layers. Their
visual direction is informal -> formal, reversing the export's formal -> informal
orientation; it does not imply a proof or a dependency. Source USES and PROVES
relationships are loaded separately for the optional informal dependency view,
preserving their original direction and labels. Full, unchanged node properties
live in lazy detail files. Index excerpts are navigation aids only.

Usage:
    python3 scripts/import_bipartite_atlas.py
    python3 scripts/import_bipartite_atlas.py --check
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import gzip
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import quote
import zipfile

ROOT = Path(__file__).resolve().parents[1]
STATUSES = {"resolved", "resolved_mathlib", "unresolved", "ambiguous"}
RESOLVED = {"resolved", "resolved_mathlib"}
LABEL_LAYERS = {
    "NaturalLanguageEntity": "informal",
    "LeanDeclaration": "formal",
    "MathlibDeclaration": "formal",
    "ExternalDeclaration": "unresolved",
}


def encode(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def excerpt(value: str, limit: int) -> str:
    """Compact source text for navigation; this is not a mathematical rewrite."""
    value = re.sub(r"(?<!\\)%[^\n]*", " ", value)
    value = re.sub(r"\\(?:begin|end|label|lean|uses|proves)\{[^{}]*\}", " ", value)
    value = re.sub(r"\\leanok\b", " ", value)
    value = re.sub(r"\\(?:textit|textbf|mathrm|mathbf|mathbb|mathcal|operatorname)\b", "", value)
    value = " ".join(value.replace("$", "").replace("{", "").replace("}", "").split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def read_sources(graph_path: Path, archives: list[Path]) -> tuple[dict, dict, list[dict], dict]:
    raw = graph_path.read_bytes()
    graph = json.loads(raw)
    sources = [{"file": graph_path.name, "sha256": digest(raw), "bytes": len(raw), "role": "canonical graph"}]
    manifest = {}
    archive_data = {}
    archive_hash = None
    for archive_path in archives:
        raw = archive_path.read_bytes()
        archive_digest = digest(raw)
        if archive_hash is not None and archive_digest != archive_hash:
            raise ValueError("The supplied KG archives differ; choose their provenance explicitly")
        archive_hash = archive_digest
        sources.append({
            "file": archive_path.name, "sha256": archive_digest, "bytes": len(raw),
            "role": "provenance archive" if not manifest else "identical provenance archive",
        })
        if not manifest:
            with zipfile.ZipFile(archive_path) as archive:
                manifest = json.loads(archive.read("kg/manifest.json"))
                archive_data = {
                    "informal": {node["id"]: node for line in archive.read("kg/nl_nodes.jsonl").decode().splitlines() if (node := json.loads(line))},
                    "formalizes": [json.loads(line) for line in archive.read("kg/formalizes.jsonl").decode().splitlines()],
                    "contains_formal_declarations": "kg/fl_nodes.jsonl" in archive.namelist(),
                }
    return graph, manifest, sources, archive_data


def pinned_urls(properties: dict) -> dict:
    repo = properties.get("repo", "")
    commit = properties.get("commit", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        return {}
    result = {"repository_url": f"https://github.com/{repo}"}
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        return result
    result["repository_url"] += f"/tree/{commit}"
    entry = properties.get("blueprint_entry", "")
    if entry and not entry.startswith("/") and ".." not in Path(entry).parts:
        result["blueprint_url"] = f"https://github.com/{repo}/blob/{commit}/{quote(entry, safe='/')}"
    return result


def build_dependencies(graph: dict, node_ids: dict[str, str], node_by_id: dict[str, dict]) -> dict:
    """Preserve every informal dependency record, including cycles and duplicates."""
    edges = []
    for relationship in graph["relationships"]:
        kind = relationship["type"]
        if kind not in {"USES", "PROVES"}:
            continue
        start, end = relationship["start"], relationship["end"]
        if start not in node_ids or end not in node_ids:
            raise ValueError(f"{kind} relationship has a missing endpoint")
        source, target = node_ids[start], node_ids[end]
        if any(node_by_id[endpoint]["layer"] != "informal" for endpoint in (source, target)):
            raise ValueError(f"{kind} endpoints must both be informal")
        edges.append({
            "source": source, "target": target, "type": kind,
            "label": relationship["properties"]["label"],
        })
    counts = Counter(edge["type"] for edge in edges)
    for kind in ("USES", "PROVES"):
        if graph.get("stats", {}).get("by_type", {}).get(kind, counts[kind]) != counts[kind]:
            raise ValueError(f"Source graph {kind} count does not match its stats")
    return {
        "schema_version": 1,
        "stats": {"uses": counts["USES"], "proves": counts["PROVES"], "total": len(edges), "formal": 0},
        "edges": edges,
    }


def build(graph: dict, manifest: dict, sources: list[dict], archive_data: dict | None = None) -> tuple[dict, dict[str, dict], dict]:
    archive_data = archive_data or {}
    original_nodes = graph["nodes"]
    original_by_id = {node["id"]: node for node in original_nodes}
    if len(original_by_id) != len(original_nodes):
        raise ValueError("Duplicate source node IDs")
    manifest_projects = {project["project"]: project for project in manifest.get("projects", [])}
    source_projects = sorted(
        (node for node in original_nodes if "Project" in node["labels"]),
        key=lambda node: node["properties"]["name"],
    )
    project_ids = {node["properties"]["name"]: f"p{index}" for index, node in enumerate(source_projects)}
    projects = []
    for source_project in source_projects:
        properties = source_project["properties"]
        key = properties["name"]
        provenance = manifest_projects.get(key, {})
        for field in ("repo", "commit"):
            if field in provenance and provenance[field] != properties[field]:
                raise ValueError(f"Archive provenance disagrees with graph for {key}: {field}")
        projects.append({
            "id": project_ids[key], "source_id": key,
            "title": properties.get("repo", key),
            **{field: properties.get(field, "") for field in (
                "repo", "commit", "blueprint_entry", "mathlib_rev", "lean_toolchain",
            )},
            "flatten_method": properties.get("flatten_method", ""),
            **pinned_urls(properties),
        })
    source_entities = sorted(
        (node for node in original_nodes if any(label in LABEL_LAYERS for label in node["labels"])),
        key=lambda node: node["id"],
    )
    node_ids = {node["id"]: f"n{index}" for index, node in enumerate(source_entities)}
    nodes = []
    details = defaultdict(dict)
    restored_informal_texts = 0
    for source_node in source_entities:
        properties = source_node["properties"]
        layer = next(LABEL_LAYERS[label] for label in source_node["labels"] if label in LABEL_LAYERS)
        project = project_ids.get(properties.get("project"), "shared")
        if layer == "informal":
            labels = properties.get("labels_tex", [])
            title = properties.get("title") or next(iter(labels), "") or next(iter(properties.get("lean_refs", [])), "")
            title = title or excerpt(properties.get("content", ""), 90) or properties.get("subkind", "Statement")
            summary = excerpt(properties.get("content", ""), 96)
            kind = properties.get("subkind", "statement")
        else:
            labels = []
            title = properties.get("name") or properties.get("fqname") or source_node["id"]
            summary = excerpt(properties.get("doc_string") or properties.get("type_full", ""), 96)
            kind = properties.get("kind") or ("reference" if layer == "unresolved" else "declaration")
        node = {
            "id": node_ids[source_node["id"]], "layer": layer, "project": project,
            "title": excerpt(title, 90), "kind": kind,
            "summary": summary, "labels": labels, "detail_key": project,
        }
        if layer != "informal":
            # Preserve the full declaration identifier for exact-name searches;
            # the shorter title is only a navigation/display excerpt.
            node["name"] = properties.get("fqname") or properties.get("name") or source_node["id"]
        if properties.get("module") and properties["module"] != "__unknown_module__":
            node["module"] = properties["module"]
        nodes.append(node)
        detail = {"source_id": source_node["id"], **properties}
        archived = archive_data.get("informal", {}).get(source_node["id"])
        if archived is not None and layer == "informal" and archived["text"] != properties["content"]:
            if len(properties["content"]) != 4000 or not archived["text"].startswith(properties["content"]):
                raise ValueError("Archived informal content differs from the graph beyond its 4,000-character truncation")
            detail["content_full"] = archived["text"]
            detail["content_full_source"] = "kg/nl_nodes.jsonl"
            restored_informal_texts += 1
        details[project][node["id"]] = detail
    edges = []
    seen_edges = set()
    node_by_id = {node["id"]: node for node in nodes}
    dependencies = build_dependencies(graph, node_ids, node_by_id)
    for relationship in graph["relationships"]:
        if relationship["type"] != "FORMALIZES":
            continue
        start, end = relationship["start"], relationship["end"]
        if start not in node_ids or end not in node_ids:
            raise ValueError("FORMALIZES relationship has a missing endpoint")
        properties = relationship["properties"]
        source, target = node_ids[end], node_ids[start]
        status = properties["resolution_status"]
        if status not in STATUSES:
            raise ValueError(f"Unexpected resolution status: {status}")
        if node_by_id[source]["layer"] != "informal":
            raise ValueError("FORMALIZES target in source graph must be informal")
        expected_layer = "formal" if status in RESOLVED else "unresolved"
        if node_by_id[target]["layer"] != expected_layer:
            raise ValueError("Formalization resolution status disagrees with target node layer")
        if (source, target) in seen_edges:
            raise ValueError("Duplicate FORMALIZES endpoint pair")
        seen_edges.add((source, target))
        edges.append({
            "source": source, "target": target, "status": status,
            "lean_ref": properties["lean_ref"], "candidate_count": properties["candidate_count"],
        })
    edges.sort(key=lambda edge: (edge["source"], edge["target"]))
    if archive_data:
        graph_references = Counter(
            (relationship["end"], relationship["properties"]["lean_ref"], relationship["properties"]["resolution_status"], relationship["properties"]["candidate_count"])
            for relationship in graph["relationships"] if relationship["type"] == "FORMALIZES"
        )
        archived_references = Counter(
            (reference["nl"], reference["lean_ref"], reference["status"], reference["candidate_count"])
            for reference in archive_data["formalizes"] if reference["project"] in project_ids
        )
        if graph_references != archived_references:
            raise ValueError("Curated archive FORMALIZES references disagree with the canonical graph")
        graph_informal = {node["id"] for node in source_entities if "NaturalLanguageEntity" in node["labels"]}
        archive_informal = {node["id"] for node in archive_data["informal"].values() if node["project"] in project_ids}
        if graph_informal != archive_informal:
            raise ValueError("Curated archive informal IDs disagree with the canonical graph")
    layer_counts = Counter(node["layer"] for node in nodes)
    statuses = Counter(edge["status"] for edge in edges)
    linked_informal = {edge["source"] for edge in edges}
    resolved_informal = {edge["source"] for edge in edges if edge["status"] in RESOLVED}
    project_nodes = defaultdict(Counter)
    project_links = defaultdict(Counter)
    for node in nodes:
        project_nodes[node["project"]][node["layer"]] += 1
    for edge in edges:
        project_links[node_by_id[edge["source"]]["project"]][edge["status"]] += 1
    for project in projects:
        project["informal"] = project_nodes[project["id"]]["informal"]
        project["formal"] = project_nodes[project["id"]]["formal"]
        project["links"] = sum(project_links[project["id"]].values())
        project["resolved_links"] = sum(project_links[project["id"]][status] for status in RESOLVED)
    stats = {
        "projects": len(projects), **dict(layer_counts), "nodes": len(nodes), "links": len(edges),
        "resolved_links": sum(statuses[status] for status in RESOLVED),
        "unresolved_links": sum(statuses[status] for status in STATUSES - RESOLVED),
        "linked_informal": len(linked_informal), "resolved_informal": len(resolved_informal),
        "unlinked_informal": layer_counts["informal"] - len(linked_informal),
        "dependencies": dependencies["stats"]["total"],
        "uses": dependencies["stats"]["uses"], "proves": dependencies["stats"]["proves"],
        "formal_dependencies": dependencies["stats"]["formal"],
        "status_counts": dict(sorted(statuses.items())),
        "source_node_labels": dict(sorted(Counter(label for node in original_nodes for label in node["labels"] if label in LABEL_LAYERS or label == "Project").items())),
    }
    # Validate the export's totals rather than trusting its summary blindly.
    if graph.get("stats", {}).get("nodes", len(original_nodes)) != len(original_nodes):
        raise ValueError("Source graph node count does not match its stats")
    if graph.get("stats", {}).get("by_type", {}).get("FORMALIZES", len(edges)) != len(edges):
        raise ValueError("Source graph FORMALIZES count does not match its stats")
    expected = manifest.get("summary", {}).get("usable_totals", {})
    if "formalizes_total" in expected and expected["formalizes_total"] != len(edges):
        raise ValueError("Archive curated FORMALIZES count does not match graph")
    return {
        "schema_version": 1,
        "description": "Source-declared informal-to-formal correspondences, not proof certification.",
        "edge_direction": "informal_to_formal",
        "details_path": "/research/bipartite-details/",
        "dependencies_path": "/research/bipartite-dependencies.json",
        "sources": sources,
        "source_scope": {
            "graph_projects": len(projects),
            "archive_projects": manifest.get("summary", {}).get("projects"),
            "archive_formal_declarations_included": archive_data.get("contains_formal_declarations", False),
            "restored_informal_texts": restored_informal_texts,
            "informal_dependencies_available": True,
            "informal_dependencies_display": "optional",
            "dependency_edge_direction": "source_start_to_end",
            "formal_dependencies_available": False,
            "original_edge_direction": "formal_to_informal",
            "unresolved_policy": "External placeholders retain the supplied unresolved or ambiguous link status.",
        },
        "stats": stats, "projects": projects, "nodes": nodes, "edges": edges,
    }, dict(details), dependencies


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", type=Path, default=ROOT / "graph.json")
    parser.add_argument("--archive", type=Path, action="append", help="Optional manifest archives; defaults to both supplied ZIP files")
    parser.add_argument("--output", type=Path, default=ROOT / "docs/public/research/bipartite-atlas.json")
    parser.add_argument("--summary", type=Path, default=ROOT / "docs/.vitepress/theme/bipartiteSummary.json")
    parser.add_argument("--check", action="store_true", help="Rebuild in memory and verify all generated files exactly")
    args = parser.parse_args()
    archives = args.archive if args.archive is not None else [ROOT / "kg.zip", ROOT / "kg (1).zip"]
    graph, manifest, sources, archive_data = read_sources(args.graph, archives)
    index, details, dependencies = build(graph, manifest, sources, archive_data)
    index_bytes = encode(index)
    dependency_bytes = encode(dependencies)
    dependency_path = args.output.parent / "bipartite-dependencies.json"
    artifacts = {
        args.output: index_bytes,
        args.output.with_suffix(args.output.suffix + ".gz"): gzip.compress(index_bytes, mtime=0),
        args.summary: encode({"schema_version": 1, "stats": index["stats"]}),
        dependency_path: dependency_bytes,
        dependency_path.with_suffix(".json.gz"): gzip.compress(dependency_bytes, mtime=0),
        **{args.output.parent / "bipartite-details" / f"{key}.json": encode(value) for key, value in details.items()},
    }
    if args.check:
        mismatches = [str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path) for path, data in artifacts.items() if not path.exists() or path.read_bytes() != data]
        detail_directory = args.output.parent / "bipartite-details"
        mismatches.extend(str(path) + " (unexpected detail file)" for path in detail_directory.glob("*.json") if path not in artifacts)
        if mismatches:
            raise SystemExit("Generated data is missing or stale: " + ", ".join(mismatches))
        print(f"Verified {len(artifacts)} reproducible artifacts and every source correspondence and dependency.")
    else:
        for path, data in artifacts.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    dependency_records = Counter(
        (edge["source"], edge["target"], edge["type"], edge["label"])
        for edge in dependencies["edges"]
    )
    print(json.dumps({
        "stats": index["stats"], "index_bytes": len(index_bytes),
        "index_gzip_bytes": len(artifacts[args.output.with_suffix(args.output.suffix + '.gz')]),
        "dependency_bytes": len(dependency_bytes),
        "dependency_gzip_bytes": len(artifacts[dependency_path.with_suffix('.json.gz')]),
        "dependency_duplicates_preserved": sum(count - 1 for count in dependency_records.values()),
        "dependency_self_references_preserved": sum(edge["source"] == edge["target"] for edge in dependencies["edges"]),
    }, indent=2))


if __name__ == "__main__":
    main()
