# TeX source bundles

Use this workflow whenever the audit input is a directory rather than one paper file.

## Prepare a closed source view

Run:

```sh
python3 <skill-dir>/scripts/prepare_latex_bundle.py <source-dir> \
  --out <run-dir>/source
```

The preparer:

- identifies a root containing `\documentclass` and `\begin{document}`;
- prefers an unambiguous `main.tex`, `paper.tex`, `manuscript.tex`, `article.tex`, or `ms.tex` when several documents exist;
- follows static local `\input`, `\include`, `\subfile`, `\import`, bibliographies, classes, packages, and graphics;
- rejects dependencies that resolve outside the submitted directory;
- copies the discovered closure into the run directory;
- writes hashes, root-selection provenance, local files, system package names, and unresolved references to `source-manifest.json`;
- omits the original absolute source path from the pass-visible manifest; keep it only in orchestrator-owned `run.json`;
- never runs TeX, Makefiles, build scripts, or shell escape.
- stops at bounded file-count and byte limits unless the operator explicitly raises them after reviewing the source bundle.
- records static-parser limitations and verifies that every copied file still matches its discovery hash.

If several root candidates remain, show the relative candidate list and ask the user for the main document. Then rerun with `--root <relative-path>`.

If the preparer exits with code 2, one or more required TeX includes are missing or dynamic. Do not launch the audit. Report the entries marked `required: true` and ask for the missing generated/source files or the correct root. Unresolved graphics or bibliographies may permit a proof audit, but disclose them in coverage notes and do not claim those portions were checked.

## Give auditors one logical paper

Pass both auditors the prepared directory, manifest, and exact root path. Require them to:

1. read the manifest before the paper;
2. start at `root_tex` and follow TeX include order rather than alphabetical filesystem order;
3. read only manifest-listed files;
4. treat `.sty` and `.cls` files as paper context when they define notation, environments, counters, or mathematical macros;
5. use relative file paths in region spans, evidence references, and finding locations;
6. record unresolved manifest entries in `coverage_notes.unreviewed_or_difficult_regions` or `external_checks_not_performed`, as appropriate.

Do not flatten the sources into one generated TeX file. Flattening can change macro scope, source locations, include order, and conditional behavior. The manifest plus root preserves the actual document structure.

## Compilation boundary

Source auditing does not require compilation. A submitted TeX project can execute surprising behavior through build rules or shell escape. Do not run `make`, repository scripts, `.latexmkrc`, or `-shell-escape` as part of automatic preparation. If visual/PDF comparison is necessary, request separate authorization and use a reviewed no-shell-escape compilation path in an isolated temporary directory.
