"""Source adapters and transparent exploration ratings for the problem catalogue.

This module reads data, never upstream executable code. Legacy compact
difference-set records remain readable, but are excluded from new catalogues.
"""
from __future__ import annotations

from collections import Counter
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import re

DATE = "2026-09-10"
LABELS = {1: "Long-term theory", 2: "Literature first", 3: "Specialist exploration",
          4: "Concrete experiments", 5: "Focused experiments"}
RATING_METHOD = {
    "name": "Exploration fit", "version": 1, "scale": 5, "as_of": DATE,
    "author": "Ansätze · AI-assisted editorial heuristic",
    "meaning": "How concrete a starting point the available metadata offers for agent-assisted exploration. Higher means a more explicit experimental target; it does not predict a solution, mathematical difficulty, or importance.",
    "levels": [{"score": k, "label": v} for k, v in LABELS.items()],
    "rules": [
        "1: A broad Millennium problem; narrow a special case before experimenting.",
        "2: A bibliographic question requiring the original statement and literature first.",
        "3: An explicit finite target whose basic verification proxy exceeds the stated threshold.",
        "4: An explicit finite target within the verification threshold.",
        "5: Level 4 with published integer bounds one apart; a precise target, not evidence that solving it is easy.",
    ],
    "thresholds": {"linear_code_words": 100000, "constant_weight_pair_comparisons": 1000000,
                   "covering_t_subsets": 100000,
                   "ramsey_lower_bound_vertices": 60},
    "limitations": "Verification proxies ignore construction/search cost, structure, solver choice, and literature. A one-unit gap can remain exceptionally difficult. Uninspected prose problems receive 2/5, with low confidence; this is a metadata limitation, not a judgment of their value.",
}


def score(value: int, reason: str, confidence: str = "low") -> dict:
    return {"score": value, "label": LABELS[value], "reason": reason,
            "confidence": confidence, "method": "exploration-fit-v1"}


def annotate(record: dict) -> dict:
    record.setdefault("kind", "research-question")
    record.setdefault("rating", score(2, "Read the source statement and recent literature to choose an approach; bibliographic metadata alone does not establish an experimental target."))
    return record


def finite_rating(kind: str, params: dict, bounds: dict | None = None) -> dict:
    if kind == "linear-code":
        proxy = params["q"] ** params["k"]
        manageable = proxy <= 100000
        reason = f"A proposed linear code can be checked by enumerating {proxy:,} codewords."
    elif kind == "constant-weight-code":
        proxy = (bounds["lower"] + 1) * bounds["lower"] // 2
        manageable = proxy <= 1000000
        reason = f"A one-step construction has about {proxy:,} word-pair distance checks."
    elif kind == "covering-design":
        proxy = math.comb(params["v"], params["t"])
        manageable = proxy <= 100000
        reason = f"A proposed covering must cover {proxy:,} subsets of size {params['t']}."
    elif kind == "difference-set":
        manageable = params["v"] <= 1000
        reason = f"Existence in this particular group is a finite construction target. Group order {params['v']:,} {'meets' if manageable else 'exceeds'} the 1,000-element exploration threshold."
    elif kind == "ramsey-number":
        manageable = bounds["lower"] <= 60
        reason = f"A finite graph-colouring target starts near {bounds['lower']} vertices; clique verification and search can still be expensive."
    else:
        raise ValueError(f"Unknown finite problem type: {kind}")
    value = 4 if manageable else 3
    if bounds and bounds["upper"] - bounds["lower"] == 1 and manageable:
        value = 5
        reason += " The recorded integer bounds are one apart, making the next target precise."
    reason += " This estimates an exploration starting point, not the cost or likelihood of finding a proof."
    return score(value, reason, "heuristic")


def record(source: str, number: str, title: str, summary: str, params: dict,
           bounds: dict | None, url: str, topics: list[str], kind: str, date: str = DATE) -> dict:
    return {"id": f"{source}-{number}", "number": number, "title": title,
            "summary": summary, "source": source, "source_url": url,
            "source_locator": title, "topics": topics, "kind": "parameter-case",
            "parameters": params, "bounds": bounds, "rating": finite_rating(kind, params, bounds),
            "status": "source-open", "status_as_of": date,
            "source_status": "published bounds differ" if bounds else "Open in source database",
            "source_revision": f"Source snapshot observed {date}",
            "formal_status": "No proof asserted by this catalogue"}


def code_records(html: str, q: int) -> list[dict]:
    result = []
    for cell in re.findall(r"<td\b[^>]*>.*?</td>", html, re.I | re.S):
        link = re.search(r'HREF="(BKLC\.php\?[^\"]+)"', cell, re.I)
        if not link:
            continue
        params = {k: int(v) for k, v in re.findall(r"[?&](q|n|k)=(\d+)", unescape(link[1]))}
        text = unescape(re.sub(r"<[^>]+>", "", cell)).strip()
        match = re.fullmatch(r"(\d+)\s*[-–]\s*(\d+)", text)
        # Red cells have a missing construction; do not count their claimed lower bound.
        if not match or re.search(r"FFA0A0", cell, re.I):
            continue
        lo, hi = map(int, match.groups())
        if lo >= hi or set(params) != {"q", "n", "k"} or params["q"] != q:
            raise ValueError("Invalid code-table range or parameters")
        n, k = params["n"], params["k"]
        result.append(record("linear-codes", f"{q}-{n}-{k}",
            f"Linear code over GF({q}): length {n}, dimension {k}",
            f"Determine the largest possible minimum Hamming distance. The source records {lo} ≤ d ≤ {hi} for these parameters.",
            params, {"lower": lo, "upper": hi, "quantity": "minimum distance d"},
            "https://codetables.de/BKLC/" + unescape(link[1]), ["coding theory", "linear codes"], "linear-code"))
    if not result:
        raise ValueError(f"No unresolved code-table cells for GF({q})")
    return result


class TableReader(HTMLParser):
    """Read table coordinates and ignore superscript reference labels."""
    def __init__(self):
        super().__init__()
        self.tables, self.rows, self.row, self.cell = [], None, None, None
        self.section, self.sup, self.lost = None, 0, False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and re.fullmatch(r"d\d+", attrs.get("name", "")):
            self.section = int(attrs["name"][1:])
        if tag == "table": self.rows = []
        if tag == "tr": self.row = []
        if tag in {"td", "th"}: self.cell, self.lost = [], False
        if tag == "sup": self.sup += 1
        if "lost" in attrs.get("class", "").split(): self.lost = True

    def handle_endtag(self, tag):
        if tag == "sup": self.sup = max(0, self.sup - 1)
        if tag in {"td", "th"} and self.cell is not None:
            if self.row is not None: self.row.append(("".join(self.cell).strip(), self.lost))
            self.cell = None
        if tag == "tr" and self.rows is not None and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        if tag == "table" and self.rows is not None:
            self.tables.append((self.section, self.rows))
            self.rows = None

    def handle_data(self, data):
        if self.cell is not None and not self.sup: self.cell.append(data)


def constant_weight_records(html: str) -> list[dict]:
    parser = TableReader()
    parser.feed(html)
    result, seen = [], set()
    for distance, rows in parser.tables:
        if distance is None or distance < 6 or not rows or not rows[0]: continue
        if "n\\w" not in rows[0][0][0]: continue
        weights = [int(text) for text, _ in rows[0][1:]]
        for row in rows[1:]:
            if not row or not row[0][0].isdigit(): continue
            n = int(row[0][0])
            for weight, (text, lost) in zip(weights, row[1:]):
                m = re.fullmatch(r"(\d+)\s*[-–]\s*(\d+)", text)
                if not m or lost: continue
                lo, hi = map(int, m.groups())
                key = (n, distance, weight)
                if lo >= hi: raise ValueError(f"Invalid constant-weight bounds {key}")
                if key in seen: raise ValueError(f"Duplicate constant-weight cell {key}")
                seen.add(key)
                result.append(record("constant-weight-codes", f"{n}-{distance}-{weight}",
                    f"Constant-weight code A({n}, {distance}, {weight})",
                    f"Maximize the number of binary words with length {n}, weight {weight}, and pairwise distance at least {distance}. Published range: {lo:,}–{hi:,} words.",
                    {"n": n, "d": distance, "w": weight}, {"lower": lo, "upper": hi, "quantity": "code size A"},
                    f"https://www.win.tue.nl/~aeb/codes/Andw.html#d{distance}",
                    ["coding theory", "constant-weight codes"], "constant-weight-code"))
    if not result: raise ValueError("No unresolved constant-weight cells")
    return result


def difference_set_record(case: list) -> dict:
    v, k, lam, group = case
    number = "-".join(map(str, [v, k, lam, *group]))
    name = " × ".join(f"Z/{n}Z" for n in group)
    result = record("difference-sets", number,
        f"Difference set ({v}, {k}, {lam}) in {name}",
        f"Does this specific abelian group contain a {k}-element subset in which every nonzero group element occurs exactly {lam} times as an ordered difference? The archived database labels this case Open.",
        {"v": v, "k": k, "lambda": lam, "group": group}, None,
        "https://dmgordon.org/difference-sets/", ["design theory", "difference sets", "finite groups"], "difference-set")
    result["source_locator"] = f"DS({v},{k},{lam},[{','.join(map(str,group))}]) in ds.json"
    result["source_name"] = "Difference Sets"
    result["additional_sources"] = [{"label": "Maintainer data · locate the DS identifier above",
        "url": "https://raw.githubusercontent.com/dmgordo/difference-sets/main/ds.json", "relation": "source-data"}]
    result["source_status"] = "Open in archived maintainer database; literature may contain later results"
    return result


def all_records(data: dict):
    """Stream ordinary entries and compact cases without duplicating storage."""
    yield from data["problems"]
    for family in data.get("compact_families", []):
        if family["encoding"] != "difference-sets-v1": raise ValueError("Unknown family encoding")
        for case in family["cases"]:
            yield difference_set_record(case)


def expand(data: dict, inputs: Path, catalog: Path) -> dict:
    manifest = json.loads((inputs.parent / "source-hashes.json").read_text())
    # Difference Sets was removed from the catalogue at the owner's request.
    manifest = {name: value for name, value in manifest.items() if not name.startswith("difference-set")}
    for name, expected in manifest.items():
        if name.endswith("sha256"): continue
        if hashlib.sha256((inputs / name).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Source snapshot hash mismatch: {name}")
    rows = [annotate(row) for row in data["problems"]]
    for q in [2, 3, 4, 5, 7, 8, 9]: rows.extend(code_records((inputs / f"codes-{q}.html").read_text(), q))
    rows.extend(constant_weight_records((inputs / "constant-weight.html").read_text()))
    # The July covering snapshot remains dated July; its retired website is not
    # evidence of current status. Preserve the named parameters and bounds only.
    covering_path = catalog / "problems/families/ljcr-covering-designs/records.json"
    if hashlib.sha256(covering_path.read_bytes()).hexdigest() != "6510dbb16fab76edef51a1868ac37b5377c3c9914ca35a72e9485554df992685":
        raise ValueError("Covering snapshot revision changed")
    for item in json.loads(covering_path.read_text())["records"]:
        if item["status"]["state"] != "open": continue
        p, f = item["parameters"], item["frontier"]
        lo, hi = f["lower_bound"], f["best_known"]
        if lo >= hi: continue
        row = record("covering-designs", f"{p['v']}-{p['k']}-{p['t']}", item["title"],
            f"Cover every {p['t']}-element subset of a {p['v']}-element set using as few {p['k']}-element blocks as possible. July source range: {lo:,}–{hi:,} blocks. Check the successor repository for improvements.",
            p, {"lower": lo, "upper": hi, "quantity": "minimum blocks C"},
            "https://dmgordon.org/covering-designs/", ["design theory", "covering designs"], "covering-design", "2026-07-23")
        row["source_status"] = "July bounds via ConjectureBench; current status not rechecked"
        row["additional_sources"] = [{"label": "Successor repository · check current bounds", "url": "https://coveringrepository.com/", "relation": "updates"}]
        rows.append(row)
    ramsey = json.loads((inputs / "ramsey.json").read_text())
    for key, item in ramsey.items():
        m = re.fullmatch(r"ramsey-(\d+)-(\d+)", key)
        if not m: continue
        lo = (item.get("lower_bound") or {}).get("decimal_approx")
        hi = (item.get("upper_bound") or {}).get("decimal_approx")
        if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)) or lo >= hi or item.get("exact_value"):
            continue
        if int(lo) != lo or int(hi) != hi: continue
        s, t = map(int, m.groups())
        if s > t: continue
        rows.append(record("ramsey-numbers", f"{s}-{t}", f"Two-colour Ramsey number R({s}, {t})",
            f"Find the exact threshold forcing a red clique of size {s} or a blue clique of size {t}. The tracker records {int(lo):,} ≤ R({s}, {t}) ≤ {int(hi):,}.",
            {"s": s, "t": t}, {"lower": int(lo), "upper": int(hi), "quantity": "Ramsey number R"},
            f"https://leapsinbounds.org/constants/{key}/", ["graph theory", "Ramsey theory"], "ramsey-number"))
    clay = clay_records()
    clay_html = (inputs / "clay.html").read_text()
    section = re.search(r"<h2>Unsolved problems</h2>(.*?)<h2>Solved problems</h2>", clay_html, re.S)
    if not section or any(f'href="{row["source_url"]}"' not in section[1] for row in clay):
        raise ValueError("CMI's unsolved list changed; review the six problem pointers")
    rows.extend(clay)
    sources = [
        ("linear-codes", "Linear Code Tables", "LC", "https://codetables.de/", "Markus Grassl; Andries E. Brouwer and the credited contributors", "Numeric bounds and bibliographic metadata only"),
        ("constant-weight-codes", "Constant-Weight Codes", "CW", "https://www.win.tue.nl/~aeb/codes/Andw.html", "Andries E. Brouwer; BSSS, Agrell–Vardy–Zeger, and credited contributors", "Numeric bounds and bibliographic metadata only"),
        ("covering-designs", "Covering Designs", "CD", "https://dmgordon.org/covering-designs/", "Daniel M. Gordon and contributing construction authors; indexed by ConjectureBench", "Numeric bounds and bibliographic metadata only"),
        ("ramsey-numbers", "Ramsey Numbers", "RN", "https://leapsinbounds.org/", "Leaps in Bounds contributors; Stanisław Radziszowski and individual bound authors linked by the tracker", "Numeric bounds and bibliographic metadata only"),
        ("clay", "Millennium Prize Problems", "CM", "https://www.claymath.org/millennium-problems/", "Clay Mathematics Institute and the authors of the official problem descriptions", "Bibliographic pointers and original short summaries"),
    ]
    for ident, name, abbreviation, url, attribution, license_name in sources:
        data["sources"].append({"id": ident, "name": name, "abbreviation": abbreviation,
            "url": url, "attribution": attribution, "license": license_name, "revision": DATE})
    names = {s["id"]: s["name"] for s in data["sources"]}
    for row in rows: row.setdefault("source_name", names[row["source"]])
    counts = dict(Counter(row["source"] for row in rows))
    data.update(schema_version=2, problems=rows, counts=counts, total=sum(counts.values()),
        compact_families=[],
        rating_method=RATING_METHOD, source_input_hashes=manifest,
        kind_counts={"research-question": sum(r["kind"] == "research-question" for r in rows),
                     "parameter-case": sum(r["kind"] == "parameter-case" for r in rows)})
    data["content_note"] = "General research questions and explicitly published finite parameter cases are counted separately. Parameter cases in the same family are not independent general conjectures. Source labels are dated observations; later literature can differ."
    next(source for source in data["sources"] if source["id"] == "covering-designs")["revision"] = "July 23, 2026 snapshot via ConjectureBench"
    return data


def clay_records() -> list[dict]:
    items = [
        ("birch-and-swinnerton-dyer-conjecture", "Birch and Swinnerton-Dyer conjecture", ["number theory", "elliptic curves"], "Relate the rank of an elliptic curve over the rationals to its L-function at s = 1."),
        ("hodge-conjecture", "Hodge conjecture", ["algebraic geometry", "topology"], "Determine which rational Hodge classes on smooth complex projective varieties arise from algebraic cycles."),
        ("navier-stokes-equation", "Navier–Stokes existence and smoothness", ["analysis", "partial differential equations"], "Resolve global existence and smoothness, or breakdown, for the three-dimensional incompressible fluid equations in the official formulation."),
        ("p-vs-np", "P versus NP", ["computer science", "complexity theory"], "Determine whether every decision problem with efficiently checkable certificates also has an efficient solving algorithm."),
        ("riemann-hypothesis", "Riemann hypothesis", ["number theory", "analysis"], "Determine whether every nontrivial zero of the Riemann zeta function has real part one half."),
        ("yang-mills-the-maths-gap", "Yang–Mills existence and mass gap", ["mathematical physics", "quantum field theory"], "Construct the four-dimensional quantum Yang–Mills theory required by the official problem and establish a positive mass gap."),
    ]
    return [{"id": f"clay-{slug}", "number": slug, "title": title, "summary": summary,
             "source": "clay", "source_url": f"https://www.claymath.org/millennium/{slug}/",
             "source_locator": title, "topics": topics, "kind": "research-question", "prize": "$1,000,000",
             "status": "source-open", "status_as_of": DATE, "source_status": "CMI unsolved problems list",
             "source_revision": f"CMI page observed {DATE}", "formal_status": "No proof asserted by this catalogue",
             "rating": score(1, "A broad foundational problem. Start with a sharply defined special case and specialist literature; the full problem is a long-term research direction.")}
            for slug, title, topics, summary in items]
