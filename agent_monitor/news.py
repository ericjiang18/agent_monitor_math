"""Bounded, stdlib-only publisher for the public mathematical research news feed.

Feed transport never follows an unvalidated redirect and connects only to a
validated public IP while preserving the publisher's TLS hostname. Source
headlines, URLs, publication dates, attribution and status are never model edits.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
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

UTC = timezone.utc
MAX_BYTES = 2 * 1024 * 1024
MAX_FEED_ITEMS = 300
MAX_MODEL_ITEMS = 8
CATEGORIES = frozenset({"mathematics", "models", "research"})
STATUSES = frozenset({"Published", "Proof announced", "Model release", "Research result", "Preprint"})
TRACKING = frozenset({"fbclid", "gclid", "mc_cid", "mc_eid"})


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


def trusted_url(value: object, hosts: tuple[str, ...] | list[str]) -> str:
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
    return urlunsplit(("https", host, url.path or "/", url.query, ""))


def canonical_url(value: object, hosts: tuple[str, ...] | list[str]) -> str:
    url = urlsplit(trusted_url(value, hosts))
    query = [(key, val) for key, val in parse_qsl(url.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in TRACKING]
    return urlunsplit((url.scheme, url.netloc, url.path, urlencode(sorted(query)), ""))


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
        keywords = {}
        for field in ("include_keywords", "exclude_keywords"):
            terms = value.get(field, [])
            if (not isinstance(terms, list) or len(terms) > 80 or
                    any(not isinstance(term, str) or not term.strip() or len(term) > 100 for term in terms)):
                raise NewsError("Invalid relevance keyword configuration")
            keywords[field] = tuple(terms)
        manual = value.get("manual", False)
        if not isinstance(manual, bool) or (manual and value.get("feed_url") is not None):
            raise NewsError("Manual sources must not declare a feed URL")
        feed_url = None if manual else trusted_url(value.get("feed_url"), hosts)
        return cls(sid, name.strip(), trusted_url(value.get("url"), hosts),
                   feed_url, hosts, category, status, manual=manual, **keywords)

    def public(self) -> dict:
        return {"id": self.id, "name": self.name, "url": self.url, "automated": not self.manual}


def load_sources(path: Path) -> list[Source]:
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise NewsError("Unsupported source configuration")
    rows = data.get("sources")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 24:
        raise NewsError("Expected between one and 24 trusted sources")
    sources = [Source.from_dict(row) for row in rows]
    if len({source.id for source in sources}) != len(sources):
        raise NewsError("Duplicate source identity")
    return sources


@dataclass(frozen=True)
class FeedResponse:
    status: int
    body: bytes = b""
    etag: str = ""
    last_modified: str = ""


def _header(value: object) -> str:
    return value if isinstance(value, str) and len(value) <= 512 and not any(ord(c) < 32 for c in value) else ""


class HTTPSFetcher:
    def __init__(self, timeout: float = 15, max_bytes: int = MAX_BYTES, resolver=None, connection_factory=None):
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.resolver = resolver or socket.getaddrinfo
        self.connection_factory = connection_factory or http.client.HTTPSConnection
        self.context = ssl.create_default_context()

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
                request_headers = {"User-Agent": "AnsatzeNews/1.0 (+public research feed)",
                                   "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml",
                                   "Accept-Encoding": "identity"}
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
                if response.getheader("Content-Encoding", "identity").lower() not in ("", "identity"):
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
    text = re.sub(r"\s+", " ", "".join(parser.parts)).strip()
    return text[:limit].rstrip()


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _field(entry, names: tuple[str, ...]) -> str:
    for name in names:
        for child in entry:
            if _local(child.tag) == name:
                return "".join(child.itertext())
    return ""


def _relevant(title: str, source: Source) -> bool:
    text = " " + re.sub(r"[^\w]+", " ", title.casefold()) + " "
    def contains(term):
        return " " + re.sub(r"[^\w]+", " ", term.casefold()).strip() + " " in text
    return not any(contains(term) for term in source.exclude_keywords) and (not source.include_keywords or any(contains(term) for term in source.include_keywords))


def _item(source: Source, title: object, summary: object, url: object, published: object, now: datetime,
          days: int, category: str | None = None, status: str | None = None) -> dict | None:
    date = publication_date(published)
    title = plain_text(title, 300)
    if not title or not date or date > now or date < now - timedelta(days=days):
        return None
    try:
        url = canonical_url(url, source.allowed_hosts)
    except NewsError:
        return None
    category, status = category or source.category, status or source.status
    if category not in CATEGORIES or status not in STATUSES:
        return None
    return {"id": "news-" + sha256(url.encode()).hexdigest()[:24], "title": title,
            "summary": plain_text(summary, 700), "url": url, "source": source.id,
            "category": category, "published_at": timestamp(date), "status": status,
            "excerpt": plain_text(summary, 1800)}


def parse_feed(body: bytes, source: Source, now: datetime, days: int = 90) -> list[dict]:
    if len(body) > MAX_BYTES:
        raise NewsError("Feed exceeds size limit")
    if b"\x00" in body:
        raise NewsError("Feed must use an ASCII-compatible XML encoding")
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)\b", body, re.I):
        raise NewsError("Feed DTDs and entity declarations are forbidden")
    if body.count(b"&") > 10000:
        raise NewsError("Feed exceeds entity reference limit")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise NewsError("Feed contains invalid XML") from None
    if _local(root.tag) not in ("rss", "feed", "rdf"):
        raise NewsError("Document is not an RSS or Atom feed")
    elements = list(root.iter())
    if len(elements) > 20000:
        raise NewsError("Feed exceeds element limit")
    result = []
    entries = (node for node in elements if _local(node.tag) in ("item", "entry"))
    for count, entry in enumerate(entries):
        if count >= MAX_FEED_ITEMS:
            break
        title = _field(entry, ("title",))
        if not _relevant(plain_text(title, 300), source):
            continue
        url = _field(entry, ("link",))
        for link in entry:
            if _local(link.tag) == "link" and link.get("href") and link.get("rel", "alternate") == "alternate":
                url = link.get("href")
                break
        if not url:
            url = _field(entry, ("guid",))
        item = _item(source, title, _field(entry, ("summary", "description", "encoded", "content")),
                     url, _field(entry, ("published", "pubdate", "date", "updated")), now, days)
        if item:
            result.append(item)
    return result


def _error(error: Exception) -> str:
    return str(error)[:180] if isinstance(error, NewsError) else "Source unavailable (" + type(error).__name__ + ")"


def _fetch_source(source, cache, fetcher, now, days, sleeper):
    headers = {}
    if cache.get("feed_url") == source.feed_url:
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
            return {"feed_url": source.feed_url, "items": items, "etag": response.etag,
                    "last_modified": response.last_modified, "checked_at": timestamp(now)}, None, True
        except Exception as error:
            last_error = error
            # Retrying malformed XML or untrusted URLs cannot repair the source.
            retryable = not isinstance(error, NewsError) or str(error).startswith("Feed returned HTTP 5") or str(error) in ("Feed returned HTTP 429", "Feed returned HTTP 408")
            if not retryable or attempt:
                break
            sleeper(.25)
    return cache, {"source_id": source.id, "error": _error(last_error)}, False


def _validated_existing(items, source, now, days):
    result = []
    for row in (items if isinstance(items, list) else [])[:MAX_FEED_ITEMS]:
        if not isinstance(row, dict):
            continue
        item = _item(source, row.get("title"), row.get("excerpt", row.get("summary")), row.get("url"), row.get("published_at"),
                     now, days, row.get("category"), row.get("status"))
        if item:
            result.append(item)
    return result


def _fingerprint(item):
    return sha256(json.dumps({key: item[key] for key in ("title", "summary", "url", "source", "published_at", "status")}, sort_keys=True).encode()).hexdigest()


def refresh(sources: list[Source], *, state: dict | None = None, previous: dict | None = None,
            seed: dict | None = None, now: datetime | None = None, fetcher=None,
            summarizer: Callable[[list[dict]], list[dict]] | None = None,
            max_items: int = 60, max_age_days: int = 90, sleeper=time.sleep) -> tuple[dict, dict]:
    if not sources or len(sources) > 24 or len({source.id for source in sources}) != len(sources):
        raise NewsError("Expected one to 24 unique trusted sources")
    if not 1 <= max_items <= 60 or not 30 <= max_age_days <= 90:
        raise NewsError("News limits must be 1–60 items and 30–90 days")
    now = (now or datetime.now(UTC)).astimezone(UTC)
    state, previous, seed = state or {}, previous or {}, seed or {}
    fetcher = fetcher or HTTPSFetcher()
    feeds = state.get("feeds", {}) if isinstance(state.get("feeds", {}), dict) else {}
    previous_items = previous.get("items", [])
    jobs = []
    for source in sources:
        if source.manual:
            continue
        cache = feeds.get(source.id, {})
        if not isinstance(cache, dict):
            cache = {}
        if "items" not in cache:
            # A lost state file must not erase the already published last good data.
            cache = {"items": [item for item in previous_items if isinstance(item, dict) and item.get("source") == source.id]}
        jobs.append((source, cache))
    results = []
    if jobs:
        with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as pool:
            results = list(pool.map(lambda args: _fetch_source(*args, fetcher, now, max_age_days, sleeper), jobs))
    errors, summary_errors, refreshed, successful, items = [], [], {}, 0, {}
    for (source, _), (cache, error, success) in zip(jobs, results):
        clean = [item for item in _validated_existing(cache.get("items", []), source, now, max_age_days)
                 if _relevant(item["title"], source)]
        refreshed[source.id] = {**cache, "items": clean}
        for item in sorted(clean, key=lambda row: (row["published_at"], row["id"]), reverse=True)[:10]:
            items.setdefault(item["url"], item)
        if error:
            errors.append(error)
        successful += success
    by_source = {source.id: source for source in sources}
    all_hosts = tuple(dict.fromkeys(host for source in sources for host in source.allowed_hosts))
    seed_ids = set()
    for row in seed.get("items", [])[:1000]:
        if not isinstance(row, dict) or row.get("source") not in by_source:
            continue
        clean = _validated_existing([row], by_source[row["source"]], now, max_age_days)
        if clean:
            # Reviewed seed context wins a duplicate source-feed URL.
            item = clean[0]
            links = []
            for link in (row.get("related_links") if isinstance(row.get("related_links"), list) else [])[:5]:
                if not isinstance(link, dict):
                    continue
                label = plain_text(link.get("label"), 120)
                try:
                    link_url = trusted_url(link.get("url"), all_hosts)
                except NewsError:
                    continue
                if label:
                    links.append({"label": label, "url": link_url})
            if links:
                item["related_links"] = links
            items[item["url"]] = item
            seed_ids.add(item["id"])
    # Model annotations must never mutate the cached source excerpts.
    priority = sorted(items.values(), key=lambda item: (item["id"] in seed_ids, item["published_at"], item["id"]), reverse=True)[:max_items]
    selected = [dict(item) for item in sorted(priority, key=lambda item: (item["published_at"], item["id"]), reverse=True)]
    current = {item["id"]: item for item in selected}
    seen = set(state.get("seen_ids", []))
    annotations = {key: value for key, value in state.get("annotations", {}).items()
                   if key in current and isinstance(value, dict)}
    pending = {key: value for key, value in state.get("pending_summaries", {}).items()
               if key in current and key not in seed_ids and isinstance(value, dict)}
    for item in selected:
        if item["id"] not in seen and item["id"] not in seed_ids:
            pending[item["id"]] = {"first_seen": timestamp(now), "attempts": 0, "retry_after": timestamp(now)}
    ready = [key for key, queue in sorted(pending.items(), key=lambda pair: (current[pair[0]]["published_at"], pair[0]), reverse=True)
             if (publication_date(queue.get("retry_after")) or now) <= now][:MAX_MODEL_ITEMS]
    if summarizer is not None and ready:
        try:
            edits = summarizer([dict(current[key]) for key in ready])
            if not isinstance(edits, list):
                raise NewsError("Summarizer returned an invalid batch")
            matched = {}
            for edit in edits[:MAX_MODEL_ITEMS * 2]:
                if not isinstance(edit, dict) or edit.get("id") not in ready or edit["id"] in matched:
                    continue
                summary = plain_text(edit.get("summary"), 700)
                if summary:
                    matched[edit["id"]] = summary
            for key, summary in matched.items():
                annotations[key] = {"fingerprint": _fingerprint(current[key]), "summary": summary}
                pending.pop(key, None)
        except Exception as error:
            summary_errors.append({"error": "Summary fallback (" + type(error).__name__ + ")"})
        for key in ready:
            if key in pending:
                attempts = min(20, int(pending[key].get("attempts", 0)) + 1)
                pending[key].update(attempts=attempts, retry_after=timestamp(now + timedelta(minutes=min(1440, 30 * 2 ** (attempts - 1)))))
    for item in selected:
        annotation = annotations.get(item["id"], {})
        if item["id"] not in seed_ids and annotation.get("fingerprint") == _fingerprint(item):
            item["summary"] = annotation["summary"]
        elif (item["id"] not in seed_ids and item["status"] == "Preprint" and
              urlsplit(item["url"]).hostname in ("arxiv.org", "export.arxiv.org")):
            # Raw arXiv abstracts contain metadata and TeX that a shortened card
            # cannot reliably render. Keep the excerpt privately for the model.
            item["summary"] = ""
        item.pop("excerpt", None)
    changed = selected != previous.get("items") or [source.public() for source in sources] != previous.get("sources")
    output = {"schema_version": 1,
              "updated_at": timestamp(now) if changed else previous.get("updated_at", timestamp(now)),
              "last_checked_at": timestamp(now), "refresh_interval_minutes": 30,
              "last_successful_refresh_at": timestamp(now) if successful else previous.get("last_successful_refresh_at"),
              "items": selected, "sources": [source.public() for source in sources], "source_errors": errors,
              "summary_errors": summary_errors}
    # Retain bounded history so a story rotating out of a source feed is not
    # repeatedly billed as a new item if it returns within the publication window.
    seen_order = list(dict.fromkeys([*state.get("seen_ids", []), *current]))[-5000:]
    next_state = {"schema_version": 1, "feeds": refreshed, "seen_ids": seen_order,
                  "annotations": annotations, "pending_summaries": pending}
    return output, next_state


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
    parser.add_argument("--no-model", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.output.resolve() == args.state.resolve():
            raise NewsError("Output and private cache must use different files")
        summarizer = None
        if not args.no_model:
            from .news_kimi import summarize
            summarizer = summarize
        output, state = refresh(load_sources(args.sources), state=_load_optional(args.state),
                                previous=_load_optional(args.output), seed=_load_optional(args.seed), summarizer=summarizer)
        if not args.dry_run:
            # Publish first: if publication fails, no cache advances and the next
            # scheduled attempt can retry the exact source/model work.
            atomic_json(args.output, output)
            atomic_json(args.state, state, mode=0o600)
        print(json.dumps({"ok": True, "dry_run": args.dry_run, "items": len(output["items"]),
                          "source_errors": len(output["source_errors"]), "summary_errors": len(output["summary_errors"]),
                          "pending_summaries": len(state["pending_summaries"])}))
        return 0
    except Exception as error:
        print(json.dumps({"ok": False, "error": _error(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
