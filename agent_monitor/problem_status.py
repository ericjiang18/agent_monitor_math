"""Live status overlay for the problem catalogue (``/research/problem-status.json``).

The ``ansatze-problems`` timer runs ``scripts/update_problem_status.py`` every
six hours.  It compares current upstream sources with the fact files the static
catalogue was built from and publishes a small overlay:

* ``entries`` change a row's category, badges or evidence, and only on
  deterministic source-of-record signals: a teorth state change, a Kourovka
  marker change in a newer arXiv version, an openai/math withdrawal of a claim
  the row relies on, or a change of the Clay listing.
* ``signals`` are new reports (arXiv, Palomar, formal-conjectures commits,
  openai/math, news links, Wikipedia edits) shown as "New reports (unreviewed)".
  They never change a category.  Kimi K3 may classify a candidate match, with a
  verbatim quote, inside a persisted daily budget.  Wikipedia text is never
  published: an edit is triaged once it has stood for a while, its quote stays
  in the private state, and the report is withdrawn when a later edit removes
  the quoted text again.

A reviewed category decision keeps its category against source changes (the
entry then adds "Sources disagree").  The overlay never adds or removes rows.
Each source fails closed: on an error its last good result is kept and the
error is listed; a run counts as successful only when an upstream source
answered.  The public output is written before the private state, both
atomically.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import quote, urlencode

from agent_monitor import problem_sources as ps
from agent_monitor.problem_catalogue import (CATALOGUE_BADGES, CATEGORY_IDS, FACT_FILES, RULES, evidence_item, expand_row,
                                             withdrawal_notice)

UTC = timezone.utc
REFRESH_MINUTES = 360
RUN_MODEL_CAP = 8
DEFAULT_DAILY_CAP = 48
MAX_SIGNALS_PER_PROBLEM = 10
MAX_SIGNALS = 400
SIGNAL_TTL_DAYS = 180
MAX_RECENT = 50
MAX_CHANGE = 400  # characters of an entry's change note (also its recent_changes line)
ARXIV_INTERVAL = timedelta(days=7)  # every target is searched about once per interval
ARXIV_QUERIES_PER_RUN = 6  # at least; more when the targets would not all fit in one interval
ARXIV_MAX_QUERIES_PER_RUN = 40  # 3.2 seconds apart: about two minutes of arXiv politeness
ARXIV_LOOKBACK = timedelta(days=14)  # how far back the first search of a target looks
ARXIV_INCOMPLETE_TRIES = 3  # an entry whose classification came back empty or cut off is tried this often in all
WIKI_SETTLE = timedelta(hours=12)  # an article edit is triaged once it has stood this long...
WIKI_SETTLE_MAX = timedelta(days=3)  # ...or once its article has kept changing this long
WIKI_INVALID_TRIES = 2  # rejected model replies before an article change is passed over
PRIVATE_SIGNAL_KEYS = ("article", "private_quote")  # kept in state.json, never published
SOURCES = ("erdos", "kourovka", "openai-math", "clay", "formal-conjectures", "palomar", "arxiv", "wikipedia", "news")
SIGNAL_RELATIONS = ("claims-resolution", "claims-partial", "disputes", "withdraws", "formalization", "mentions")
SIGNAL_SOURCES = ("arxiv", "palomar", "openai-math", "news", "wikipedia", "formal-conjectures")
MATCHED_BY = ("kimi-k3", "deterministic")
SOLVED_ONLY_BADGES = ("ai-assisted-solution", "ai-confirmed")  # the builder drops them from other categories
NEWS_RELATIONS = {"mentions": "mentions", "claims-partial-progress": "claims-partial", "claims-resolution": "claims-resolution",
                  "claims-counterexample": "claims-resolution", "disputes-claim": "disputes",
                  "withdraws-claim": "withdraws", "formalization": "formalization"}
ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,159}")


class StatusError(ValueError):
    """A public-safe failure of the whole run (bad catalogue or configuration)."""


def timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if result.tzinfo else result.replace(tzinfo=UTC)


def daily_cap() -> int:
    raw = os.environ.get("ANSATZE_PROBLEMS_MODEL_CALLS_PER_DAY", str(DEFAULT_DAILY_CAP)).strip()
    try:
        return max(0, min(500, int(raw)))
    except ValueError:
        return DEFAULT_DAILY_CAP


def clip_words(text: str, limit: int) -> str:
    """``text`` in at most ``limit`` characters, cut at a word and ending with "…" when shortened.

    The page shortens long change notes the same way, so a published note is
    never cut again or left to stop mid-word.  A text without a space in its
    second half (a long link) is cut where it reaches the limit.
    """
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    space = cut.rfind(" ")
    return (cut[:space] if space > limit // 2 else cut).rstrip(" ,;:") + "…"


# ── signals ─────────────────────────────────────────────────────────────────


def make_signal(*, problem_id: str, title: str, url: str, relation: str, source: str, matched_by: str,
                first_seen: str, date: str | None = None, evidence_quote: str = "") -> dict[str, Any]:
    """A validated overlay signal (raises ValueError when unusable)."""
    link = ps.safe_url(url)
    title = ps.clean_text(title, 300)
    if not ID.fullmatch(problem_id) or not link or not title:
        raise ValueError("signal needs a problem id, an https url and a title")
    if relation not in SIGNAL_RELATIONS or source not in SIGNAL_SOURCES or matched_by not in MATCHED_BY:
        raise ValueError("signal enum")
    signal = {"title": title, "url": link}
    day = str(date or "")[:10]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        signal["date"] = day
    signal.update(relation=relation, source=source, matched_by=matched_by)
    quote_text = " ".join(str(evidence_quote or "").split())
    if quote_text:
        signal["evidence_quote"] = quote_text[:240]
    signal["first_seen"] = first_seen
    return signal


def signal_key(problem_id: str, url: str) -> str:
    return problem_id + "|" + sha256(url.encode("utf-8")).hexdigest()[:16]


# ── model access ──────────────────────────────────────────────────────────────


class _RunShare:
    """The daily budget, limited to the ``left`` calls this run still has.

    One task may send more than one request (a call Kimi refused before
    processing it is sent again, kimi_tasks.call_json), and each request must
    fit the run cap as well as the daily one.
    """

    def __init__(self, budget: Any, left: int) -> None:
        self.budget, self.left = budget, left

    def take(self) -> bool:
        if self.left <= 0 or not self.budget.take():
            return False
        self.left -= 1
        return True


class Model:
    """Kimi K3 through the broker with a per-run and a persisted daily cap."""

    def __init__(self, budget_state: dict, *, cap: int, run_cap: int = RUN_MODEL_CAP, request=None,
                 today: str | None = None):
        from agent_monitor.kimi_tasks import DailyBudget

        self.budget = DailyBudget(budget_state, cap, today=today)
        self.run_left = run_cap
        self.request = request
        self.stopped = False
        self.errors: list[tuple[str, str]] = []  # (kind, public-safe message)
        # Why the last call returned None: "capacity" (not called: run cap, daily
        # budget or back-off), or a KimiTaskError kind ("invalid" = a rejected reply).
        self.last_kind: str | None = None
        self.last_incomplete = False  # the rejected reply was empty or cut off (worth another try later)

    def available(self) -> bool:
        return not self.stopped and self.run_left > 0 and self.budget.remaining() > 0

    def _run(self, call: Callable[[Any], Any]) -> Any:
        from agent_monitor.kimi_tasks import KimiTaskError

        self.last_kind = None
        self.last_incomplete = False
        if not self.available():
            self.last_kind = "capacity"
            return None
        before = self.budget.used
        try:
            return call(_RunShare(self.budget, self.run_left))
        except KimiTaskError as error:
            # Busy, over quota or budget: stop calling for this run (back off).
            if error.kind in ("refused", "budget", "unavailable"):
                self.stopped = True
            self.last_kind = error.kind
            self.last_incomplete = bool(getattr(error, "incomplete", False))
            self.errors.append((error.kind, str(error)))
            return None
        finally:
            self.run_left -= max(1, self.budget.used - before)

    def match(self, item: Mapping[str, Any], candidates: list[Mapping[str, Any]]) -> list[dict] | None:
        from agent_monitor import problem_status_kimi as tasks

        return self._run(lambda budget: tasks.match_claims(item, candidates, budget=budget, request=self.request))

    def triage(self, change: Mapping[str, Any]) -> dict | None:
        from agent_monitor import problem_status_kimi as tasks

        return self._run(lambda budget: tasks.article_change_triage(change, budget=budget, request=self.request))

    def public(self) -> dict[str, Any]:
        return {"name": "kimi-k3", "calls_today": self.budget.used, "daily_cap": self.budget.cap}


# ── run context ───────────────────────────────────────────────────────────────


@dataclass
class Run:
    catalogue: Mapping[str, Any]
    facts: Mapping[str, Mapping]
    state: dict
    decisions: Mapping[str, Any]
    fetcher: Any
    now: datetime
    model: Model | None
    news: Mapping[str, Any] | None = None
    sleeper: Callable[[float], None] = time.sleep
    rows: dict[str, dict] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.rows = {row["id"]: expand_row(self.catalogue, row) for row in self.catalogue["problems"]}

    @property
    def stamp(self) -> str:
        return timestamp(self.now)

    def source_state(self, source: str) -> dict:
        return self.state.setdefault("sources", {}).setdefault(source, {})

    def cached(self, url: str, hosts: Iterable[str], cache: dict, parse: Callable[[bytes], Any], *,
               max_bytes: int = 2 * 1024 * 1024, accept: str = "*/*") -> Any:
        """GET with ETag / Last-Modified validators and parse the body.

        The validators are stored only together with a successfully parsed value:
        a body that fails to parse is downloaded (and fails) again on the next
        run instead of hiding behind a 304.
        """
        usable = cache.get("url") == url and "value" in cache
        headers = {}
        if usable and cache.get("etag"):
            headers["If-None-Match"] = cache["etag"]
        if usable and cache.get("last_modified"):
            headers["If-Modified-Since"] = cache["last_modified"]
        result = self.fetcher.get(url, allowed_hosts=hosts, max_bytes=max_bytes, headers=headers or None, accept=accept)
        if result.status == 304 and usable:
            return cache["value"]
        if result.status != 200:
            raise ps.SourceError(f"Source returned HTTP {result.status}")
        value = parse(result.body)
        cache.clear()
        cache.update(url=url, etag=result.etag, last_modified=result.last_modified, value=value)
        return value


class CountingFetcher:
    """Counts upstream responses (200 or 304), to tell a refresh from a total outage."""

    def __init__(self, fetcher):
        self.fetcher = fetcher
        self.responses = 0

    def get(self, url: str, **kwargs) -> ps.FetchResult:
        result = self.fetcher.get(url, **kwargs)
        if result.status in (200, 304):
            self.responses += 1
        return result


@dataclass
class Result:
    entries: dict[str, dict] = field(default_factory=dict)
    signals: list[tuple[str, dict]] = field(default_factory=list)
    revision: str | None = None
    note: str | None = None
    # A failure part-way through a signal source: the source is reported as
    # failed, but the signals found and the state advanced so far are kept.
    error: str | None = None
    retract: list[str] = field(default_factory=list)  # ledger keys of earlier signals that no longer hold


def _commit_atom(run: Run, url: str, source: str) -> list[dict[str, str]]:
    cache = run.source_state(source).setdefault("atom", {})
    return run.cached(url, ("github.com",), cache, lambda body: ps.parse_commits_atom(body)[:30], max_bytes=512 * 1024)


# ── deterministic adapters: teorth, Kourovka, openai/math, Clay ───────────────


def adapter_erdos(run: Run) -> Result:
    base = run.facts["erdos"]
    state = run.source_state("erdos")
    commits = _commit_atom(run, ps.TEORTH_COMMITS_ATOM, "erdos")
    head = commits[0]["sha"]
    if head == base["revision"]:
        state.update(revision=head, snapshot=None)
        return Result(revision=head)
    if state.get("revision") != head or not state.get("snapshot"):
        text = ps.fetch_ok(run.fetcher, ps.TEORTH_RAW.format(commit=head), allowed_hosts=("raw.githubusercontent.com",),
                           max_bytes=4 * 1024 * 1024).decode("utf-8")
        parsed = ps.parse_erdos_yaml(text)
        ps.check_shrink(len(parsed), len(base["problems"]), what="Erdős problems")
        state["snapshot"] = {number: [item["state"], item.get("formal_state", "unformalized"), item.get("line"),
                                      item.get("formal_url")] for number, item in parsed.items()}
        state["revision"] = head
    return erdos_entries(run, state["snapshot"], head)


def erdos_entries(run: Run, snapshot: Mapping[str, list], head: str) -> Result:
    base = run.facts["erdos"]["problems"]
    result = Result(revision=head)
    blob = f"https://github.com/teorth/erdosproblems/blob/{head}/data/problems.yaml"
    added = sorted(set(snapshot) - set(base), key=int)
    if added:
        result.note = f"{len(added)} new problem(s) upstream; rebuild the catalogue to add them"
    for number, (state, formal, line, formal_url) in snapshot.items():
        old = base.get(number)
        row = run.rows.get(f"erdos-{int(number):04d}")
        if old is None or row is None:
            continue
        changes = []
        entry: dict[str, Any] = {}
        if state != old["state"]:
            changes.append(f"Erdős problems database: state changed from “{old['state']}” to “{state}”")
            marker = (evidence_item({"kind": "source-marker", "actor": "maintainer",
                                     "claimant": "Erdős problems database (teorth/erdosproblems)",
                                     "scope": "full", "outcome": ps.ERDOS_SOLVED_STATES[state],
                                     "date": run.stamp[:10], "title": f"Erdős problems database: state “{state}”",
                                     "url": f"{blob}#L{line}" if line else blob, "acceptance": "accepted-by-maintainer"})
                      if state in ps.ERDOS_SOLVED_STATES else None)
            if state in ps.ERDOS_SOLVED_STATES and (row["category"], row["category_rule"]) != ("solved", "S1"):
                entry.update(category="solved", category_rule="S1",
                             status_headline=f"{state.capitalize()} according to the Erdős problems database.",
                             badges_remove=["upstream-substatus", "lean-pending", "sources-conflict"], evidence_add=[marker])
            elif state in ps.ERDOS_SOLVED_STATES:
                # Still answered, but differently (proved -> disproved, solved -> proved): the headline
                # and the source marker follow the source; the category stays.
                entry.update(status_headline=f"{state.capitalize()} according to the Erdős problems database.",
                             evidence_add=[marker])
            elif state in ps.ERDOS_OPEN_STATES and row["category"] == "solved" and row["category_rule"] == "S1":
                listed = "open" if state == "open" else f"open ({state})"
                entry.update(category="open", category_rule="O",
                             status_headline=f"Listed as {listed} in the Erdős problems database, which previously listed it as {old['state']}.")
            elif state in ps.ERDOS_OPEN_STATES and row["category"] == "open":
                # The overlay cannot change source_status (the sub-status badge label), so say it in the headline.
                entry["status_headline"] = f"Listed as open{'' if state == 'open' else f' ({state})'} in the Erdős problems database."
                if state == "open":
                    entry["badges_remove"] = ["upstream-substatus"]
        lean_now = formal == "Lean" and state in ps.ERDOS_OPEN_STATES
        lean_before = old.get("formal_state") == "Lean" and old["state"] in ps.ERDOS_OPEN_STATES
        if lean_now and not lean_before and "lean-pending" not in row.get("badges", []):
            changes.append("A Lean solution is now recorded upstream (not yet digested by a human reader)")
            entry.setdefault("badges_add", []).append("lean-pending")
            if formal_url:
                entry.setdefault("evidence_add", []).append(evidence_item({
                    "kind": "formalization", "actor": "unknown", "scope": "full", "outcome": "proof",
                    "title": "Lean solution recorded in the Erdős problems database (not yet digested by a human reader)",
                    "url": formal_url, "acceptance": "lean-checked-unreviewed", "note": "Not re-checked by this server."}))
        elif lean_before and not lean_now and "lean-pending" in row.get("badges", []) and "category" not in entry:
            changes.append("The upstream Lean record changed")
            entry.setdefault("badges_remove", []).append("lean-pending")
        if changes:
            entry["change"] = changes
            result.entries[row["id"]] = entry
    return result


def _kourovka_category(item: Mapping[str, Any] | None) -> tuple[str, str] | None:
    if not item:
        return None
    if item.get("header_marker") == "*":
        return ("solved", "S1")
    if item.get("header_marker") == "#":
        return ("ai-proposed", "A1")
    if any(part["marker"] == "*" for part in item.get("part_markers") or []):
        return ("partial", "P1")
    return None


def adapter_kourovka(run: Run) -> Result:
    base = run.facts["kourovka"]
    state = run.source_state("kourovka")
    version = ps.kourovka_latest_version(run.fetcher)
    if version <= int(base["arxiv_version"]):
        state.update(version=version, snapshot=None)
        return Result(revision=f"v{version}")
    if state.get("version") != version or not state.get("snapshot"):
        data = ps.fetch_ok(run.fetcher, ps.KOUROVKA_PDF.format(version=version), allowed_hosts=("arxiv.org",),
                           max_bytes=ps.MAX_BYTES, accept="application/pdf")
        parsed = ps.parse_kourovka_pages(ps.pdf_pages(data))
        ps.check_shrink(len(parsed["problems"]), len(base["problems"]), what="Kourovka problems")
        # Bulk loss of problems or markers, or growing parse anomalies, mean the
        # extraction changed: refuse rather than reopen every marked problem.
        ps.check_kourovka_drift(parsed, base)
        state["snapshot"] = {number: {"page": item["page"], "header_marker": item["header_marker"],
                                      "part_markers": item["part_markers"], "ai": item.get("ai")}
                             for number, item in parsed["problems"].items()}
        state["version"] = version
    return kourovka_entries(run, state["snapshot"], version)


def kourovka_entries(run: Run, snapshot: Mapping[str, dict], version: int) -> Result:
    base = run.facts["kourovka"]["problems"]
    result = Result(revision=f"v{version}", note=f"arXiv:1401.0300v{version} is newer than the catalogue's "
                    f"v{run.facts['kourovka']['arxiv_version']}; rebuild the catalogue for its page links")
    for row in run.rows.values():
        if row["source"] != "kourovka":
            continue
        number = row["number"]
        old, new = base.get(number), snapshot.get(number)
        url = f"https://arxiv.org/pdf/1401.0300v{version}#page={new['page']}" if new else None
        entry: dict[str, Any] = {}
        changes = []
        if old and not new and "not-in-current-edition" not in row.get("badges", []):
            entry["badges_add"] = ["not-in-current-edition"]
            changes.append(f"Not listed in arXiv:1401.0300v{version}")
        if new and "not-in-current-edition" in row.get("badges", []):
            entry["badges_remove"] = ["not-in-current-edition"]
            changes.append(f"Listed again in arXiv:1401.0300v{version}")
        before, after = _kourovka_category(old), _kourovka_category(new)
        if new and after != before:
            if after and (row["category"], row["category_rule"]) != after:
                text = {"solved": "marked ∗ (answered)", "ai-proposed": "marked ♯ (unverified AI solution)",
                        "partial": "has a part marked ∗"}[after[0]]
                entry.update(category=after[0], category_rule=after[1],
                             status_headline=f"Kourovka {number} is {text} in arXiv:1401.0300v{version}."
                             + (" The editors regard it as unsolved until confirmed." if after[0] == "ai-proposed" else ""),
                             evidence_add=[evidence_item({"kind": "source-marker", "actor": "maintainer",
                                                          "claimant": "Kourovka Notebook editors",
                                                          "scope": "part" if after[0] == "partial" else "full",
                                                          "outcome": "other", "date": run.stamp[:10],
                                                          "title": f"Kourovka {number} {text}", "url": url,
                                                          "acceptance": "announced" if after[0] == "ai-proposed" else "accepted-by-maintainer"})])
                changes.append(f"Kourovka marker changed in v{version}: {text}")
            elif after is None and row["category_rule"] in ("S1", "A1", "P1") and before is not None:
                entry.update(category="open", category_rule="O",
                             status_headline=f"The marker on Kourovka {number} was removed in arXiv:1401.0300v{version}.")
                changes.append(f"Kourovka marker removed in v{version}")
        if changes:
            entry["change"] = changes
            result.entries[row["id"]] = entry
    return result


def adapter_openai(run: Run) -> Result:
    base = run.facts["openai_math"]
    state = run.source_state("openai-math")
    commits = _commit_atom(run, ps.OPENAI_COMMITS_ATOM, "openai-math")
    head = commits[0]["sha"]
    if head == base["revision"]:
        state.update(revision=head, withdrawn=None, unmatched=None)
        return openai_entries(run, {"unmatched": [row["title"] for row in base.get("unmatched_withdrawals") or []]}, head)
    if state.get("revision") != head or state.get("withdrawn") is None:
        def raw(commit: str, path: str) -> str:
            return ps.fetch_ok(run.fetcher, ps.OPENAI_RAW.format(commit=commit, path=path),
                               allowed_hosts=("raw.githubusercontent.com",), max_bytes=4 * 1024 * 1024).decode("utf-8")
        history = raw(head, "history.md")
        fact = ps.openai_math_fact_file(raw(head, "CONTENTS.md"), history, commit=head, fetched_at=run.stamp,
                                        previous_contents_md=raw(base["revision"], "CONTENTS.md"),
                                        previous_commit=base["revision"], previous=base)
        state["withdrawn"] = [{key: row.get(key) for key in ("family", "dir", "title", "withdrawn_on")} for row in fact["withdrawn"]]
        state["unmatched"] = [row["title"] for row in fact.get("unmatched_withdrawals") or []]
        state["history"] = history[:200_000]
        state["revision"] = head
        state["lean_docs"] = sorted(family for family, item in fact["families"].items() if item["lean_doc"])
    return openai_entries(run, state, head)


def openai_entries(run: Run, state: Mapping[str, Any], head: str) -> Result:
    base = run.facts["openai_math"]
    result = Result(revision=head)
    unmatched = state.get("unmatched") or []
    if unmatched:
        # Matched withdrawals still apply; these need a person (and a refreshed fact file).
        result.note = (f"history.md lists {len(unmatched)} withdrawal(s) that match no manuscript of the pinned "
                       f"revision {base['revision'][:7]}; review them and refresh openai_math.json")
    known = {row["dir"] for row in base.get("withdrawn", [])}
    fresh = [row for row in state.get("withdrawn") or [] if row["dir"] not in known]
    withdrawn_all = known | {row["dir"] for row in fresh}
    by_dir = {row["dir"]: row for row in fresh}
    # Each withdrawal links to the history.md of the head at which it was first seen, so
    # later, unrelated commits change neither its link nor its signal key (a rejection sticks).
    first_seen = run.source_state("openai-math").setdefault("withdrawal_heads", {})
    for directory in list(first_seen):
        if directory not in by_dir:
            del first_seen[directory]
    for directory in by_dir:
        first_seen.setdefault(directory, [run.stamp, head])

    def pinned(directories: Iterable[str]) -> str:
        """The newest of their first heads: its history.md lists all of them."""
        return max((first_seen[directory] for directory in directories), default=[run.stamp, head])[1]
    for row in run.rows.values():
        evidence = row.get("status_evidence") or []
        cited = {ps.openai_manuscript_dir(item["url"]) for item in evidence} - {None}
        hit = sorted(cited & set(by_dir))
        if not hit:
            continue
        # One withdrawal record, as the builder writes it, and every record that
        # cites a withdrawn manuscript re-sent as withdrawn with its own kind and
        # link: the page replaces its copy by that, strikes it, and offers
        # Analyze a claim that still stands.
        resent = [evidence_item(item | {"acceptance": "withdrawn"}) for item in evidence
                  if ps.openai_manuscript_dir(item["url"]) in by_dir and item["acceptance"] not in ("withdrawn", "refuted")]
        entry: dict[str, Any] = {
            "badges_add": ["withdrawn-claim"], "evidence_add": [withdrawal_notice([by_dir[directory] for directory in hit], pinned(hit)), *resent],
            "change": [f"openai/math withdrew a manuscript this entry cites ({by_dir[directory]['title']})" for directory in hit]}
        if row["category"] == "ai-proposed" and row["category_rule"] == "A2":
            live = [item for item in evidence if item["actor"] in ("ai", "ai-assisted") and item["scope"] == "full"
                    and item["kind"] in ("claim", "accepted-result") and item["acceptance"] not in ("withdrawn", "refuted")
                    and ps.openai_manuscript_dir(item["url"]) not in withdrawn_all]
            if not live:
                entry.update(category="open", category_rule="O",
                             status_headline="The claimed AI solution was withdrawn by its authors; the problem is open.")
        result.entries[row["id"]] = entry
    # Hints from the family-to-problem table (never a category change).  The
    # lookup is deterministic, but a link the table marks as a candidate is an
    # unreviewed guess, and the signal says so.
    history = state.get("history") or ""
    for link in base.get("mapping") or []:
        withdrawn = [row for row in fresh if row["family"] == link.get("family")]
        if withdrawn and link.get("catalogue_id") in run.rows and link["catalogue_id"] not in result.entries:
            quote_text = next((row["title"] for row in withdrawn if row["title"] in history), "")
            caveat = "" if link.get("review") == "reviewed" else " (unreviewed family-to-problem link)"
            history_url = f"https://github.com/openai/math/blob/{pinned(row['dir'] for row in withdrawn)}/history.md"
            result.signals.append((link["catalogue_id"], {
                "title": f"openai/math withdrew a manuscript of family {link['family']}{caveat}", "url": history_url,
                "date": max((row.get("withdrawn_on") or "" for row in withdrawn), default="") or None,
                "relation": "withdraws", "source": "openai-math", "matched_by": "deterministic",
                "evidence_quote": quote_text}))
    return result


def adapter_clay(run: Run) -> Result:
    base = run.facts["clay"]
    sections = run.cached(ps.CLAY_PAGE, ("www.claymath.org",), run.source_state("clay").setdefault("http", {}),
                          lambda body: ps.parse_clay_listing(body.decode("utf-8", "replace")), accept="text/html")
    result = Result(revision=timestamp(run.now))
    where = {slug: name for name, slugs in sections.items() for slug in slugs}
    before = {slug: name for name, slugs in base["sections"].items() for slug in slugs}
    for slug, name in where.items():
        row = run.rows.get(f"clay-{slug}")
        if row is None or before.get(slug) == name:
            continue
        entry: dict[str, Any] = {"change": [f"The Clay Millennium page now lists this under {name.capitalize()} problems"]}
        if name == "solved" and row["category"] != "solved":
            entry.update(category="solved", category_rule="S1",
                         status_headline="The Clay Mathematics Institute now lists this among the solved Millennium problems.",
                         evidence_add=[evidence_item({"kind": "source-marker", "actor": "maintainer",
                                                      "claimant": "Clay Mathematics Institute", "scope": "full",
                                                      "outcome": "other", "date": run.stamp[:10],
                                                      "title": "Clay Millennium page: listed under Solved problems",
                                                      "url": ps.CLAY_PAGE, "acceptance": "accepted-by-maintainer"})])
        elif before.get(slug) == "solved" and row["category"] == "solved" and row["category_rule"] == "S1":
            entry.update(category="open", category_rule="O",
                         status_headline="The Clay Millennium page no longer lists this as solved.")
        elif row["category"] == "open":
            entry["status_headline"] = f"Listed as {name} on the Clay Millennium page."
        result.entries[row["id"]] = entry
    return result


# ── signal adapters ───────────────────────────────────────────────────────────

_ERDOS_REF = re.compile(r"(?:ErdosProblems/|erdos[_ ]|Erd[őo]s\s+(?:problem\s+|Problem\s+)?#?)(\d{1,4})\b", re.I)
_KOUROVKA_REF = re.compile(r"Kourovka(?:/|\s+(?:problem\s+)?)(\d{1,2})[._](\d{1,3})", re.I)


def references(text: str, rows: Mapping[str, Any]) -> list[str]:
    """Catalogue ids named explicitly in a title (Erdős numbers, Kourovka numbers)."""
    found = []
    for match in _ERDOS_REF.finditer(text):
        problem_id = f"erdos-{int(match.group(1)):04d}"
        if problem_id in rows and problem_id not in found:
            found.append(problem_id)
    for match in _KOUROVKA_REF.finditer(text):
        problem_id = f"kourovka-{int(match.group(1)):02d}-{int(match.group(2)):03d}"
        if problem_id in rows and problem_id not in found:
            found.append(problem_id)
    return found[:5]


FC_RAW = "https://raw.githubusercontent.com/google-deepmind/formal-conjectures/{commit}/FormalConjectures/ErdosProblems/{number}.lean"
FC_FILES_PER_RUN = 10


def adapter_formal_conjectures(run: Run) -> Result:
    """Commits naming catalogue problems, and research-category changes of the Erdős files they touch."""
    state = run.source_state("formal-conjectures")
    commits = _commit_atom(run, ps.FC_COMMITS_ATOM, "formal-conjectures")
    baseline = run.facts["formal_conjectures"]
    last = state.get("last_seen") or baseline["revision"]
    result = Result(revision=commits[0]["sha"])
    if all(commit["sha"] != last for commit in commits):
        # The feed lists only the newest commits (20 on GitHub); bursts of merges can push the
        # last commit seen out of it, and the commits in between are never inspected.
        result.note = (f"more than {len(commits)} commits since {last[:7]}: the older ones fell out of the commit feed "
                       "and were not inspected")
    fetched = 0
    for commit in commits:
        if commit["sha"] == last:
            break
        commit_url = f"https://github.com/google-deepmind/formal-conjectures/commit/{commit['sha']}"
        for problem_id in references(commit["title"], run.rows):
            signal = {"title": f"formal-conjectures: {commit['title']}", "date": commit["updated"], "url": commit_url,
                      "relation": "formalization", "source": "formal-conjectures", "matched_by": "deterministic",
                      "evidence_quote": commit["title"]}
            number = str(int(problem_id.split("-")[1])) if problem_id.startswith("erdos-") else None
            if number and fetched < FC_FILES_PER_RUN:
                fetched += 1
                try:
                    text = ps.fetch_ok(run.fetcher, FC_RAW.format(commit=commit["sha"], number=number),
                                       allowed_hosts=("raw.githubusercontent.com",), max_bytes=1024 * 1024).decode("utf-8", "replace")
                except ps.SourceError:
                    text = ""  # the commit signal stands without the category comparison
                now = (ps.erdos_file_category(text, number) or {}).get("category")
                before = (baseline.get("erdos", {}).get(number) or {}).get("category")
                if now != before and now in ("research solved", "partially solved"):
                    marker = re.search(r"@\[category research solved[^\]]*\]", text)
                    signal.update(title=f"formal-conjectures now marks Erdős #{number} “{now}”",
                                  relation="claims-resolution" if now == "research solved" else "claims-partial",
                                  evidence_quote=marker.group(0)[:240] if marker else commit["title"])
            result.signals.append((problem_id, signal))
    state["last_seen"] = commits[0]["sha"]
    return result


def _classified(run: Run, item: Mapping[str, Any], candidates: list[str],
                default: str | None) -> list[tuple[str, str, str, str]] | None:
    """(problem id, relation, matched_by, quote) for an item and its candidate rows.

    None when the model could not classify the item (no capacity, busy or
    unavailable) and there is no deterministic ``default``: the caller keeps the
    item for a later run.  A reply the validator rejected counts as classified
    with no match.  A deterministic match carries no quote: its basis is the
    title shown with the signal.
    """
    rows = [{"id": problem_id, "title": run.rows[problem_id]["title"],
             "summary": (run.rows[problem_id].get("statement") or {}).get("text") or run.rows[problem_id].get("summary", "")}
            for problem_id in candidates]
    matches = run.model.match(item, rows) if run.model else None
    if matches is not None:
        return [(row["problem_id"], row["relation"], "kimi-k3", row["evidence_quote"]) for row in matches]
    if default is None:
        # A reply that broke the schema counts as "no match"; an empty or cut-off one does not.
        rejected = run.model and run.model.last_kind == "invalid" and not run.model.last_incomplete
        return [] if rejected else None
    return [(problem_id, default, "deterministic", "") for problem_id in candidates]


def adapter_palomar(run: Run) -> Result:
    state = run.source_state("palomar")
    since = (parse_time(state.get("checked_at")) or run.now - timedelta(days=14)) - timedelta(days=1)
    result = Result(revision=timestamp(run.now))
    seen = set(state.get("seen") or [])
    for term in ("erdos", "kourovka"):
        query = urlencode({"q": term, "order": "registered", "from": since.strftime("%Y-%m-%d")})
        body = ps.fetch_ok(run.fetcher, f"{ps.PALOMAR_API}?{query}", allowed_hosts=("data.palomar-registry.org",),
                           max_bytes=600 * 1024, accept="application/json")
        for entry in ps.parse_palomar_results(body):
            key = f"{entry['id']}v{entry['version']}"
            if key in seen:
                continue
            seen.add(key)
            candidates = references(" ".join([entry["title"], *entry["theorem_names"]]), run.rows)
            if not candidates:
                continue
            item = {"title": entry["title"], "text": entry["abstract"]}
            for problem_id, relation, matched_by, quote_text in _classified(run, item, candidates, "formalization") or []:
                if relation == "mentions":
                    continue
                result.signals.append((problem_id, {"title": f"Palomar: {entry['title']}", "url": entry["url"],
                                                    "date": entry["published_at"], "relation": relation, "source": "palomar",
                                                    "matched_by": matched_by, "evidence_quote": quote_text}))
    state["seen"] = sorted(seen)[-2000:]
    return result


def _arxiv_targets(run: Run) -> list[tuple[str, str]]:
    """Named, unsolved rows worth a targeted arXiv title search: (problem id, phrase)."""
    targets = []
    for row in run.rows.values():
        if row["category"] == "solved":
            continue
        title = row["title"]
        if row["source"] == "erdos":
            if "·" not in title:
                continue
            phrase = title.split("·", 1)[1].strip()
        elif row["source"] in ("clay", "hilbert", "smale", "ramanujan", "wikipedia"):
            phrase = re.sub(r"^(?:Hilbert|Smale)\s+\d+[^:]*:\s*", "", title).split("(")[0].strip()
        else:
            continue
        phrase = re.sub(r"[\"“”]", "", phrase)
        if 6 <= len(phrase) <= 80:
            targets.append((row["id"], phrase))
    return sorted(targets)


def arxiv_quota(targets: int) -> int:
    """Queries per run that search every target once per ``ARXIV_INTERVAL`` (six-hourly runs)."""
    runs = max(1, int(ARXIV_INTERVAL / timedelta(minutes=REFRESH_MINUTES)))
    return min(ARXIV_MAX_QUERIES_PER_RUN, max(ARXIV_QUERIES_PER_RUN, -(-targets // runs)))


def adapter_arxiv(run: Run) -> Result:
    """Targeted title searches, each target about once a week; each new entry is classified by the model.

    Every target remembers when its search last completed (``last``), and a search
    looks back to that time (two weeks for a target never searched), so a paper
    published between two searches of its target is not skipped.  A run searches
    the targets that are due, oldest first, enough of them (``arxiv_quota``) to
    search every target once per ``ARXIV_INTERVAL``.  An entry counts as seen only
    once the model classified it, and a target's time advances only when all its
    entries were.  When the model runs out (run cap, daily budget, broker busy) or
    a query fails, the rest waits for the next run.
    """
    state = run.source_state("arxiv")
    state.pop("cursor", None)  # the single weekly batch this replaced
    result = Result(revision=state.get("queried_at") or timestamp(run.now))
    if run.model is None:
        result.note = "skipped: targeted arXiv matches need the model"
        return result
    targets = _arxiv_targets(run)
    phrases = dict(targets)
    last = {problem_id: when for problem_id, when in (state.get("last") or {}).items()
            if problem_id in phrases and parse_time(when)}
    due = sorted((last.get(problem_id, ""), problem_id) for problem_id in phrases
                 if problem_id not in last or run.now - parse_time(last[problem_id]) >= ARXIV_INTERVAL)
    batch = [problem_id for _, problem_id in due[:arxiv_quota(len(targets))]]
    if not batch:
        state["last"] = last
        result.note = "weekly; not due"
        return result
    seen = set(state.get("seen") or [])
    done = 0
    for index, problem_id in enumerate(batch):
        if not run.model.available():
            break
        if index:
            run.sleeper(3.2)  # arXiv API politeness
        query = urlencode({"search_query": f'ti:"{phrases[problem_id]}"', "sortBy": "submittedDate", "sortOrder": "descending",
                           "max_results": "5"})
        try:
            entries = ps.parse_arxiv_feed(ps.fetch_ok(run.fetcher, f"{ps.ARXIV_API}?{query}", allowed_hosts=("export.arxiv.org",),
                                                      max_bytes=512 * 1024))
        except ps.SourceError as error:
            result.error = str(error)
            break
        cutoff = (parse_time(last.get(problem_id)) or run.now - ARXIV_LOOKBACK) - timedelta(days=2)
        complete = True
        for entry in entries:
            published = parse_time(entry["published"])
            key = entry["id"] + entry["version"]
            if key in seen or (published and published < cutoff):
                continue
            classified = _classified(run, {"title": entry["title"], "text": entry["summary"]}, [problem_id], None)
            if classified is None and run.model and run.model.last_kind == "invalid":
                # An empty or cut-off reply: try this entry again on a later run, a limited number of times.
                tries = state.setdefault("incomplete", {})
                tries[key] = int(tries.get(key) or 0) + 1
                if tries[key] < ARXIV_INCOMPLETE_TRIES:
                    complete = False
                    break
                classified = []
            if classified is None:
                complete = False
                break
            seen.add(key)
            state.get("incomplete", {}).pop(key, None)
            for target, relation, matched_by, quote_text in classified:
                if relation == "mentions":
                    continue
                result.signals.append((target, {"title": f"arXiv: {entry['title']}", "url": entry["url"],
                                                "date": entry["published"], "relation": relation, "source": "arxiv",
                                                "matched_by": matched_by, "evidence_quote": quote_text}))
        if not complete:
            break
        last[problem_id] = run.stamp
        done += 1
    state["last"] = last
    state["seen"] = sorted(seen)[-3000:]
    if done:
        state["queried_at"] = result.revision = run.stamp
    if done < len(batch) and not result.error:
        result.note = f"model unavailable or out of budget after {done} of {len(batch)} queries; resumes next run"
    return result


class _DiffLines(HTMLParser):
    """Text of the added (or deleted) lines of a MediaWiki diff table."""

    def __init__(self, line_class: str = "diff-addedline"):
        super().__init__(convert_charrefs=True)
        self.line_class = line_class
        self.depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        classes = dict(attrs).get("class", "") or ""
        if tag == "td" and self.line_class in classes.split():
            self.depth = 1
        elif self.depth:
            self.depth += 1

    def handle_endtag(self, tag):
        if self.depth:
            self.depth -= 1
            if not self.depth:
                self.parts.append("\n")

    def handle_data(self, data):
        if self.depth:
            self.parts.append(data)


def added_text(diff_html: str, limit: int = 6000, *, line_class: str = "diff-addedline") -> str:
    parser = _DiffLines(line_class)
    parser.feed(diff_html[:400_000])
    return re.sub(r"[ \t]+", " ", "".join(parser.parts)).strip()[:limit]


def removed_text(diff_html: str, limit: int = 60_000) -> str:
    return added_text(diff_html, limit, line_class="diff-deletedline")


def _wiki_articles(run: Run) -> dict[str, list[str]]:
    """Article title → catalogue ids, for rows that came from (or merged) Wikipedia entries."""
    fact = run.facts["wikipedia"]
    by_key = {row["wp_id"]: row for row in [*fact["entries"], *fact["solved"]]}
    by_name = {}
    for row in [*fact["entries"], *fact["solved"]]:
        if row.get("name") and row.get("article_title"):
            by_name.setdefault(ps.slugify(row["name"]), row["article_title"])
    articles: dict[str, list[str]] = {}
    for row in run.rows.values():
        titles = []
        if row.get("source_key") in by_key and by_key[row["source_key"]].get("article_title"):
            titles.append(by_key[row["source_key"]]["article_title"])
        for alias in row.get("aliases") or []:
            if ps.slugify(alias) in by_name:
                titles.append(by_name[ps.slugify(alias)])
        for title in dict.fromkeys(titles):
            articles.setdefault(title, []).append(row["id"])
    return articles


def adapter_wikipedia(run: Run) -> Result:
    fact = run.facts["wikipedia"]
    state = run.source_state("wikipedia")
    revids = state.setdefault("revids", {})
    baseline = {row["article_title"]: row.get("article_revid") for row in [*fact["entries"], *fact["solved"]] if row.get("article_title")}
    articles = _wiki_articles(run)
    titles = sorted(articles)
    list_title = fact.get("page") or "List of unsolved problems in mathematics"
    result = Result()
    current: dict[str, int] = {}
    # Up to 50 titles per query, and never a URL longer than the fetcher accepts.
    for index, (batch, url) in enumerate(ps.title_query_batches([list_title, *titles])):
        if index:
            run.sleeper(1.0)
        current.update(ps.wikipedia_revids(run.fetcher, url, batch))
    list_revid = current.get(list_title)
    result.revision = str(list_revid or fact["revision_id"])
    if list_revid and list_revid != fact["revision_id"]:
        result.note = f"the list has revision {list_revid}; the catalogue is pinned to {fact['revision_id']}"
    pending = state.setdefault("pending", {})  # title → when a change was first left to settle
    invalid = state.setdefault("invalid", {})  # title → rejected model replies for its current change
    reported: dict[str, list[tuple[str, str]]] = {}
    for key, signal in (run.state.get("signals") or {}).items():
        if signal.get("source") == "wikipedia" and signal.get("article") and signal.get("private_quote"):
            reported.setdefault(signal["article"], []).append((key, signal["private_quote"]))
    for title in titles:
        old = revids.get(title) or baseline.get(title)
        new = current.get(title)
        if not new or not old or new == old:
            if new:
                revids.setdefault(title, new)
            continue
        triage = run.model is not None and run.model.available()
        if not triage and title not in reported:
            continue  # keep the old revid so the change is triaged on a later run
        query = urlencode({"action": "compare", "format": "json", "formatversion": "2", "fromrev": str(old),
                           "torev": str(new), "prop": "diff|ids|timestamp"})
        run.sleeper(1.0)
        try:
            data = json.loads(ps.fetch_ok(run.fetcher, f"{ps.WIKIPEDIA_API}?{query}", allowed_hosts=("en.wikipedia.org",),
                                          max_bytes=2 * 1024 * 1024, accept="application/json"))
        except (ps.SourceError, ValueError) as error:
            # Later articles keep their revids; what was triaged so far is kept.
            result.error = str(error) if isinstance(error, ps.SourceError) else "Wikipedia returned an invalid diff"
            break
        compare = data.get("compare") if isinstance(data, dict) else None
        if not isinstance(compare, dict):
            # An error reply (maxlag, a deleted revision) must not pass for an empty diff,
            # or the change would be marked triaged without being read.
            result.error = "The Wikipedia API answered with an error"
            break
        body = str(compare.get("body") or "")
        text, gone = added_text(body), " ".join(removed_text(body).split())
        # A report whose quoted text this change removed again (a reverted edit) is withdrawn.
        for key, quote_text in reported.get(title, []):
            if quote_text in gone and quote_text not in " ".join(text.split()):
                result.retract.append(key)
        if not triage:
            continue
        edited = parse_time(compare.get("totimestamp"))
        waiting = parse_time(pending.get(title)) or run.now
        if edited and run.now - edited < WIKI_SETTLE and run.now - waiting < WIKI_SETTLE_MAX:
            pending.setdefault(title, run.stamp)  # most vandalism is reverted within hours
            continue
        verdicts = []
        for problem_id in articles[title]:
            verdict = run.model.triage({"title": run.rows[problem_id]["title"], "article_title": title, "added_text": text})
            if verdict is None:
                break
            verdicts.append((problem_id, verdict))
        if len(verdicts) < len(articles[title]):
            if run.model.last_kind != "invalid":
                continue  # the model is unavailable: triage this change on a later run
            invalid[title] = invalid.get(title, 0) + 1
            if invalid[title] < WIKI_INVALID_TRIES:
                continue
        for problem_id, verdict in verdicts:
            if verdict.get("status_relevant"):
                # Wikipedia text (CC BY-SA, editable by anyone) is never published:
                # the quote stays in the private state for reviewers.
                result.signals.append((problem_id, {
                    "title": f"Wikipedia: “{title}” was edited (a secondary source)",
                    "url": f"https://en.wikipedia.org/w/index.php?diff={new}&oldid={old}",
                    "date": str(compare.get("totimestamp") or "")[:10], "relation": verdict["relation"],
                    "source": "wikipedia", "matched_by": "kimi-k3",
                    "article": title, "private_quote": verdict["evidence_quote"]}))
        revids[title] = new
        pending.pop(title, None)
        invalid.pop(title, None)
    return result


def adapter_news(run: Run) -> Result:
    news = run.news or {}
    if news.get("_load_error"):
        raise ps.SourceError(news["_load_error"])
    result = Result(revision=str(news.get("updated_at") or "") or None)
    for item in news.get("items") or []:
        if not isinstance(item, Mapping):
            continue
        for link in item.get("problem_links") or []:
            if not isinstance(link, Mapping) or link.get("problem_id") not in run.rows:
                continue
            relation = NEWS_RELATIONS.get(link.get("relation"))
            if not relation or relation == "mentions":
                continue
            result.signals.append((link["problem_id"], {"title": f"News: {item.get('title', '')}", "url": item.get("url"),
                                                        "date": str(item.get("published_at") or "")[:10], "relation": relation,
                                                        "source": "news", "matched_by": "kimi-k3",
                                                        "evidence_quote": str(link.get("evidence") or "")[:240]}))
    return result


ADAPTERS: dict[str, Callable[[Run], Result]] = {
    "erdos": adapter_erdos, "kourovka": adapter_kourovka, "openai-math": adapter_openai, "clay": adapter_clay,
    "formal-conjectures": adapter_formal_conjectures, "palomar": adapter_palomar, "arxiv": adapter_arxiv,
    "wikipedia": adapter_wikipedia, "news": adapter_news,
}
DETERMINISTIC = ("erdos", "kourovka", "openai-math", "clay")


# ── assembly ──────────────────────────────────────────────────────────────────


def _merge_entries(results: Mapping[str, Result], rows: Mapping[str, dict], decided: Mapping[str, str],
                   headlined: set[str] = frozenset()) -> dict[str, dict]:
    """Deterministic entries merged per row.

    ``decided`` maps rows with a reviewed category decision to that category: a
    source change keeps it, and adds "Sources disagree" when the source now says
    something else (the catalogue may not be rebuilt with the decision yet, so the
    decision, not the row, is what the source is compared with).  ``headlined``
    rows have a reviewed headline, which a headline-only change does not replace.
    A decision with badges only stops nothing.  A category change also drops the
    badges the new category cannot carry (as the builder does).
    """
    merged: dict[str, dict] = {}
    for source in DETERMINISTIC:
        for problem_id, entry in (results.get(source).entries if results.get(source) else {}).items():
            if problem_id not in rows:
                continue
            target = merged.setdefault(problem_id, {"change": [], "sources": []})
            target["sources"].append(source)
            if "category" in entry and problem_id in decided:
                if entry["category"] != decided[problem_id]:
                    target["badges_add"] = sorted(set(target.get("badges_add", [])) | {"sources-conflict"})
                    target["change"].append("a reviewed decision keeps the category")
            elif "category" in entry:
                # Source-of-record solved outranks a withdrawal or marker removal.
                if "category" not in target or entry["category"] == "solved":
                    for key in ("category", "category_rule", "status_headline"):
                        target[key] = entry[key]
            elif entry.get("status_headline") and "category" not in target and problem_id not in headlined:
                target["status_headline"] = entry["status_headline"]
            for key in ("badges_add", "badges_remove"):
                values = [badge for badge in entry.get(key, []) if badge in CATALOGUE_BADGES]
                if values:
                    target[key] = sorted(set(target.get(key, [])) | set(values))
            if entry.get("evidence_add"):
                target.setdefault("evidence_add", []).extend(entry["evidence_add"])
            target["change"].extend(entry.get("change", []))
    final = {}
    for problem_id, entry in sorted(merged.items()):
        row = rows[problem_id]
        if entry.get("category") == row["category"] and entry.get("category_rule") == row["category_rule"]:
            for key in ("category", "category_rule"):
                entry.pop(key, None)
        if entry.get("category"):
            unfit = set() if entry["category"] == "solved" else set(SOLVED_ONLY_BADGES)
            if entry["category"] in ("solved", "ai-proposed"):
                unfit.add("ai-partial")
            entry["badges_remove"] = sorted(set(entry.get("badges_remove", [])) | unfit)
        added = [badge for badge in entry.get("badges_add", []) if badge not in row.get("badges", [])]
        removed = [badge for badge in entry.get("badges_remove", []) if badge in row.get("badges", []) and badge not in added]
        entry["badges_add"], entry["badges_remove"] = added, removed
        if not any(entry.get(key) for key in ("category", "status_headline", "badges_add", "badges_remove", "evidence_add")):
            continue
        final[problem_id] = {key: value for key, value in entry.items() if value not in (None, [], "")}
    return final


def _fingerprint(entry: Mapping[str, Any]) -> str:
    """What an entry changes.  Evidence links are left out: they name the upstream revision that
    was read (teorth's head, openai/math's head, the arXiv version), which moves with every
    unrelated upstream commit and would re-date a change that happened earlier."""
    body = {key: entry.get(key) for key in ("category", "category_rule", "badges_add", "badges_remove")}
    body["evidence"] = sorted([item["kind"], item["acceptance"], item["title"]] for item in entry.get("evidence_add", []))
    return sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


# The shapes of the private state, so that a hand-edited or damaged part is dropped
# (and rebuilt by its source) instead of failing every run with an AttributeError.
_SOURCE_STATE_TYPES = {"atom": dict, "http": dict, "snapshot": dict, "entries": dict, "status": dict, "error": dict,
                       "revids": dict, "pending": dict, "invalid": dict, "last": dict, "withdrawal_heads": dict, "incomplete": dict,
                       "seen": list, "withdrawn": list, "unmatched": list, "lean_docs": list, "revision": str,
                       "last_seen": str, "checked_at": str, "revision_public": str, "queried_at": str, "history": str,
                       "version": int}


def repair_state(state: dict) -> dict:
    """``state`` with every part of the wrong shape removed (its source rebuilds it on the next check)."""
    for key in ("sources", "signals", "entries_seen", "budget"):
        if not isinstance(state.get(key), dict):
            state.pop(key, None)
    for key in ("signals", "entries_seen"):
        if key in state:
            state[key] = {name: value for name, value in state[key].items() if isinstance(value, dict)}
    sources = state.get("sources") or {}
    for name in list(sources):
        if not isinstance(sources[name], dict):
            del sources[name]
            continue
        for key, kind in _SOURCE_STATE_TYPES.items():
            if sources[name].get(key) is not None and not isinstance(sources[name][key], kind):
                del sources[name][key]
        if isinstance(sources[name].get("entries"), dict):
            sources[name]["entries"] = {row: entry for row, entry in sources[name]["entries"].items() if isinstance(entry, dict)}
        for key, kind in (("atom", list), ("http", dict)):  # a cached body would be reused on every 304
            cache = sources[name].get(key)
            if isinstance(cache, dict) and "value" in cache and not isinstance(cache["value"], kind):
                del sources[name][key]
    return state


def refresh(catalogue: Mapping[str, Any], facts: Mapping[str, Mapping], *, state: dict | None = None,
            previous: Mapping[str, Any] | None = None, decisions: Mapping[str, Any] | None = None, fetcher=None,
            now: datetime | None = None, model_cap: int | None = None, model_request=None,
            news: Mapping[str, Any] | None = None, sources: Iterable[str] = SOURCES,
            sleeper: Callable[[float], None] = time.sleep) -> tuple[dict, dict]:
    """One updater run: returns (public overlay, private state).

    ``model_cap`` is the daily Kimi call cap (None or 0: no model calls); the
    count persists in ``state["budget"]``.  The input state is not modified.
    """
    if catalogue.get("schema_version") != 3 or not isinstance(catalogue.get("problems"), list):
        raise StatusError("The status overlay needs a schema-3 catalogue")
    now = (now or datetime.now(UTC)).astimezone(UTC)
    state = repair_state(json.loads(json.dumps(state or {})))
    state["schema_version"] = 1
    decisions = decisions or {}
    model = (Model(state.setdefault("budget", {}), cap=model_cap, request=model_request, today=now.strftime("%Y-%m-%d"))
             if model_cap else None)
    counter = CountingFetcher(fetcher or ps.HTTPSFetcher())
    run = Run(catalogue, facts, state, decisions, counter, now, model, news, sleeper)
    selected = [source for source in sources if source in ADAPTERS]
    results: dict[str, Result] = {}
    statuses: dict[str, dict] = {}
    errors: dict[str, dict] = {}
    upstream: set[str] = set()  # sources that got an answer from upstream in this run
    mismatch = []
    hashes = catalogue.get("source_input_hashes") or {}
    for name, fact in facts.items():
        recorded = hashes.get(f"problem_sources/{FACT_FILES.get(name, '')}")
        if recorded and fact.get("_sha256") and recorded != fact["_sha256"]:
            mismatch.append({"source_id": "catalogue", "error": "The fact files differ from the ones the catalogue was built from"})
            break
    for source in selected:
        cache = run.source_state(source)
        before = counter.responses
        try:
            result = ADAPTERS[source](run)
            ok = result.error is None
            if source in DETERMINISTIC:
                cache["entries"] = result.entries
            if result.error:
                errors[source] = {"source_id": source, "error": ps.clean_text(result.error, 180)}
        except Exception as error:  # noqa: BLE001 - each source fails closed
            ok = False
            message = str(error)[:180] if isinstance(error, (ps.SourceError, ValueError)) else f"Source unavailable ({type(error).__name__})"
            errors[source] = {"source_id": source, "error": message}
            result = Result(entries=cache.get("entries") or {}, revision=cache.get("revision_public"))
        results[source] = result
        if counter.responses > before:
            upstream.add(source)
        if ok:
            cache["checked_at"] = run.stamp
            cache["revision_public"] = result.revision
        status = {"id": source, "revision": result.revision or cache.get("revision_public"),
                  "checked_at": cache.get("checked_at"), "ok": ok}
        if result.note:
            status["note"] = ps.clean_text(result.note, 240)
        statuses[source] = cache["status"] = {key: value for key, value in status.items() if value is not None}
        cache["error"] = errors.get(source)
    # Sources left out of this run (--only) keep their last published entries,
    # status and error, so a partial run never retracts or re-dates them.
    for source in SOURCES:
        cache = (state.get("sources") or {}).get(source) or {}
        if source in selected:
            continue
        if source in DETERMINISTIC and cache.get("entries"):
            results[source] = Result(entries=cache["entries"], revision=cache.get("revision_public"))
        if cache.get("status"):
            statuses[source] = cache["status"]
        if cache.get("error"):
            errors[source] = cache["error"]
    reviewed = {problem_id: decision for problem_id, decision in (decisions.get("decisions") or {}).items()
                if isinstance(decision, Mapping)}
    entries = _merge_entries(results, run.rows, {problem_id: decision["category"] for problem_id, decision in reviewed.items()
                                                 if decision.get("category")},
                             {problem_id for problem_id, decision in reviewed.items() if decision.get("status_headline")})
    # changed_at: when this exact change was first observed.
    seen_entries = state.setdefault("entries_seen", {})
    for problem_id, entry in entries.items():
        mark = _fingerprint(entry)
        known = seen_entries.get(problem_id)
        if not known or known.get("fingerprint") != mark:
            seen_entries[problem_id] = known = {"fingerprint": mark, "changed_at": run.stamp}
        entry["changed_at"] = known["changed_at"]
        entry["change"] = clip_words("; ".join(entry.pop("change", [])), MAX_CHANGE) or "Source update"
    for problem_id in list(seen_entries):
        if problem_id not in entries:
            del seen_entries[problem_id]
    entry_sources = {problem_id: entry.pop("sources")[0] for problem_id, entry in entries.items()}
    # Signal ledger: new signals keep their first_seen; rejected and retracted ones disappear.
    ledger = state.setdefault("signals", {})
    rejected = set((decisions.get("rejected_signals") or {}).keys())
    for source, result in results.items():
        for key in result.retract:
            ledger.pop(key, None)
        for problem_id, raw in result.signals:
            if problem_id not in run.rows:
                continue
            try:
                url = ps.safe_url(raw.get("url")) or ""
                key = signal_key(problem_id, url)
                first = (ledger.get(key) or {}).get("first_seen") or run.stamp
                public = {name: value for name, value in raw.items() if name not in PRIVATE_SIGNAL_KEYS}
                private = {name: raw[name] for name in PRIVATE_SIGNAL_KEYS if raw.get(name)}
                ledger[key] = {"problem_id": problem_id, **make_signal(problem_id=problem_id, first_seen=first, **public), **private}
            except (ValueError, TypeError):
                continue
    cutoff = now - timedelta(days=SIGNAL_TTL_DAYS)
    for key in list(ledger):
        signal = ledger[key]
        if signal.get("problem_id") not in run.rows or (parse_time(signal.get("first_seen")) or now) < cutoff:
            del ledger[key]
    signals: dict[str, list] = {}
    for key, signal in sorted(ledger.items(), key=lambda pair: (pair[1]["first_seen"], pair[0]), reverse=True):
        if key in rejected:
            continue
        bucket = signals.setdefault(signal["problem_id"], [])
        if len(bucket) < MAX_SIGNALS_PER_PROBLEM and sum(len(items) for items in signals.values()) < MAX_SIGNALS:
            bucket.append({k: v for k, v in signal.items() if k != "problem_id" and k not in PRIVATE_SIGNAL_KEYS})
    signals = {key: value for key, value in sorted(signals.items()) if value}
    recent = sorted(({"id": problem_id, "date": entry["changed_at"][:10], "change": entry["change"],
                      "source": entry_sources[problem_id]} for problem_id, entry in entries.items()),
                    key=lambda row: (row["date"], row["id"]), reverse=True)[:MAX_RECENT]
    counts = {category: 0 for category in CATEGORY_IDS}
    for problem_id, row in run.rows.items():
        counts[entries.get(problem_id, {}).get("category", row["category"])] += 1
    # The broker being down or refusing is an operator signal (budget use is in "model").
    broker = [{"source_id": "kimi-k3", "error": message} for kind, message in (model.errors if model else [])
              if kind in ("unavailable", "refused")][:1]
    # A refresh counts as successful only when an upstream source actually answered:
    # the local news file and sources that were not due do not keep the page fresh.
    successful = any(statuses[source]["ok"] for source in upstream)
    previous = previous or {}
    overlay = {
        "schema_version": 1, "catalogue_sha256": catalogue["content_sha256"], "generated_at": run.stamp,
        "last_checked_at": run.stamp,
        "last_successful_refresh_at": run.stamp if successful else previous.get("last_successful_refresh_at"),
        "refresh_interval_minutes": REFRESH_MINUTES, "sources": [statuses[source] for source in SOURCES if source in statuses],
        "entries": entries, "signals": signals, "recent_changes": recent, "category_counts": counts,
        "model": model.public() if model else {"name": "kimi-k3", "calls_today": 0, "daily_cap": 0},
        "source_errors": mismatch + [errors[source] for source in SOURCES if source in errors] + broker,
    }
    validate_overlay(overlay, catalogue)
    return overlay, state


def validate_overlay(overlay: Mapping[str, Any], catalogue: Mapping[str, Any]) -> None:
    """The overlay contract: ids exist, enums only, no new rows, https links."""
    ids = {row["id"] for row in catalogue["problems"]}
    if overlay.get("catalogue_sha256") != catalogue.get("content_sha256"):
        raise StatusError("Overlay computed against another catalogue")
    for problem_id, entry in overlay.get("entries", {}).items():
        if problem_id not in ids:
            raise StatusError("Overlay entries may not add rows")
        if "category" in entry and (entry["category"] not in RULES or entry.get("category_rule") not in RULES[entry["category"]]):
            raise StatusError("Overlay entry has an invalid category")
        for badge in entry.get("badges_add", []) + entry.get("badges_remove", []):
            if badge not in CATALOGUE_BADGES:
                raise StatusError("Overlay entry has an unknown badge")
        for item in entry.get("evidence_add", []):
            evidence_item(item)
    for problem_id, items in overlay.get("signals", {}).items():
        if problem_id not in ids:
            raise StatusError("Overlay signals must refer to catalogue rows")
        for item in items:
            make_signal(problem_id=problem_id, **{key: value for key, value in item.items()})
    if sum(overlay["category_counts"].values()) != len(ids):
        raise StatusError("Overlay counts do not cover the catalogue exactly")


def atomic_json(path: Path, data: Mapping[str, Any], mode: int = 0o644) -> None:
    from agent_monitor.news import atomic_json as write

    write(path, dict(data), mode=mode)


def load_facts(directory: Path) -> dict[str, dict]:
    facts = {}
    for name, filename in FACT_FILES.items():
        content = (directory / filename).read_bytes()
        value = json.loads(content)
        value["_sha256"] = sha256(content).hexdigest()
        facts[name] = value
    return facts


def load_optional(path: Path | None, *, limit: int = 32 * 1024 * 1024) -> dict:
    if path is None or not path.exists():
        return {}
    if path.stat().st_size > limit:
        raise StatusError(f"{path.name} exceeds the size limit")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        raise StatusError(f"{path.name} is not valid JSON") from None
    if not isinstance(value, dict):
        raise StatusError(f"{path.name} is not a JSON object")
    return value
