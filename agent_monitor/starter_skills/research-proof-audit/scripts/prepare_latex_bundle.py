#!/usr/bin/env python3
"""Prepare an allowlisted, reproducible TeX source bundle for paper auditing.

Usage:
  python3 prepare_latex_bundle.py SOURCE_DIR --out OUTPUT_DIR [--root MAIN.tex]

The script identifies the root document, follows static local TeX dependencies,
copies only the discovered closure, and writes source-manifest.json. It never
executes TeX, Makefiles, or repository scripts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from collections import deque
from pathlib import Path
from typing import Any, Iterable


TEXT_SUFFIXES = {".tex", ".sty", ".cls", ".bst"}
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "build",
    "dist",
    "out",
    "paper-audit-output",
}
ROOT_NAMES = ("main.tex", "paper.tex", "manuscript.tex", "article.tex", "ms.tex")
GRAPHIC_EXTENSIONS = (".pdf", ".png", ".jpg", ".jpeg", ".eps", ".svg")
DEFAULT_MAX_FILES = 5000
DEFAULT_MAX_BYTES = 250 * 1024 * 1024


def strip_comments(text: str) -> str:
    cleaned: list[str] = []
    for line in text.splitlines(keepends=True):
        cut = len(line)
        for index, char in enumerate(line):
            if char != "%":
                continue
            backslashes = 0
            cursor = index - 1
            while cursor >= 0 and line[cursor] == "\\":
                backslashes += 1
                cursor -= 1
            if backslashes % 2 == 0:
                cut = index
                break
        suffix = "\n" if line.endswith("\n") and cut < len(line) else ""
        cleaned.append(line[:cut] + suffix)
    return "".join(cleaned)


def is_skipped(path: Path, source: Path, out: Path) -> bool:
    try:
        relative = path.relative_to(source)
    except ValueError:
        return True
    if out == path or out in path.parents:
        return True
    return any(part in SKIP_DIRS or part.startswith(".") for part in relative.parts[:-1])


def tex_files(source: Path, out: Path, max_files: int) -> list[Path]:
    files: list[Path] = []
    for path in source.rglob("*.tex"):
        if (
            path.is_file()
            and inside_source(path.resolve(), source)
            and not is_skipped(path, source, out)
        ):
            files.append(path)
            if len(files) > max_files:
                raise ValueError(
                    f"source contains more than {max_files} TeX files; "
                    "raise --max-files explicitly after reviewing the bundle"
                )
    return sorted(files)


def choose_root(
    source: Path, out: Path, requested: str | None, max_files: int
) -> tuple[Path, str]:
    if requested:
        candidate = Path(requested).expanduser()
        if not candidate.is_absolute():
            candidate = source / candidate
        candidate = candidate.resolve()
        if not candidate.is_file():
            raise ValueError(f"root TeX file does not exist: {candidate}")
        if source not in candidate.parents:
            raise ValueError("root TeX file must be inside SOURCE_DIR")
        return candidate, "explicit"

    candidates: list[Path] = []
    for path in tex_files(source, out, max_files):
        text = strip_comments(path.read_text(encoding="utf-8", errors="replace"))
        if re.search(r"\\documentclass(?:\s*\[[^\]]*\])?\s*\{", text) and re.search(
            r"\\begin\s*\{document\}", text
        ):
            candidates.append(path.resolve())

    if not candidates:
        raise ValueError(
            "no root candidate contains both \\documentclass and \\begin{document}"
        )
    if len(candidates) == 1:
        return candidates[0], "single-document-candidate"

    for preferred in ROOT_NAMES:
        matches = [path for path in candidates if path.name.lower() == preferred]
        if len(matches) == 1:
            return matches[0], f"preferred-name:{preferred}"

    rendered = "\n  - ".join(str(path.relative_to(source)) for path in candidates)
    raise ValueError(
        "multiple root TeX candidates; rerun with --root RELATIVE_PATH:\n  - " + rendered
    )


def is_dynamic(target: str) -> bool:
    return any(marker in target for marker in ("\\", "#", "$"))


def inside_source(path: Path, source: Path) -> bool:
    return path == source or source in path.parents


def resolve_local(
    target: str,
    base: Path,
    source: Path,
    extensions: Iterable[str],
    extra_dirs: Iterable[Path] = (),
) -> tuple[Path | None, str | None]:
    target = target.strip()
    if not target:
        return None, "empty target"
    if is_dynamic(target):
        return None, "dynamic target"
    raw = Path(target)
    variants = [raw] if raw.suffix else [Path(f"{target}{ext}") for ext in extensions]
    search_dirs = [base, *extra_dirs]
    if source not in search_dirs:
        search_dirs.append(source)
    saw_outside = False
    for directory in search_dirs:
        for variant in variants:
            candidate = (directory / variant).resolve()
            if not inside_source(candidate, source):
                saw_outside = True
                continue
            if candidate.is_file():
                return candidate, None
    return None, "resolves outside source bundle" if saw_outside else "not found in source bundle"


def matches(pattern: str, text: str) -> Iterable[re.Match[str]]:
    return re.finditer(pattern, text, flags=re.MULTILINE | re.DOTALL)


def graphic_paths(path: Path, root_base: Path) -> list[Path]:
    text = strip_comments(path.read_text(encoding="utf-8", errors="replace"))
    paths: list[Path] = []
    for match in matches(r"\\graphicspath\s*\{((?:\s*\{[^{}]*\}\s*)+)\}", text):
        for entry in re.findall(r"\{([^{}]*)\}", match.group(1)):
            if not is_dynamic(entry):
                paths.append((root_base / entry).resolve())
    return paths


def discover_dependencies(
    path: Path,
    source: Path,
    root_base: Path,
    root_graphic_dirs: Iterable[Path],
) -> list[dict[str, Any]]:
    text = strip_comments(path.read_text(encoding="utf-8", errors="replace"))
    # Standard TeX file lookup is relative to the root document's compilation
    # directory, not the directory of the currently included source file.
    base = root_base
    found: list[dict[str, Any]] = []

    for match in matches(r"\\(input|include|subfile)\s*\{([^{}]+)\}", text):
        found.append({"command": match.group(1), "target": match.group(2), "base": base, "extensions": (".tex",), "required": True})

    # Plain TeX also permits an unbraced form such as ``\\input chapter``.
    # The first character class excludes the braced form to avoid duplicates.
    for match in matches(r"\\input\s+([^\s%{}]+)", text):
        found.append({"command": "input", "target": match.group(1), "base": base, "extensions": (".tex",), "required": True})

    for match in matches(r"\\(import|subimport)\s*\{([^{}]+)\}\s*\{([^{}]+)\}", text):
        import_base = (root_base / match.group(2)).resolve()
        found.append({"command": match.group(1), "target": match.group(3), "base": import_base, "extensions": (".tex",), "required": True})

    for match in matches(r"\\bibliography\s*\{([^{}]+)\}", text):
        for target in match.group(1).split(","):
            found.append({"command": "bibliography", "target": target, "base": base, "extensions": (".bib",), "required": False})

    for match in matches(r"\\addbibresource(?:\s*\[[^\]]*\])?\s*\{([^{}]+)\}", text):
        found.append({"command": "addbibresource", "target": match.group(1), "base": base, "extensions": (".bib",), "required": False})

    local_libraries = (
        ("documentclass|LoadClass|LoadClassWithOptions", ".cls"),
        ("usepackage|RequirePackage|RequirePackageWithOptions", ".sty"),
        ("bibliographystyle", ".bst"),
    )
    for commands, extension in local_libraries:
        pattern = rf"\\({commands})(?:\s*\[[^\]]*\])?\s*\{{([^{{}}]+)\}}"
        for match in matches(pattern, text):
            for target in match.group(2).split(","):
                found.append({"command": match.group(1), "target": target, "base": base, "extensions": (extension,), "required": False, "system_ok": True})

    graphic_dirs = [*root_graphic_dirs, *graphic_paths(path, root_base)]
    for match in matches(r"\\includegraphics(?:\s*\[[^\]]*\])?\s*\{([^{}]+)\}", text):
        found.append({"command": "includegraphics", "target": match.group(1), "base": base, "extensions": GRAPHIC_EXTENSIONS, "required": False, "extra_dirs": graphic_dirs})

    return found


def file_kind(path: Path) -> str:
    return {
        ".tex": "tex",
        ".sty": "style",
        ".cls": "class",
        ".bst": "bibliography-style",
        ".bib": "bibliography",
        ".pdf": "graphic",
        ".png": "graphic",
        ".jpg": "graphic",
        ".jpeg": "graphic",
        ".eps": "graphic",
        ".svg": "graphic",
    }.get(path.suffix.lower(), "asset")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_dir", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--root", help="root TeX path, relative to SOURCE_DIR")
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    args = parser.parse_args()
    if args.max_files < 1 or args.max_bytes < 1:
        raise SystemExit("--max-files and --max-bytes must be positive")

    source = args.source_dir.expanduser().resolve()
    out = args.out.expanduser().resolve()
    if not source.is_dir():
        raise SystemExit(f"SOURCE_DIR is not a directory: {source}")
    if out.exists():
        if not out.is_dir() or any(out.iterdir()):
            raise SystemExit(f"OUTPUT_DIR must be absent or empty: {out}")

    try:
        root, root_selection = choose_root(source, out, args.root, args.max_files)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    queue: deque[Path] = deque([root])
    discovered: dict[Path, dict[str, Any]] = {}
    unresolved: list[dict[str, Any]] = []
    system_dependencies: set[str] = set()
    root_base = root.parent
    root_graphic_dirs = graphic_paths(root, root_base)
    total_bytes = 0

    while queue:
        current = queue.popleft().resolve()
        if current in discovered:
            continue
        if not inside_source(current, source):
            unresolved.append({"from": None, "command": "root", "target": str(current), "reason": "outside source bundle", "required": True})
            continue
        if len(discovered) >= args.max_files:
            raise SystemExit(
                f"discovered dependency closure exceeds --max-files={args.max_files}"
            )
        relative = current.relative_to(source).as_posix()
        size = current.stat().st_size
        if total_bytes + size > args.max_bytes:
            raise SystemExit(
                f"discovered dependency closure exceeds --max-bytes={args.max_bytes}"
            )
        total_bytes += size
        discovered[current] = {
            "path": relative,
            "kind": file_kind(current),
            "bytes": size,
            "sha256": sha256(current),
        }
        if current.suffix.lower() not in TEXT_SUFFIXES:
            continue

        for dependency in discover_dependencies(
            current, source, root_base, root_graphic_dirs
        ):
            target, reason = resolve_local(
                dependency["target"],
                dependency["base"],
                source,
                dependency["extensions"],
                dependency.get("extra_dirs", ()),
            )
            if target is not None:
                queue.append(target)
            elif dependency.get("system_ok") and reason == "not found in source bundle":
                system_dependencies.add(f"{dependency['command']}:{dependency['target'].strip()}")
            else:
                unresolved.append(
                    {
                        "from": relative,
                        "command": dependency["command"],
                        "target": dependency["target"].strip(),
                        "reason": reason,
                        "required": dependency["required"],
                    }
                )

    out.mkdir(parents=True, exist_ok=True)
    files = sorted(discovered.values(), key=lambda item: item["path"])
    aggregate = hashlib.sha256()
    for item in files:
        aggregate.update(item["path"].encode("utf-8"))
        aggregate.update(bytes.fromhex(item["sha256"]))
        src = source / item["path"]
        dst = out / item["path"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src.resolve(), dst)
        if sha256(dst) != item["sha256"]:
            raise SystemExit(
                f"source changed while preparing bundle: {item['path']}"
            )

    manifest = {
        "schema_version": "ensemble-paper-audit-app.source-manifest.v1",
        "root_tex": root.relative_to(source).as_posix(),
        "root_selection": root_selection,
        "aggregate_sha256": aggregate.hexdigest(),
        "limits": {"max_files": args.max_files, "max_bytes": args.max_bytes},
        "total_bytes": total_bytes,
        "parser_limitations": [
            "Static dependency discovery cannot expand custom TeX macros or generated includes.",
            "Dynamic or unresolved required includes must be supplied before a full audit.",
        ],
        "files": files,
        "system_dependencies": sorted(system_dependencies),
        "unresolved": unresolved,
    }
    manifest_path = out / "source-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    required_missing = [item for item in unresolved if item["required"]]
    print(
        json.dumps(
            {
                "manifest": str(manifest_path),
                "root_tex": str(out / manifest["root_tex"]),
                "files": len(files),
                "unresolved": len(unresolved),
                "required_missing": len(required_missing),
            },
            indent=2,
        )
    )
    if required_missing:
        print("required TeX dependencies are unresolved; do not audit this prepared bundle", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
