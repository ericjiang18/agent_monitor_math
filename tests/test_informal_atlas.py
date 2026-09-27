"""Integrity contracts for the complete, source-backed informal atlas."""
from collections import Counter, defaultdict, deque
import importlib.util
import hashlib
import json
import math
import os
from pathlib import Path
import re

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("import_informal_atlas", ROOT / "scripts/import_informal_atlas.py")
importer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(importer)


@pytest.fixture(scope="module")
def atlas():
    return json.loads((ROOT / "docs/public/research/informal-atlas.json").read_text())


def test_public_catalogue_is_real_distinct_informal_statements(atlas):
    nodes = atlas["nodes"]
    assert len(nodes) == atlas["stats"]["catalogue_nodes"] == 14_754
    assert len({node["id"] for node in nodes}) == len(nodes)
    assert len({node["tag"] for node in nodes}) == len(nodes)
    assert len({node["source_url"] for node in nodes}) == len(nodes)
    assert all(node["id"] == f"stacks:{node['tag']}" for node in nodes)
    assert all(node["source_url"] == f"https://stacks.math.columbia.edu/tag/{node['tag']}" for node in nodes)
    assert all(node["layer"] == "informal" and node["memory_eligible"] is False for node in nodes)
    assert all(node["summary"] and node["statement_latex"] and node["source_line"] > 0 for node in nodes)
    assert all(node["kind"] in importer.KINDS for node in nodes)
    assert all(math.isfinite(node[axis]) for node in nodes for axis in ("x", "y"))
    assert atlas["stats"]["kind_counts"] == dict(Counter(node["kind"] for node in nodes))
    assert atlas["stats"]["chapter_count"] == len({node["chapter_id"] for node in nodes})
    assert all(node["source_end_line"] >= node["source_line"] for node in nodes)
    assert all(node["statement_sha256"] == hashlib.sha256(node["statement_latex"].encode()).hexdigest() for node in nodes)
    assert Counter(node["chapter_id"] for node in nodes)["examples"] == 94
    assert Counter(node["chapter_id"] for node in nodes)["exercises"] == 39
    assert not any(node["chapter_id"] in ("obsolete", "coding") for node in nodes)


def test_edges_have_existing_endpoints_and_are_acyclic_with_honest_component_counts(atlas):
    nodes = {node["id"]: node for node in atlas["nodes"]}
    outgoing, undirected, indegree = defaultdict(list), defaultdict(list), Counter()
    pairs = set()
    for edge in atlas["edges"]:
        source, target = edge["source"], edge["target"]
        assert source in nodes and target in nodes and source != target
        assert (source, target) not in pairs
        pairs.add((source, target))
        assert edge["kind"] == "reference" and edge["context"] in ("statement", "proof")
        assert nodes[source]["source_rank"] < nodes[target]["source_rank"]
        outgoing[source].append(target)
        undirected[source].append(target)
        undirected[target].append(source)
        indegree[target] += 1
    queue = deque(node_id for node_id in nodes if indegree[node_id] == 0)
    visited = 0
    while queue:
        node_id = queue.popleft()
        visited += 1
        for target in outgoing[node_id]:
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    assert visited == len(nodes), "Source-reference graph contains a cycle"
    unseen, sizes = set(nodes), []
    while unseen:
        seen, pending = set(), [next(iter(unseen))]
        while pending:
            node_id = pending.pop()
            if node_id in seen:
                continue
            seen.add(node_id)
            pending.extend(target for target in undirected[node_id] if target not in seen)
        unseen.difference_update(seen)
        sizes.append(len(seen))
    assert len(sizes) == atlas["stats"]["weakly_connected_components"]
    assert max(sizes) == atlas["stats"]["largest_source_component_nodes"]
    assert sizes.count(1) == atlas["stats"]["isolated_nodes"] > 0
    assert {str(size): count for size, count in Counter(sizes).items()} == atlas["stats"]["component_size_distribution"]
    assert len(pairs) == atlas["stats"]["catalogue_edges"]


def test_clusters_and_backend_index_match_public_catalogue(atlas):
    clusters = {cluster["id"]: cluster for cluster in atlas["clusters"]}
    counts = Counter(node["cluster"] for node in atlas["nodes"])
    assert len(clusters) == 24
    assert set(counts) == set(clusters)
    assert all(cluster["count"] == counts[cluster_id] for cluster_id, cluster in clusters.items())
    backend = json.loads((ROOT / "agent_monitor/data/informal-atlas.json").read_text())
    assert backend["source"] == atlas["source"]
    assert [node["id"] for node in backend["nodes"]] == [node["id"] for node in atlas["nodes"]]
    for public, index in zip(atlas["nodes"], backend["nodes"], strict=True):
        assert all(public[key] == value for key, value in index.items())


def test_source_license_and_omissions_remain_explicit(atlas):
    source_dir = ROOT / "docs/public/research/stacks-source"
    assert "GNU Free Documentation License" in (source_dir / "COPYING.txt").read_text()
    assert "Johan de Jong" in (source_dir / "introduction.tex.txt").read_text()
    assert len((source_dir / "CONTRIBUTORS.txt").read_text().splitlines()) > 500
    assert atlas["source"]["revision"] == importer.SOURCE_COMMIT
    assert atlas["source"]["archive_sha256"] == importer.SOURCE_SHA256
    assert atlas["source"]["license"] == "GFDL-1.2-or-later"
    assert "not a complete" in atlas["selection"]["proof_claim"]
    assert atlas["stats"]["forward_references_omitted"] > 0
    assert atlas["stats"]["nodes_outside_selection"] == 0
    assert atlas["stats"]["references_outside_selection"] == 0
    assert atlas["stats"]["untagged_statements_omitted"] == len(atlas["source_audit"]["untagged_statements"]) == 6
    exclusions = {entry["chapter_id"]: entry for entry in atlas["source_audit"]["excluded_chapters"]}
    assert exclusions["coding"]["theorem_like_environments"] == 1
    assert exclusions["obsolete"]["theorem_like_environments"] == 98


def test_importer_preserves_statement_context_and_only_actual_references():
    files = {
        "tags/tags": "0001,algebra-lemma-first\n0002,algebra-definition-second\n0003,algebra-lemma-third\n",
        "chapters.tex": r"\item \hyperref[algebra-section-phantom]{Commutative Algebra}",
        "algebra.tex": r"""\section{Test section}
% \begin{lemma}\label{lemma-commented}this must not become a node\end{lemma}
\begin{lemma}[First result]\label{lemma-first}
Let $R$ be a ring. Compare Definition \ref{definition-second}.
\begin{enumerate}\item\label{item-first}An assertion.\end{enumerate}
\end{lemma}
\begin{proof}A direct proof.\end{proof}
\begin{definition}\label{definition-second}A definition using Lemma \ref{lemma-first}.\end{definition}
\begin{lemma}\label{lemma-third}A result.\end{lemma}
\begin{proof}Apply \ref{item-first} and \ref{definition-second}; see \ref{section-other}.\end{proof}
""",
    }
    nodes, owners, tags = importer.source_nodes(files)
    assert [node["tag"] for node in nodes] == ["0001", "0002", "0003"]
    assert nodes[0]["source_line"] == 3
    assert "A direct proof" not in nodes[0]["statement_latex"]
    assert "First result" in nodes[0]["title"]
    assert "Tag 0002" in nodes[0]["summary"]
    edges, stats = importer.reference_edges(nodes, owners)
    assert {(e["source"], e["target"], e["context"]) for e in edges} == {
        ("stacks:0001", "stacks:0002", "statement"),
        ("stacks:0001", "stacks:0003", "proof"),
        ("stacks:0002", "stacks:0003", "proof"),
    }
    assert stats["forward_references_omitted"] == 1
    assert stats["references_to_sections_or_unrepresented_labels"] == 1


def test_archive_verification_rejects_changed_source(tmp_path):
    archive = tmp_path / "upstream.tar.gz"
    archive.write_bytes(b"untrusted or changed upstream data")
    with pytest.raises(ValueError, match="SHA256"):
        importer.read_archive(archive)


def test_uncapped_import_retains_isolated_statements_and_audits_excluded_material():
    files = {
        "tags/tags": "0001,algebra-lemma-first\n0002,algebra-definition-isolated\n0003,coding-lemma-sample\n0004,obsolete-lemma-old\n",
        "chapters.tex": "\n".join(r"\hyperref[" + name + r"-section-phantom]{" + name + "}" for name in ("algebra", "coding", "obsolete")),
        "algebra.tex": r"""\section{A source section}
\begin{lemma}\label{lemma-first}A valid independent statement.\end{lemma}
\begin{definition}\label{definition-isolated}A genuine disconnected definition.\end{definition}
\begin{lemma}\label{lemma-untagged}A statement awaiting a tag.\end{lemma}
""",
        "coding.tex": r"\begin{verbatim}\begin{lemma}\label{lemma-sample}...\end{lemma}\end{verbatim}",
        "obsolete.tex": r"\begin{lemma}\label{lemma-old}Superseded source statement.\end{lemma}",
    }
    result = importer.build(files)
    assert [node["id"] for node in result["nodes"]] == ["stacks:0001", "stacks:0002"]
    assert result["edges"] == []
    assert result["stats"]["weakly_connected_components"] == result["stats"]["isolated_nodes"] == 2
    assert result["stats"]["nodes_outside_selection"] == 0
    assert result["stats"]["untagged_statements_omitted"] == 1
    assert result["stats"]["excluded_chapter_environments"] == 2


@pytest.fixture(scope="module")
def pinned_source():
    path = Path(os.environ.get("STACKS_SOURCE_ARCHIVE", "/tmp/stacks-project-a04446e.tar.gz"))
    if not path.exists():
        pytest.skip("Set STACKS_SOURCE_ARCHIVE to run full pinned-source integrity checks")
    return importer.read_archive(path)


def test_every_source_location_and_statement_matches_the_pinned_archive(atlas, pinned_source):
    tags = dict(line.split(",", 1) for line in pinned_source["tags/tags"].splitlines() if line and not line.startswith("#"))
    source_lines = {name: pinned_source[name].splitlines(keepends=True) for name in {node["source_file"] for node in atlas["nodes"]}}
    for node in atlas["nodes"]:
        lines = source_lines[node["source_file"]]
        located = "".join(lines[node["source_line"] - 1:node["source_end_line"]])
        located = re.sub(r"(?<!\\)%[^\n]*", "", located)
        block = re.search(r"\\begin\{" + node["kind"] + r"\}(?:\[[^\]]*\])?(.*?)\\end\{" + node["kind"] + r"\}", located, re.DOTALL)
        assert block, node["id"]
        label = re.search(r"\\label\{([^{}]+)\}", block.group(1)).group(1)
        assert tags[node["tag"]] == node["chapter_id"] + "-" + label
        statement = re.sub(r"\\label\{[^{}]+\}", "", block.group(1)).strip()
        assert statement == node["statement_latex"], node["id"]
        assert node["source_git_url"].endswith(f"/{node['source_file']}#L{node['source_line']}-L{node['source_end_line']}")


def test_full_source_rebuild_is_reproducible(atlas, pinned_source):
    rebuilt = importer.build(pinned_source)
    # JSON round-trip also normalizes integer histogram keys to their persisted
    # form, while preserving ordering, source excerpts and every edge.
    assert json.loads(json.dumps(rebuilt, ensure_ascii=False)) == atlas
