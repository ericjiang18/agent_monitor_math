"""Build a reproducible informal atlas from an official Stacks Project archive.

The source is data, never executed. See docs/dag-sources.md for attribution and
the exact download command. Only theorem-like environments become nodes;
arrows are actual source citations, restricted to earlier statements so this
selected reference graph is acyclic. They are not certified proof dependencies.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import io
import json
import math
from pathlib import Path
import re
import tarfile

ROOT = Path(__file__).resolve().parents[1]
SOURCE_COMMIT = "a04446e57ec1fbc252a871afcec7752fb2807b14"
SOURCE_SHA256 = "fa2cce9de74122a420c0724538ce46fc2693461c851f3df89a09de70234806b2"
SOURCE_ARCHIVE = f"https://codeload.github.com/stacks/stacks-project/tar.gz/{SOURCE_COMMIT}"
BASE_URL = "https://stacks.math.columbia.edu/tag/"
KINDS = ("theorem", "proposition", "lemma", "corollary", "definition")
ENVIRONMENT = re.compile(r"\\begin\{(" + "|".join(KINDS) + r")\}(?:\[([^\]]*)\])?")
LABEL = re.compile(r"\\label\{([^{}]+)\}")
REFERENCE = re.compile(r"\\(?:ref|eqref)\{([^{}]+)\}")
SECTION = re.compile(r"\\section\{([^\n]+)\}")
PALETTE = ("#42d6bc", "#a69bff", "#69b7ff", "#f0bc72", "#ef91b0", "#a4d881")

# Curated navigation groups, not additional mathematical assertions.
AREA_CHAPTERS = {
    "Categories & Foundations": "sets categories",
    "Topology": "topology",
    "Sheaves & Sites": "sheaves sites stacks modules sites-modules injectives",
    "Fields & Brauer Groups": "fields brauer",
    "Commutative Algebra": "algebra",
    "Homological Algebra": "homology simplicial dga dpa sdga",
    "Derived Categories": "derived perfect equiv spaces-perfect stacks-perfect",
    "Advanced Algebra": "more-algebra smoothing",
    "Sheaf Cohomology": "cohomology sites-cohomology hypercovering",
    "Schemes": "schemes constructions properties",
    "Scheme Morphisms": "morphisms more-morphisms flat limits",
    "Divisors & Intersection Theory": "divisors chow intersection relative-cycles",
    "Varieties & Curves": "varieties curves resolve models",
    "Descent": "topologies descent",
    "Groupoids & Quotients": "groupoids more-groupoids spaces-groupoids spaces-more-groupoids groupoids-quotients",
    "Étale Geometry": "etale pione etale-cohomology more-etale proetale trace",
    "Duality & Cohomology": "coherent weil dualizing duality spaces-duality",
    "Local & Crystalline Methods": "derham local-cohomology crystalline discriminant adequate",
    "Algebraic Spaces": "spaces spaces-properties spaces-morphisms decent-spaces spaces-over-fields spaces-topologies spaces-descent bootstrap",
    "Geometry of Spaces": "spaces-cohomology spaces-limits spaces-divisors spaces-more-morphisms spaces-flat spaces-pushouts spaces-chow spaces-more-cohomology spaces-simplicial spaces-resolve",
    "Algebraic & Formal Geometry": "algebraization formal-spaces restricted",
    "Deformation Theory": "formal-defos defos cotangent examples-defos",
    "Algebraic Stacks": "algebraic examples-stacks stacks-sheaves criteria artin stacks-properties stacks-morphisms stacks-limits stacks-cohomology stacks-introduction stacks-more-morphisms stacks-geometry",
    "Moduli Theory": "pic functors quot moduli moduli-curves",
}
CHAPTER_AREA = {chapter: area for area, chapters in AREA_CHAPTERS.items() for chapter in chapters.split()}

# These chapters are not active mathematical reference material. In particular,
# Coding Style contains a literal sample lemma, and Obsolete preserves results
# superseded by the active chapters. Neither is used to inflate the catalogue.
EXCLUDED_CHAPTERS = {
    "introduction": "Project introduction and attribution, not a mathematical statement chapter.",
    "conventions": "Conventions without theorem-like statement environments.",
    "guide": "Bibliographic guide, not a mathematical statement chapter.",
    "desirables": "Editorial requests, not a mathematical statement chapter.",
    "coding": "LaTeX coding instructions, including a verbatim sample lemma.",
    "obsolete": "Historical statements superseded by or duplicated in active chapters.",
    "fdl": "License text, not mathematical statements.",
    "index": "Automatically generated index; no source statement file.",
}

# The two miscellaneous mathematical chapters span several subjects. Group
# their genuine results by section, rather than treating a chapter heading as
# a new mathematical field. Exercise prompts themselves never become nodes.
SECTION_AREAS = {
    "examples": {
        "Topology": "Non-quasi-compact inverse limit of quasi-compact spaces|Different colimit topologies|The spectrum of the integers is not quasi-compact",
        "Commutative Algebra": "Noncomplete completion|Noncomplete quotient|Completion is not exact|The category of complete modules is not abelian|Nonflat completions|Regular sequences and base change|Another local ring with nonreduced completion|Dimension in Noetherian Jacobson rings|A noninvertible ideal invertible in stalks|A finite flat module which is not projective|A projective module which is not locally free|Zero dimensional local ring with nonzero flat ideal|An epimorphism of zero-dimensional rings which is not surjective|Finite type, not finitely presented, flat at prime|Finite type, flat and not of finite presentation|Topology of a finite type ring map|Pure not universally pure|Ideals generated by sets of idempotents and localization|Flat maps are not directed limits of finitely presented flat maps",
        "Derived Categories": "The category of derived complete modules|Derived pushforward of quasi-coherent modules|Derived base change|An interesting compact object",
        "Sheaves & Sites": "Nonabelian category of quasi-coherent modules|Sheaves and specializations|Sheaves and constructible functions|Sheaves on the category of Noetherian schemes",
        "Sheaf Cohomology": "Nonsplit locally split sequence|Non flasque quasi-coherent sheaf associated to injective module",
        "Varieties & Curves": "Non-quasi-affine variety with quasi-affine normalization",
        "Schemes": "Images of locally closed subsets|Nonexistence of suitable opens|Nonexistence of quasi-compact dense open subscheme|Weakly associated points and scheme theoretic density|Being projective is not local on the base",
        "Algebraic Spaces": "Affines over algebraic spaces|A family of curves whose total space is not a scheme",
        "Scheme Morphisms": "Pushforward of quasi-coherent modules|Universally submersive but not V covering",
        "Advanced Algebra": "A formally smooth non-flat ring map|A formally etale non-flat ring map|Flat and formally unramified is not formally etale|A ring map which identifies local rings which is not ind-etale",
        "Deformation Theory": "A formally etale ring map with nontrivial cotangent complex|An algebraic stack not satisfying strong formal effectiveness",
        "Groupoids & Quotients": "A non-separated flat group scheme|A non-flat group scheme with flat identity component|A non-separated group algebraic space over a field|A torsor which is not an fppf torsor",
        "Étale Geometry": "Specializations between points in fibre etale morphism|The etale topology vs Zariski and finite etale Covers",
        "Algebraic Stacks": "Stack with quasi-compact flat covering which is not algebraic|Limit preserving on objects, not limit preserving|Sheaf with quasi-compact flat covering which is not algebraic|The lisse-etale site is not functorial|The stack of proper algebraic spaces is not algebraic|An example of a non-algebraic Hom-stack",
        "Categories & Foundations": "A big abelian category|The category of modules modulo torsion modules",
        "Duality & Cohomology": "Example of non-additivity of traces|Nonfinite cohomology of the structure sheaf of a projective scheme",
        "Descent": "Non-effective descent data for projective schemes",
        "Algebraic & Formal Geometry": "A counter example to Grothendieck's existence theorem|Affine formal algebraic spaces",
    },
    "exercises": {
        "Categories & Foundations": "Colimits",
        "Commutative Algebra": "The Spectrum of a ring|Length|Catenary rings|Finite locally free modules|Going up and going down|Hilbert functions|Cohen-Macaulay rings of dimension 1",
        "Schemes": "Proj of a ring|Schemes|Tangent Spaces|Quasi-coherent Sheaves",
        "Derived Categories": "Filtered derived category",
        "Divisors & Intersection Theory": "Invertible sheaves|Divisors",
        "Sheaf Cohomology": "Cech Cohomology",
    },
}


def navigation_area(chapter: str, section: str) -> str:
    if chapter in CHAPTER_AREA:
        return CHAPTER_AREA[chapter]
    # Match typographic accents to hand-reviewed section names without changing
    # the actual source title shown in the node inspector.
    key = section.replace("é", "e").replace("É", "E").replace("v C", "C").replace("\\'", "")
    for area, sections in SECTION_AREAS.get(chapter, {}).items():
        if key in sections.split("|"):
            return area
    raise ValueError(f"Unreviewed mathematical section: {chapter}: {section}")


def read_archive(path: Path) -> dict[str, str]:
    """Verify the exact upstream archive and read only regular UTF-8 text files."""
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != SOURCE_SHA256:
        raise ValueError("Stacks source archive SHA256 does not match the pinned revision")
    files = {}
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            name = member.name.partition("/")[2]
            if name.endswith(".tex") or name in ("tags/tags", "COPYING", "CONTRIBUTORS"):
                handle = archive.extractfile(member)
                if handle is not None:
                    files[name] = handle.read().decode("utf-8")
    return files


def strip_comments(value: str) -> str:
    # Preserve newlines so source line locators remain valid.
    return re.sub(r"(?<!\\)%[^\n]*", "", value)


def plain_text(value: str, tag_lookup: dict[str, str] | None = None, chapter: str = "") -> str:
    """A search/display excerpt; original, unabridged source TeX is kept separately."""
    value = strip_comments(value)
    value = LABEL.sub("", value)
    def ref(match: re.Match) -> str:
        label = match.group(1)
        tag = (tag_lookup or {}).get(f"{chapter}-{label}") or (tag_lookup or {}).get(label)
        return f"[Tag {tag}]" if tag else label.replace("-", " ")
    value = REFERENCE.sub(ref, value)
    value = re.sub(r"\\(?:cite|citeauthor|citeyear)(?:\[[^]]*\])?\{([^}]*)\}", r"[\1]", value)
    value = re.sub(r"\\(?:begin|end)\{[^}]*\}", " ", value)
    value = re.sub(r"\\(?:label|slogan|history|reference)\{[^}]*\}", "", value)
    value = re.sub(r"\\(?:footnote|href)\{[^}]*\}", "", value)
    value = value.replace(r"\'E", "É").replace(r"\'e", "é").replace(r'\"o', "ö")
    replacements = {
        "longrightarrow": "→", "rightarrow": "→", "longmapsto": "↦", "mapsto": "↦",
        "leftarrow": "←", "leftrightarrow": "↔", "Rightarrow": "⇒", "Leftrightarrow": "⇔",
        "subseteq": "⊆", "subset": "⊂", "supseteq": "⊇", "supset": "⊃",
        "infty": "∞", "otimes": "⊗", "oplus": "⊕", "times": "×", "circ": "∘",
        "emptyset": "∅", "notin": "∉", "in": "∈", "to": "→", "geq": "≥", "leq": "≤",
        "neq": "≠", "cong": "≅", "simeq": "≃", "sum": "∑", "prod": "∏",
        "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
        "lambda": "λ", "mu": "μ", "nu": "ν", "pi": "π", "rho": "ρ", "sigma": "σ",
        "tau": "τ", "phi": "φ", "varphi": "φ", "psi": "ψ", "omega": "ω",
        "Omega": "Ω", "Gamma": "Γ", "Delta": "Δ", "Psi": "Ψ", "Phi": "Φ",
        "forall": "∀", "exists": "∃", "cdots": "…", "ldots": "…", "cdot": "·",
        "cup": "∪", "cap": "∩", "bigcup": "⋃", "bigcap": "⋂", "partial": "∂",
        "ell": "ℓ", "mathbf": "", "mathcal": "", "mathfrak": "", "mathrm": "",
        "mathbb": "", "operatorname": "", "text": "", "textit": "", "textbf": "",
        "it": "", "rm": "", "bf": "", "left": "", "right": "", "noindent": "",
        "medskip": "", "smallskip": "", "bigskip": "", "item": " • ", "quad": " ",
        "qquad": " ", "limits": "", "nolimits": "", "displaystyle": "",
    }
    value = re.sub(r"\\([A-Za-z]+)(?![A-Za-z])", lambda m: replacements.get(m.group(1), m.group(1)), value)
    value = value.replace("$$", " ").replace("$", "").replace("~", " ")
    value = re.sub(r"\\[ ,;!:\\]", " ", value).replace(r"\{", "(").replace(r"\}", ")")
    value = value.replace("{", "").replace("}", "").replace("``", '"').replace("''", '"')
    return " ".join(value.split()).strip()


def source_nodes(files: dict[str, str], audit: dict | None = None) -> tuple[list[dict], dict[str, str], dict[str, str]]:
    tags = {}
    for line in files["tags/tags"].splitlines():
        if line and not line.startswith("#"):
            tag, label = line.split(",", 1)
            tags[label] = tag
    chapters = re.findall(r"\\hyperref\[([^\]]+)-section-phantom\]\{([^\n]+)\}", files["chapters.tex"])
    nodes = []
    seen_tags = set()
    audit = audit if audit is not None else {}
    audit.update({"untagged_statements": [], "excluded_chapters": []})
    # Item labels inside a mathematical statement/proof resolve to that statement.
    owners = {}
    for chapter, chapter_title in chapters:
        if chapter in EXCLUDED_CHAPTERS:
            audit["excluded_chapters"].append({
                "chapter_id": chapter, "chapter": plain_text(chapter_title),
                "reason": EXCLUDED_CHAPTERS[chapter],
                "theorem_like_environments": len(ENVIRONMENT.findall(strip_comments(files.get(f"{chapter}.tex", "")))),
            })
            continue
        if chapter not in CHAPTER_AREA and chapter not in SECTION_AREAS:
            raise ValueError(f"Unreviewed source chapter: {chapter}")
        original = files[f"{chapter}.tex"]
        tex = strip_comments(original)
        # Offsets in comment-stripped text differ; line counts do not.
        section_matches = list(SECTION.finditer(tex))
        section_index = -1
        for match in ENVIRONMENT.finditer(tex):
            kind, named_title = match.groups()
            end = tex.find(r"\end{" + kind + "}", match.end())
            if end < 0:
                raise ValueError(f"Unclosed {kind} in {chapter}")
            body = tex[match.end():end]
            label_match = LABEL.search(body)
            if not label_match:
                raise ValueError(f"Unlabelled {kind} in {chapter}")
            label = f"{chapter}-{label_match.group(1)}"
            if label not in tags:
                audit["untagged_statements"].append({"label": label, "kind": kind,
                    "source_file": f"{chapter}.tex", "source_line": tex.count("\n", 0, match.start()) + 1,
                    "reason": "The pinned upstream tag registry has not assigned a permanent tag."})
                continue
            while section_index + 1 < len(section_matches) and section_matches[section_index + 1].start() < match.start():
                section_index += 1
            section = plain_text(section_matches[section_index].group(1)) if section_index >= 0 else "Introduction"
            after = end + len(r"\end{" + kind + "}")
            proof_start = re.match(r"\s*\\begin\{proof\}(?:\[[^]]*\])?", tex[after:])
            proof = ""
            if proof_start:
                proof_end = tex.find(r"\end{proof}", after + proof_start.end())
                if proof_end < 0:
                    raise ValueError(f"Unclosed proof after {label}")
                proof = tex[after + proof_start.end():proof_end]
            # Source metadata macros can contain nested braces; retain source
            # faithfully but do not use them as the display title.
            tag = tags[label]
            if tag in seen_tags:
                raise ValueError(f"Duplicate permanent source tag: {tag}")
            seen_tags.add(tag)
            title = plain_text(named_title, tags, chapter) if named_title else section
            title = f"{title} · {kind.title()} {tag}"
            statement = LABEL.sub("", body).strip()
            source_line = tex.count("\n", 0, match.start()) + 1
            source_end_line = tex.count("\n", 0, after) + 1
            summary = plain_text(statement, tags, chapter)
            if len(summary) > 480:
                summary = summary[:477].rsplit(" ", 1)[0] + "…"
            node = {
                "id": f"stacks:{tag}", "tag": tag, "title": title, "summary": summary,
                "statement_latex": statement, "area": navigation_area(chapter, section),
                "chapter": plain_text(chapter_title), "chapter_id": chapter,
                "section": section, "kind": kind, "layer": "informal", "status": "Published source",
                "source_url": BASE_URL + tag, "source_file": f"{chapter}.tex",
                "source_line": source_line, "source_end_line": source_end_line,
                "source_git_url": f"https://github.com/stacks/stacks-project/blob/{SOURCE_COMMIT}/{chapter}.tex#L{source_line}-L{source_end_line}",
                "statement_sha256": hashlib.sha256(statement.encode("utf-8")).hexdigest(),
                "origin": "The Stacks Project", "memory_eligible": False,
                "source_rank": len(nodes), "_label": label,
                "_references": [(reference, context) for context, text in (("statement", body), ("proof", proof))
                                for reference in REFERENCE.findall(text)],
            }
            nodes.append(node)
            for owner_label in LABEL.findall(body + "\n" + proof):
                owners[f"{chapter}-{owner_label}"] = node["id"]
    return nodes, owners, tags


def reference_edges(nodes: list[dict], owners: dict[str, str]) -> tuple[list[dict], dict]:
    by_id = {node["id"]: node for node in nodes}
    edges = {}
    counts = Counter()
    for target in nodes:
        for reference, context in target["_references"]:
            source_id = owners.get(f"{target['chapter_id']}-{reference}") or owners.get(reference)
            if source_id is None:
                counts["references_to_sections_or_unrepresented_labels"] += 1
                continue
            if source_id == target["id"]:
                counts["self_references_omitted"] += 1
                continue
            pair = (source_id, target["id"])
            if pair in edges:
                if context == "proof":
                    edges[pair]["context"] = "proof"
                continue
            edges[pair] = {"source": source_id, "target": target["id"], "kind": "reference", "context": context}
    counts["unique_source_references"] = len(edges)
    kept = []
    for edge in edges.values():
        if by_id[edge["source"]]["source_rank"] < by_id[edge["target"]]["source_rank"]:
            kept.append(edge)
        else:
            counts["forward_references_omitted"] += 1
    counts["acyclic_source_references"] = len(kept)
    return kept, dict(counts)


def graph_statistics(nodes: list[dict], edges: list[dict]) -> dict:
    """Count the real components; disconnected statements remain first-class nodes."""
    neighbours = defaultdict(set)
    for edge in edges:
        neighbours[edge["source"]].add(edge["target"])
        neighbours[edge["target"]].add(edge["source"])
    unseen = {node["id"] for node in nodes}
    component_sizes = []
    while unseen:
        pending = [min(unseen)]
        size = 0
        while pending:
            node_id = pending.pop()
            if node_id not in unseen:
                continue
            unseen.remove(node_id)
            size += 1
            pending.extend(neighbours[node_id] & unseen)
        component_sizes.append(size)
    component_sizes.sort(reverse=True)
    return {"corpus_nodes": len(nodes), "catalogue_nodes": len(nodes), "catalogue_edges": len(edges),
            "largest_source_component_nodes": max(component_sizes, default=0),
            "nodes_outside_selection": 0, "references_outside_selection": 0,
            "weakly_connected_components": len(component_sizes),
            "isolated_nodes": component_sizes.count(1),
            "component_size_distribution": dict(sorted(Counter(component_sizes).items(), reverse=True))}


def position_nodes(nodes: list[dict], edges: list[dict]) -> list[dict]:
    by_id = {node["id"]: node for node in nodes}
    incoming = defaultdict(list)
    for edge in edges:
        incoming[edge["target"]].append(edge["source"])
    groups = defaultdict(list)
    for rank, node in enumerate(nodes):
        node["rank"] = rank
        node["depth"] = max((by_id[source]["depth"] + 1 for source in incoming[node["id"]]), default=0)
        groups[node["area"]].append(node)
    clusters = []
    for index, (area, chapter_names) in enumerate(AREA_CHAPTERS.items()):
        members = groups[area]
        if not members:
            continue
        center_x, center_y = 230 + (index % 6) * 450, 220 + (index // 6) * 460
        color = PALETTE[index % len(PALETTE)]
        cluster_id = re.sub(r"[^a-z0-9]+", "-", area.lower()).strip("-")
        clusters.append({"id": cluster_id, "title": area, "area": area,
                         "x": center_x, "y": center_y, "count": len(members), "color": color})
        # Stable low-discrepancy disk avoids force-layout costs for the full corpus.
        for position, node in enumerate(members):
            radius = 175 * math.sqrt((position + 0.5) / len(members))
            angle = position * math.pi * (3 - math.sqrt(5))
            node["x"] = round(center_x + radius * math.cos(angle), 3)
            node["y"] = round(center_y + radius * math.sin(angle) * 0.84, 3)
            node["cluster"] = cluster_id
            node.pop("_label", None)
            node.pop("_references", None)
    return clusters


def build(files: dict[str, str]) -> dict:
    audit = {}
    nodes, owners, _ = source_nodes(files, audit)
    edges, reference_stats = reference_edges(nodes, owners)
    stats = graph_statistics(nodes, edges)
    stats.update(reference_stats)
    stats["untagged_statements_omitted"] = len(audit["untagged_statements"])
    stats["excluded_chapter_environments"] = sum(item["theorem_like_environments"] for item in audit["excluded_chapters"])
    duplicate_bodies = defaultdict(list)
    for node in nodes:
        duplicate_bodies[node["statement_sha256"]].append(node["id"])
    stats["identical_statement_groups"] = [ids for ids in duplicate_bodies.values() if len(ids) > 1]
    stats["identical_statement_group_count"] = len(stats["identical_statement_groups"])
    stats["distinct_statement_bodies"] = len(duplicate_bodies)
    stats["kind_counts"] = dict(Counter(node["kind"] for node in nodes))
    stats["chapter_count"] = len({node["chapter_id"] for node in nodes})
    clusters = position_nodes(nodes, edges)
    return {
        "version": 2, "title": "Ansätze Informal Mathematics Atlas",
        "description": f"{len(nodes):,} source-linked mathematical statements from the Stacks Project, spanning algebra, geometry, cohomology and moduli. All eligible tagged statements from active mathematical chapters are included. Arrows retain earlier source references; they do not certify proof dependencies.",
        "source": {"name": "The Stacks Project", "url": "https://stacks.math.columbia.edu/",
                   "revision": SOURCE_COMMIT, "revision_date": "2026-07-28", "archive_url": SOURCE_ARCHIVE,
                   "archive_sha256": SOURCE_SHA256, "license": "GFDL-1.2-or-later",
                   "license_url": "/research/stacks-source/COPYING.txt",
                   "attribution_url": "/dag-sources", "contributors_url": "/research/stacks-source/CONTRIBUTORS.txt",
                   "original_introduction_url": "/research/stacks-source/introduction.tex.txt",
                   "transparent_copy_url": "/research/informal-atlas.json"},
        "selection": {"method": "All permanently tagged theorem, proposition, lemma, corollary and definition environments from active mathematical chapters in the pinned source. No node-count or connected-component limit; isolated statements are retained.",
                      "edge_meaning": "Earlier source statement → citing statement; references extracted from the statement or its immediately following proof.",
                      "omissions": f"Forward references, section references and self references are omitted. {len(audit['untagged_statements'])} statements without permanent upstream tags, the superseded Obsolete chapter and non-mathematical editorial chapters are excluded; all omissions are enumerated in source_audit. No eligible tagged statement is omitted for lack of connections. Source order makes the displayed reference graph acyclic.",
                      "proof_claim": "This is a navigational subset of source references, not a complete or formally verified proof dependency graph.",
                      "memory_policy": "Published source catalogue. These imported statements are not model-generated memories; accepted model-run memories are stored and attributed separately."},
        "stats": stats, "source_audit": audit, "clusters": clusters, "nodes": nodes, "edges": edges,
    }


def write_outputs(atlas: dict, files: dict[str, str], output_dir: Path, backend_index: Path | None) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "informal-atlas.json").write_text(json.dumps(atlas, ensure_ascii=False, separators=(",", ":")) + "\n")
    source_dir = output_dir / "stacks-source"
    source_dir.mkdir(exist_ok=True)
    for source, target in (("COPYING", "COPYING.txt"), ("CONTRIBUTORS", "CONTRIBUTORS.txt"), ("introduction.tex", "introduction.tex.txt")):
        (source_dir / target).write_text(files[source])
    if backend_index:
        backend_index.parent.mkdir(parents=True, exist_ok=True)
        keys = ("id", "tag", "title", "summary", "area", "chapter", "section", "kind", "layer", "source_url", "memory_eligible")
        index = {"version": atlas["version"], "title": atlas["title"], "source": atlas["source"],
                 "nodes": [{key: node[key] for key in keys} for node in atlas["nodes"]]}
        backend_index.write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "docs/public/research")
    parser.add_argument("--backend-index", type=Path, default=ROOT / "agent_monitor/data/informal-atlas.json")
    args = parser.parse_args()
    files = read_archive(args.archive)
    atlas = build(files)
    write_outputs(atlas, files, args.output_dir, args.backend_index)
    print(json.dumps(atlas["stats"], indent=2))


if __name__ == "__main__":
    main()
