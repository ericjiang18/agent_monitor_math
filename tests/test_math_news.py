"""Source transport, publication and model boundaries for the scheduled news feed."""
from datetime import datetime, timedelta, timezone
from html import escape
import json
from pathlib import Path
import socket

import pytest

from agent_monitor import news


NOW = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)


def source(sid="publisher", **changes):
    data = dict(id=sid, name=sid.title(), url="https://news.example.org/",
                feed_url="https://news.example.org/" + sid + ".xml",
                allowed_hosts=["news.example.org"], category="research", status="Published")
    data.update(changes)
    return news.Source.from_dict(data)


def story(index=1, **changes):
    row = dict(title=f"New theorem {index}", summary="Researchers describe a partial result, not a complete proof.",
               url=f"https://news.example.org/article/{index}", published_at="2026-09-15T12:00:00Z")
    row.update(changes)
    return row


def rss(*rows):
    content = []
    for row in rows:
        content.append("<item>" + "".join(f"<{tag}>{escape(str(row.get(field, '')))}</{tag}>"
                       for tag, field in (("title", "title"), ("link", "url"),
                                          ("description", "summary"), ("pubDate", "published_at"))) + "</item>")
    return ("<rss version='2.0'><channel>" + "".join(content) + "</channel></rss>").encode()


class Fetcher:
    def __init__(self, responses):
        self.responses = {key: list(value) for key, value in responses.items()}
        self.calls = []

    def fetch(self, source, headers):
        self.calls.append((source.id, dict(headers)))
        value = self.responses[source.id].pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def run(*, sources=None, rows=None, **kwargs):
    sources = sources or [source()]
    kwargs.setdefault("fetcher", Fetcher({s.id: [news.FeedResponse(200, rss(*(rows or [story()])))] for s in sources}))
    return news.refresh(sources, now=kwargs.pop("now", NOW), sleeper=lambda _: None, **kwargs)


def test_rss_dates_limits_missing_and_future_never_acquire_current_date():
    rows = news.parse_feed(rss(story(1, published_at="Tue, 15 Sep 2026 16:00:00 +0200"),
                              story(2, published_at="2026-09-16T12:00:01Z"),
                              story(3, published_at=""), story(4, published_at="2026-06-01T00:00:00Z")), source(), NOW)
    assert len(rows) == 1
    assert rows[0]["published_at"] == "2026-09-15T14:00:00Z"


def test_atom_alternate_link_and_html_summary_are_plain_text():
    body = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
    <title>A mathematical result</title><link rel="self" href="https://evil.example.org/metadata"/>
    <link rel="alternate" href="https://news.example.org/result?utm_source=feed"/>
    <published>2026-09-15T10:00:00-04:00</published>
    <summary type="html">&lt;p&gt;A &lt;b&gt;partial&lt;/b&gt; result.&lt;/p&gt;&lt;script&gt;steal()&lt;/script&gt;</summary>
    </entry></feed>'''
    rows = news.parse_feed(body, source(), NOW)
    assert rows[0]["url"] == "https://news.example.org/result"
    assert rows[0]["summary"] == "A partial result."
    assert rows[0]["published_at"] == "2026-09-15T14:00:00Z"


def test_source_relevance_uses_title_word_phrases_and_exclusions():
    config = source(include_keywords=["new model", "theorem"], exclude_keywords=["sponsor"])
    rows = news.parse_feed(rss(story(1, title="A new-model release"), story(2, title="Sponsor: theorem prize"),
                              story(3, title="News about modelled services"), story(4, title="Company office opens")), config, NOW)
    assert [row["title"] for row in rows] == ["A new-model release"]


@pytest.mark.parametrize("url", ["http://news.example.org/a", "https://evil.example.org/a",
    "https://news.example.org.evil.example.org/a", "https://user:password@news.example.org/a",
    "https://news.example.org:444/a", "https://@news.example.org/a", "https://127.0.0.1/a", "https://[::1]/a",
    "https://news.example.org/\nsecret"])
def test_untrusted_article_and_feed_urls_rejected(url):
    with pytest.raises(news.NewsError):
        news.trusted_url(url, source().allowed_hosts)
    assert news.parse_feed(rss(story(url=url)), source(), NOW) == []


def test_canonical_urls_deduplicate_tracking_but_preserve_semantic_parameters():
    output, _ = run(rows=[story(1, url="https://news.example.org/a?b=2&utm_source=feed&a=1#top"),
                         story(2, url="https://news.example.org/a?a=1&b=2&gclid=ad"),
                         story(3, url="https://news.example.org/a?a=2&b=2")])
    assert len(output["items"]) == 2
    assert {item["url"] for item in output["items"]} == {"https://news.example.org/a?a=1&b=2", "https://news.example.org/a?a=2&b=2"}
    assert len({item["id"] for item in output["items"]}) == 2


@pytest.mark.parametrize("body", [b'<!DOCTYPE rss [<!ENTITY x "bomb">]><rss/>',
    b'<rss>' + b'&amp;' * 10001 + b'</rss>', b'<rss>' + b' ' * news.MAX_BYTES + b'</rss>', b'<html/>', b'<rss>',
    '<!DOCTYPE rss [<!ENTITY x "bomb">]><rss/>'.encode('utf-16')])
def test_xml_and_entity_limits_fail_closed(body):
    with pytest.raises(news.NewsError):
        news.parse_feed(body, source(), NOW)


def test_item_selection_is_newest_first_and_bounded():
    rows = [story(i, published_at=news.timestamp(NOW - timedelta(hours=i + 1))) for i in range(80)]
    output, _ = run(rows=rows)
    assert len(output["items"]) == 10
    assert output["items"][0]["title"] == "New theorem 0"
    assert output["items"][-1]["title"] == "New theorem 9"


def test_conditional_requests_reuse_cached_items_and_keep_content_timestamp():
    first_fetcher = Fetcher({"publisher": [news.FeedResponse(200, rss(story()), '"version-1"', "Tue, 15 Sep 2026 12:00:00 GMT")]})
    initial, state = run(fetcher=first_fetcher)
    fetcher = Fetcher({"publisher": [news.FeedResponse(304)]})
    later = NOW + timedelta(minutes=30)
    output, next_state = run(fetcher=fetcher, state=state, previous=initial, now=later)
    assert fetcher.calls == [("publisher", {"If-None-Match": '"version-1"', "If-Modified-Since": "Tue, 15 Sep 2026 12:00:00 GMT"})]
    assert output["items"] == initial["items"]
    assert output["updated_at"] == initial["updated_at"]
    assert output["last_checked_at"] == output["last_successful_refresh_at"] == news.timestamp(later)
    assert next_state["feeds"]["publisher"]["etag"] == '"version-1"'


@pytest.mark.parametrize("cache_available", [True, False])
def test_changed_filters_apply_to_304_cache_and_public_fallback_but_not_seed(cache_available):
    initial, state = run(rows=[story(1, title="Promotional office update"), story(2, title="A new theorem")])
    filtered = source(include_keywords=["theorem"])
    responses = [news.FeedResponse(304)] if cache_available else [TimeoutError(), TimeoutError()]
    output, _ = run(sources=[filtered], state=state if cache_available else {}, previous=initial,
                    fetcher=Fetcher({"publisher": responses}))
    assert [item["title"] for item in output["items"]] == ["A new theorem"]
    responses = [news.FeedResponse(304)] if cache_available else [TimeoutError(), TimeoutError()]
    seed = {"items": [{**story(3, title="Reviewed major announcement"), "source": "publisher"}]}
    output, _ = run(sources=[filtered], state=state if cache_available else {}, previous=initial, seed=seed,
                    fetcher=Fetcher({"publisher": responses}))
    assert {item["title"] for item in output["items"]} == {"A new theorem", "Reviewed major announcement"}


def test_partial_failure_keeps_old_source_and_updates_healthy_source_without_error_leak():
    sources = [source("one"), source("two")]
    first, state = run(sources=sources, fetcher=Fetcher({"one": [news.FeedResponse(200, rss(story(1)))], "two": [news.FeedResponse(200, rss(story(2)))]}))
    fetcher = Fetcher({"one": [TimeoutError("Bearer SECRET https://private/url"), TimeoutError("SECRET")],
                       "two": [news.FeedResponse(200, rss(story(3)))]})
    output, _ = run(sources=sources, state=state, previous=first, fetcher=fetcher, now=NOW + timedelta(minutes=30))
    assert {item["title"] for item in output["items"]} == {"New theorem 1", "New theorem 3"}
    assert output["source_errors"] == [{"source_id": "one", "error": "Source unavailable (TimeoutError)"}]
    assert "SECRET" not in json.dumps(output)
    assert len([key for key, _ in fetcher.calls if key == "one"]) == 2


def test_full_outage_retains_last_success_and_recovers_from_public_snapshot_without_state():
    previous, _ = run()
    output, _ = run(previous=previous, now=NOW + timedelta(minutes=30),
                    fetcher=Fetcher({"publisher": [OSError("private data"), OSError("private data")]}))
    assert output["items"] == previous["items"]
    assert output["last_successful_refresh_at"] == previous["last_successful_refresh_at"]
    assert output["updated_at"] == previous["updated_at"]
    assert output["last_checked_at"] != previous["last_checked_at"]


def test_retry_only_transient_failures_and_do_not_accept_uncached_304():
    fetcher = Fetcher({"publisher": [news.FeedResponse(503), news.FeedResponse(200, rss(story()))]})
    output, _ = run(fetcher=fetcher)
    assert len(output["items"]) == 1 and len(fetcher.calls) == 2
    for response in (news.FeedResponse(200, b'<html/>'), news.FeedResponse(304), news.FeedResponse(403)):
        fetcher = Fetcher({"publisher": [response]})
        output, _ = run(fetcher=fetcher)
        assert output["items"] == [] and len(fetcher.calls) == 1
        assert len(output["source_errors"]) == 1


def test_model_edits_are_bound_to_known_ids_and_only_summary_can_change():
    received = []
    def malicious(rows):
        received.extend(rows)
        return [{**rows[0], "summary": "A cautious model summary.", "title": "Solved everything", "url": "https://evil.example.org/",
                 "status": "Solved", "published_at": "2099-01-01T00:00:00Z"},
                {"id": "invented", "summary": "An invented story"},
                {"id": rows[0]["id"], "summary": "Duplicate override"}]
    raw = "Details of a partial result. " * 100
    output, state = run(rows=[story(summary=raw)], summarizer=malicious)
    item = output["items"][0]
    assert item["summary"] == "A cautious model summary."
    assert item["title"] == "New theorem 1" and item["status"] == "Published"
    assert item["url"] == story()["url"] and item["published_at"] == story()["published_at"]
    assert len(received[0]["excerpt"]) <= 1800 and len(received[0]["excerpt"]) > 1700
    assert "excerpt" not in item
    assert state["feeds"]["publisher"]["items"][0]["summary"] != item["summary"]
    next_output, _ = run(state=state, previous=output, fetcher=Fetcher({"publisher": [news.FeedResponse(304)]}),
                         summarizer=lambda _: pytest.fail("Processed items must not call model again"), now=NOW + timedelta(minutes=30))
    assert next_output["items"] == output["items"]


def test_model_batches_are_capped_and_pending_queue_drains_without_repeat():
    batches = []
    def summarize(rows):
        batches.append([row["id"] for row in rows])
        return [{"id": row["id"], "summary": "Cautious source-based summary."} for row in rows]
    output, state = run(rows=[story(i) for i in range(10)], summarizer=summarize)
    assert len(batches[0]) == 8 and len(state["pending_summaries"]) == 2
    _, state = run(state=state, previous=output, fetcher=Fetcher({"publisher": [news.FeedResponse(304)]}),
                   summarizer=summarize, now=NOW + timedelta(minutes=30))
    assert [len(batch) for batch in batches] == [8, 2]
    assert not set(batches[0]) & set(batches[1])
    assert not state["pending_summaries"]


def test_model_ready_batch_prioritizes_newest_published_articles():
    batches = []
    def summarize(rows):
        batches.append(rows)
        return [{"id": row["id"], "summary": "A cautious summary."} for row in rows]
    rows = [story(i, published_at=news.timestamp(NOW - timedelta(hours=i + 1))) for i in reversed(range(10))]
    _, state = run(rows=rows, summarizer=summarize)
    assert [row["title"] for row in batches[0]] == [f"New theorem {i}" for i in range(8)]
    assert len(state["pending_summaries"]) == 2


def test_source_correction_discards_stale_annotation_without_rewriting_metadata():
    output, state = run(summarizer=lambda rows: [{"id": rows[0]["id"], "summary": "Summary of original source text."}])
    updated, _ = run(state=state, previous=output, rows=[story(title="Correction: proof claim withdrawn", summary="The authors withdrew their original claim.")],
                     summarizer=lambda _: pytest.fail("Only newly observed articles are sent to the model"), now=NOW + timedelta(minutes=30))
    assert updated["items"][0]["title"] == "Correction: proof claim withdrawn"
    assert updated["items"][0]["summary"] == "The authors withdrew their original claim."


def test_failed_model_keeps_source_summary_and_retries_after_delay_only():
    calls = []
    def failed(rows):
        calls.append(rows)
        raise RuntimeError("SECRET token")
    output, state = run(summarizer=failed)
    assert output["items"][0]["summary"] == story()["summary"]
    assert output["source_errors"] == []
    assert output["summary_errors"] == [{"error": "Summary fallback (RuntimeError)"}]
    assert output["last_successful_refresh_at"] == news.timestamp(NOW)
    assert "SECRET" not in json.dumps(output)
    output, state = run(state=state, previous=output, fetcher=Fetcher({"publisher": [news.FeedResponse(304)]}),
                        summarizer=failed, now=NOW + timedelta(minutes=10))
    assert len(calls) == 1
    _, state = run(state=state, previous=output, fetcher=Fetcher({"publisher": [news.FeedResponse(304)]}),
                   summarizer=failed, now=NOW + timedelta(minutes=30))
    assert len(calls) == 2
    assert next(iter(state["pending_summaries"].values()))["retry_after"] == news.timestamp(NOW + timedelta(minutes=90))


def test_reviewed_seed_wins_duplicate_url_and_bypasses_model():
    seed = {"items": [{**story(), "source": "publisher", "summary": "Reviewed context: proof is announced, not accepted.",
                       "status": "Proof announced", "category": "mathematics"}]}
    output, state = run(seed=seed, summarizer=lambda _: pytest.fail("Reviewed seed must bypass summarizer"))
    assert output["items"][0]["summary"] == seed["items"][0]["summary"]
    assert output["items"][0]["status"] == "Proof announced"
    assert not state["pending_summaries"]


def test_manual_sources_accept_seed_but_do_not_fetch_or_claim_refresh_success():
    manual = source("manual", manual=True, feed_url=None)
    seed = {"items": [{**story(), "source": "manual", "status": "Model release"}]}
    output, state = run(sources=[manual], seed=seed, fetcher=Fetcher({}))
    assert len(output["items"]) == 1 and output["items"][0]["status"] == "Model release"
    assert output["source_errors"] == [] and output["last_successful_refresh_at"] is None
    assert output["sources"][0]["automated"] is False
    assert state["feeds"] == {}
    with pytest.raises(news.NewsError):
        source("manual", manual=True)


def test_reviewed_related_links_bounded_and_trusted_model_cannot_add_links():
    other = source("other", manual=True, feed_url=None, url="https://secondary.example.org/", allowed_hosts=["secondary.example.org"])
    row = {**story(), "source": "publisher", "related_links": [
        {"label": "Primary response", "url": "https://secondary.example.org/statement"},
        {"label": "Unknown", "url": "https://evil.example.org/"},
        {"label": "Credentials", "url": "https://user:secret@secondary.example.org/"},
        {"label": "", "url": "https://news.example.org/empty-label"},
        {"label": "Additional " * 30, "url": "https://news.example.org/valid"},
        {"label": "Beyond cap", "url": "https://news.example.org/beyond-cap"}]}
    output, _ = run(sources=[source(), other], seed={"items": [row]}, fetcher=Fetcher({"publisher": [news.FeedResponse(200, rss(story()))]}))
    links = output["items"][0]["related_links"]
    assert len(links) == 2 and links[0]["url"] == "https://secondary.example.org/statement"
    assert len(links[1]["label"]) <= 120
    output, _ = run(summarizer=lambda rows: [{"id": rows[0]["id"], "summary": "Some context", "related_links": [{"label": "Fake", "url": "https://evil.example.org/"}]}])
    assert "related_links" not in output["items"][0]


def test_editorial_seed_reserved_and_automatic_sources_balanced_within_total_cap():
    sources = [source(f"source-{i}") for i in range(7)]
    responses = {s.id: [news.FeedResponse(200, rss(*[story(i * 100 + k) for k in range(30)]))] for i, s in enumerate(sources)}
    seed = {"items": [{**story(9999, title="Major older announcement", published_at="2026-08-01T00:00:00Z"), "source": sources[0].id}]}
    output, _ = run(sources=sources, seed=seed, fetcher=Fetcher(responses))
    assert len(output["items"]) == 60
    assert output["items"][-1]["title"] == "Major older announcement"
    for s in sources:
        assert len([i for i in output["items"] if i["source"] == s.id and i["title"] != "Major older announcement"]) <= 10


def test_preprints_keep_source_status_without_published_label():
    output, _ = run(sources=[source(status="Preprint")])
    assert output["items"][0]["status"] == "Preprint"


def test_arxiv_pending_summary_is_headline_only_but_private_excerpt_and_annotation_survive():
    arxiv = source("arxiv-ai", url="https://arxiv.org/list/cs.AI/recent", feed_url="https://rss.arxiv.org/rss/cs.AI",
                   allowed_hosts=["arxiv.org", "rss.arxiv.org"], status="Preprint")
    abstract = "Authors: Researchers. Abstract: We study $x^2+y^2=z^2$ under additional assumptions. " * 30
    row = story(url="https://arxiv.org/abs/2609.12345", summary=abstract)
    output, state = run(sources=[arxiv], rows=[row])
    assert output["items"][0]["summary"] == "" and output["items"][0]["title"] == row["title"]
    excerpt = state["feeds"][arxiv.id]["items"][0]["excerpt"]
    assert 1700 < len(excerpt) <= 1800 and "$x^2+y^2=z^2$" in excerpt
    received = []
    def summarize(rows):
        received.extend(rows)
        return [{"id": rows[0]["id"], "summary": "The authors study an equation under additional assumptions."}]
    updated, _ = run(sources=[arxiv], state=state, previous=output, fetcher=Fetcher({arxiv.id: [news.FeedResponse(304)]}),
                     summarizer=summarize, now=NOW + timedelta(minutes=30))
    assert received[0]["excerpt"] == excerpt
    assert updated["items"][0]["summary"] == "The authors study an equation under additional assumptions."
    reviewed, _ = run(sources=[arxiv], rows=[row], seed={"items": [{**row, "source": arxiv.id, "summary": "A reviewed summary of this preprint."}]})
    assert reviewed["items"][0]["summary"] == "A reviewed summary of this preprint."


def test_atomic_publication_failure_preserves_old_document_and_cleans_temp(tmp_path, monkeypatch):
    output = tmp_path / "news.json"
    output.write_text('{"old":true}\n')
    monkeypatch.setattr(news.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("disk failure")))
    with pytest.raises(OSError):
        news.atomic_json(output, {"new": True})
    assert output.read_text() == '{"old":true}\n'
    assert list(tmp_path.iterdir()) == [output]


def test_atomic_state_permissions_and_valid_json(tmp_path):
    output = tmp_path / "state.json"
    news.atomic_json(output, {"schema_version": 1, "text": "Mathematics π"}, mode=0o600)
    assert json.loads(output.read_text())["text"] == "Mathematics π"
    assert output.stat().st_mode & 0o777 == 0o600


def test_cli_dry_run_has_no_writes_and_no_model_call(tmp_path, monkeypatch, capsys):
    sources = tmp_path / "sources.json"
    sources.write_text(json.dumps({"schema_version": 1, "sources": [{**source().__dict__, "allowed_hosts": list(source().allowed_hosts),
                                                                   "include_keywords": [], "exclude_keywords": []}]}))
    monkeypatch.setattr(news, "HTTPSFetcher", lambda: Fetcher({"publisher": [news.FeedResponse(200, rss(story()))]}))
    output, state = tmp_path / "news.json", tmp_path / "state.json"
    code = news.main(["--sources", str(sources), "--output", str(output), "--state", str(state), "--no-model", "--dry-run"])
    assert code == 0
    assert not output.exists() and not state.exists()
    assert json.loads(capsys.readouterr().out)["dry_run"] is True


def test_failed_publication_does_not_advance_private_state(tmp_path, monkeypatch, capsys):
    config = tmp_path / "sources.json"
    config.write_text(json.dumps({"schema_version": 1, "sources": [{**source().__dict__, "allowed_hosts": list(source().allowed_hosts),
                                                                  "include_keywords": [], "exclude_keywords": []}]}))
    monkeypatch.setattr(news, "HTTPSFetcher", lambda: Fetcher({"publisher": [news.FeedResponse(200, rss(story()))]}))
    output, state = tmp_path / "news.json", tmp_path / "state.json"
    state.write_text('{"schema_version":1,"retained":true}\n')
    original = state.read_bytes()
    writes = []
    def fail_publish(path, *args, **kwargs):
        writes.append(path)
        raise OSError("SECRET private failure context")
    monkeypatch.setattr(news, "atomic_json", fail_publish)
    assert news.main(["--sources", str(config), "--output", str(output), "--state", str(state), "--no-model"]) == 1
    assert writes == [output] and state.read_bytes() == original
    assert "SECRET" not in capsys.readouterr().out


def public_dns(host, port, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


class HTTPResponse:
    def __init__(self, status=200, headers=None, body=b"<rss/>"):
        self.status, self.headers, self.body = status, headers or {}, body

    def getheader(self, key, default=None):
        return self.headers.get(key, default)

    def read1(self, size):
        data, self.body = self.body[:size], self.body[size:]
        return data


class Connections:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, host, port, **kwargs):
        parent = self
        response = self.responses.pop(0)
        class Connection:
            sock = None
            def request(self, method, target, headers):
                parent.calls.append((host, port, method, target, headers, self))
            def getresponse(self):
                return response
            def close(self):
                self.closed = True
        return Connection()


@pytest.mark.parametrize("private", ["127.0.0.1", "10.0.0.2", "169.254.169.254", "::1", "fc00::1"])
def test_dns_private_addresses_rejected_even_mixed_with_public(private):
    def resolver(*args, **kwargs):
        return public_dns(*args, **kwargs) + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (private, 443))]
    factory = Connections()
    with pytest.raises(news.NewsError, match="non-public"):
        news.HTTPSFetcher(resolver=resolver, connection_factory=factory).fetch(source(), {})
    assert factory.calls == []


def test_transport_pins_public_ip_and_preserves_tls_hostname(monkeypatch):
    factory = Connections(HTTPResponse(headers={"ETag": '"v1"'}))
    fetcher = news.HTTPSFetcher(resolver=public_dns, connection_factory=factory)
    result = fetcher.fetch(source(), {"If-None-Match": '"v0"', "Authorization": "SECRET"})
    host, port, method, target, headers, connection = factory.calls[0]
    assert host == "news.example.org" and port == 443 and target == "/publisher.xml"
    assert headers["If-None-Match"] == '"v0"' and "Authorization" not in headers
    connected = []
    monkeypatch.setattr(news.socket, "create_connection", lambda *args: connected.append(args))
    connection._create_connection(("rebound.example.org", 443), 15)
    assert connected[0][0] == ("93.184.216.34", 443)
    assert result.etag == '"v1"' and connection.closed


@pytest.mark.parametrize("location", ["http://news.example.org/feed", "https://evil.example.org/feed", "https://127.0.0.1/feed", "https://u:p@news.example.org/feed"])
def test_redirects_outside_exact_https_allowlist_are_never_followed(location):
    factory = Connections(HTTPResponse(302, {"Location": location}))
    with pytest.raises(news.NewsError):
        news.HTTPSFetcher(resolver=public_dns, connection_factory=factory).fetch(source(), {})
    assert len(factory.calls) == 1 and factory.calls[0][-1].closed


def test_same_host_relative_redirect_is_validated_and_followed():
    factory = Connections(HTTPResponse(302, {"Location": "/new-feed.xml"}), HTTPResponse())
    result = news.HTTPSFetcher(resolver=public_dns, connection_factory=factory).fetch(source(), {})
    assert result.status == 200
    assert [call[3] for call in factory.calls] == ["/publisher.xml", "/new-feed.xml"]


@pytest.mark.parametrize("response", [HTTPResponse(headers={"Content-Length": "1000"}),
    HTTPResponse(body=b"x" * 65), HTTPResponse(headers={"Content-Encoding": "gzip"})])
def test_transport_size_and_compression_limits(response):
    with pytest.raises(news.NewsError):
        news.HTTPSFetcher(resolver=public_dns, connection_factory=Connections(response), max_bytes=64).fetch(source(), {})
