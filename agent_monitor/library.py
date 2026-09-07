"""User library: persistent memory / skills / tools shared across all engines.

Items live in data/library/library.json and are injected into every run:
- a "USER LIBRARY" block is prepended to the problem prompt;
- files are materialized into the run workspace under _library/
  (MEMORY.md, SKILLS.md, tools/<name>.sh, tools.json) so agents with shell or
  file tools can read and execute them.
"""
from __future__ import annotations

import json
import shlex
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from agent_monitor import DATA_DIR

LIBRARY_DIR = DATA_DIR / "library"
LIBRARY_FILE = LIBRARY_DIR / "library.json"
SEED_MARKER = LIBRARY_DIR / ".seeded"

VALID_TYPES = ("memory", "skill", "tool")

_LOCK = threading.Lock()

_PROOF_SANITY_MARKER_CHECK_V1 = r"""if grep -nEi '\b(TODO|FIXME|sorry|admit)\b' "$target"; then
  echo "WARNING: unfinished proof markers found above."
else
  echo "OK: no TODO/FIXME/sorry/admit markers."
fi"""

_PROOF_SANITY_MARKER_CHECK_V2 = 'python3 - "$target" <<\'PY\'\nimport re\nimport sys\nfrom pathlib import Path\n\nlines = Path(sys.argv[1]).read_text(encoding="utf-8").splitlines()\nunfinished = []\nlean_fence = False\nfor number, line in enumerate(lines, 1):\n    stripped = line.strip()\n    fence = re.match(r"^```\\s*([A-Za-z0-9_+-]*)", stripped)\n    if fence:\n        if lean_fence:\n            lean_fence = False\n        elif fence.group(1).lower() in {"lean", "lean4"}:\n            lean_fence = True\n        continue\n    todo = re.search(r"\\b(?:TODO|FIXME)\\b", line, re.IGNORECASE)\n    lean_gap = re.search(r"\\b(?:sorry|admit)\\b", line) if lean_fence else None\n    if not lean_gap:\n        lean_gap = re.search(\n            r"(?:^\\s*(?:sorry|admit)\\b|\\b(?:by|exact)\\s+(?:sorry|admit)\\b|:=\\s*(?:sorry|admit)\\b)",\n            line,\n        )\n    if todo or lean_gap:\n        unfinished.append((number, line))\n\nif unfinished:\n    for number, line in unfinished:\n        print(f"{number}:{line}")\n    print("WARNING: unfinished proof markers found above.")\nelse:\n    print("OK: no executable TODO/FIXME/sorry/admit markers.")\nPY'

_LEAN_MATHLIB_PROJECT_CHECK_V1 = '[[ -n "${AGENT_MONITOR_MATHLIB_PROJECT:-}" && -f "${AGENT_MONITOR_MATHLIB_PROJECT}/lakefile.toml" ]]'
_LEAN_MATHLIB_PROJECT_CHECK_V2 = '[[ -n "${AGENT_MONITOR_MATHLIB_PROJECT:-}" && ( -f "${AGENT_MONITOR_MATHLIB_PROJECT}/lakefile.toml" || -f "${AGENT_MONITOR_MATHLIB_PROJECT}/lakefile.lean" ) ]]'

_CITATION_AUDIT_STRUCTURE_V1 = 'refs=$(grep -oE "\\[[0-9]+\\]" "$resolved" | sort -u | tr "\\n" " " || true)\necho "Numeric citations: ${refs:-none}"\nif grep -qEi "^#{1,6}[[:space:]]+(References|Bibliography)\\b|\\\\begin\\{thebibliography\\}" "$resolved"; then echo "OK: bibliography section found."; else echo "NOTE: no bibliography section found."; fi'
_CITATION_AUDIT_STRUCTURE_V2 = 'python3 - "$resolved" <<\'PY\'\nimport re\nimport sys\nfrom pathlib import Path\n\ntext = Path(sys.argv[1]).read_text(encoding="utf-8")\nnumeric = sorted(set(re.findall(r"\\[[0-9]+\\]", text)))\nprint("Numeric citations: " + (" ".join(numeric) if numeric else "none"))\nhas_markdown = bool(re.search(r"^#{1,6}\\s+(?:References|Bibliography)\\b", text, re.I | re.M))\nhas_latex = r"\\begin{thebibliography}" in text\nprint("OK: bibliography section found." if has_markdown or has_latex else "NOTE: no bibliography section found.")\ncite_groups = re.findall(r"\\\\cite[a-zA-Z]*(?:\\[[^]]*\\])*\\{([^}]*)\\}", text)\ncites = {key.strip() for group in cite_groups for key in group.split(",") if key.strip()}\nbibitems = set(re.findall(r"\\\\bibitem(?:\\[[^]]*\\])?\\{([^}]*)\\}", text))\nif cites:\n    print("LaTeX citation keys: " + " ".join(sorted(cites)))\n    missing = sorted(cites - bibitems)\n    if missing:\n        print("ERROR: citation keys without matching bibitems: " + " ".join(missing))\n        raise SystemExit(1)\n    print("OK: all LaTeX citation keys resolve to bibitems.")\nPY'

_STARTER_TOOL = {
    "id": "tool_proof_sanity_check",
    "type": "tool",
    "name": "proof-sanity-check",
    "description": "Read-only preflight for proof files: size, unfinished markers, references and citations.",
    "content": """#!/usr/bin/env bash
set -euo pipefail
target=\"${1:-proof.md}\"
root=\"$(realpath .)\"
resolved=\"$(realpath -m -- \"$target\")\"
case \"$resolved\" in \"$root\"|\"$root\"/*) ;; *) echo \"ERROR: path must stay inside the run workspace\" >&2; exit 2;; esac
target=\"$resolved\"
if [[ ! -f \"$target\" ]]; then
  echo \"ERROR: file not found: $target\" >&2
  exit 1
fi
if [[ ! -s \"$target\" ]]; then
  echo \"ERROR: proof file is empty: $target\" >&2
  exit 1
fi
echo \"Proof preflight: $target ($(wc -c < \"$target\") bytes, $(wc -l < \"$target\") lines)\"
python3 - \"$target\" <<'PY'
import sys
from pathlib import Path

data = Path(sys.argv[1]).read_bytes()
bad = [(offset, byte) for offset, byte in enumerate(data) if byte < 32 and byte not in {9, 10, 13}]
bad.extend((offset, byte) for offset, byte in enumerate(data) if byte == 127)
if bad:
    sample = \", \".join(f\"offset {offset}: 0x{byte:02x}\" for offset, byte in bad[:12])
    print(f\"ERROR: disallowed ASCII control bytes in proof artifact ({sample})\")
    raise SystemExit(1)
print(\"OK: no disallowed ASCII control bytes.\")
PY
__PROOF_SANITY_MARKER_CHECK__
if grep -qEi '^#{1,6}[[:space:]]+(References|Bibliography)\\b' \"$target\"; then
  echo \"OK: references section found.\"
else
  echo \"NOTE: no References/Bibliography heading found.\"
fi
citations=$(grep -oE '\\[[0-9]+\\]' \"$target\" | sort -u | tr '\\n' ' ' || true)
echo \"Numeric citations: ${citations:-none}\"
""",
    "enabled": True,
    "tags": ["proof", "audit", "read-only"],
    "source": "starter",
    "read_only": True,
    "approval_mode": "auto",
    "timeout_seconds": 20,
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative proof path", "default": "proof.md"}
        },
        "additionalProperties": False,
    },
}

_STARTER_TOOL["content"] = _STARTER_TOOL["content"].replace(
    "__PROOF_SANITY_MARKER_CHECK__", _PROOF_SANITY_MARKER_CHECK_V2
)


_LEAN_CHECK_TOOL = {
    "id": "tool_lean_check",
    "type": "tool",
    "name": "lean-check",
    "description": "Compile a workspace-relative Lean file with the server toolchain and report sorry/admit markers.",
    "content": """#!/usr/bin/env bash
set -euo pipefail
target="${1:-lean/Proof.lean}"
root="$(realpath .)"
resolved="$(realpath -m -- "$target")"
case "$resolved" in "$root"|"$root"/*) ;; *) echo "ERROR: path must stay inside the run workspace" >&2; exit 2;; esac
[[ -f "$resolved" ]] || { echo "ERROR: file not found: $target" >&2; exit 1; }
command -v lean >/dev/null || { echo "ERROR: Lean is not installed" >&2; exit 127; }
if grep -nE "(^|[^[:alnum:]_])(sorry|admit)([^[:alnum:]_]|$)" "$resolved"; then echo "WARNING: unfinished Lean markers found above"; fi
if [[ -n "${AGENT_MONITOR_MATHLIB_PROJECT:-}" && ( -f "${AGENT_MONITOR_MATHLIB_PROJECT}/lakefile.toml" || -f "${AGENT_MONITOR_MATHLIB_PROJECT}/lakefile.lean" ) ]]; then
  (cd "$AGENT_MONITOR_MATHLIB_PROJECT" && timeout 90 lake env lean "$resolved")
else
  timeout 90 lean "$resolved"
fi
""",
    "enabled": True,
    "tags": ["lean", "verification", "read-only"],
    "source": "starter",
    "read_only": True,
    "approval_mode": "auto",
    "timeout_seconds": 100,
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative Lean source", "default": "lean/Proof.lean"}
        },
        "additionalProperties": False,
    },
}

_CITATION_AUDIT_TOOL = {
    "id": "tool_citation_audit",
    "type": "tool",
    "name": "citation-audit",
    "description": "Read-only audit for citation placeholders, numeric citations, bibliography entries and unverifiable-source warnings.",
    "content": r"""#!/usr/bin/env bash
set -euo pipefail
target="${1:-proof.md}"
root="$(realpath .)"
resolved="$(realpath -m -- "$target")"
case "$resolved" in "$root"|"$root"/*) ;; *) echo "ERROR: path must stay inside the run workspace" >&2; exit 2;; esac
[[ -f "$resolved" ]] || { echo "ERROR: file not found: $target" >&2; exit 1; }
echo "Citation audit: $target"
grep -nEi "citation needed|source needed|verify source|\[\?\]|TODO.*cit" "$resolved" || echo "OK: no citation placeholders found."
python3 - "$resolved" <<'PY'
import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
numeric = sorted(set(re.findall(r"\[[0-9]+\]", text)))
print("Numeric citations: " + (" ".join(numeric) if numeric else "none"))
has_markdown = bool(re.search(r"^#{1,6}\s+(?:References|Bibliography)\b", text, re.I | re.M))
has_latex = r"\begin{thebibliography}" in text
print("OK: bibliography section found." if has_markdown or has_latex else "NOTE: no bibliography section found.")
cite_groups = re.findall(r"\\cite[a-zA-Z]*(?:\[[^]]*\])*\{([^}]*)\}", text)
cites = {key.strip() for group in cite_groups for key in group.split(",") if key.strip()}
bibitems = set(re.findall(r"\\bibitem(?:\[[^]]*\])?\{([^}]*)\}", text))
if cites:
    print("LaTeX citation keys: " + " ".join(sorted(cites)))
    missing = sorted(cites - bibitems)
    if missing:
        print("ERROR: citation keys without matching bibitems: " + " ".join(missing))
        raise SystemExit(1)
    print("OK: all LaTeX citation keys resolve to bibitems.")
PY
""",
    "enabled": True,
    "tags": ["citation", "audit", "read-only"],
    "source": "starter",
    "read_only": True,
    "approval_mode": "auto",
    "timeout_seconds": 20,
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative proof path", "default": "proof.md"}
        },
        "additionalProperties": False,
    },
}

_SOURCE_FETCH_ALLOWLIST_V1 = '''    "tandfonline.com", "ams.org",'''
_SOURCE_FETCH_ALLOWLIST_V2 = '''    "tandfonline.com", "ams.org", "raw.githubusercontent.com",'''
_SOURCE_FETCH_ENTRY_V1 = '''current = validate(sys.argv[1] if len(sys.argv) > 1 else "")'''
_SOURCE_FETCH_ENTRY_V2 = '''def normalize(raw):
    parsed = urlsplit(raw)
    host = (parsed.hostname or "").lower().rstrip(".")
    parts = [part for part in parsed.path.split("/") if part]
    # Support only the public FormalConjectures repository.  Other GitHub
    # pages remain denied; this is a narrow source adapter, not a general web
    # fetch escape hatch.  A one-segment ref covers the canonical `main` URLs.
    if (
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and parsed.port in (None, 443)
        and host == "github.com"
        and len(parts) >= 5
        and parts[:3] == ["google-deepmind", "formal-conjectures", "blob"]
        and parts[3] not in {".", ".."}
        and all(part not in {".", ".."} for part in parts[4:])
    ):
        return "https://raw.githubusercontent.com/" + "/".join(
            [parts[0], parts[1], parts[3], *parts[4:]]
        )
    return raw

current = validate(normalize(sys.argv[1] if len(sys.argv) > 1 else ""))'''
_SOURCE_FETCH_RAW_GUARD_V1 = '''    if parsed.port not in (None, 443):
        raise SystemExit("ERROR: only HTTPS port 443 is allowed")
    if not any(host == domain or host.endswith("." + domain) for domain in ALLOWED):'''
_SOURCE_FETCH_RAW_GUARD_V2 = '''    if parsed.port not in (None, 443):
        raise SystemExit("ERROR: only HTTPS port 443 is allowed")
    if host == "raw.githubusercontent.com":
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 4 or parts[:2] != ["google-deepmind", "formal-conjectures"]:
            raise SystemExit("ERROR: raw source repository is not allowlisted")
    if not any(host == domain or host.endswith("." + domain) for domain in ALLOWED):'''


_SOURCE_FETCH_TOOL = {
    "id": "tool_primary_source_fetch",
    "type": "tool",
    "name": "primary-source-fetch",
    "description": "Fetch bounded text from allowlisted HTTPS mathematics sources for existence and entailment checks.",
    "content": r'''#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  printf '%s\n' \
    'Usage: primary-source-fetch.sh HTTPS_URL' \
    'Fetch bounded text from an allowlisted HTTPS mathematics source.'
  exit 0
fi
url="${1:-}"
python3 - "$url" <<'PY'
import html
import sys
from html.parser import HTMLParser
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ALLOWED = (
    "erdosproblems.com", "arxiv.org", "doi.org", "oeis.org",
    "dartmouth.edu", "combinatorica.hu", "cambridge.org",
    "tandfonline.com", "ams.org",
)
MAX_BYTES = 160_000

def validate(raw):
    parsed = urlsplit(raw)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise SystemExit("ERROR: only credential-free HTTPS URLs are allowed")
    if parsed.port not in (None, 443):
        raise SystemExit("ERROR: only HTTPS port 443 is allowed")
    if not any(host == domain or host.endswith("." + domain) for domain in ALLOWED):
        raise SystemExit("ERROR: source host is not allowlisted")
    return raw

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

class Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0
    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}: self.skip += 1
    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self.skip: self.skip -= 1
    def handle_data(self, data):
        if not self.skip and data.strip(): self.parts.append(data.strip())

current = validate(sys.argv[1] if len(sys.argv) > 1 else "")
opener = build_opener(NoRedirect)
for _ in range(4):
    req = Request(current, headers={"User-Agent": "ProvingConsole-SourceAudit/1.0"})
    try:
        response = opener.open(req, timeout=20)
        break
    except HTTPError as exc:
        if 300 <= exc.code < 400 and exc.headers.get("Location"):
            current = validate(urljoin(current, exc.headers["Location"]))
            continue
        raise SystemExit(f"ERROR: HTTP {exc.code}")
else:
    raise SystemExit("ERROR: too many redirects")

kind = response.headers.get_content_type()
data = response.read(MAX_BYTES + 1)
truncated = len(data) > MAX_BYTES
data = data[:MAX_BYTES]
charset = response.headers.get_content_charset() or "utf-8"
print(f"SOURCE_URL {response.geturl()}")
print(f"CONTENT_TYPE {kind}")
print(f"TRUNCATED {str(truncated).lower()}")
if kind not in {"text/html", "text/plain", "application/json", "application/xml", "text/xml"}:
    raise SystemExit("ERROR: source is not a supported text response")
text = data.decode(charset, errors="replace")
if kind == "text/html":
    parser = Text(); parser.feed(text); text = "\n".join(parser.parts)
print(html.unescape(text))
PY
''',
    "enabled": True,
    "tags": ["citation", "source", "research", "read-only"],
    "source": "starter",
    "read_only": True,
    "approval_mode": "auto",
    "timeout_seconds": 30,
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Allowlisted HTTPS source URL (Erdos Problems, arXiv, DOI, OEIS, or listed publisher)",
            }
        },
        "required": ["url"],
        "additionalProperties": False,
    },
}
_SOURCE_FETCH_TOOL["content"] = _SOURCE_FETCH_TOOL["content"].replace(
    _SOURCE_FETCH_ALLOWLIST_V1, _SOURCE_FETCH_ALLOWLIST_V2, 1
).replace(_SOURCE_FETCH_ENTRY_V1, _SOURCE_FETCH_ENTRY_V2, 1).replace(
    _SOURCE_FETCH_RAW_GUARD_V1, _SOURCE_FETCH_RAW_GUARD_V2, 1
)

_LITERATURE_SEARCH_TOOL = {
    "id": "tool_literature_search",
    "type": "tool",
    "name": "literature-search",
    "description": "Search arXiv and Crossref with a bounded query/date range and emit a reproducible hit log for research-status audits.",
    "content": r'''#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  printf '%s\n' \
    'Usage: literature-search.sh QUERY [FROM_DATE] [TO_DATE] [LIMIT]' \
    '  QUERY      1-240 characters; quote exact phrases with double quotes' \
    '  FROM_DATE  inclusive YYYY-MM-DD (default: 1900-01-01)' \
    '  TO_DATE    inclusive YYYY-MM-DD (default: 9999-12-31)' \
    '  LIMIT      1-20 results per index (default: 10)'
  exit 0
fi
query="${1:-}"
from_date="${2:-1900-01-01}"
to_date="${3:-9999-12-31}"
limit="${4:-10}"
python3 - "$query" "$from_date" "$to_date" "$limit" <<'PY'
import json
import os
import re
import sys
import unicodedata
import xml.etree.ElementTree as ET
from datetime import date
from time import sleep
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

query, from_date, to_date, raw_limit = sys.argv[1:5]
if not query.strip() or len(query) > 240:
    raise SystemExit("ERROR: query must contain 1-240 characters")
try:
    start = date.fromisoformat(from_date)
    end = date.fromisoformat(to_date)
except ValueError:
    raise SystemExit("ERROR: dates must use YYYY-MM-DD")
if start > end:
    raise SystemExit("ERROR: from_date must not exceed to_date")
try:
    limit = max(1, min(int(raw_limit), 20))
except ValueError:
    raise SystemExit("ERROR: limit must be an integer")

def clean(value):
    return " ".join(str(value or "").split())

def fetch(url):
    last_error = None
    for attempt in range(3):
        req = Request(url, headers={"User-Agent": "ProvingConsole-LiteratureSearch/1.1"})
        try:
            with urlopen(req, timeout=8) as response:
                return response.read(1_500_000)
        except HTTPError as exc:
            last_error = exc
            if exc.code not in {429, 502, 503, 504} or attempt == 2:
                raise
            retry_after = exc.headers.get("Retry-After", "").strip()
            try:
                wait_s = float(retry_after)
            except ValueError:
                wait_s = float(2 ** attempt)
            wait_s = max(0.5, min(wait_s, 8.0))
            print(f"INDEX_RETRY HTTP_{exc.code} attempt={attempt + 2}/3 wait_s={wait_s:g}")
            sleep(wait_s)
    raise last_error

print(f"SEARCH_QUERY {query}")
print(f"DATE_RANGE {start.isoformat()} {end.isoformat()}")
print(f"RESULT_LIMIT {limit}")

successes = 0
ascii_query = unicodedata.normalize("NFKD", query).encode("ascii", "ignore").decode("ascii")
ascii_query = clean(ascii_query)
arxiv_ids = list(dict.fromkeys(re.findall(
    r"(?:arxiv\s*:\s*)?(\d{4}\.\d{4,5}(?:v\d+)?)", ascii_query, flags=re.IGNORECASE
)))[:5]
if arxiv_ids:
    arxiv_query = " OR ".join(f"id:{identifier}" for identifier in arxiv_ids)
    arxiv_params = {
        "id_list": ",".join(arxiv_ids),
        "start": 0,
        "max_results": min(50, max(10, limit * 5)),
    }
else:
    raw_phrases = re.findall(r'"([^"]+)"', ascii_query)[:6]
    phrases = []
    for raw_phrase in raw_phrases:
        phrase = " ".join(re.findall(r"[A-Za-z0-9]+(?:[-+][A-Za-z0-9]+)*", raw_phrase))
        if phrase and phrase not in phrases:
            phrases.append(phrase)
    residual = re.sub(r'"[^"]*"', " ", ascii_query)
    terms = re.findall(r"[A-Za-z0-9]+", residual)[:max(0, 12 - len(phrases))]
    clauses = [f'all:"{phrase}"' for phrase in phrases]
    clauses.extend(f"all:{term}" for term in terms)
    arxiv_query = " AND ".join(clauses)
    arxiv_params = {
        "search_query": arxiv_query or "all:mathematics",
        "start": 0,
        "max_results": min(50, max(10, limit * 5)),
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
print(f"ARXIV_NORMALIZED_TEXT {ascii_query}")
print(f"ARXIV_PARSED_QUERY {arxiv_query or 'all:mathematics'}")
arxiv_url = "https://export.arxiv.org/api/query?" + urlencode(arxiv_params)
print(f"ARXIV_ENDPOINT {arxiv_url}")
if os.environ.get("LITERATURE_SEARCH_DRY_RUN", "").strip().lower() in {"1", "true", "yes"}:
    print("DRY_RUN true")
    raise SystemExit(0)
try:
    root = ET.fromstring(fetch(arxiv_url))
    ns = {"a": "http://www.w3.org/2005/Atom", "o": "http://a9.com/-/spec/opensearch/1.1/"}
    total = clean(root.findtext("o:totalResults", default="0", namespaces=ns))
    entries = []
    for entry in root.findall("a:entry", ns):
        published = clean(entry.findtext("a:published", default="", namespaces=ns))[:10]
        try:
            published_date = date.fromisoformat(published)
        except ValueError:
            continue
        if not start <= published_date <= end:
            continue
        entries.append({
            "id": clean(entry.findtext("a:id", default="", namespaces=ns)).replace("http://", "https://"),
            "published": published,
            "updated": clean(entry.findtext("a:updated", default="", namespaces=ns))[:10],
            "title": clean(entry.findtext("a:title", default="", namespaces=ns)),
            "authors": [clean(a.findtext("a:name", default="", namespaces=ns)) for a in entry.findall("a:author", ns)],
            "summary": clean(entry.findtext("a:summary", default="", namespaces=ns))[:700],
        })
    print(f"ARXIV_TOTAL_RESULTS {total}")
    print(f"ARXIV_DATE_FILTERED_HITS {len(entries)}")
    for index, item in enumerate(entries[:limit], 1):
        print("ARXIV_HIT", index, json.dumps(item, ensure_ascii=False, sort_keys=True))
    successes += 1
except Exception as exc:
    print(f"ARXIV_ERROR {type(exc).__name__}: {exc}")

crossref_params = {
    "query.bibliographic": query,
    "filter": f"from-pub-date:{start.isoformat()},until-pub-date:{end.isoformat()}",
    "rows": limit,
    "select": "DOI,title,author,published,type,publisher,URL",
}
crossref_url = "https://api.crossref.org/works?" + urlencode(crossref_params)
print(f"CROSSREF_ENDPOINT {crossref_url}")
try:
    message = json.loads(fetch(crossref_url).decode("utf-8", errors="replace"))["message"]
    items = list(message.get("items") or [])
    print(f"CROSSREF_TOTAL_RESULTS {message.get('total-results', 0)}")
    print(f"CROSSREF_RETURNED_HITS {len(items)}")
    for index, item in enumerate(items[:limit], 1):
        parts = (((item.get("published") or {}).get("date-parts") or [[]])[0])
        published = "-".join(str(v) for v in parts)
        authors = [clean(" ".join((a.get("given", ""), a.get("family", "")))) for a in item.get("author") or []]
        payload = {
            "doi": clean(item.get("DOI")), "url": clean(item.get("URL")),
            "published": published, "title": clean((item.get("title") or [""])[0]),
            "authors": authors, "type": clean(item.get("type")),
            "publisher": clean(item.get("publisher")),
        }
        print("CROSSREF_HIT", index, json.dumps(payload, ensure_ascii=False, sort_keys=True))
    successes += 1
except Exception as exc:
    print(f"CROSSREF_ERROR {type(exc).__name__}: {exc}")

if successes == 0:
    raise SystemExit("ERROR: all literature indexes failed")
PY
''',
    "enabled": True,
    "tags": ["citation", "literature", "research", "read-only"],
    "source": "starter",
    "read_only": True,
    "approval_mode": "auto",
    "timeout_seconds": 60,
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Distinctive title, object, theorem, or author keywords"},
            "from_date": {"type": "string", "description": "Inclusive YYYY-MM-DD lower date", "default": "1900-01-01"},
            "to_date": {"type": "string", "description": "Inclusive YYYY-MM-DD upper date", "default": "9999-12-31"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}


_PDF_TEXT_EXTRACT_TOOL = {
    "id": "tool_pdf_text_extract",
    "type": "tool",
    "name": "pdf-text-extract",
    "description": "Extract bounded text from selected pages of a workspace-local PDF using the server's safe PDF parser.",
    "content": r'''#!/usr/bin/env bash
set -euo pipefail
target="${1:-}"
start_page="${2:-1}"
end_page="${3:-0}"
max_chars="${4:-120000}"
root="$(realpath .)"
resolved="$(realpath -m -- "$target")"
case "$resolved" in "$root"|"$root"/*) ;; *) echo "ERROR: path must stay inside the run workspace" >&2; exit 2;; esac
[[ -f "$resolved" ]] || { echo "ERROR: PDF file not found: $target" >&2; exit 1; }
"${AGENT_MONITOR_PYTHON:-python3}" - "$resolved" "$start_page" "$end_page" "$max_chars" <<'PY'
import sys
from pathlib import Path

try:
    import pymupdf
except ImportError as exc:
    raise SystemExit("ERROR: server PDF parser is unavailable") from exc

path = Path(sys.argv[1])
if path.stat().st_size > 25 * 1024 * 1024:
    raise SystemExit("ERROR: PDF exceeds the 25 MiB safety limit")
try:
    start_page = int(sys.argv[2])
    end_page = int(sys.argv[3])
    max_chars = int(sys.argv[4])
except ValueError:
    raise SystemExit("ERROR: page numbers and max_chars must be integers")
if start_page < 1 or end_page < 0:
    raise SystemExit("ERROR: start_page must be >= 1 and end_page must be 0 or greater")
if not 1_000 <= max_chars <= 200_000:
    raise SystemExit("ERROR: max_chars must be between 1000 and 200000")

try:
    document = pymupdf.open(path)
except Exception as exc:
    raise SystemExit(f"ERROR: cannot open PDF: {type(exc).__name__}: {exc}") from exc
with document:
    pages = document.page_count
    if pages < 1:
        raise SystemExit("ERROR: PDF has no pages")
    last = pages if end_page == 0 else end_page
    if start_page > pages or last < start_page or last > pages:
        raise SystemExit(f"ERROR: requested page range is outside 1-{pages}")
    parts = []
    used = 0
    truncated = False
    for page_number in range(start_page, last + 1):
        text = document.load_page(page_number - 1).get_text("text")
        block = f"\n===== PAGE {page_number} =====\n{text}"
        remaining = max_chars - used
        if len(block) > remaining:
            parts.append(block[:remaining])
            truncated = True
            break
        parts.append(block)
        used += len(block)
    print(f"PDF_PATH {path.name}")
    print(f"PDF_PAGES {pages}")
    print(f"PAGE_RANGE {start_page}-{last}")
    print(f"TRUNCATED {str(truncated).lower()}")
    print("".join(parts))
PY
''',
    "enabled": True,
    "tags": ["pdf", "citation", "research", "read-only"],
    "source": "starter",
    "read_only": True,
    "approval_mode": "auto",
    "timeout_seconds": 60,
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative PDF path"},
            "start_page": {"type": "integer", "minimum": 1, "default": 1},
            "end_page": {"type": "integer", "minimum": 0, "default": 0, "description": "Inclusive last page; 0 means the final page"},
            "max_chars": {"type": "integer", "minimum": 1000, "maximum": 200000, "default": 120000},
        },
        "required": ["path"],
        "additionalProperties": False,
    },
}


_EXACT_MATH_CERTIFICATE_TOOL = {
    "id": "tool_exact_math_certificate",
    "type": "tool",
    "name": "exact-math-certificate",
    "description": "Evaluate bounded integer/rational identities and inequalities exactly and emit a reproducible SHA-256 certificate.",
    "content": r"""#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  echo 'Usage: exact-math-certificate.sh JSON_OR_@FILE  (reads JSON from stdin when omitted)'
  exit 0
fi
exec "$AGENT_MONITOR_PYTHON" -m agent_monitor.proof_tools exact-math-certificate "${1:-}"
""",
    "enabled": True,
    "tags": ["math", "exact", "certificate", "read-only"],
    "source": "starter",
    "read_only": True,
    "approval_mode": "auto",
    "timeout_seconds": 20,
    "input_schema": {
        "type": "object",
        "properties": {
            "expressions": {
                "type": "array",
                "minItems": 1,
                "maxItems": 50,
                "items": {"type": "string"},
                "description": "Exact expressions or boolean checks in the restricted arithmetic language",
            },
            "variables": {
                "type": "object",
                "additionalProperties": {"type": "integer"},
                "description": "Optional integer variable bindings",
            },
        },
        "required": ["expressions"],
        "additionalProperties": False,
    },
    "example_input": {
        "expressions": [
            "16*5**2 + 112*5 + 120 == 1080",
            "4455*4480 == 19958400",
            "binomial(12, 4)",
        ]
    },
}

_BOUNDED_COUNTEREXAMPLE_TOOL = {
    "id": "tool_bounded_counterexample_search",
    "type": "tool",
    "name": "bounded-counterexample-search",
    "description": "Exhaust a declared finite Cartesian range against an exact predicate and return checked witnesses and scope certificate.",
    "content": r"""#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  echo 'Usage: bounded-counterexample-search.sh JSON_OR_@FILE  (reads JSON from stdin when omitted)'
  exit 0
fi
exec "$AGENT_MONITOR_PYTHON" -m agent_monitor.proof_tools bounded-counterexample-search "${1:-}"
""",
    "enabled": True,
    "tags": ["math", "counterexample", "finite", "certificate", "read-only"],
    "source": "starter",
    "read_only": True,
    "approval_mode": "auto",
    "timeout_seconds": 20,
    "input_schema": {
        "type": "object",
        "properties": {
            "predicate": {
                "type": "string",
                "description": "Boolean claim expected to hold for every point in the declared ranges",
            },
            "ranges": {
                "type": "array",
                "minItems": 1,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "start": {"type": "integer"},
                        "end": {"type": "integer"},
                        "step": {"type": "integer", "default": 1},
                    },
                    "required": ["name", "start", "end"],
                    "additionalProperties": False,
                },
            },
            "fixed": {"type": "object", "additionalProperties": {"type": "integer"}},
            "max_examples": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
        },
        "required": ["predicate", "ranges"],
        "additionalProperties": False,
    },
    "example_input": {
        "predicate": "divides(n, binomial(n, 2))",
        "ranges": [{"name": "n", "start": 2, "end": 30}],
        "max_examples": 8,
    },
}

_STATEMENT_FIDELITY_TOOL = {
    "id": "tool_statement_fidelity_audit",
    "type": "tool",
    "name": "statement-fidelity-audit",
    "description": "Compare original, informal and formal statements for missing quantifiers, domains, constants, relations and unfinished Lean markers.",
    "content": r"""#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  echo 'Usage: statement-fidelity-audit.sh JSON_OR_@FILE  (reads JSON from stdin when omitted)'
  exit 0
fi
exec "$AGENT_MONITOR_PYTHON" -m agent_monitor.proof_tools statement-fidelity-audit "${1:-}"
""",
    "enabled": True,
    "tags": ["proof", "lean", "fidelity", "audit", "read-only"],
    "source": "starter",
    "read_only": True,
    "approval_mode": "auto",
    "timeout_seconds": 20,
    "input_schema": {
        "type": "object",
        "properties": {
            "original": {"type": "string", "description": "Exact original mathematical statement"},
            "candidate": {"type": "string", "description": "Informal restatement or claimed result"},
            "formal": {"type": "string", "description": "Optional Lean declaration or formal statement"},
        },
        "required": ["original", "candidate"],
        "additionalProperties": False,
    },
    "example_input": {
        "original": "For every positive natural number n, there exists an integer k with k > n.",
        "candidate": "For every integer n, choose k = n + 1.",
        "formal": "theorem main (n : Int) : exists k : Int, k > n := by sorry",
    },
}

_AUDIT_OUTPUT_VALIDATOR_TOOL = {
    "id": "tool_audit_output_validator",
    "type": "tool",
    "name": "audit-output-validator",
    "description": "Validate a raw verifier-output.v1 audit object (or a {document: object} envelope) against the locked schema, severity order, IDs, evidence references and semantic invariants.",
    "content": r"""#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  echo 'Usage: audit-output-validator.sh JSON_OR_@FILE  (reads JSON from stdin when omitted)'
  exit 0
fi
exec "$AGENT_MONITOR_PYTHON" -m agent_monitor.proof_tools audit-output-validator "${1:-}"
""",
    "enabled": True,
    "tags": ["proof", "audit", "schema", "validator", "read-only"],
    "source": "starter",
    "read_only": True,
    "approval_mode": "auto",
    "timeout_seconds": 20,
    "input_schema": {
        "type": "object",
        "properties": {
            "document": {
                "type": "object",
                "description": "Decoded verifier-output.v1 JSON object",
            }
        },
        "required": ["document"],
        "additionalProperties": False,
    },
    "example_input": {
        "document": {
            "verdict": "accept",
            "summary": "No mathematical defect found in the declared scope.",
            "findings": [],
            "coverage_notes": {
                "reviewed_regions": ["complete candidate"],
                "unreviewed_or_difficult_regions": [],
                "external_checks_not_performed": ["none requested"],
            },
        }
    },
}

_STARTER_TOOLS = (
    _STARTER_TOOL,
    _LEAN_CHECK_TOOL,
    _CITATION_AUDIT_TOOL,
    _SOURCE_FETCH_TOOL,
    _LITERATURE_SEARCH_TOOL,
    _PDF_TEXT_EXTRACT_TOOL,
    _EXACT_MATH_CERTIFICATE_TOOL,
    _BOUNDED_COUNTEREXAMPLE_TOOL,
    _STATEMENT_FIDELITY_TOOL,
    _AUDIT_OUTPUT_VALIDATOR_TOOL,
)

_STARTER_TOOL_BY_ID = {tool["id"]: tool for tool in _STARTER_TOOLS}


def _now() -> float:
    return time.time()


def _load() -> dict[str, Any]:
    if LIBRARY_FILE.exists():
        try:
            data = json.loads(LIBRARY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("items", [])
                data.setdefault("settings", {})
                data["settings"].setdefault("auto_memory", True)
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"items": [], "settings": {"auto_memory": True}}


def _save(data: dict[str, Any]) -> None:
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LIBRARY_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(LIBRARY_FILE)


def ensure_seeded() -> None:
    """Add missing safe starter tools without overwriting user edits."""
    with _LOCK:
        data = _load()
        changed = False
        now = _now()
        by_id = {item.get("id"): item for item in data["items"]}
        for starter in _STARTER_TOOLS:
            existing = by_id.get(starter["id"])
            if existing is None:
                data["items"].append({**starter, "created_at": now, "updated_at": now})
                changed = True
                continue
            for key in ("read_only", "approval_mode", "timeout_seconds", "input_schema"):
                if key not in existing:
                    existing[key] = starter[key]
                    changed = True
            if starter["id"] == _STARTER_TOOL["id"]:
                content = str(existing.get("content") or "")
                if _PROOF_SANITY_MARKER_CHECK_V1 in content:
                    existing["content"] = content.replace(
                        _PROOF_SANITY_MARKER_CHECK_V1,
                        _PROOF_SANITY_MARKER_CHECK_V2,
                    )
                    existing["updated_at"] = now
                    changed = True
            if starter["id"] == _LEAN_CHECK_TOOL["id"]:
                content = str(existing.get("content") or "")
                if _LEAN_MATHLIB_PROJECT_CHECK_V1 in content:
                    existing["content"] = content.replace(
                        _LEAN_MATHLIB_PROJECT_CHECK_V1,
                        _LEAN_MATHLIB_PROJECT_CHECK_V2,
                    )
                    existing["updated_at"] = now
                    changed = True
            if starter["id"] == _CITATION_AUDIT_TOOL["id"]:
                content = str(existing.get("content") or "")
                if _CITATION_AUDIT_STRUCTURE_V1 in content:
                    existing["content"] = content.replace(
                        _CITATION_AUDIT_STRUCTURE_V1,
                        _CITATION_AUDIT_STRUCTURE_V2,
                    )
                    existing["updated_at"] = now
                    changed = True
            if starter["id"] == _SOURCE_FETCH_TOOL["id"]:
                content = str(existing.get("content") or "")
                old = 'set -euo pipefail\nurl="${1:-}"'
                new = '''set -euo pipefail
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  printf '%s\\n' \\
    'Usage: primary-source-fetch.sh HTTPS_URL' \\
    'Fetch bounded text from an allowlisted HTTPS mathematics source.'
  exit 0
fi
url="${1:-}"'''
                if old in content:
                    existing["content"] = content.replace(old, new, 1)
                    existing["updated_at"] = now
                    changed = True
                    content = str(existing["content"])
                if (
                    str(existing.get("source") or "") == "starter"
                    and _SOURCE_FETCH_ALLOWLIST_V1 in content
                    and _SOURCE_FETCH_ENTRY_V1 in content
                ):
                    existing["content"] = content.replace(
                        _SOURCE_FETCH_ALLOWLIST_V1,
                        _SOURCE_FETCH_ALLOWLIST_V2,
                        1,
                    ).replace(
                        _SOURCE_FETCH_ENTRY_V1,
                        _SOURCE_FETCH_ENTRY_V2,
                        1,
                    )
                    existing["updated_at"] = now
                    changed = True
                    content = str(existing["content"])
                if (
                    str(existing.get("source") or "") == "starter"
                    and _SOURCE_FETCH_RAW_GUARD_V1 in content
                    and _SOURCE_FETCH_RAW_GUARD_V2 not in content
                ):
                    existing["content"] = content.replace(
                        _SOURCE_FETCH_RAW_GUARD_V1,
                        _SOURCE_FETCH_RAW_GUARD_V2,
                        1,
                    )
                    existing["updated_at"] = now
                    changed = True
            cli_migrations = {
                _EXACT_MATH_CERTIFICATE_TOOL["id"]: (
                    'exec "$AGENT_MONITOR_PYTHON" -m agent_monitor.proof_tools exact-math-certificate "$1"',
                    _EXACT_MATH_CERTIFICATE_TOOL["content"].split("\n", 2)[2].rsplit("\n", 1)[0],
                ),
                _BOUNDED_COUNTEREXAMPLE_TOOL["id"]: (
                    'exec "$AGENT_MONITOR_PYTHON" -m agent_monitor.proof_tools bounded-counterexample-search "$1"',
                    _BOUNDED_COUNTEREXAMPLE_TOOL["content"].split("\n", 2)[2].rsplit("\n", 1)[0],
                ),
                _STATEMENT_FIDELITY_TOOL["id"]: (
                    'exec "$AGENT_MONITOR_PYTHON" -m agent_monitor.proof_tools statement-fidelity-audit "$1"',
                    _STATEMENT_FIDELITY_TOOL["content"].split("\n", 2)[2].rsplit("\n", 1)[0],
                ),
            }
            legacy, replacement = cli_migrations.get(starter["id"], ("", ""))
            if legacy and legacy in str(existing.get("content") or ""):
                existing["content"] = str(existing["content"]).replace(legacy, replacement, 1)
                existing["updated_at"] = now
                changed = True
            if (
                starter["id"] == _AUDIT_OUTPUT_VALIDATOR_TOOL["id"]
                and str(existing.get("source") or "") == "starter"
                and str(existing.get("name") or "") == starter["name"]
                and str(existing.get("content") or "") == starter["content"]
            ):
                metadata_changed = False
                for key in ("description", "input_schema", "example_input"):
                    if existing.get(key) != starter[key]:
                        existing[key] = starter[key]
                        changed = True
                        metadata_changed = True
                if metadata_changed:
                    existing["updated_at"] = now
        if changed:
            _save(data)
        LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
        if not SEED_MARKER.exists():
            SEED_MARKER.write_text("starter library seeded\n", encoding="utf-8")


def _native_skill_item(skill: dict[str, Any]) -> dict[str, Any]:
    name = str(skill.get("name") or "")
    return {
        "id": f"agent_skill:{name}",
        "type": "skill",
        "name": name,
        "description": str(skill.get("description") or ""),
        "content": str(skill.get("content") or ""),
        "enabled": bool(skill.get("enabled", True)),
        "tags": ["SKILL.md"],
        "source": "agent skill",
        "native_skill": True,
        "created_at": 0,
        "updated_at": 0,
    }


def _tool_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    tags = {str(tag).lower() for tag in (payload.get("tags") or [])}
    read_only = bool(payload.get("read_only", "read-only" in tags))
    approval_mode = str(payload.get("approval_mode") or ("auto" if read_only else "prompt"))
    if approval_mode not in {"auto", "prompt", "writes"}:
        raise ValueError("approval_mode must be auto, prompt, or writes")
    if approval_mode == "auto" and not read_only:
        raise ValueError("Only read-only tools may use automatic approval")
    schema = payload.get("input_schema")
    if not isinstance(schema, dict):
        schema = {
            "type": "object",
            "properties": {
                "args": {"type": "string", "description": "Optional space-separated arguments", "default": ""}
            },
            "additionalProperties": False,
        }
    try:
        timeout_seconds = max(1, min(300, int(payload.get("timeout_seconds") or 120)))
    except (TypeError, ValueError):
        raise ValueError("timeout_seconds must be an integer from 1 to 300") from None
    return {
        "read_only": read_only,
        "approval_mode": approval_mode,
        "timeout_seconds": timeout_seconds,
        "input_schema": schema,
    }


def _trusted_runnable(item: dict[str, Any]) -> bool:
    """Only unmodified deterministic starters may execute in the web broker."""
    starter = _STARTER_TOOL_BY_ID.get(str(item.get("id") or ""))
    if starter is None:
        return False
    try:
        from agent_monitor.proof_tools import TRUSTED_TOOL_HANDLERS
    except ImportError:
        return False
    return (
        starter["id"] in TRUSTED_TOOL_HANDLERS
        and str(item.get("name") or "") == starter["name"]
        and str(item.get("content") or "") == starter["content"]
        and str(item.get("source") or "") == "starter"
        and bool(item.get("read_only"))
        and str(item.get("approval_mode") or "") == "auto"
    )


def _present_item(item: dict[str, Any]) -> dict[str, Any]:
    if item.get("type") != "tool":
        return item
    presented = {**item, **_tool_metadata(item)}
    presented["runnable"] = _trusted_runnable(presented)
    return presented


def get_library() -> dict[str, Any]:
    ensure_seeded()
    with _LOCK:
        data = _load()
    # SKILL.md files are the single source of truth for skills. Present them in
    # the Skills pane alongside generic memory/tools, rather than duplicating
    # them under Agent profile and in library.json.
    try:
        from agent_monitor import agent_config

        native_skills = [_native_skill_item(skill) for skill in agent_config.list_skills()]
    except (OSError, ValueError):
        native_skills = []
    native_names = {item["name"] for item in native_skills}
    stored = [
        _present_item(item)
        for item in data["items"]
        if not (item.get("type") == "skill" and item.get("name") in native_names)
    ]
    items = sorted(stored, key=lambda i: -(i.get("updated_at") or 0))
    items.extend(native_skills)
    counts = {t: sum(1 for i in items if i.get("type") == t) for t in VALID_TYPES}
    return {"items": items, "counts": counts, "settings": data["settings"]}


def run_trusted_tool(item_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Run an unmodified built-in tool through its bounded Python handler."""
    ensure_seeded()
    with _LOCK:
        data = _load()
        item = next((dict(i) for i in data["items"] if i.get("id") == item_id), None)
    if item is None or item.get("type") != "tool":
        raise ValueError("unknown tool")
    item = _present_item(item)
    if not item.get("enabled", True):
        raise ValueError("tool is disabled")
    if not item.get("runnable"):
        raise ValueError("only unmodified trusted starter tools can run from the interface")
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be a JSON object")
    from agent_monitor.proof_tools import ToolInputError, run_trusted_tool as execute

    started = time.monotonic()
    try:
        result = execute(item_id, arguments)
    except ToolInputError as exc:
        raise ValueError(str(exc)) from exc
    return {
        **result,
        "broker": {
            "trusted": True,
            "read_only": True,
            "approval_mode": "auto",
            "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
        },
    }


def upsert_item(payload: dict[str, Any]) -> dict[str, Any]:
    itype = str(payload.get("type") or "").strip().lower()
    if itype not in VALID_TYPES:
        raise ValueError(f"type must be one of {VALID_TYPES}")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    content = str(payload.get("content") or "")
    if not content.strip():
        raise ValueError("content is required")

    if itype == "skill":
        from agent_monitor import agent_config

        old_id = str(payload.get("id") or "")
        old_name = old_id.removeprefix("agent_skill:") if old_id.startswith("agent_skill:") else ""
        agent_config.save_skill(name, content)
        agent_config.set_skill_enabled(name, bool(payload.get("enabled", True)))
        if old_name and old_name != name:
            try:
                agent_config.delete_skill(old_name)
            except FileNotFoundError:
                pass
        skill = next(item for item in agent_config.list_skills() if item["name"] == name)
        return _native_skill_item(skill)

    item = {
        "id": str(payload.get("id") or "").strip() or f"{itype}_{uuid.uuid4().hex[:8]}",
        "type": itype,
        "name": name,
        "description": str(payload.get("description") or "").strip(),
        "content": content,
        "enabled": bool(payload.get("enabled", True)),
        "tags": [str(t) for t in (payload.get("tags") or [])],
        "source": str(payload.get("source") or "user"),
        "updated_at": _now(),
    }
    if itype == "tool":
        item.update(_tool_metadata(payload))
    with _LOCK:
        data = _load()
        existing = next((i for i in data["items"] if i.get("id") == item["id"]), None)
        if existing:
            item["created_at"] = existing.get("created_at") or _now()
            data["items"] = [item if i.get("id") == item["id"] else i for i in data["items"]]
        else:
            item["created_at"] = _now()
            data["items"].append(item)
        _save(data)
    return item


def delete_item(item_id: str) -> bool:
    if item_id.startswith("agent_skill:"):
        from agent_monitor import agent_config

        try:
            agent_config.delete_skill(item_id.removeprefix("agent_skill:"))
            return True
        except (FileNotFoundError, ValueError):
            return False
    with _LOCK:
        data = _load()
        before = len(data["items"])
        data["items"] = [i for i in data["items"] if i.get("id") != item_id]
        if len(data["items"]) != before:
            _save(data)
            return True
    return False


def update_settings(settings: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        data = _load()
        if "auto_memory" in settings:
            data["settings"]["auto_memory"] = bool(settings["auto_memory"])
        _save(data)
        return dict(data["settings"])


def enabled_items(itype: str | None = None) -> list[dict[str, Any]]:
    ensure_seeded()
    with _LOCK:
        data = _load()
    items = [i for i in data["items"] if i.get("enabled", True)]
    items = [
        ({**item, **_tool_metadata(item)} if item.get("type") == "tool" else item)
        for item in items
    ]
    if itype:
        items = [i for i in items if i.get("type") == itype]
    return items


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:60] or "item"


def materialize(workspace: Path) -> dict[str, Any]:
    """Write enabled library items into <workspace>/_library/ for agent access."""
    memories = enabled_items("memory")
    skills = enabled_items("skill")
    tools = enabled_items("tool")
    if not (memories or skills or tools):
        return {"written": []}

    lib_dir = workspace / "_library"
    lib_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    if memories:
        lines = ["# Memory\n"]
        for m in memories:
            lines.append(f"## {m['name']}\n")
            if m.get("description"):
                lines.append(f"_{m['description']}_\n")
            lines.append(m["content"].rstrip() + "\n")
        (lib_dir / "MEMORY.md").write_text("\n".join(lines), encoding="utf-8")
        written.append("_library/MEMORY.md")

    if skills:
        lines = ["# Skills\n"]
        for s in skills:
            lines.append(f"## {s['name']}\n")
            if s.get("description"):
                lines.append(f"_{s['description']}_\n")
            lines.append(s["content"].rstrip() + "\n")
        (lib_dir / "SKILLS.md").write_text("\n".join(lines), encoding="utf-8")
        written.append("_library/SKILLS.md")

    if tools:
        tdir = lib_dir / "tools"
        tdir.mkdir(exist_ok=True)
        manifest = []
        for t in tools:
            fname = _safe_name(t["name"]) + ".sh"
            script = tdir / fname
            body = t["content"]
            if _trusted_runnable(t):
                # Agent shells (notably OpenClaw) may intentionally sanitize
                # inherited environment variables, and a run workspace can
                # shadow an editable package root. Bind trusted starter tools
                # to both this server's interpreter and the reviewed entry
                # point so invocation is independent of cwd/module discovery.
                python = shlex.quote(sys.executable)
                proof_tools = shlex.quote(
                    str(Path(__file__).with_name("proof_tools.py").resolve())
                )
                body = body.replace(
                    '"$AGENT_MONITOR_PYTHON" -m agent_monitor.proof_tools',
                    f"{python} {proof_tools}",
                )
                body = body.replace(
                    '"$AGENT_MONITOR_PYTHON"', python
                )
            if not body.startswith("#!"):
                body = "#!/bin/bash\n" + body
            script.write_text(body, encoding="utf-8")
            script.chmod(0o755)
            written.append(f"_library/tools/{fname}")
            manifest.append(
                {
                    "name": t["name"],
                    "description": t.get("description") or "",
                    "script": f"_library/tools/{fname}",
                    "input_schema": t.get("input_schema"),
                    "read_only": bool(t.get("read_only")),
                    "approval_mode": t.get("approval_mode"),
                    "timeout_seconds": t.get("timeout_seconds"),
                }
            )
        (tdir.parent / "tools.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        written.append("_library/tools.json")

    return {"written": written}


def compose_context() -> str:
    """Build the USER LIBRARY prompt block (empty string when nothing enabled)."""
    memories = enabled_items("memory")
    skills = enabled_items("skill")
    tools = enabled_items("tool")
    if not (memories or skills or tools):
        return ""

    parts = ["===== USER LIBRARY (memory / skills / tools) ====="]
    if memories:
        parts.append("\n[MEMORY] Facts and context from previous work — take into account:")
        for m in memories:
            head = m["name"] + (f" — {m['description']}" if m.get("description") else "")
            parts.append(f"- {head}:\n{m['content'].strip()}")
    if skills:
        parts.append("\n[SKILLS] Methods/strategies you should apply when relevant:")
        for s in skills:
            head = s["name"] + (f" — {s['description']}" if s.get("description") else "")
            parts.append(f"- {head}:\n{s['content'].strip()}")
    if tools:
        parts.append(
            "\n[TOOLS] Typed helper scripts in the workspace under _library/tools/. "
            "Only approval_mode=auto tools may run without asking the human; "
            "prompt/writes tools require explicit approval. Run with: "
            "bash _library/tools/<name>.sh [args]:"
        )
        for t in tools:
            fname = _safe_name(t["name"]) + ".sh"
            desc = t.get("description") or ""
            policy = f"{t.get('approval_mode')} · {'read-only' if t.get('read_only') else 'writes'}"
            parts.append(f"- _library/tools/{fname}: {desc} [{policy}]")
    copies: list[str] = []
    if memories:
        copies.append("_library/MEMORY.md")
    if skills:
        copies.append("_library/SKILLS.md")
    if tools:
        copies.extend(("_library/tools/", "_library/tools.json"))
    parts.append("\nEnabled library files in the workspace: " + ", ".join(copies) + ".")
    parts.append("===== END USER LIBRARY =====\n")
    return "\n".join(parts)


def auto_memory_enabled() -> bool:
    with _LOCK:
        return bool(_load()["settings"].get("auto_memory", True))


def record_run_memory(
    *, run_id: str, engine: str, problem_id: str, status: str, summary: str
) -> dict[str, Any] | None:
    """Append a compact memory entry when a run finishes (if auto_memory on)."""
    if not auto_memory_enabled():
        return None
    return upsert_item(
        {
            "id": f"memory_run_{run_id}",
            "type": "memory",
            "name": f"Run {problem_id} ({engine})",
            "description": f"auto record · status {status}",
            "content": summary.strip()[:4000] or f"Run {run_id} finished with status {status}.",
            "enabled": False,  # auto entries start disabled; user can enable in UI
            "source": f"run:{run_id}",
        }
    )
