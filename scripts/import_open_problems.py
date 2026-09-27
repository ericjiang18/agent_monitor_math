"""Build the public catalogue from pinned, attributed research collections.

Inputs are local files: this command never downloads or executes upstream code.
Install PyYAML and pypdf for an import. See docs/problem-sources.md for provenance.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

try:
    from scripts.problem_expansion import all_records
    from scripts.curate_open_problems import curate, CURATION
except ModuleNotFoundError:
    from problem_expansion import all_records
    from curate_open_problems import curate, CURATION

ROOT = Path(__file__).resolve().parents[1]
ERDOS_COMMIT = "3c68e941162f81d650fc886eed34e58bed3a6a01"
CATALOG_COMMIT = "357bcb1a1daf93917d42e8206ceaa55645729a09"
NOTEBOOK_URL = "https://kourovkanotebookorg.wordpress.com/wp-content/uploads/2026/09/21tkt.pdf"
SNAPSHOT_DATE = "2026-09-10"
ERDOS_SHA256 = "3e9ab5fb8b479274af52112aec8e7910e9bd9470feef8d4830d67a8d5fd20bcb"
NOTEBOOK_SHA256 = "2fcce9b98a4df10267fe120229217bfe556c70510704e311540da0cef438f911"

TOPICS = {
    "finite groups": r"\bfinite group",
    "infinite groups": r"\binfinite group",
    "group rings": r"\bgroup (?:ring|algebra)",
    "free groups": r"\bfree (?:\w+ )?group",
    "automorphisms": r"automorphism",
    "representations": r"representation|character(?:s|istic)?\b",
    "subgroups": r"subgroup",
    "nilpotent groups": r"nilpoten",
    "soluble groups": r"solubl|solvabl",
    "simple groups": r"simple group",
    "p-groups": r"p-group|p−group",
    "profinite groups": r"profinite",
    "orderable groups": r"orderable|order(?:ed|ing) group",
    "group presentations": r"presentation|presented|defining relation",
    "algorithmic group theory": r"algorithm|decidab|recursive|word problem",
    "geometric group theory": r"hyperbolic|quasi-isometr|growth|amenab",
    "group cohomology": r"cohomolog|homolog",
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def erdos_records(items: list[dict]) -> list[dict]:
    records = []
    for item in items:
        status = item.get("informal_status", item.get("status", {}))
        if status.get("state") != "open":
            continue
        number = int(item["number"])
        # The maintainer explicitly reports an ambiguous formulation for 1163;
        # its overlapping context is linked under 1162 instead of counted again.
        if number == 1163:
            continue
        tags = item.get("tags", [])
        records.append({
            "id": f"erdos-{number:04d}", "number": str(number),
            "title": f"Erdős problem #{number}",
            "summary": "Research question in " + ", ".join(tags[:3]) + ". Read the current statement and discussion in the original collection.",
            "source": "erdos", "source_name": "Erdős Problems",
            "source_url": f"https://www.erdosproblems.com/{number}",
            "source_locator": f"Erdős problem {number}",
            "topics": tags, "status": "source-open", "status_as_of": "2026-09-09",
            "source_status": "open", "source_status_updated": status.get("last_update"),
            "prize": item.get("prize") if item.get("prize") != "no" else None,
            "formal_status": "No proof asserted by this catalogue",
            "source_revision": ERDOS_COMMIT,
        })
    return records


def notebook_records(catalog_dir: Path, pages: list[str]) -> list[dict]:
    previous = {}
    for path in sorted(catalog_dir.glob("*.json")):
        item = json.loads(path.read_text())
        if item["status_observation"]["state"] != "listed-unsolved":
            continue
        _, section, issue, number = item["id"].split("-")
        if section == "main":
            previous[f"{int(issue)}.{int(number)}"] = item

    # Stop before the solved archive. The table of contents and prose references
    # do not match this entire-line heading. Fail closed if the heading is absent.
    archive = next((i for i, page in enumerate(pages)
                    if re.search(r"(?mi)^Archive of solved problems\s*$", page)), None)
    if archive is None:
        raise ValueError("Cannot locate the notebook's solved archive")
    pages = pages[:archive]
    text = "\n".join(pages)
    offsets, offset = [], 0
    for page in pages:
        offsets.append(offset)
        offset += len(page) + 1
    matches = list(re.finditer(r"(?m)^([∗*]?)(\d{1,2}\.\d{1,3})\.\s", text))
    records, seen = [], set()
    for index, match in enumerate(matches):
        number = match[2]
        if number not in previous or number in seen:
            continue
        seen.add(number)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.end():end]
        # The editors mark answers with an asterisk. Exclude all such entries,
        # including answered subparts; this is deliberately conservative.
        if match[1] or "∗" in block or "*" in block:
            continue
        topics = [topic for topic, pattern in TOPICS.items()
                  if re.search(pattern, block, flags=re.I)]
        page_number = sum(start <= match.start() for start in offsets)
        issue, problem = (int(x) for x in number.split("."))
        records.append({
            "id": f"kourovka-{issue:02d}-{problem:03d}", "number": number,
            "title": f"Kourovka problem {number}",
            "summary": "A research question from issue " + str(issue) + " of the Kourovka Notebook. "
                + ("Explore " + ", ".join(topics[:3]) + ". " if topics else "")
                + "The original statement and author credits are on page " + str(page_number) + ".",
            "source": "kourovka", "source_name": "Kourovka Notebook",
            "source_url": f"{NOTEBOOK_URL}#page={page_number}",
            "source_locator": f"Kourovka {number}, 21st edition, page {page_number}",
            "topics": ["group theory", *topics],
            "status": "source-open", "status_as_of": "2026-09-01",
            "source_status": "listed-unsolved; no answer marker in September edition",
            "prize": None, "formal_status": "No proof asserted by this catalogue",
            "source_revision": "21st edition, September 2026",
        })
    return records


def build(args: argparse.Namespace) -> dict:
    import yaml
    from pypdf import PdfReader

    for path, expected in ((args.erdos, ERDOS_SHA256), (args.notebook, NOTEBOOK_SHA256)):
        if digest(path) != expected:
            raise ValueError(f"Unexpected source revision: {path}. Review and update the pinned hashes and status dates before importing a new snapshot.")
    records = erdos_records(yaml.safe_load(args.erdos.read_text()))
    records.extend(notebook_records(
        args.catalog / "problems/extended-catalog/kourovka-notebook",
        [page.extract_text() for page in PdfReader(args.notebook).pages],
    ))
    records = merge_related_references(records)
    ids = [record["id"] for record in records]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate canonical problem identifiers")
    counts = dict(Counter(record["source"] for record in records))
    return {
        "schema_version": 1, "snapshot_date": SNAPSHOT_DATE,
        "total": len(records), "counts": counts,
        "status_note": "Source-listed open at the dates shown. This is a linked research catalogue, not an independent verification of current open status. Read the original source for the statement, updates, and author credits.",
        "content_note": "Bibliographic pointers and topic metadata; original problem statements are not reproduced. No AI-generated problem variants or solved archive entries are included.",
        "sources": [
            {"id": "erdos", "name": "Erdős Problems", "url": "https://github.com/teorth/erdosproblems",
             "attribution": "Thomas Bloom, Terence Tao, and the Erdős Problems contributors",
             "license": "Apache-2.0 (maintainer metadata)", "license_url": "https://github.com/teorth/erdosproblems/blob/main/LICENSE",
             "revision": ERDOS_COMMIT, "sha256": digest(args.erdos)},
            {"id": "kourovka", "name": "The Kourovka Notebook", "url": "https://kourovkanotebookorg.wordpress.com/",
             "attribution": "E. I. Khukhro and V. D. Mazurov, editors; individual problem authors credited in the source",
             "license": "Original book copyright retained by the editors; bibliographic pointers only",
             "revision": "21st edition, September 2026", "sha256": digest(args.notebook)},
            {"id": "conjecturebench", "name": "ConjectureBench", "url": "https://github.com/bespokelabsai/conjecture-bench",
             "attribution": "Anirudha Ramesh and Shreyas Pimpalgaonkar, Bespoke Labs (2026)",
             "license": "CC BY 4.0 (curation metadata)", "license_url": "https://creativecommons.org/licenses/by/4.0/",
             "revision": CATALOG_COMMIT, "modifications": "Used canonical Kourovka identifiers; rechecked against September source, excluded all answer markers, omitted upstream statement text, and added topic labels."},
        ],
        "problems": records,
    }


def merge_related_references(records: list[dict]) -> list[dict]:
    by_id = {record["id"]: record for record in records}
    # Both sources formulate the Herzog–Schönheim coset partition conjecture.
    duplicate = by_id.get("kourovka-21-093")
    primary = by_id.get("erdos-0274")
    if duplicate and primary:
        primary["additional_sources"] = [{
            "label": "Same conjecture · Kourovka 21.93",
            "url": duplicate["source_url"], "relation": "same-problem",
        }]
        records = [record for record in records if record["id"] != duplicate["id"]]
    if "erdos-1162" in by_id:
        by_id["erdos-1162"]["additional_sources"] = [{
            "label": "Related question · Erdős #1163 (formulation uncertain)",
            "url": "https://www.erdosproblems.com/1163", "relation": "related-ambiguous-formulation",
        }]
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--erdos", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--notebook", type=Path, required=True)
    parser.add_argument("--curation", type=Path, default=CURATION)
    parser.add_argument("--output", type=Path, default=ROOT / "agent_monitor/data/open_problems.json")
    parser.add_argument("--public-output", type=Path, default=ROOT / "docs/public/open-problems.json")
    args = parser.parse_args()
    data = curate(build(args), args.curation)
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n"
    for path in (args.output, args.public_output):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(encoded)
    # This existing Caddy route compresses JSON; keep the original URL compatible.
    compressed_route = args.public_output.parent / "research/open-problems.json"
    compressed_route.parent.mkdir(parents=True, exist_ok=True)
    compressed_route.write_text(encoded)
    print(json.dumps({"total": data["total"], "counts": data["counts"]}))


if __name__ == "__main__":
    main()
