"""Bounded, stdlib-only publisher for the public mathematical research news feed.

Feed transport never follows an unvalidated redirect and connects only to a
validated public IP while preserving the publisher's TLS hostname. Source
headlines, URLs, publication dates, attribution and status are never model edits.
Kimi K3 tasks (summaries, triage, story grouping, problem links) are injected
callables bound to supplied ids, and their results are validated again here.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
import html
from html.parser import HTMLParser
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
import tempfile
import time
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET
import zlib

UTC = timezone.utc
MAX_BYTES = 2 * 1024 * 1024
MAX_FEED_ENTRIES = 1000
MAX_FEED_ITEMS = 300
MAX_MODEL_ITEMS = 6
MAX_SUMMARY_CALLS = 2
MAX_TRIAGE_ITEMS = 20
MAX_LINK_ITEMS = 8
MAX_SOURCES = 40
MAX_CHANGELOG_EXCERPT = 4000
# Revisions of a changelog fetched per run to verify which commit added a section.
MAX_REVISION_FETCHES = 4
# Model stages start only within this many seconds of a run's start, so a run
# (and a backfill) ends well inside the unit's 15-minute start timeout.
MODEL_SECONDS = 600
# The timer starts within a minute of :00/:30; a retry due a little after the
# next start must not wait a further half hour.
RETRY_SLACK = timedelta(minutes=5)
CATEGORIES = frozenset({"mathematics", "models", "research"})
STATUSES = frozenset({"Published", "Proof announced", "Model release", "Research result", "Preprint", "Update", "Correction"})
SOURCE_TYPES = frozenset({"rss", "atom", "github-commits", "changelog", "arxiv-api", "manual"})
ROLES = frozenset({"primary", "journalism", "commentary", "preprint", "repository"})
STORY_ROLES = frozenset({"primary", "journalism", "repository"})
# Anyone can post a preprint or a blog comment, so a model rating of their text
# never makes them a highlight.
LOW_TRUST_ROLES = frozenset({"preprint", "commentary"})
CORRECTIVE = frozenset({"Update", "Correction"})
TOPICS = frozenset({"ai-math-result", "formalization", "model-release", "community-debate", "prize-or-award",
                    "problem-status", "tooling", "other"})
RELATIONS = frozenset({"mentions", "claims-partial-progress", "claims-resolution", "claims-counterexample",
                       "disputes-claim", "withdraws-claim", "formalization"})
MATCH_FIELDS = frozenset({"title", "excerpt", "feed_categories"})
TRIAGE_MODES = frozenset({"none", "borderline", "all"})
# Excerpts are long, so they get a deliberately narrow vocabulary: broad title
# terms such as "reasoning" match product posts and benchmarks of every kind.
EXCERPT_KEYWORDS = ("mathematics", "mathematical", "theorem", "proof", "proofs", "lean", "conjecture", "erdős",
                    "erdos", "formalization", "formalized", "olympiad", "imo", "frontiermath")
PROBLEM_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,159}")
STORY_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
SHA = re.compile(r"[0-9a-f]{40}")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
TRACKING = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid"})
ROLE_RANK = {"primary": 0, "repository": 1, "journalism": 2, "preprint": 3, "commentary": 4}


class NewsError(ValueError):
    """A public-safe error whose message contains no provider response or URL."""


def timestamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def publication_date(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 160:
        return None
    try:
        result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        try:
            result = parsedate_to_datetime(value.strip())
        except (ValueError, TypeError, IndexError, OverflowError):
            return None
    if result is None:
        return None
    # A few RSS publishers omit the zone; their unzoned calendar dates are UTC.
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    try:
        return result.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def _hostname(value: str) -> str:
    try:
        host = value.rstrip(".").encode("idna").decode("ascii").lower()
    except (UnicodeError, AttributeError):
        raise NewsError("Invalid trusted hostname") from None
    if (not host or len(host) > 253 or "." not in host or
            not re.fullmatch(r"[a-z0-9.-]+", host) or ".." in host or
            host.endswith((".localhost", ".local", ".internal", ".invalid", ".test"))):
        raise NewsError("Invalid trusted hostname")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise NewsError("IP-literal URLs are not permitted")


def trusted_url(value: object, hosts: tuple[str, ...] | list[str], keep_fragment: bool = False) -> str:
    if (not isinstance(value, str) or len(value) > 4096 or
            any(ord(char) < 33 for char in value)):
        raise NewsError("Invalid source URL")
    try:
        url = urlsplit(value)
        host = _hostname(url.hostname or "")
        if (url.scheme != "https" or url.username is not None or url.password is not None or
                url.port not in (None, 443) or host not in hosts):
            raise NewsError("URL is outside the trusted HTTPS source list")
    except (ValueError, UnicodeError):
        raise NewsError("URL is outside the trusted HTTPS source list") from None
    # Fragments are dropped except where they identify a section (changelogs,
    # reviewed links); an unusual fragment is dropped rather than trusted.
    fragment = url.fragment if keep_fragment and re.fullmatch(r"[A-Za-z0-9._~-]{1,120}", url.fragment or "") else ""
    return urlunsplit(("https", host, url.path or "/", url.query, fragment))


def canonical_url(value: object, hosts: tuple[str, ...] | list[str], keep_fragment: bool = False) -> str:
    url = urlsplit(trusted_url(value, hosts, keep_fragment))
    query = [(key, val) for key, val in parse_qsl(url.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in TRACKING]
    return urlunsplit((url.scheme, url.netloc, url.path, urlencode(sorted(query)), url.fragment))


def item_id(url: str) -> str:
    # A GitHub file keeps its identity across commits, so a section pinned to a
    # commit and the same section on the default branch are one item.
    identity = re.sub(r"^(https://github\.com/[^/]+/[^/]+/blob/)[^/]+/", r"\1-/", url)
    return "news-" + sha256(identity.encode()).hexdigest()[:24]


def _words(text: object) -> str:
    return " " + re.sub(r"[^\w]+", " ", str(text or "").casefold()) + " "


def _contains(text: str, terms) -> bool:
    return any(" " + re.sub(r"[^\w]+", " ", term.casefold()).strip() + " " in text for term in terms)


def _terms(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if (not isinstance(value, list) or len(value) > 80 or
            any(not isinstance(term, str) or not term.strip() or len(term) > 100 for term in value)):
        raise NewsError("Invalid relevance keyword configuration")
    return tuple(value)


def _bounded_int(value: object, default: int, low: int, high: int, message: str) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise NewsError(message)
    return value


@dataclass(frozen=True)
class CategoryRule:
    keywords: tuple[str, ...]
    category: str
    fields: tuple[str, ...] = ("title", "excerpt")


@dataclass(frozen=True)
class Changelog:
    """A Markdown history file split into one item per dated ``## Month D, YYYY`` section.

    With an ``evidence_source`` (a commit feed of the file), a section is
    published only once it is pinned to the commit that added it: the file at
    that commit (``revision_url``) has the section and the file at the commit
    before it does not.
    """
    item_url: str
    title: str
    evidence_source: str = ""
    evidence_days: int = 2
    correction_headings: tuple[str, ...] = ("withdrawals", "retractions", "corrections")
    commit_label: str = "Commit {sha} with these changes"
    related_links: tuple[tuple[str, str], ...] = ()
    revision_url: str = ""


@dataclass(frozen=True)
class Source:
    id: str
    name: str
    url: str
    feed_url: str | None
    allowed_hosts: tuple[str, ...]
    category: str = "research"
    status: str = "Published"
    include_keywords: tuple[str, ...] = ()
    exclude_keywords: tuple[str, ...] = ()
    manual: bool = False
    type: str = "rss"
    role: str = "primary"
    match_fields: tuple[str, ...] = ("title",)
    excerpt_keywords: tuple[str, ...] = ()
    category_keywords: tuple[str, ...] = ()
    category_rules: tuple[CategoryRule, ...] = ()
    retention: str = "replace"
    retention_days: int = 30
    announce_types: tuple[str, ...] = ()
    min_interval_minutes: int = 0
    triage: str = "none"
    publish: bool = True
    max_items: int = 10
    url_normalizer: str = ""
    changelog: Changelog | None = None
    importance: int | None = None
    topic: str = ""
    proposals: str = ""
    backfill_url: str | None = None
    # False: a card shows only its headline until it has a model summary.
    excerpt_fallback: bool = True

    @classmethod
    def from_dict(cls, value: dict) -> "Source":
        if not isinstance(value, dict):
            raise NewsError("Invalid source configuration")
        sid, name = value.get("id"), value.get("name")
        if (not isinstance(sid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", sid)
                or not isinstance(name, str) or not name.strip() or len(name) > 100):
            raise NewsError("Invalid source identity")
        hosts = value.get("allowed_hosts")
        if not isinstance(hosts, list) or not 1 <= len(hosts) <= 12:
            raise NewsError("Source must declare a bounded exact hostname list")
        hosts = tuple(dict.fromkeys(_hostname(host) for host in hosts))
        category, status = value.get("category", "research"), value.get("status", "Published")
        if category not in CATEGORIES or status not in STATUSES:
            raise NewsError("Invalid source category or status")
        keywords = {field: _terms(value.get(field, [])) for field in
                    ("include_keywords", "exclude_keywords", "excerpt_keywords", "category_keywords")}
        kind = value.get("type") or ("manual" if value.get("manual") is True else "rss")
        manual = value.get("manual", kind == "manual")
        if not isinstance(manual, bool) or kind not in SOURCE_TYPES or manual != (kind == "manual"):
            raise NewsError("Invalid source type")
        if manual and value.get("feed_url") is not None:
            raise NewsError("Manual sources must not declare a feed URL")
        feed_url = None if manual else trusted_url(value.get("feed_url"), hosts)
        role = value.get("role") or {"arxiv-api": "preprint", "changelog": "repository",
                                     "github-commits": "repository"}.get(kind, "primary")
        fields = value.get("match_fields") or ["title"]
        if (role not in ROLES or not isinstance(fields, list) or not set(fields) <= MATCH_FIELDS or
                value.get("triage", "none") not in TRIAGE_MODES or value.get("retention", "replace") not in ("replace", "rolling")
                or value.get("url_normalizer") not in (None, "", "arxiv-unversioned")
                or value.get("topic") not in (None, "", *TOPICS) or value.get("proposals") not in (None, "", *RELATIONS)
                or not isinstance(value.get("publish", True), bool) or not isinstance(value.get("excerpt_fallback", True), bool)):
            raise NewsError("Invalid source options")
        rules = value.get("category_rules") or []
        if not isinstance(rules, list) or len(rules) > 10:
            raise NewsError("Invalid category rules")
        category_rules = []
        for rule in rules:
            if (not isinstance(rule, dict) or rule.get("category") not in CATEGORIES or not rule.get("keywords") or
                    not set(rule.get("fields") or ["title", "excerpt"]) <= {"title", "excerpt"}):
                raise NewsError("Invalid category rules")
            category_rules.append(CategoryRule(_terms(rule.get("keywords")), rule["category"],
                                               tuple(rule.get("fields") or ("title", "excerpt"))))
        announce = value.get("announce_types") or []
        if not isinstance(announce, list) or len(announce) > 10 or any(
                not isinstance(term, str) or not re.fullmatch(r"[a-z-]{1,20}", term) for term in announce):
            raise NewsError("Invalid announce types")
        changelog = value.get("changelog")
        if (kind == "changelog") != isinstance(changelog, dict):
            raise NewsError("Changelog sources need a changelog section")
        spec = _changelog(changelog, hosts) if changelog else None
        backfill = value.get("backfill_url")
        return cls(sid, name.strip(), trusted_url(value.get("url"), hosts), feed_url, hosts, category, status,
                   manual=manual, type=kind, role=role, match_fields=tuple(fields), category_rules=tuple(category_rules),
                   retention=value.get("retention", "replace"),
                   retention_days=_bounded_int(value.get("retention_days"), 30, 1, 90, "Invalid retention"),
                   announce_types=tuple(announce),
                   min_interval_minutes=_bounded_int(value.get("min_interval_minutes"), 0, 0, 1440, "Invalid source interval"),
                   triage=value.get("triage", "none"), publish=value.get("publish", True),
                   max_items=_bounded_int(value.get("max_items"), 10, 1, 60, "Invalid per-source item cap"),
                   url_normalizer=value.get("url_normalizer") or "", changelog=spec,
                   importance=_bounded_int(value.get("importance"), None, 0, 3, "Invalid source importance"),
                   topic=value.get("topic") or "", proposals=value.get("proposals") or "",
                   backfill_url=None if backfill is None else trusted_url(backfill, hosts),
                   excerpt_fallback=value.get("excerpt_fallback", True), **keywords)

    def public(self) -> dict:
        record = {"id": self.id, "name": self.name, "url": self.url, "automated": not self.manual, "role": self.role}
        if not self.publish:
            record["evidence_only"] = True
        return record


def _changelog(value: dict, hosts: tuple[str, ...]) -> Changelog:
    item_url, title = value.get("item_url"), value.get("title")
    if (not isinstance(item_url, str) or "{anchor}" not in item_url or not isinstance(title, str) or
            "{date}" not in title or len(title) > 200):
        raise NewsError("Invalid changelog configuration")
    try:
        trusted_url(item_url.format(ref="main", anchor="section"), hosts, keep_fragment=True)
        str(value.get("commit_label", "{sha}")).format(sha="0" * 7)
    except (KeyError, IndexError, ValueError):
        raise NewsError("Invalid changelog configuration") from None
    links = value.get("related_links") or []
    if not isinstance(links, list) or len(links) > 5:
        raise NewsError("Invalid changelog configuration")
    related = []
    for link in links:
        label = plain_text(link.get("label") if isinstance(link, dict) else None, 120)
        if not label:
            raise NewsError("Invalid changelog configuration")
        related.append((label, trusted_url(link.get("url"), hosts, keep_fragment=True)))
    evidence = value.get("evidence_source") or ""
    headings = _terms(value.get("correction_headings")) or Changelog.correction_headings
    commit_label = value.get("commit_label") or Changelog.commit_label
    revision = value.get("revision_url") or ""
    if not isinstance(evidence, str) or not isinstance(commit_label, str) or len(commit_label) > 120:
        raise NewsError("Invalid changelog configuration")
    # A commit is accepted as a section's evidence only after reading the file at that commit.
    if evidence and (not isinstance(revision, str) or "{ref}" not in revision):
        raise NewsError("A changelog with an evidence source needs a revision_url with {ref}")
    if revision:
        try:
            trusted_url(revision.format(ref="0" * 40), hosts)
        except (KeyError, IndexError, ValueError, AttributeError):
            raise NewsError("Invalid changelog configuration") from None
    return Changelog(item_url, title, evidence, _bounded_int(value.get("evidence_days"), 2, 0, 14, "Invalid changelog configuration"),
                     tuple(term.casefold() for term in headings), commit_label, tuple(related), revision)


@dataclass(frozen=True)
class StoryRule:
    """A reviewed, config-defined story: its known member sources and article URLs."""
    id: str
    label: str
    sources: tuple[str, ...] = ()
    item_ids: tuple[str, ...] = ()
    since: str = ""
    until: str = ""


def _story_rules(rows: object, sources: list[Source]) -> list[StoryRule]:
    if rows is None:
        return []
    if not isinstance(rows, list) or len(rows) > 20:
        raise NewsError("Invalid story configuration")
    known = {source.id for source in sources}
    hosts = tuple(dict.fromkeys(host for source in sources for host in source.allowed_hosts))
    by_id = {source.id: source for source in sources}
    result = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not STORY_ID.fullmatch(row["id"]):
            raise NewsError("Invalid story configuration")
        label = plain_text(row.get("label"), 120)
        members = row.get("sources") or []
        urls = row.get("urls") or []
        if (not label or not isinstance(members, list) or not set(members) <= known or not isinstance(urls, list)
                or len(urls) > 50):
            raise NewsError("Invalid story configuration")
        ids = []
        for url in urls:
            canonical = canonical_url(url, hosts, keep_fragment=True)
            # Member URLs pass through the same normalizer as their source's items.
            for source in by_id.values():
                if urlsplit(canonical).hostname in source.allowed_hosts:
                    canonical = _normalize(canonical, source)
            ids.append(item_id(canonical))
        bounds = []
        for field in ("since", "until"):
            value = row.get(field)
            date = publication_date(value) if value else None
            if value and date is None:
                raise NewsError("Invalid story configuration")
            bounds.append(timestamp(date) if date else "")
        result.append(StoryRule(row["id"], label, tuple(members), tuple(ids), *bounds))
    if len({rule.id for rule in result}) != len(result):
        raise NewsError("Duplicate story identity")
    return result


def load_config(path: Path) -> tuple[list[Source], list[StoryRule]]:
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get("schema_version") not in (1, 2):
        raise NewsError("Unsupported source configuration")
    rows = data.get("sources")
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_SOURCES:
        raise NewsError("Expected between one and 40 trusted sources")
    sources = [Source.from_dict(row) for row in rows]
    if len({source.id for source in sources}) != len(sources):
        raise NewsError("Duplicate source identity")
    by_id = {source.id: source for source in sources}
    for source in sources:
        evidence = by_id.get(source.changelog.evidence_source) if source.changelog and source.changelog.evidence_source else None
        # Every commit that touched the file is needed to tell which one added a
        # section, so the evidence feed must not filter commits by keyword.
        if source.changelog and source.changelog.evidence_source and (
                evidence is None or evidence.type != "github-commits" or evidence.include_keywords or evidence.exclude_keywords):
            raise NewsError("A changelog evidence source must be a configured, unfiltered commit feed")
    return sources, _story_rules(data.get("stories"), sources)


def load_sources(path: Path) -> list[Source]:
    return load_config(path)[0]


@dataclass(frozen=True)
class FeedResponse:
    status: int
    body: bytes = b""
    etag: str = ""
    last_modified: str = ""


def _header(value: object) -> str:
    return value if isinstance(value, str) and len(value) <= 512 and not any(ord(c) < 32 for c in value) else ""


def _gunzip(data: bytes, limit: int) -> bytes:
    """Decompress one gzip member, never producing more than ``limit`` bytes."""
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        body = decompressor.decompress(data, limit + 1)
    except zlib.error:
        raise NewsError("Feed contains invalid compressed data") from None
    if len(body) > limit or decompressor.unconsumed_tail:
        raise NewsError("Feed exceeds size limit")
    if not decompressor.eof or decompressor.unused_data.strip(b"\0\r\n\t "):
        raise NewsError("Feed contains invalid compressed data")
    if len(body) > max(1024 * 1024, 200 * len(data)):
        raise NewsError("Feed exceeds compression ratio limit")
    return body


class HTTPSFetcher:
    def __init__(self, timeout: float = 15, max_bytes: int = MAX_BYTES, resolver=None, connection_factory=None):
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.resolver = resolver or socket.getaddrinfo
        self.connection_factory = connection_factory or http.client.HTTPSConnection
        self.context = ssl.create_default_context()
        # Advertise the protocol we speak: WordPress.com and other CDNs answer
        # TLS clients that offer no ALPN with HTTP 403.
        self.context.set_alpn_protocols(["http/1.1"])

    def public_addresses(self, host: str) -> list[str]:
        addresses = list(dict.fromkeys(row[4][0] for row in self.resolver(host, 443, type=socket.SOCK_STREAM)))
        if not addresses:
            raise NewsError("Source hostname did not resolve")
        if any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise NewsError("Source hostname resolved to a non-public address")
        return addresses

    def fetch(self, source: Source, headers: dict[str, str]) -> FeedResponse:
        current = source.feed_url
        for redirects in range(4):
            current = trusted_url(current, source.allowed_hosts)
            url = urlsplit(current)
            addresses = self.public_addresses(url.hostname)
            connection = self.connection_factory(url.hostname, 443, timeout=self.timeout, context=self.context)
            # Pin the checked address for TCP; HTTPSConnection still verifies TLS
            # against its original publisher hostname, avoiding a second DNS lookup.
            address = addresses[0]
            connection._create_connection = lambda ignored, timeout, source_address=None: socket.create_connection((address, 443), timeout, source_address)
            try:
                accept = ("text/plain, text/markdown;q=0.9, */*;q=0.1" if source.type == "changelog" else
                          "application/atom+xml, application/rss+xml, application/xml, text/xml")
                # gzip is accepted (and bounded) because some publishers' caches
                # serve it even to identity-only clients.
                request_headers = {"User-Agent": "AnsatzeNews/1.1 (+https://ansatze.ai/news)",
                                   "Accept": accept, "Accept-Encoding": "gzip"}
                for key in ("If-None-Match", "If-Modified-Since"):
                    if _header(headers.get(key)):
                        request_headers[key] = headers[key]
                connection.request("GET", urlunsplit(("", "", url.path or "/", url.query, "")), headers=request_headers)
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    if redirects == 3:
                        raise NewsError("Too many feed redirects")
                    location = response.getheader("Location")
                    if not location:
                        raise NewsError("Feed redirect has no target")
                    current = trusted_url(urljoin(current, location), source.allowed_hosts)
                    continue
                if response.status == 304:
                    return FeedResponse(304, etag=_header(response.getheader("ETag")), last_modified=_header(response.getheader("Last-Modified")))
                if response.status != 200:
                    return FeedResponse(response.status)
                encoding = response.getheader("Content-Encoding", "identity").strip().lower()
                if encoding not in ("", "identity", "gzip", "x-gzip"):
                    raise NewsError("Unsupported feed content encoding")
                length = response.getheader("Content-Length", "")
                if length.isdigit() and int(length) > self.max_bytes:
                    raise NewsError("Feed exceeds size limit")
                deadline = time.monotonic() + self.timeout
                body = bytearray()
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Feed body deadline exceeded")
                    if connection.sock is not None:
                        connection.sock.settimeout(min(self.timeout, remaining))
                    chunk = response.read1(min(65536, self.max_bytes + 1 - len(body)))
                    if not chunk:
                        break
                    body.extend(chunk)
                    if len(body) > self.max_bytes:
                        raise NewsError("Feed exceeds size limit")
                if encoding in ("gzip", "x-gzip"):
                    body = _gunzip(bytes(body), self.max_bytes)
                return FeedResponse(200, bytes(body), _header(response.getheader("ETag")), _header(response.getheader("Last-Modified")))
            finally:
                connection.close()
        raise NewsError("Too many feed redirects")


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        elif tag in ("br", "p", "div", "li"):
            self.parts.append(" ")
        elif tag in ("sup", "sub"):
            # "log<sup>0.99</sup> n" must not read "log0.99 n".
            self.parts.append("^" if tag == "sup" else "_")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        elif tag in ("p", "div", "li"):
            self.parts.append(" ")

    def handle_data(self, value):
        if not self.hidden:
            self.parts.append(value)


def plain_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    parser = _PlainText()
    parser.feed(value[:30000])
    # Without close() the parser holds back text after a trailing "&" ("Q&A").
    parser.close()
    text = re.sub(r"\s+", " ", "".join(parser.parts)).strip()
    return text[:limit].rstrip()


def _source_text(value: object, limit: int) -> str:
    """Plain source text without e-mail addresses, which are never news content."""
    text = plain_text(value, limit + 200)
    if "@" in text:
        text = re.sub(r"\s+", " ", EMAIL.sub("", text)).strip()
    return text[:limit].rstrip()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _field(entry, names: tuple[str, ...]) -> str:
    for name in names:
        for child in entry:
            if _local(child.tag) == name:
                return "".join(child.itertext())
    return ""


def _relevance(source: Source, title: str, excerpt: str = "", categories=()) -> str | None:
    """None, "strong" (title or feed-category match) or "weak" (excerpt-only match)."""
    text = _words(title)
    if _contains(text, source.exclude_keywords):
        return None
    by_category = "feed_categories" in source.match_fields and source.category_keywords
    by_excerpt = "excerpt" in source.match_fields
    if not source.include_keywords and not by_category and not by_excerpt:
        return "strong"
    if _contains(text, source.include_keywords):
        return "strong"
    if by_category and categories and _contains(_words(" | ".join(categories)), source.category_keywords):
        return "strong"
    if by_excerpt and excerpt and _contains(_words(excerpt[:1800]), source.excerpt_keywords or EXCERPT_KEYWORDS):
        return "weak"
    return None


def _category(source: Source, title: str, excerpt: str) -> str:
    for rule in source.category_rules:
        text = _words(" ".join([title if "title" in rule.fields else "", (excerpt or "")[:600] if "excerpt" in rule.fields else ""]))
        if _contains(text, rule.keywords):
            return rule.category
    return source.category


def _normalize(url: str, source: Source) -> str:
    if source.url_normalizer == "arxiv-unversioned":
        return re.sub(r"^(https://arxiv\.org/abs/(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Za-z]{2})?/\d{7}))v\d+$", r"\1", url)
    return url


def _item(source: Source, title: object, summary: object, url: object, published: object, now: datetime,
          days: int, category: str | None = None, status: str | None = None, *, categories=(), updated: object = None,
          excerpt_limit: int = 1800, keep_fragment: bool = False, date_precision: object = None) -> dict | None:
    date = publication_date(published)
    title = plain_text(title, 300)
    if not title or not date or date > now or date < now - timedelta(days=days):
        return None
    try:
        url = _normalize(canonical_url(url, source.allowed_hosts, keep_fragment), source)
    except NewsError:
        return None
    category, status = category or source.category, status or source.status
    if category not in CATEGORIES or status not in STATUSES:
        return None
    item = {"id": item_id(url), "title": title, "summary": _source_text(summary, 700), "url": url, "source": source.id,
            "category": category, "published_at": timestamp(date), "status": status,
            "excerpt": _source_text(summary, excerpt_limit)}
    labels = [label for label in (plain_text(value, 100) for value in list(categories or ())[:20]) if label]
    if labels:
        item["feed_categories"] = labels
    changed = publication_date(updated)
    if changed and date + timedelta(hours=1) < changed <= now:
        item["updated_at"] = timestamp(changed)
    if date_precision in ("day", "month", "year"):
        item["date_precision"] = date_precision
    return item


def _xml_root(body: bytes):
    if len(body) > MAX_BYTES:
        raise NewsError("Feed exceeds size limit")
    if b"\x00" in body:
        raise NewsError("Feed must use an ASCII-compatible XML encoding")
    # Banning DTDs rules out entity expansion: the references left (&amp;,
    # &#8217; ...) stand for one character each, so MAX_BYTES bounds them.
    # They are not counted: a maths blog's feed carries tens of thousands.
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", body, re.I):
        raise NewsError("Feed DTDs and entity declarations are forbidden")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise NewsError("Feed contains invalid XML") from None
    if _local(root.tag) not in ("rss", "feed", "rdf"):
        raise NewsError("Document is not an RSS or Atom feed")
    elements = list(root.iter())
    if len(elements) > 20000:
        raise NewsError("Feed exceeds element limit")
    return elements


def parse_feed(body: bytes, source: Source, now: datetime, days: int = 90) -> list[dict]:
    if source.type == "changelog":
        return parse_changelog(body, source, now, days)
    elements = _xml_root(body)
    result = []
    entries = (node for node in elements if _local(node.tag) in ("item", "entry"))
    for count, entry in enumerate(entries):
        if count >= MAX_FEED_ENTRIES or len(result) >= MAX_FEED_ITEMS:
            break
        title = plain_text(_field(entry, ("title",)), 300)
        summary = _field(entry, ("summary", "description", "encoded", "content"))
        excerpt = plain_text(summary, 1800)
        categories = [(child.text or child.get("term") or "").strip() for child in entry if _local(child.tag) == "category"]
        announce = (_field(entry, ("announce_type",)).strip().lower() or
                    (re.match(r"arXiv:\S+\s+Announce Type:\s*([a-z-]+)", excerpt, re.I) or [None, ""])[1].lower())
        if source.announce_types and announce and announce not in source.announce_types:
            continue
        match = _relevance(source, title, excerpt, categories)
        if not match:
            continue
        url = _field(entry, ("link",))
        for link in entry:
            if _local(link.tag) == "link" and link.get("href") and link.get("rel", "alternate") == "alternate":
                url = link.get("href")
                break
        if not url:
            url = _field(entry, ("guid",))
        item = _item(source, title, summary, url, _field(entry, ("published", "pubdate", "date", "updated")), now, days,
                     _category(source, title, excerpt), categories=categories, updated=_field(entry, ("updated",)))
        if item:
            item["match"] = match
            if announce:
                item["announce_type"] = announce[:20]
            result.append(item)
    return result


def _anchor(heading: str) -> str:
    # GitHub's heading anchors: lower case, punctuation removed, spaces to hyphens.
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def parse_changelog(body: bytes, source: Source, now: datetime, days: int = 90) -> list[dict]:
    """One item per dated ``## <Month D, YYYY>`` section of a repository history file.

    Titles and URLs come from configured templates plus the source heading; the
    date is the source-stated calendar date (day precision). A configured
    sub-heading such as "Withdrawals" makes the item a Correction.
    """
    if len(body) > MAX_BYTES or b"\x00" in body:
        raise NewsError("Changelog exceeds size limit")
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        raise NewsError("Changelog must be UTF-8 text") from None
    spec, items, section = source.changelog, [], None
    sections = []
    for line in text.splitlines()[:20000]:
        heading = re.fullmatch(r"(#{1,2})\s+(.+?)\s*#*\s*", line)
        if heading:
            section = [heading.group(2), []] if heading.group(1) == "##" else None
            if section:
                sections.append(section)
        elif section is not None:
            section[1].append(line)
    for heading, lines in sections[:200]:
        date = None
        for pattern in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
            try:
                date = datetime.strptime(heading.strip(), pattern).replace(tzinfo=UTC)
                break
            except ValueError:
                continue
        if date is None or date > now or date < now - timedelta(days=days):
            continue
        marks = [re.sub(r"[*_#`:]+", "", line).strip().casefold() for line in lines
                 if re.fullmatch(r"\s*(?:\*\*[^*]+\*\*|__[^_]+__|#{3,6}\s+\S.*)\s*:?\s*", line)]
        plain = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if re.fullmatch(r"(?:\*\*[^*]+\*\*|__[^_]+__|#{3,6}\s+\S.*)\s*:?", line):
                line = re.sub(r"[*_#`:]+", "", line).strip() + ":"
            elif re.match(r"[-*+]\s+", line):
                line = "• " + re.sub(r"^[-*+]\s+", "", line)
            plain.append(re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", line).replace("**", "").replace("`", ""))
        # A bullet list ends before the next sentence: "• last item. Next sentence", not "• last item Next sentence".
        for index, line in enumerate(plain):
            last_bullet = line.startswith("• ") and (index + 1 == len(plain) or not plain[index + 1].startswith("• "))
            if last_bullet and not re.search(r"[.;:!?…]$", line):
                plain[index] = line + "."
        excerpt = " ".join(plain)
        status = "Correction" if any(mark in spec.correction_headings for mark in marks) else None
        item = _item(source, spec.title.format(date=heading.strip()), excerpt,
                     spec.item_url.format(ref="main", anchor=_anchor(heading)), timestamp(date), now, days, None, status,
                     excerpt_limit=MAX_CHANGELOG_EXCERPT, keep_fragment=True, date_precision="day")
        if item:
            item["match"] = "strong"
            items.append(item)
    return items


def _error(error: Exception) -> str:
    return str(error)[:180] if isinstance(error, NewsError) else "Source unavailable (" + type(error).__name__ + ")"


_RETRYABLE = frozenset({"Feed returned HTTP 429", "Feed returned HTTP 408", "Unsupported feed content encoding",
                        "Feed contains invalid compressed data"})


def _merge_rolling(cached: object, fresh: list[dict], now: datetime, days: int) -> list[dict]:
    """Daily-batch feeds (arXiv RSS) replace their content every announcement;
    keep earlier items (first-seen date wins) until the retention window ends."""
    merged = {row["id"]: row for row in (cached if isinstance(cached, list) else [])
              if isinstance(row, dict) and isinstance(row.get("id"), str) and isinstance(row.get("published_at"), str)}
    for row in fresh:
        old = merged.get(row["id"])
        if old and old["published_at"] < row["published_at"]:
            row = {**row, "published_at": old["published_at"]}
        merged[row["id"]] = row
    cutoff = timestamp(now - timedelta(days=days))
    rows = [row for row in merged.values() if row["published_at"] >= cutoff]
    return sorted(rows, key=lambda row: (row["published_at"], row["id"]), reverse=True)[:MAX_FEED_ITEMS]


def _parse_key(source: Source) -> str:
    """The configuration that decides which entries of a feed become items.

    A 304 answer only says the feed is unchanged; entries that today's rules
    admit but the rules of the cached fetch did not (a new keyword) are not in
    the cache, so a changed configuration reads the whole feed once."""
    rules = (source.type, source.category, source.status, source.allowed_hosts, source.include_keywords,
             source.exclude_keywords, source.match_fields, source.excerpt_keywords, source.category_keywords,
             source.category_rules, source.announce_types, source.retention, source.retention_days,
             source.url_normalizer, source.changelog)
    return sha256(repr(rules).encode()).hexdigest()[:16]


def _fetch_source(source, cache, fetcher, now, days, sleeper, force=False):
    headers = {}
    if cache.get("feed_url") == source.feed_url and cache.get("parse_key") == _parse_key(source) and not force:
        for field, header in (("etag", "If-None-Match"), ("last_modified", "If-Modified-Since")):
            if _header(cache.get(field)):
                headers[header] = cache[field]
    last_error = None
    for attempt in range(2):
        try:
            response = fetcher.fetch(source, headers)
            if response.status == 304:
                if cache.get("feed_url") != source.feed_url or not isinstance(cache.get("items"), list):
                    raise NewsError("Feed returned 304 without a matching cache")
                return {**cache, "checked_at": timestamp(now), "etag": response.etag or cache.get("etag", ""),
                        "last_modified": response.last_modified or cache.get("last_modified", "")}, None, True
            if response.status != 200:
                raise NewsError("Feed returned HTTP " + str(response.status))
            items = parse_feed(response.body, source, now, days)
            if source.retention == "rolling":
                items = _merge_rolling(cache.get("items"), items, now, min(days, source.retention_days))
            return {"feed_url": source.feed_url, "items": items, "etag": response.etag,
                    "last_modified": response.last_modified, "checked_at": timestamp(now),
                    "parse_key": _parse_key(source)}, None, True
        except Exception as error:
            last_error = error
            # Retrying malformed XML or untrusted URLs cannot repair the source.
            retryable = not isinstance(error, NewsError) or str(error).startswith("Feed returned HTTP 5") or str(error) in _RETRYABLE
            if not retryable or attempt:
                break
            sleeper(.25)
    return cache, {"source_id": source.id, "error": _error(last_error)}, False


def _restore(row, source, now, days, *, seed=False):
    """Revalidate a cached or reviewed record against the current source configuration."""
    if not isinstance(row, dict):
        return None
    excerpt = row.get("excerpt", row.get("summary"))
    categories = row.get("feed_categories") if isinstance(row.get("feed_categories"), list) else ()
    if seed:
        category, status = row.get("category"), row.get("status")
    else:
        # Feed records follow today's rules, so a configuration change applies to cached items too.
        category = _category(source, plain_text(row.get("title"), 300), plain_text(excerpt, 1800))
        status = row.get("status") if source.type == "changelog" else None
    item = _item(source, row.get("title"), excerpt, row.get("url"), row.get("published_at"), now, days, category, status,
                 categories=categories, updated=row.get("updated_at"),
                 excerpt_limit=MAX_CHANGELOG_EXCERPT if source.type == "changelog" or seed else 1800,
                 keep_fragment=seed or source.type == "changelog", date_precision=row.get("date_precision"))
    if not item or seed:
        return item
    announce = row.get("announce_type")
    if source.announce_types and isinstance(announce, str) and announce and announce not in source.announce_types:
        return None
    item["match"] = "strong" if source.type == "changelog" else _relevance(source, item["title"], item["excerpt"], categories)
    if not item["match"]:
        return None
    if isinstance(announce, str) and announce:
        item["announce_type"] = announce[:20]
    return item


def _validated_existing(items, source, now, days):
    result = []
    for row in (items if isinstance(items, list) else [])[:MAX_FEED_ITEMS]:
        item = _restore(row, source, now, days)
        if item:
            result.append(item)
    return result


def _fingerprint(item):
    # Legacy (v1) annotation key; kept so state written by either version stays usable.
    return sha256(json.dumps({key: item[key] for key in ("title", "summary", "url", "source", "published_at", "status")}, sort_keys=True).encode()).hexdigest()


_VOLATILE = re.compile(r"^arXiv:\S+\s+Announce Type:\s*[a-z-]+\s*(?:Abstract:\s*)?", re.I)


def content_fingerprint(item: dict) -> str:
    """What a summary was written from: the title and the excerpt without volatile
    feed prefixes, so a re-announced preprint or a date change keeps its summary."""
    excerpt = _VOLATILE.sub("", str(item.get("excerpt") or item.get("summary") or ""))
    data = {"title": item.get("title", ""), "excerpt": excerpt[:MAX_CHANGELOG_EXCERPT]}
    return sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]


def _annotation_valid(annotation: object, item: dict) -> bool:
    if (not isinstance(annotation, dict) or not isinstance(annotation.get("summary"), str) or not annotation["summary"].strip()
            or _INPUT_TALK.search(annotation["summary"])):
        return False
    if annotation.get("content_fingerprint"):
        return annotation["content_fingerprint"] == content_fingerprint(item)
    return annotation.get("fingerprint") == _fingerprint(item)


def _recent(values: object, now: datetime, days: int) -> list[str]:
    cutoff = now - timedelta(days=days)
    return [value for value in (values if isinstance(values, list) else [])
            if isinstance(value, str) and (publication_date(value) or cutoff) > cutoff][-10:]


def _bounded(mapping: object, now: datetime, keep: set[str], days: int = 90, cap: int = 2000) -> dict:
    """Entries still in use, or written within ``days``; newest ``cap`` entries."""
    cutoff = timestamp(now - timedelta(days=days))
    rows = [(key, value) for key, value in (mapping.items() if isinstance(mapping, dict) else [])
            if isinstance(key, str) and isinstance(value, dict) and (key in keep or str(value.get("at") or "") >= cutoff)]
    rows.sort(key=lambda pair: (pair[0] in keep, str(pair[1].get("at") or "")), reverse=True)
    return dict(rows[:cap])


def _clean_text(value: object, limit: int) -> str:
    text = " ".join(str(value).split()) if isinstance(value, str) else ""
    # A character reference ("&lt;", "&sol;") would decode into markup or a link later.
    if (not text or len(text) > limit or re.search(r"https?://|www\.|<[A-Za-z/!]|\]\(|`", text, re.I) or
            any(ord(c) < 32 for c in text) or html.unescape(text) != text):
        return ""
    return text


# Model text about its own input ("the supplied excerpt gives no details") is not a summary of the source.
_INPUT_TALK = re.compile(r"\b(?:excerpts?|supplied (?:text|record|article|item)s?|(?:article|source) records?)\b", re.I)
# Footers that publishers append to every feed excerpt; they are not part of the quoted text.
_FEED_FOOTER = re.compile(r"\s*The post .{1,300}? (?:first appeared|appeared first) on [^.]{1,120}\.?\s*$")
_READ_MORE = re.compile(r"\s*(?:…|\.\.\.)?\s*Continue reading\s*(?:→|»)?\s*$")


def _model_text(value: object, limit: int) -> str:
    """Model-written text that is published exactly as written: already plain (the
    HTML-to-text pass leaves it unchanged), without links, markup or references,
    and about the source rather than about the text the model was given."""
    text = _clean_text(value, limit)
    return text if text and plain_text(text, limit) == text and not _INPUT_TALK.search(text) else ""


def _quoted_source(text: str) -> str:
    """A source excerpt as quoted on a card: without the feed's footer or "Continue reading" link text."""
    text = _FEED_FOOTER.sub("", text)
    shortened = _READ_MORE.sub("", text)
    return shortened.rstrip(" ,;:") + " …" if shortened != text and shortened else shortened


def _count(record: object, field: str) -> int:
    value = record.get(field) if isinstance(record, dict) else 0
    return min(20, value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _backoff(record: object, now: datetime) -> dict:
    failures = min(20, _count(record, "failures") + 1)
    return {"failures": failures, "retry_after": timestamp(now + timedelta(minutes=min(1440, 30 * 2 ** (failures - 1))))}


def _due(record: object, now: datetime) -> bool:
    return not isinstance(record, dict) or (publication_date(record.get("retry_after")) or now) <= now + RETRY_SLACK


def _source_due(source: Source, cache: dict, now: datetime) -> bool:
    checked = publication_date(cache.get("checked_at"))
    # Five minutes of slack absorbs the timer's randomized start.
    return (not source.min_interval_minutes or checked is None or cache.get("feed_url") != source.feed_url or
            now - checked >= timedelta(minutes=source.min_interval_minutes - 5))


def _anchors(body: bytes) -> list[str]:
    """Anchors of the ``## `` section headings in one revision of a changelog."""
    if len(body) > MAX_BYTES or b"\x00" in body:
        raise NewsError("Changelog exceeds size limit")
    found = {_anchor(heading.group(1)) for heading in
             (re.fullmatch(r"##\s+(.+?)\s*#*\s*", line) for line in body.decode("utf-8").splitlines()[:20000]) if heading}
    return sorted(found)[:500]


def _fragment(url: str) -> str:
    return url.rsplit("#", 1)[1] if "#" in url else ""


def _pin(value: object, item: dict, source: Source) -> dict | None:
    """``item`` as published when pinned to ``value`` (``{"sha", "commit_url"}``), or None."""
    spec = source.changelog
    if not isinstance(value, dict) or not isinstance(value.get("sha"), str) or not SHA.fullmatch(value["sha"]):
        return None
    sha, anchor = value["sha"], _fragment(item["url"])
    try:
        url = trusted_url(spec.item_url.format(ref=sha, anchor=anchor), source.allowed_hosts, keep_fragment=True)
        commit_url = trusted_url(value.get("commit_url"), source.allowed_hosts)
    except NewsError:
        return None
    if not commit_url.endswith("/commit/" + sha):
        return None
    links = [{"label": label, "url": link} for label, link in spec.related_links]
    links.append({"label": spec.commit_label.format(sha=sha[:7]), "url": commit_url})
    return {**item, "url": url, "related_links": links[:5]}


def _published_pin(row: object) -> dict | None:
    """The pin of a section as published before; used when the private state was lost."""
    match = re.search(r"/blob/([0-9a-f]{40})/", str(row.get("url") or "")) if isinstance(row, dict) else None
    for link in (row.get("related_links") if match and isinstance(row.get("related_links"), list) else []):
        if isinstance(link, dict) and isinstance(link.get("url"), str) and link["url"].endswith("/commit/" + match.group(1)):
            return {"sha": match.group(1), "commit_url": link["url"]}
    return None


def _verify(item: dict, spec: Changelog, commits: list[tuple], anchors) -> dict | str | None:
    """The commit that added the section: the file at that commit has the section's
    heading and the file at the previous commit of the feed does not.

    Returns the pin, None while that cannot be shown (no such commit yet, or a
    revision could not be read), or "before-window" when the section is older
    than the commits dated in its window."""
    stated, anchor = publication_date(item["published_at"]), _fragment(item["url"])
    if not anchor:
        return None
    # From 14 hours before the stated day (the earliest time zone) to evidence_days after it.
    start, end = stated - timedelta(hours=14), stated + timedelta(days=spec.evidence_days + 1)
    for index, (date, sha, url) in enumerate(commits):
        if not start <= date < end:
            continue
        present = anchors(sha)
        if present is None:
            return None
        if anchor not in present:
            continue
        if index:
            before = anchors(commits[index - 1][1])
            if before is None:
                return None
            if anchor in before:
                return "before-window"
        return {"sha": sha, "commit_url": url}
    return None


def _bind_changelogs(sources, cached, record, previous_items, skip, fetcher, now):
    """Pin every dated changelog section to the commit that added it, once.

    Pins live in the private state (and are recovered from the published
    document if the state is lost), so a commit that later leaves the feed
    changes nothing.  A section that cannot be pinned is not published; it is
    reported in the journal line with the time it was first held and why:
    "waiting" (retried every run) or "before-window" (older than the commits in
    its window; final, so publish it as a reviewed seed record if it matters).
    """
    record = record if isinstance(record, dict) else {}

    def section(key):
        value = record.get(key)
        return value if isinstance(value, dict) else {}

    pins = {key: value for key, value in section("pins").items() if isinstance(key, str) and isinstance(value, dict)}
    revisions = {key: value for key, value in section("revisions").items() if isinstance(key, str) and SHA.fullmatch(key)
                 and isinstance(value, dict) and isinstance(value.get("anchors"), list)}
    held_before = {key: value for key, value in section("held").items()
                   if isinstance(key, str) and isinstance(value, dict) and isinstance(value.get("since"), str)}
    previous = {row["id"]: row for row in previous_items if isinstance(row, dict) and isinstance(row.get("id"), str)}
    fetched, held, still_held, in_use = [0], [], {}, set()
    for source in sources:
        spec = source.changelog
        if not spec or source.id not in cached:
            continue
        if not spec.evidence_source:
            links = [{"label": label, "url": url} for label, url in spec.related_links]
            cached[source.id] = [{**item, "related_links": links} if links else item for item in cached[source.id]]
            continue
        commits = []
        for commit in cached.get(spec.evidence_source) or []:
            match = re.search(r"/commit/([0-9a-f]{40})$", str(commit.get("url") or ""))
            date = publication_date(commit.get("published_at"))
            if match and date:
                commits.append((date, match.group(1), commit["url"]))
        commits.sort()

        def anchors(sha, source=source, spec=spec):
            if sha in revisions:
                return {str(value) for value in revisions[sha]["anchors"]}
            if fetched[0] >= MAX_REVISION_FETCHES:
                return None
            fetched[0] += 1
            try:
                response = fetcher.fetch(replace(source, feed_url=spec.revision_url.format(ref=sha)), {})
                found = _anchors(response.body) if response.status == 200 else None
            except Exception:
                found = None
            if found is None:
                return None
            revisions[sha] = {"anchors": found, "at": timestamp(now)}
            return set(found)

        bound = []
        for item in cached[source.id]:
            if item["id"] in skip:
                # A reviewed seed record replaces this section.
                bound.append(item)
                continue
            in_use.add(item["id"])
            published, reason = _pin(pins.get(item["id"]), item, source), "waiting"
            if published is None:
                pin = _published_pin(previous.get(item["id"]))
                if pin is None and held_before.get(item["id"], {}).get("reason") == "before-window":
                    reason = "before-window"
                elif pin is None:
                    pin = _verify(item, spec, commits, anchors)
                    if pin == "before-window":
                        pin, reason = None, "before-window"
                published = _pin(pin, item, source)
                if published is not None:
                    pins[item["id"]] = {"sha": pin["sha"], "commit_url": pin["commit_url"], "at": timestamp(now)}
            if published is None:
                since = held_before.get(item["id"], {}).get("since") or timestamp(now)
                still_held[item["id"]] = {"since": since, "reason": reason}
                held.append({"section": source.id + "#" + _fragment(item["url"]), "since": since, "reason": reason})
                continue
            bound.append(published)
        cached[source.id] = bound
    return {"pins": _bounded(pins, now, in_use, 100, 500), "revisions": _bounded(revisions, now, set(), 30, 60),
            "held": still_held}, held


def _role(item: dict, by_source: dict) -> str:
    source = by_source.get(item.get("source"))
    return source.role if source else "primary"


def _story_lead(members: list[dict], by_source: dict) -> dict:
    return min(members, key=lambda item: (ROLE_RANK.get(_role(item, by_source), 5), -int(item.get("importance") or 0),
                                          item["published_at"], item["id"]))


def _model_stage(stage, function, payload, run):
    """Invoke one injected model task (one call), with the run's call cap, deadline and backoff.

    Returns None, without a call, when the stage is skipped."""
    if function is None:
        return None
    if (run["stopped"] or run["calls"] >= run["max_calls"] or not _due(run["backoff"].get(stage), run["now"])
            or run["clock"]() >= run["deadline"]):
        if stage not in run["skipped"]:
            run["skipped"].append(stage)
        return None
    run["calls"] += 1
    try:
        result = function(payload)
        run["backoff"].pop(stage, None)
        return result
    except Exception as error:
        kind = getattr(error, "kind", "")
        if kind == "budget":
            run["calls"] -= 1
        else:
            run["backoff"][stage] = _backoff(run["backoff"].get(stage), run["now"])
        run["errors"][stage] = kind if isinstance(kind, str) and kind else type(error).__name__
        # A busy or missing broker, or a spent budget, fails every later task too.
        if kind in ("budget", "unavailable", "refused"):
            run["stopped"] = True
        raise


def refresh(sources: list[Source], *, state: dict | None = None, previous: dict | None = None,
            seed: dict | None = None, now: datetime | None = None, fetcher=None,
            summarizer: Callable[[list[dict]], list[dict]] | None = None,
            triager: Callable[[list[dict]], list[dict]] | None = None,
            linker: Callable[[list[dict]], list[dict]] | None = None,
            grouper: Callable[[dict], dict] | None = None,
            changelog_summarizer: Callable[[dict], str] | None = None,
            problem_index=None, stories: list[StoryRule] | tuple = (), max_items: int = 60,
            max_age_days: int = 90, max_model_calls: int = 4, backfill: bool = False,
            sleeper=time.sleep, clock=time.monotonic, model_seconds: float = MODEL_SECONDS) -> tuple[dict, dict]:
    started = clock()
    if not sources or len(sources) > MAX_SOURCES or len({source.id for source in sources}) != len(sources):
        raise NewsError("Expected one to 40 unique trusted sources")
    if not 1 <= max_items <= 60 or not 30 <= max_age_days <= 90 or not 0 <= max_model_calls <= 20:
        raise NewsError("News limits must be 1–60 items, 30–90 days and at most 20 model calls")
    now = (now or datetime.now(UTC)).astimezone(UTC)
    state, previous, seed = (value if isinstance(value, dict) else {} for value in (state, previous, seed))
    fetcher = fetcher or HTTPSFetcher()
    by_source = {source.id: source for source in sources}
    feeds = state.get("feeds", {}) if isinstance(state.get("feeds", {}), dict) else {}
    previous_items = previous.get("items", []) if isinstance(previous.get("items", []), list) else []
    jobs, waiting = [], []
    for source in sources:
        if source.manual:
            continue
        cache = feeds.get(source.id, {})
        if not isinstance(cache, dict):
            cache = {}
        if "items" not in cache:
            # A lost state file must not erase the already published last good data.
            cache = {"items": [item for item in previous_items if isinstance(item, dict) and item.get("source") == source.id]}
        if backfill and source.backfill_url:
            jobs.append((replace(source, feed_url=source.backfill_url), cache, True))
        elif backfill or _source_due(source, cache, now):
            # A backfill re-reads every feed in full (no conditional request).
            jobs.append((source, cache, backfill))
        else:
            waiting.append((source, cache))
    results = []
    if jobs:
        with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as pool:
            results = list(pool.map(lambda args: _fetch_source(args[0], args[1], fetcher, now, max_age_days, sleeper, args[2]), jobs))
    errors, summary_errors, refreshed, successful, cached = [], [], {}, 0, {}
    outcomes = [(by_source[job[0].id], cache, error, success) for job, (cache, error, success) in zip(jobs, results)]
    outcomes += [(source, cache, None, False) for source, cache in waiting]
    for source, cache, error, success in outcomes:
        clean = _validated_existing(cache.get("items", []), source, now, max_age_days)
        refreshed[source.id] = {**cache, "items": clean}
        cached[source.id] = clean
        if error:
            errors.append(error)
        successful += success

    all_hosts = tuple(dict.fromkeys(host for source in sources for host in source.allowed_hosts))
    seed_items, seed_ids, seed_stories = [], set(), {}
    for row in seed.get("items", [])[:1000] if isinstance(seed.get("items", []), list) else []:
        if not isinstance(row, dict) or row.get("source") not in by_source:
            continue
        item = _restore(row, by_source[row["source"]], now, max_age_days, seed=True)
        if item:
            # Reviewed seed context wins a duplicate source-feed URL.
            links = []
            for link in (row.get("related_links") if isinstance(row.get("related_links"), list) else [])[:5]:
                if not isinstance(link, dict):
                    continue
                label = plain_text(link.get("label"), 120)
                try:
                    link_url = trusted_url(link.get("url"), all_hosts, keep_fragment=True)
                except NewsError:
                    continue
                if label:
                    links.append({"label": label, "url": link_url})
            if links:
                item["related_links"] = links
            if row.get("importance") in (0, 1, 2, 3) and not isinstance(row.get("importance"), bool):
                item["importance"] = row["importance"]
            if row.get("topic") in TOPICS:
                item["topic"] = row["topic"]
            if isinstance(row.get("story_id"), str) and STORY_ID.fullmatch(row["story_id"]):
                seed_stories[item["id"]] = row["story_id"]
            seed_items.append(item)
            seed_ids.add(item["id"])

    # Changelog sections are published only when pinned to the commit that added them.
    changelog_state, held = _bind_changelogs(sources, cached, state.get("changelog"), previous_items, seed_ids, fetcher, now)
    model_state = state.get("model") if isinstance(state.get("model"), dict) else {}
    run = {"now": now, "calls": 0, "max_calls": max_model_calls, "stopped": False, "errors": {}, "skipped": [],
           "clock": clock, "deadline": started + model_seconds,
           "backoff": {key: value for key, value in (model_state.get("backoff") or {}).items() if isinstance(value, dict)}
           if isinstance(model_state.get("backoff"), dict) else {}}

    # Triage: include or exclude weak candidates, and rate new items (importance, topic).
    triage = _bounded(state.get("triage"), now, set(), 90, 3000)
    gated, rate = [], []
    for source in sources:
        if not source.publish:
            continue
        rows = sorted(cached.get(source.id, []), key=lambda item: (item["published_at"], item["id"]), reverse=True)
        strong = 0
        for item in rows:
            record = triage.get(content_fingerprint(item))
            needs = source.triage == "all" or (source.triage == "borderline" and item.get("match") == "weak")
            if item["id"] in seed_ids:
                continue
            if needs and (not record or record.get("decision") is None) and len(gated) < 200:
                if not record or _due({"retry_after": record.get("retry_after")}, now):
                    gated.append((item, source))
            elif not needs and strong < source.max_items:
                strong += 1
                if not record:
                    rate.append((item, source))
    queue = sorted(gated, key=lambda pair: pair[0]["published_at"], reverse=True)
    queue += sorted(rate, key=lambda pair: pair[0]["published_at"], reverse=True)
    triage_calls, asked = (max(1, max_model_calls // 2) if backfill else 1), set()
    for _ in range(triage_calls):
        batch = [pair for pair in queue if content_fingerprint(pair[0]) not in asked][:MAX_TRIAGE_ITEMS]
        if not batch or triager is None:
            break
        asked.update(content_fingerprint(item) for item, _source in batch)
        rows = [{"id": item["id"], "source": source.name, "role": source.role, "title": item["title"],
                 "published_at": item["published_at"], "categories": item.get("feed_categories", [])[:10],
                 "excerpt": item["excerpt"][:1200]} for item, source in batch]
        try:
            decisions = _model_stage("triage", triager, rows, run)
        except Exception:
            break
        if decisions is None:
            break
        answered = {}
        for row in decisions if isinstance(decisions, list) else []:
            if (isinstance(row, dict) and row.get("decision") in ("include", "exclude") and
                    row.get("importance") in (0, 1, 2, 3) and row.get("topic") in TOPICS):
                answered.setdefault(row.get("id"), row)
        for item, _source in batch:
            row = answered.get(item["id"])
            triage[content_fingerprint(item)] = ({"decision": row["decision"], "importance": row["importance"],
                                                  "topic": row["topic"], "at": timestamp(now)} if row else
                                                 {"decision": None, "at": timestamp(now),
                                                  "retry_after": timestamp(now + timedelta(hours=24))})

    # Candidates: the newest relevant items of each publishing source, then reviewed seeds.
    pool = {}
    for source in sources:
        if not source.publish:
            continue
        eligible = []
        for item in cached.get(source.id, []):
            record = triage.get(content_fingerprint(item)) or {}
            needs = source.triage == "all" or (source.triage == "borderline" and item.get("match") == "weak")
            if needs and record.get("decision") != "include":
                continue
            item = dict(item)
            # A strong item the model excludes keeps its place but gets no model rating,
            # and a model rating never makes a preprint or a blog post a highlight.
            if (record.get("decision") == "include" and record.get("importance") in (0, 1, 2, 3)
                    and not isinstance(record.get("importance"), bool) and record.get("topic") in TOPICS):
                item["importance"] = min(1, record["importance"]) if source.role in LOW_TRUST_ROLES else record["importance"]
                item["topic"] = record["topic"]
            elif source.importance is not None:
                item["importance"] = source.importance
            if source.topic and "topic" not in item:
                item["topic"] = source.topic
            eligible.append(item)
        ranked = sorted(eligible, key=lambda item: (int(item.get("importance") or 0) >= 2, item["published_at"], item["id"]),
                        reverse=True)[:source.max_items]
        for item in ranked:
            # One record per article; the earliest date (first version) wins a duplicate.
            if item["id"] not in pool or item["published_at"] < pool[item["id"]]["published_at"]:
                pool[item["id"]] = item
    for item in seed_items:
        pool[item["id"]] = item
    recent = timestamp(now - timedelta(days=30))

    def select(candidates):
        # Model annotations must never mutate the cached source excerpts.
        priority = sorted(candidates, key=lambda item: (item["id"] in seed_ids, int(item.get("importance") or 0) >= 2 and
                                                        item["published_at"] >= recent, item["published_at"], item["id"]),
                          reverse=True)[:max_items]
        return [dict(item) for item in sorted(priority, key=lambda item: (item["published_at"], item["id"]), reverse=True)]

    selected = select(pool.values())

    # Stories: reviewed configuration first, then the model's assignments.
    story_state = state.get("stories") if isinstance(state.get("stories"), dict) else {}
    defined = {key: value for key, value in (story_state.get("defined") or {}).items()
               if isinstance(key, str) and STORY_ID.fullmatch(key) and isinstance(value, dict) and _clean_text(value.get("label"), 80)} \
        if isinstance(story_state.get("defined"), dict) else {}
    labels = {rule.id: rule.label for rule in stories}
    assigned = {key: value for key, value in story_state["assign"].items()
                if isinstance(key, str) and isinstance(value, str) and (value in defined or value in labels)} \
        if isinstance(story_state.get("assign"), dict) else {}
    labels.update({key: value["label"] for key, value in defined.items() if key not in labels})
    offered = {key for key in story_state["offered"] if isinstance(key, str)} if isinstance(story_state.get("offered"), list) else set()
    checked_at = story_state.get("checked_at") if isinstance(story_state.get("checked_at"), str) else ""

    def story_of(item):
        if item["id"] in seed_stories and seed_stories[item["id"]] in labels:
            return seed_stories[item["id"]]
        for rule in stories:
            if ((item["source"] in rule.sources or item["id"] in rule.item_ids) and item["published_at"] >= rule.since
                    and (not rule.until or item["published_at"] <= rule.until)):
                return rule.id
        return assigned.get(item["id"])

    window = timestamp(now - timedelta(days=14))
    offer = [item for item in selected if item["published_at"] >= window and story_of(item) is None]
    arrivals = {item["id"] for item in selected if _role(item, by_source) in STORY_ROLES} - offered
    stories_at = publication_date(checked_at)
    if grouper is not None and len(offer) >= 2 and (stories_at is None or now - stories_at >= timedelta(hours=6) or
                                                    arrivals & {item["id"] for item in offer}):
        grouped = {}
        for item in selected:
            sid = story_of(item)
            if sid:
                grouped.setdefault(sid, []).append(item)
        payload = {"stories": [{"story_id": sid, "label": labels[sid],
                                "members": [{"id": item["id"], "title": item["title"], "source": by_source[item["source"]].name}
                                            for item in members[:8]]} for sid, members in list(grouped.items())[:10]],
                   "items": [{"id": item["id"], "title": item["title"], "source": by_source[item["source"]].name,
                              "role": _role(item, by_source), "published_at": item["published_at"],
                              "excerpt": item["excerpt"][:300]} for item in offer[:30]]}
        try:
            result = _model_stage("stories", grouper, payload, run)
        except Exception:
            result = None
        if isinstance(result, dict):
            candidates = {item["id"]: item for item in offer[:30]}
            known = {entry["story_id"] for entry in payload["stories"]}
            used = set()
            for row in result.get("assign") or []:
                if isinstance(row, dict) and row.get("id") in candidates and row.get("story_id") in known and row["id"] not in used:
                    used.add(row["id"])
                    assigned[row["id"]] = row["story_id"]
            for row in result.get("new") or []:
                label = _clean_text(row.get("label") if isinstance(row, dict) else None, 80)
                members = [key for key in dict.fromkeys(row.get("member_ids") or []) if key in candidates and key not in used] \
                    if label and isinstance(row.get("member_ids"), list) else []
                if len(members) < 2 or not any(_role(candidates[key], by_source) in STORY_ROLES for key in members):
                    continue
                sid = "story-" + sha256(("|".join(sorted(members)) + label).encode()).hexdigest()[:12]
                defined[sid] = {"label": label, "at": timestamp(now)}
                labels.setdefault(sid, label)
                for key in members:
                    assigned[key] = sid
                    used.add(key)
            checked_at, offered = timestamp(now), {item["id"] for item in selected}

    def build_stories(rows):
        groups = {}
        for item in rows:
            sid = story_of(item)
            if sid:
                groups.setdefault(sid, []).append(item)
        result, membership = [], {}
        for sid, members in groups.items():
            # A story needs two published records, at least one of them a primary source or journalism.
            if len(members) < 2 or not any(_role(item, by_source) in STORY_ROLES for item in members):
                continue
            lead = _story_lead(members, by_source)
            # Blog posts and preprints join a story without moving it up the list.
            moving = [item for item in members if _role(item, by_source) in STORY_ROLES or item["status"] in CORRECTIVE]
            result.append({"id": sid, "label": labels[sid], "item_ids": [item["id"] for item in members], "lead_id": lead["id"],
                           "updated_at": max(item.get("updated_at") or item["published_at"] for item in moving or [lead])})
            membership.update((item["id"], sid) for item in members)
        result.sort(key=lambda story: (story["updated_at"], story["id"]), reverse=True)
        return result, membership

    story_output, membership = build_stories(selected)
    # Commentary is published only as related coverage inside a story (a reviewed
    # seed record excepted); a blog post in no story stays cached (for later
    # grouping) and its place goes to the next item.
    def shown(item):
        return _role(item, by_source) != "commentary" or item["id"] in membership or item["id"] in seed_ids

    if not all(shown(item) for item in selected):
        selected = select([item for item in pool.values() if shown(item)])
        story_output, membership = build_stories(selected)
    for item in selected:
        if item["id"] in membership:
            item["story_id"] = membership[item["id"]]
    current = {item["id"]: item for item in selected}
    keep_assign = {key: value for key, value in assigned.items() if key in pool}
    next_stories = {"assign": keep_assign, "offered": sorted(offered)[:500],
                    "defined": {key: value for key, value in defined.items() if key in set(keep_assign.values())}}
    if checked_at:
        next_stories["checked_at"] = checked_at

    # Summaries: every published record without a summary of its current content.
    annotations = {}
    for key, value in (state.get("annotations", {}) if isinstance(state.get("annotations", {}), dict) else {}).items():
        if isinstance(value, dict) and isinstance(key, str):
            if key in current and not value.get("content_fingerprint") and _annotation_valid(value, current[key]):
                # A legacy annotation of unchanged content is upgraded, not re-billed.
                value = {**value, "content_fingerprint": content_fingerprint(current[key]), "at": timestamp(now)}
            annotations[key] = value
    annotations = _bounded(annotations, now, set(current))
    pending = {key: value for key, value in (state.get("pending_summaries", {}) if isinstance(state.get("pending_summaries", {}), dict) else {}).items()
               if key in current and key not in seed_ids and isinstance(value, dict)}
    for item in selected:
        key = item["id"]
        # A record without source text (a headline-only feed entry) has nothing to summarize.
        if key in seed_ids or _annotation_valid(annotations.get(key), item) or len(_VOLATILE.sub("", item["excerpt"]).strip()) < 20:
            pending.pop(key, None)
            continue
        previous_summary = annotations.get(key, {})
        # A changed record is summarized again at most twice a week.
        if key not in pending and len(_recent(previous_summary.get("history"), now, 7)) < 3:
            pending[key] = {"first_seen": timestamp(now), "attempts": 0, "retry_after": timestamp(now)}

    def summarized(key, summary):
        history = _recent(annotations.get(key, {}).get("history"), now, 7) + [timestamp(now)]
        annotations[key] = {"fingerprint": _fingerprint(current[key]), "content_fingerprint": content_fingerprint(current[key]),
                            "summary": summary, "at": timestamp(now), "history": history}
        pending.pop(key, None)

    def failed(keys):
        for key in keys:
            if key in pending:
                attempts = min(20, _count(pending[key], "attempts") + 1)
                pending[key].update(attempts=attempts, retry_after=timestamp(now + timedelta(minutes=min(1440, 30 * 2 ** (attempts - 1)))))

    ready = [key for key, queue in sorted(pending.items(), key=lambda pair: (current[pair[0]]["published_at"], pair[0]), reverse=True)
             if _due(queue, now)]
    changelogs = [key for key in ready if by_source[current[key]["source"]].type == "changelog"]
    if changelog_summarizer is not None and changelogs:
        key = changelogs[0]
        try:
            written = _model_stage("changelog_summary", changelog_summarizer, dict(current[key]), run)
            # A skipped stage made no call, so the section is not backed off.
            if written is not None:
                summary = _model_text(written, 700)
                if summary:
                    summarized(key, summary)
                else:
                    failed([key])
        except Exception as error:
            if getattr(error, "kind", "") != "budget":
                failed([key])
                summary_errors.append({"error": "Summary fallback (" + type(error).__name__ + ")"})
        ready = [key for key in ready if key not in changelogs]
    for _ in range(MAX_SUMMARY_CALLS):
        batch = [key for key in ready if key in pending][:MAX_MODEL_ITEMS]
        if summarizer is None or not batch:
            break
        try:
            edits = _model_stage("summarize", summarizer, [dict(current[key]) for key in batch], run)
            if edits is None:
                break
            if not isinstance(edits, list):
                raise NewsError("Summarizer returned an invalid batch")
            matched = {}
            for edit in edits[:MAX_MODEL_ITEMS * 2]:
                if not isinstance(edit, dict) or edit.get("id") not in batch or edit["id"] in matched:
                    continue
                summary = _model_text(edit.get("summary"), 700)
                if summary:
                    matched[edit["id"]] = summary
            for key, summary in matched.items():
                summarized(key, summary)
        except Exception as error:
            if getattr(error, "kind", "") == "budget":
                break
            summary_errors.append({"error": "Summary fallback (" + type(error).__name__ + ")"})
        failed(batch)
        ready = [key for key in ready if key not in batch]

    # Problem links: catalogue candidates found deterministically, related by the model with a verbatim quote.
    links = _bounded(state.get("links"), now, set(current))
    asks = []
    for item in selected:
        if item["id"] in seed_ids or problem_index is None:
            continue
        if links.get(item["id"], {}).get("content_fingerprint") == content_fingerprint(item):
            continue
        text = item["title"] + "\n" + item["excerpt"]
        candidates = problem_index.candidates(text)
        if candidates:
            asks.append({"id": item["id"], "title": item["title"], "text": text[:MAX_CHANGELOG_EXCERPT + 320],
                         "candidates": [{"problem_id": candidate.problem_id, "title": candidate.title} for candidate in candidates]})
    batch = asks[:MAX_LINK_ITEMS]
    if batch and linker is not None:
        try:
            found = _model_stage("link_problems", linker, batch, run)
        except Exception:
            found = None
        if found is not None:
            for ask in batch:
                allowed = {candidate["problem_id"] for candidate in ask["candidates"]}
                rows, seen = [], set()
                for row in found if isinstance(found, list) else []:
                    if (not isinstance(row, dict) or row.get("id") != ask["id"] or row.get("problem_id") not in allowed or
                            row.get("relation") not in RELATIONS or (row["problem_id"], row["relation"]) in seen):
                        continue
                    evidence = " ".join(str(row.get("evidence") or "").split())
                    if not 8 <= len(evidence) <= 240 or evidence not in " ".join(ask["text"].split()):
                        continue
                    seen.add((row["problem_id"], row["relation"]))
                    rows.append({"problem_id": row["problem_id"], "relation": row["relation"], "evidence": evidence,
                                 "title": problem_index.title(row["problem_id"])[:120]})
                links[ask["id"]] = {"content_fingerprint": content_fingerprint(current[ask["id"]]), "links": rows[:3],
                                    "at": timestamp(now)}

    # Status proposals: hints for the problems updater; they never change a problem or a story.
    ledger = _bounded(state.get("proposals"), now, set(), 90, 500)

    def propose(item, link, matched_by):
        key = "|".join((item["id"], link["problem_id"], link["relation"]))
        first = ledger.get(key, {}).get("first_seen") or timestamp(now)
        ledger[key] = {"problem_id": link["problem_id"], "relation": link["relation"], "evidence": link["evidence"],
                       "item_id": item["id"], "item_url": item["url"], "item_title": item["title"], "source_id": item["source"],
                       "source_role": _role(item, by_source), "published_at": item["published_at"],
                       "matched_by": matched_by, "first_seen": first, "last_seen": timestamp(now), "at": timestamp(now)}

    for item in selected:
        record = links.get(item["id"])
        if item["id"] in seed_ids or not isinstance(record, dict) or record.get("content_fingerprint") != content_fingerprint(item):
            continue
        valid = [link for link in record.get("links") or [] if isinstance(link, dict) and link.get("relation") in RELATIONS
                 and PROBLEM_ID.fullmatch(str(link.get("problem_id") or "")) and isinstance(link.get("evidence"), str)
                 and 8 <= len(link["evidence"]) <= 240 and (problem_index is None or link["problem_id"] in problem_index)]
        if valid:
            item["problem_links"] = [{key: link[key] for key in ("problem_id", "relation", "evidence", "title")
                                      if isinstance(link.get(key), str) and link[key]} for link in valid[:3]]
            for link in valid[:3]:
                propose(item, link, "kimi-k3")
    if problem_index is not None:
        for source in sources:
            if not source.proposals:
                continue
            for item in cached.get(source.id, [])[:100]:
                evidence = item["title"][:240]
                for candidate in problem_index.candidates(evidence, limit=3):
                    propose(item, {"problem_id": candidate.problem_id, "relation": source.proposals, "evidence": evidence},
                            "deterministic")
    ledger = _bounded(ledger, now, set(), 90, 500)

    for item in selected:
        source = by_source[item["source"]]
        annotation = annotations.get(item["id"], {})
        # Where the published summary comes from: the editors (reviewed seed),
        # Kimi K3, or the source's own text, which the page shows as a quotation.
        origin = None
        if item["id"] in seed_ids:
            origin = "editor"
        elif _annotation_valid(annotation, item):
            item["summary"], origin = annotation["summary"], "model"
        elif not source.excerpt_fallback or (item["status"] == "Preprint" and
                                             urlsplit(item["url"]).hostname in ("arxiv.org", "export.arxiv.org")):
            # Raw arXiv abstracts contain metadata and TeX that a shortened card
            # cannot reliably render, and some sources' text is not republished
            # (excerpt_fallback false). Keep the excerpt privately for the model.
            item["summary"] = ""
        else:
            origin = "source"
            item["summary"] = _quoted_source(item["summary"])
            if len(item["summary"]) >= 690 and " " in item["summary"][400:]:
                # A source excerpt cut at the length limit ends at a word, visibly shortened.
                item["summary"] = item["summary"][:item["summary"].rindex(" ")].rstrip(" ,;:") + " …"
        if item["summary"] and origin:
            item["summary_source"] = origin
        item["source_role"] = _role(item, by_source)
        for key in ("excerpt", "feed_categories", "match", "announce_type"):
            item.pop(key, None)
    public_sources = [source.public() for source in sources]
    changed = (selected != previous.get("items") or public_sources != previous.get("sources")
               or story_output != previous.get("stories", []))
    output = {"schema_version": 1,
              "updated_at": timestamp(now) if changed else previous.get("updated_at", timestamp(now)),
              "last_checked_at": timestamp(now), "refresh_interval_minutes": 30,
              "last_successful_refresh_at": timestamp(now) if successful else previous.get("last_successful_refresh_at"),
              "items": selected, "sources": public_sources, "source_errors": errors,
              "summary_errors": summary_errors, "stories": story_output}
    # Retain bounded history so a story rotating out of a source feed is not
    # repeatedly billed as a new item if it returns within the publication window.
    seen = state.get("seen_ids") if isinstance(state.get("seen_ids"), list) else []
    seen_order = list(dict.fromkeys([*(key for key in seen if isinstance(key, str)), *current]))[-5000:]
    next_state = {"schema_version": 1, "feeds": refreshed, "seen_ids": seen_order,
                  "annotations": annotations, "pending_summaries": pending, "triage": triage, "links": links,
                  "stories": next_stories, "proposals": ledger, "changelog": changelog_state,
                  "model": {**model_state, "backoff": run["backoff"]},
                  "last_run": {"model_calls": run["calls"], "model_errors": run["errors"], "model_skipped": run["skipped"],
                               "sources_waiting": len(waiting), "stories": len(story_output),
                               "problem_links": sum(len(item.get("problem_links", [])) for item in selected),
                               "proposals": len(ledger), "held_changelog": held}}
    if isinstance(state.get("budget"), dict):
        next_state["budget"] = state["budget"]
    return output, next_state

def proposals_document(state: dict, problem_index, now: datetime | None = None) -> dict:
    """The public hint file for the problems updater (``status_proposals.json``).

    ``{"schema_version": 1, "generated_at", "producer": "ansatze-news", "note",
    "catalogue": {"schema_version", "sha256", "snapshot_date"} | null,
    "proposals": [{"problem_id", "relation", "evidence", "item_id", "item_url",
    "item_title", "source_id", "source_role", "published_at",
    "matched_by": "kimi-k3" | "deterministic", "first_seen", "last_seen"}]}``,
    newest first, at most 500, each seen within 90 days. ``relation`` uses the
    news vocabulary (``RELATIONS``); ``evidence`` is verbatim source text. These
    are unreviewed hints and never a status change.
    """
    rows = sorted((value for value in (state.get("proposals") or {}).values() if isinstance(value, dict)),
                  key=lambda row: (str(row.get("last_seen")), str(row.get("problem_id"))), reverse=True)
    catalogue = None
    if problem_index is not None:
        catalogue = {"schema_version": problem_index.schema_version, "sha256": problem_index.sha256,
                     "snapshot_date": problem_index.snapshot_date}
    return {"schema_version": 1, "generated_at": timestamp(now or datetime.now(UTC)), "producer": "ansatze-news",
            "note": "Matched from public news items. Unreviewed hints; never a status change.",
            "catalogue": catalogue, "proposals": [{key: value for key, value in row.items() if key != "at"} for row in rows]}


def atomic_json(path: Path, data: dict, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix="." + path.name + ".", delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), mode)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _load_optional(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    if path.stat().st_size > 16 * MAX_BYTES:
        raise NewsError("Saved news document exceeds size limit")
    try:
        value = json.loads(path.read_text())
    except (ValueError, UnicodeError):
        raise NewsError("Saved news document is invalid") from None
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise NewsError("Saved news schema is unsupported")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--sources", type=Path, default=Path(__file__).parent / "data/news_sources.json")
    parser.add_argument("--seed", type=Path)
    parser.add_argument("--proposals", type=Path, help="status hints for the problems updater (default: next to --output)")
    parser.add_argument("--catalogue", type=Path, help="problem catalogue for problem links (default: the checkout's)")
    parser.add_argument("--max-model-calls", type=int, default=4, help="model calls per run (0-20)")
    parser.add_argument("--backfill", action="store_true", help="fetch sources' backfill queries and triage more")
    parser.add_argument("--no-model", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="write nothing; never calls the model")
    args = parser.parse_args(argv)
    try:
        proposals = args.proposals or args.output.with_name("status_proposals.json")
        if len({args.output.resolve(), args.state.resolve(), proposals.resolve()}) != 3:
            raise NewsError("Output, proposals and private cache must use different files")
        if not 0 <= args.max_model_calls <= 20:
            raise NewsError("Model calls per run must be between 0 and 20")
        sources, stories = load_config(args.sources)
        state = _load_optional(args.state)
        problem_index, notes = None, []
        try:
            from .news_links import load_index
            problem_index = load_index(args.catalogue)
        except Exception as error:
            # Problem links are optional; the feed itself does not depend on them.
            notes.append("Problem catalogue unavailable (" + type(error).__name__ + ")")
        tasks, budget = {}, None
        if args.dry_run and not args.no_model:
            # Calls in a dry run would be spent but recorded nowhere.
            notes.append("Dry run: the model is off")
        if not args.no_model and not args.dry_run:
            from . import news_kimi
            cap = news_kimi.daily_cap()
            if cap and args.max_model_calls:
                if not isinstance(state.get("budget"), dict):
                    state["budget"] = {}
                budget = news_kimi.NewsBudget(state["budget"], cap, args.max_model_calls)
                reserved = min(budget.run_cap, budget.daily.remaining())
                if reserved:
                    # Count this run's possible calls before making any, so a failed
                    # publication or a killed run cannot spend calls that the daily
                    # cap never sees; the final state write records the calls made.
                    atomic_json(args.state, {**state, "schema_version": 1,
                                             "budget": {**state["budget"], "calls": budget.daily.used + reserved}}, mode=0o600)
                tasks = news_kimi.bound_tasks(budget)
        output, next_state = refresh(sources, state=state, previous=_load_optional(args.output), seed=_load_optional(args.seed),
                                     stories=stories, problem_index=problem_index, max_model_calls=args.max_model_calls,
                                     backfill=args.backfill, **tasks)
        if not args.dry_run:
            # Publish first: if publication fails, no cache advances and the next
            # scheduled attempt can retry the exact source/model work.
            atomic_json(args.output, output)
            try:
                atomic_json(proposals, proposals_document(next_state, problem_index))
            except Exception as error:
                # The hint file is optional; the feed is published, so the state advances.
                notes.append("Status proposals not written (" + type(error).__name__ + ")")
            atomic_json(args.state, next_state, mode=0o600)
        run = next_state.get("last_run", {})
        print(json.dumps({"ok": True, "dry_run": args.dry_run, "items": len(output["items"]),
                          "source_errors": len(output["source_errors"]),
                          "source_error_ids": [error["source_id"] for error in output["source_errors"]],
                          "summary_errors": len(output["summary_errors"]),
                          "pending_summaries": len(next_state["pending_summaries"]),
                          "stories": run.get("stories", 0), "problem_links": run.get("problem_links", 0),
                          "proposals": run.get("proposals", 0), "sources_waiting": run.get("sources_waiting", 0),
                          "held_changelog": run.get("held_changelog", []),
                          "model": {"calls": run.get("model_calls", 0), "errors": run.get("model_errors", {}),
                                    "skipped": run.get("model_skipped", []),
                                    "calls_today": budget.daily.used if budget else 0, "daily_cap": budget.daily.cap if budget else 0},
                          "notes": notes}, ensure_ascii=False))
        return 0
    except Exception as error:
        print(json.dumps({"ok": False, "error": _error(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
