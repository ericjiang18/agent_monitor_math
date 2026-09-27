---
title: Research atlas sources
description: Provenance and interpretation of the informal–formal bridge and the Stacks Project reference DAG.
---

# Research atlas sources

The [research atlas](/dag) has two views: an **informal–formal bridge** built
from the supplied mathematical knowledge graph, and the **Stacks Project
reference DAG**. Their sources and connections have different meanings,
described below.

## Informal–formal bridge {#informal-formal-bridge}

The bridge connects mathematical statements and proofs in natural language
to the Lean declarations named in their blueprints. It uses the supplied
`graph.json` as its canonical graph, with provenance and original blueprint
text from `kg.zip`. The second archive, `kg (1).zip`, is byte-for-byte identical;
its records are counted once.

### Scope and selection

The archives describe **87 projects**, of which the supplied manifest marks
**60 as usable** and excludes 27. Its stated criteria require at least five
informal statements, at least one Lean reference, and Lean knowledge-graph
type information. The supplied `graph.json` contains those 60 curated projects;
the bridge preserves their complete informal catalogue and every supplied
formalization correspondence.

| Imported item | Count |
| --- | ---: |
| Informal statements and proof environments | 24,950 |
| Project-local Lean declarations | 16,302 |
| Mathlib declarations | 380 |
| Unresolved or ambiguous reference placeholders | 3,892 |
| Informal-to-formal correspondences | 20,656 |

The informal side contains **17,381 statements and 7,569 proof environments**.
Of these 24,950 entries, **15,293 have a supplied formalization link** and
**9,657 have none**. An entry without a link remains part of the catalogue;
the bridge does not infer a match from similar names or wording. Counts refer
to source records, rather than mathematically distinct propositions.

### What a connection means

Every bridge connection comes from a source `FORMALIZES` relationship. The
export directs it from a formal declaration to an informal entry; the bridge
draws it from the informal side to the formal side for reading. It retains
the original Lean reference, resolution status and candidate count.

| Source resolution | Connections | Meaning |
| --- | ---: | --- |
| `resolved` | 16,374 | One declaration in the project matched the reference |
| `resolved_mathlib` | 387 | A declaration in the project's Mathlib matched |
| `unresolved` | 2,492 | No declaration matched; the endpoint is a placeholder |
| `ambiguous` | 1,403 | Several candidates matched; the endpoint is a placeholder |

There are **16,761 resolved correspondences** and **3,895 unresolved or
ambiguous references**. Placeholder endpoints are separate from the
**16,682 actual formal declarations**. Several links can share an endpoint,
so link and node counts differ. Mathlib declarations and external placeholders
can be shared across projects.

These statuses report reference resolution in the supplied export. The import
does not compile Lean, check proofs, or establish that an informal statement
and its linked declaration are semantically equivalent. The source's `leanok`
flag is retained as source metadata. Informal `USES` and `PROVES` relationships
appear within the informal layer when **Show dependencies** is enabled.

### Connections within a layer

The optional dependency view retains all **54,128 `USES` references** and
**72 `PROVES` links** supplied by the graph. A `USES` arrow points from an
informal entry to the entry it references. A `PROVES` arrow points from a proof
environment to the statement it proves according to the source. Both retain
the export's exact `start` → `end` direction and original label. Selecting a
node highlights its connections so the two-layer layout remains readable.

These relationships describe the source blueprints; they do not certify a
proof or imply that a cited result is a necessary premise. The importer keeps
every record, including any cycles, duplicate records or self references; it
does not force these dependencies into a DAG. This supplied graph has no
duplicate dependency records or self references.

The supplied files contain **no formal-to-formal declaration dependencies**.
Those would require extracting dependency information from the Lean projects.
The bridge does not infer formal dependencies through shared informal entries.

### Text, declarations and source links

The inspector loads original node properties separately from the compact
navigation index. **2,222 project-local declarations and all 380 Mathlib
declarations lack a type signature in the supplied graph**; the retained
declaration name and module identify those records without inventing a type.
The archives mention a formal-declaration file in their manifest but do not
contain that file, so they cannot fill these signature gaps.

The graph truncates **56 informal texts at 4,000 characters**. Their archive
records preserve the complete text, and each graph excerpt exactly matches
its corresponding prefix. The import retains both the graph's original
`content` and the archive's `content_full` for those entries.

Project provenance records the repository, commit, Lean toolchain, Mathlib
revision and blueprint entry. Source links point to the supplied repository
revision and blueprint entry. Flattened blueprint line numbers are not used
as file anchors, and a declaration's module name is not treated as a verified
source-file location. Attribution and licensing for these project records
remain with their respective source projects; the Stacks license notice below
applies to the Stacks-derived atlas.

### Reproduce the bridge import

Place the three supplied files at the repository root, then run:

```sh
python3 scripts/import_bipartite_atlas.py
python3 scripts/import_bipartite_atlas.py --check
```

The importer uses the Python standard library and reads the files as data.
It checks archive equality, project provenance, endpoint existence, partition
membership, reference statuses, duplicate formalization edges, and exact
correspondence between the graph's links and the curated archive references.
It also verifies dependency counts against the source totals and requires both
endpoints to be informal. Dependency duplicates and self references are reported
and preserved. The check command compares every generated artifact byte-for-byte
with a fresh import, including the dependency JSON and its compressed copy.

The [bridge index](/research/bipartite-atlas.json) contains all navigation
records, bridge connections, project provenance and source hashes. The optional
[dependency file](/research/bipartite-dependencies.json) uses the same compact
node IDs and loads when dependencies are enabled. Full properties are loaded
from the index's referenced detail files. Exact original graph IDs remain in
those details as `source_id`; compact index IDs serve navigation only.

```text
graph.json SHA-256
54c78e798c1e6ca635e9aa4f2516f02eb6a56332446359cdd59d4f9babc7f333

kg.zip and kg (1).zip SHA-256
324f575f305cf598566a5535e753b51c6c466944fcba238293cf666d59edd2be
```

## Stacks Project reference DAG

### Ansätze Informal Mathematics Atlas

This atlas contains **14,754 source-tagged mathematical statements** from
**The Stacks Project**, an open reference on algebraic geometry and its
foundations. It spans **24 fields and 109 chapters**, from commutative algebra
and categories to étale cohomology, deformation theory and moduli spaces.
It provides useful background for research programs such as Langlands; it is
not a claim to map the entire Langlands program.

The original work is by **The Stacks Project Authors**, including Johan de Jong,
Jarod Alper, Bhargav Bhatt, Kiran Kedlaya, Akhil Mathew and Alex Perry.
The complete [list of contributors](/research/stacks-source/CONTRIBUTORS.txt)
and the original [Introduction and Attribution](/research/stacks-source/introduction.tex.txt)
are preserved with this edition. See also the
[original attribution](https://stacks.math.columbia.edu/tag/06LB).

Selection, indexing, excerpt formatting and graph layout: **Ansätze contributors**.
Publisher of this modified edition: **Ansätze**, 2026.

Copyright (C) 2005 -- 2025 Johan de Jong.
Copyright (C) 2026 Ansätze contributors, for the selection and modifications.

Permission is granted to copy, distribute and/or modify this document under
the terms of the GNU Free Documentation License, Version 1.2 or any later
version published by the Free Software Foundation; with no Invariant Sections,
no Front-Cover Texts, and no Back-Cover Texts. A copy of the license is included
under [GNU Free Documentation License](#gnu-free-documentation-license).

### What a node means

Every node identifies one actual lemma, proposition, theorem or definition
using its permanent Stacks tag. The inspector provides a short excerpt, the
full statement in source LaTeX, its chapter and section, and a link to the
original page. The data also records its exact source file, start and end line,
a link into the pinned Git revision, and a SHA-256 digest of the statement. Formatting the excerpt into plain text can simplify mathematical
notation; the original statement and source page preserve the precise notation.
Nothing in this catalogue claims machine verification.

The edition contains **12,495 lemmas, 328 propositions, 212 theorems and
1,719 definitions**. A permanent source tag is included once, and all nodes
belong to the informal layer. Nodes are not generated variations or
placeholders. Results from the Examples chapter and definitions from the
Exercises chapter are included; exercise prompts are not turned into theorems.

The count is of **distinct source tags**, not a claim that every entry expresses
a mathematically different proposition. The upstream project sometimes uses
the same wording in different contexts: for example, a property of schemes,
algebraic spaces and stacks. It also contains a few restated results. There
are **14,627 different statement bodies**, with **121 groups** of repeated
wording across 248 source tags. These genuine upstream entries retain their
own context and citations. The data explicitly lists these groups; we do not
generate copies to increase the node count.

### What an arrow means

The atlas contains **37,440 source-reference arrows**. An arrow runs from an
earlier mathematical statement to a later statement that cites it in its text
or immediately following proof. References to numbered subitems resolve to
the containing statement. References in a statement and in its proof are
distinguished in the data.

This is a **selected reference DAG**, not a complete or formally certified
proof dependency graph. A citation can supply context or compare a result;
it does not always express a necessary hypothesis or a logical dependency.
The importer preserves only arrows that go forward in the book's statement
order. This guarantees acyclicity without inventing mathematical dependencies.

The importer includes **every eligible permanently tagged statement** in the
active mathematical chapters of the pinned revision. There is no 10,000-node
cap and no requirement that a statement connect to the rest of the graph.
The reference graph has **1,167 weakly connected components**: the largest
contains **13,501 nodes**, and **1,102 real source statements are isolated**
under the retained reference rule. We retain these statements and do not
invent arrows to connect them.

The import records these omissions explicitly:

| Source item | Count | Treatment |
| --- | ---: | --- |
| Eligible tagged statements outside the catalogue | 0 | All are included |
| Statements not yet assigned permanent upstream tags | 6 | Omitted; exact source labels and lines recorded |
| Theorem-like environments in the Obsolete chapter | 98 | Superseded historical results excluded |
| Sample lemma in Coding Style | 1 | Verbatim documentation example excluded |
| Forward references in the complete active source graph | 75 | Omitted to maintain source order |
| Retained references outside the catalogue | 0 | All eligible endpoint pairs are included |
| References to sections or other unrepresented labels | 5,623 | Not converted into statement nodes |
| Self references | 485 | Not shown as arrows |

The last two counts are reference occurrences. Counts of arrows are unique
pairs of source and target statements. Editorial chapters, licensing and the
generated index do not supply mathematical nodes. The six untagged statements
remain in the pinned source, but the upstream tag registry has not assigned
them stable public identities; no tag or theorem is fabricated for them.
The data file contains all component sizes, omission records, repeated-wording
groups and source metadata.

### Catalogue and model memory

These source imports are a reference catalogue. They are **not** model-run
memories, do not carry invented model provenance, and have
`memory_eligible: false`. Accepted memories from eligible completed model runs
are stored separately with their run and model provenance. A memory may link
to a catalogue node without changing that node's published source attribution.

### Reproduce the import

The source archive is pinned to revision
`a04446e57ec1fbc252a871afcec7752fb2807b14`, dated July 28, 2026.
The importer checks the complete archive's SHA-256 digest before reading any
source. It uses the Python standard library, never executes upstream code,
and does not download files itself.

```sh
curl -sS -L https://codeload.github.com/stacks/stacks-project/tar.gz/a04446e57ec1fbc252a871afcec7752fb2807b14 -o /tmp/stacks-project-a04446e.tar.gz
python3 scripts/import_informal_atlas.py /tmp/stacks-project-a04446e.tar.gz
```

Expected SHA-256:

```text
fa2cce9de74122a420c0724538ce46fc2693461c851f3df89a09de70234806b2
```

The public [complete transparent JSON copy](/research/informal-atlas.json)
contains the statements, references and provenance. The all-source import is deterministic and has no node-count option. It also writes
a smaller index at `agent_monitor/data/informal-atlas.json` for matching
model memories with stable source tags. Both data copies are licensed under
GFDL 1.2 or later. This license applies to this source-derived atlas; independent
website software and separately authored user contributions retain their own
applicable licenses.

### History

**The Stacks Project**, 2005–2026, The Stacks Project Authors, published by
The Stacks Project. This edition uses the July 28, 2026 source revision.
Original locations: [project website](https://stacks.math.columbia.edu/),
[source repository](https://github.com/stacks/stacks-project), and
[pinned transparent source archive](https://codeload.github.com/stacks/stacks-project/tar.gz/a04446e57ec1fbc252a871afcec7752fb2807b14).

**Ansätze Informal Mathematics Atlas**, 2026, source authors as credited above
and Ansätze contributors; published by Ansätze. The initial 10,000-node selection
has been expanded to all 14,754 eligible tagged statements in active
mathematical chapters. Modifications extract actual references, create
readable excerpts and a graph layout, and add searchable metadata, exact
source locators and reproducible integrity checks. Original statement LaTeX, attribution,
contributors and licensing are retained. The original project's authors have
not endorsed this modified edition.

### GNU Free Documentation License

The complete, unaltered [GNU Free Documentation License, Version 1.2](/research/stacks-source/COPYING.txt)
is included with the atlas. This edition and its source-derived data may be
used under version 1.2 or any later version.
