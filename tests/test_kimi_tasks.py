import io
import json
import urllib.error

import pytest

from agent_monitor import kimi_tasks
from agent_monitor import sponsored_kimi_client as client
from agent_monitor.kimi_tasks import DailyBudget, KimiTaskError, call_json, exact_quote, extract_json_object


def reply(content, finish="stop"):
    return {"choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": finish}]}


class Recorder:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.bodies = []

    def __call__(self, body, *, client_id, cache_key, timeout):
        self.bodies.append(json.loads(json.dumps(body)))
        self.kwargs = {"client_id": client_id, "cache_key": cache_key, "timeout": timeout}
        item = self.replies.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def assert_unchained(error):
    """No provider or transport detail rides along as the error's cause or context."""
    assert error.__cause__ is None and (error.__context__ is None or error.__suppress_context__)


def keep_items(value):
    if set(value) != {"items"} or not isinstance(value["items"], list):
        raise ValueError("schema")
    return value


def test_success_returns_validated_object_and_sends_strict_request():
    fake = Recorder(reply('{"items": [1, 2]}'))
    result = call_json("triage", "Rank the items.", {"items": [{"id": "a"}]}, client_id="ansatze-news",
                       cache_key="news", validate=keep_items, request=fake, max_tokens=2048, effort="low", timeout=90)
    assert result == {"items": [1, 2]}
    body = fake.bodies[0]
    assert body["model"] == "kimi-k3" and body["stream"] is False and body["max_tokens"] == 2048
    assert body["reasoning_effort"] == "low" and "tools" not in body
    system, user = body["messages"]
    assert system["role"] == "system" and kimi_tasks.UNTRUSTED_DATA_RULE in system["content"]
    assert json.loads(user["content"]) == {"task": "triage", "data": {"items": [{"id": "a"}]}}
    assert fake.kwargs == {"client_id": "ansatze-news", "cache_key": "news", "timeout": 90.0}


@pytest.mark.parametrize("content", ['```json\n{"items": []}\n```', '```\n{"items": []}\n```'])
def test_fenced_json_is_accepted(content):
    assert call_json("t", "s", {}, client_id="c", cache_key="k", validate=keep_items,
                     request=Recorder(reply(content))) == {"items": []}


@pytest.mark.parametrize("content", ["Sure! {\"items\": []}", "[1, 2]", "", "```python\n{}\n```", "x" * 70_000])
def test_non_json_replies_are_invalid(content):
    with pytest.raises(KimiTaskError) as caught:
        call_json("t", "s", {}, client_id="c", cache_key="k", validate=keep_items, request=Recorder(reply(content)))
    assert caught.value.kind == "invalid"


def test_validator_rejection_is_invalid_and_not_retried():
    fake = Recorder(reply('{"other": 1}'), reply('{"items": []}'))
    with pytest.raises(KimiTaskError) as caught:
        call_json("t", "s", {}, client_id="c", cache_key="k", validate=keep_items, request=fake)
    assert caught.value.kind == "invalid" and len(fake.bodies) == 1


def test_empty_completion_retries_once_at_lower_effort():
    fake = Recorder(reply(""), reply('{"items": []}'))
    assert call_json("t", "s", {}, client_id="c", cache_key="k", validate=keep_items, request=fake,
                     effort="high") == {"items": []}
    assert [b["reasoning_effort"] for b in fake.bodies] == ["high", "low"]


def test_empty_completion_at_low_effort_is_invalid():
    fake = Recorder(reply(""), reply('{"items": []}'))
    with pytest.raises(KimiTaskError) as caught:
        call_json("t", "s", {}, client_id="c", cache_key="k", validate=keep_items, request=fake, effort="low")
    assert caught.value.kind == "invalid" and len(fake.bodies) == 1


def test_truncated_reply_is_invalid():
    with pytest.raises(KimiTaskError) as caught:
        call_json("t", "s", {}, client_id="c", cache_key="k", validate=keep_items,
                  request=Recorder(reply('{"items": []}', finish="length")))
    assert caught.value.kind == "invalid"


def test_budget_is_taken_per_attempt_and_persists_in_state():
    state = {}
    budget = DailyBudget(state, 2, today="2026-10-11")
    fake = Recorder(reply(""), reply('{"items": []}'), reply('{"items": []}'))
    call_json("t", "s", {}, client_id="c", cache_key="k", validate=keep_items, request=fake, effort="high", budget=budget)
    assert state == {"day": "2026-10-11", "calls": 2}
    with pytest.raises(KimiTaskError) as caught:
        call_json("t", "s", {}, client_id="c", cache_key="k", validate=keep_items, request=fake, budget=budget)
    assert caught.value.kind == "budget" and len(fake.bodies) == 2
    assert DailyBudget(state, 2, today="2026-10-12").remaining() == 2 and state["calls"] == 0


def test_zero_cap_never_calls():
    fake = Recorder()
    with pytest.raises(KimiTaskError) as caught:
        call_json("t", "s", {}, client_id="c", cache_key="k", validate=keep_items, request=fake,
                  budget=DailyBudget({}, 0))
    assert caught.value.kind == "budget" and fake.bodies == []


@pytest.mark.parametrize(("error", "kind"), [
    (client.SponsoredKimiError("Sponsored Kimi request failed: Sponsored Kimi call quota reached"), "refused"),
    (client.SponsoredKimiError("Sponsored Kimi service is temporarily unavailable"), "unavailable"),
    (RuntimeError("provider said sk-secret-123456789 and the prompt"), "unavailable"),
])
def test_failures_are_public_safe(error, kind):
    with pytest.raises(KimiTaskError) as caught:
        call_json("t", "s", {}, client_id="c", cache_key="k", validate=keep_items, request=Recorder(error))
    assert caught.value.kind == kind
    assert "sk-secret" not in str(caught.value) and "prompt" not in str(caught.value)
    assert_unchained(caught.value)


def test_oversized_payload_is_refused_before_any_call():
    fake = Recorder()
    with pytest.raises(KimiTaskError) as caught:
        call_json("t", "s", {"text": "x" * 200_000}, client_id="c", cache_key="k", validate=keep_items, request=fake)
    assert caught.value.kind == "invalid" and fake.bodies == []


def test_unknown_effort_is_refused():
    with pytest.raises(KimiTaskError):
        call_json("t", "s", {}, client_id="c", cache_key="k", validate=keep_items, request=Recorder(), effort="medium")


def test_exact_quote_requires_verbatim_substring():
    source = "The  Kourovka Notebook marks problem 12.20\nwith a sharp sign."
    assert exact_quote("marks problem 12.20 with a sharp", source) == "marks problem 12.20 with a sharp"
    for bad in ("marks problem 12.21", "short", 42, "x" * 300):
        with pytest.raises(ValueError):
            exact_quote(bad, source)


def test_extract_json_object_rejects_arrays_and_trailing_text():
    assert extract_json_object(' {"a": 1} ') == {"a": 1}
    for bad in ('{"a": 1} extra', "[]", None):
        with pytest.raises(KimiTaskError):
            extract_json_object(bad)


def test_default_request_uses_loopback_broker_with_deadline_header(monkeypatch):
    seen = {}
    monkeypatch.setattr(client, "issue_credentials", lambda **kw: seen.setdefault("issue", kw) and {"KIMI_API_KEY": "tok"} or {"KIMI_API_KEY": "tok"})

    def fake_request(path, **kw):
        seen["path"], seen["kw"] = path, kw
        return reply('{"items": []}'), ""

    monkeypatch.setattr(client, "_request", fake_request)
    assert call_json("t", "s", {}, client_id="ansatze-problems", cache_key="problems", validate=keep_items,
                     timeout=100) == {"items": []}
    assert seen["issue"] == {"engine": "plain", "client_id": "ansatze-problems", "cache_key": "problems",
                             "minimum_ttl_seconds": 130}
    assert seen["path"] == "/internal/v1/chat/completions"
    assert seen["kw"]["bearer"] == "tok" and seen["kw"]["timeout"] == 100.0
    assert seen["kw"]["extra_headers"] == {"X-Ansatze-Timeout": "100"}


def test_empty_or_cut_off_replies_are_marked_incomplete_and_schema_breaks_are_not():
    for content, finish in (('{"items": []', "length"), ("", "stop"), ("   ", "stop")):
        with pytest.raises(KimiTaskError) as caught:
            call_json("t", "s", {}, client_id="c", cache_key="k", validate=keep_items, request=Recorder(reply(content, finish)))
        assert caught.value.kind == "invalid" and caught.value.incomplete is True, (content, finish)
    with pytest.raises(KimiTaskError) as caught:
        call_json("t", "s", {}, client_id="c", cache_key="k", validate=keep_items, request=Recorder(reply('{"other": 1}')))
    assert caught.value.kind == "invalid" and caught.value.incomplete is False


# ── a call Kimi refused before processing it ─────────────────────────────────


def refused(status=503, *, kind="upstream_rejected", elapsed_ms=126, retry_after=None, forwarded=True):
    """The broker's typed reply to a refused call (``public_kimi_gateway.error_body``)."""
    upstream = kind == "upstream_rejected"
    error = {"message": "Kimi request was rejected", "type": kind, "forwarded": forwarded,
             "upstream_status": status if upstream else None, "upstream_elapsed_ms": elapsed_ms if upstream else None,
             "retry_after": retry_after}
    return client.SponsoredKimiError("Sponsored Kimi request failed: Kimi request was rejected", status=status,
                                     error=error)


class Clock:
    """A monotonic clock that only sleeping advances."""

    def __init__(self):
        self.now, self.sleeps = 1000.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def call(fake, clock=None, **kwargs):
    clock = clock or Clock()
    return call_json("t", "s", {}, client_id="c", cache_key="k", validate=keep_items, request=fake,
                     sleep=clock.sleep, clock=clock, **kwargs)


@pytest.mark.parametrize("status", [429, 503])
def test_a_fast_refusal_is_sent_again_after_its_delay(status):
    clock = Clock()
    fake = Recorder(refused(status), reply('{"items": []}'))
    assert call(fake, clock, timeout=90) == {"items": []}
    assert len(fake.bodies) == 2 and fake.bodies[0] == fake.bodies[1] and clock.sleeps == [5.0]
    # The resend has what is left of the call's own deadline, not a new one.
    assert fake.kwargs["timeout"] == 85.0


@pytest.mark.parametrize(("retry_after", "wait"), [(1, 2.0), (7, 7.0), (120, 30.0)])
def test_the_refusals_retry_after_sets_the_wait(retry_after, wait):
    clock = Clock()
    assert call(Recorder(refused(429, retry_after=retry_after), reply('{"items": []}')), clock) == {"items": []}
    assert clock.sleeps == [wait]


def test_a_call_the_gateway_never_forwarded_is_sent_again():
    clock = Clock()
    fake = Recorder(refused(429, kind="gateway_busy", forwarded=False, retry_after=2), reply('{"items": []}'))
    assert call(fake, clock) == {"items": []} and len(fake.bodies) == 2 and clock.sleeps == [2.0]


def test_a_job_resends_a_refusal_once_and_reports_the_second_as_before():
    # Runs allow three attempts; a job runs again soon, so it sends a refused call only twice.
    assert kimi_tasks.JOB_RESENDS == 1
    clock = Clock()
    fake = Recorder(refused(503), refused(429), reply('{"items": []}'))
    with pytest.raises(KimiTaskError) as caught:
        call(fake, clock, timeout=90)
    assert len(fake.bodies) == 2 and clock.sleeps == [5.0]
    assert (caught.value.kind, str(caught.value)) == ("unavailable", "Kimi is temporarily unavailable")
    assert_unchained(caught.value)


@pytest.mark.parametrize("error", [
    refused(503, elapsed_ms=2500),  # refused only after 2.5 s: Kimi may have processed it
    refused(500, elapsed_ms=100),
    refused(502, kind="upstream_unavailable"),
    refused(504, kind="upstream_timeout"),
    client.SponsoredKimiError("Sponsored Kimi request failed: HTTP 503", status=503),  # untyped
    client.SponsoredKimiError("Sponsored Kimi service is temporarily unavailable"),  # no reply
    ConnectionResetError("reset after the request was sent"),
], ids=["slow-503", "500", "unavailable", "timeout", "untyped", "no-reply", "reset"])
def test_a_failure_kimi_may_have_processed_is_never_sent_again(error):
    clock = Clock()
    fake = Recorder(error, reply('{"items": []}'))
    with pytest.raises(KimiTaskError) as caught:
        call(fake, clock)
    assert len(fake.bodies) == 1 and clock.sleeps == []
    assert (caught.value.kind, str(caught.value)) == ("unavailable", "Kimi is temporarily unavailable")
    assert_unchained(caught.value)


def test_a_quota_refusal_is_not_sent_again_and_still_reads_as_refused():
    quota = client.SponsoredKimiError(
        "Sponsored Kimi request failed: Sponsored Kimi call quota reached", status=429,
        error={"message": "Sponsored Kimi call quota reached", "type": "sponsor_quota", "forwarded": False})
    fake = Recorder(quota, reply('{"items": []}'))
    with pytest.raises(KimiTaskError) as caught:
        call(fake)
    assert caught.value.kind == "refused" and len(fake.bodies) == 1


def test_each_resend_takes_one_call_from_the_budget():
    state = {}
    budget = DailyBudget(state, 5, today="2026-11-11")
    assert call(Recorder(refused(503), reply('{"items": []}')), budget=budget) == {"items": []}
    assert state["calls"] == 2


def test_a_resend_the_budget_cannot_pay_for_is_not_sent():
    state = {}
    clock = Clock()
    fake = Recorder(refused(503), reply('{"items": []}'))
    with pytest.raises(KimiTaskError) as caught:
        call(fake, clock, budget=DailyBudget(state, 1, today="2026-11-11"))
    # The refusal is reported (the job backs off), not a spent budget.
    assert caught.value.kind == "unavailable" and len(fake.bodies) == 1 and clock.sleeps == []
    assert state["calls"] == 1


def test_a_resend_needs_thirty_seconds_of_the_call_left():
    clock = Clock()
    fake = Recorder(refused(503, retry_after=15), reply('{"items": []}'))
    with pytest.raises(KimiTaskError):
        call(fake, clock, timeout=40)  # 40 - 15 = 25 s would be left
    assert len(fake.bodies) == 1 and clock.sleeps == []


def test_a_refused_request_keeps_the_lower_effort_resend_of_an_empty_reply():
    clock = Clock()
    fake = Recorder(refused(429), reply(""), refused(503), reply('{"items": []}'))
    assert call(fake, clock, effort="high") == {"items": []}
    assert [body["reasoning_effort"] for body in fake.bodies] == ["high", "high", "low", "low"]
    assert clock.sleeps == [5.0, 5.0]


def test_the_default_request_sends_the_resend_with_the_time_left(monkeypatch):
    monkeypatch.setattr(client, "issue_credentials", lambda **kw: {"KIMI_API_KEY": "tok"})
    sent = []

    def fake_request(path, **kw):
        sent.append((kw["timeout"], kw["extra_headers"]))
        if len(sent) == 1:
            raise refused(503)
        return reply('{"items": []}'), ""

    monkeypatch.setattr(client, "_request", fake_request)
    clock = Clock()
    assert call_json("t", "s", {}, client_id="ansatze-problems", cache_key="problems", validate=keep_items,
                     timeout=90, sleep=clock.sleep, clock=clock) == {"items": []}
    assert sent == [(90.0, {"X-Ansatze-Timeout": "90"}), (85.0, {"X-Ansatze-Timeout": "85"})]


TYPED_REFUSAL = {"message": "Kimi request was rejected", "type": "upstream_rejected", "forwarded": True,
                 "upstream_status": 503, "upstream_elapsed_ms": 126, "retry_after": None}


@pytest.mark.parametrize(("status", "raw", "message", "error"), [
    (503, json.dumps({"error": TYPED_REFUSAL}).encode(), "Sponsored Kimi request failed: Kimi request was rejected",
     TYPED_REFUSAL),
    (429, b'{"error": "Public run quota reached; try again later"}',
     "Sponsored Kimi request failed: Public run quota reached; try again later", None),
    (503, b"<html>busy</html>", "Sponsored Kimi request failed: HTTP 503", None),
], ids=["typed", "untyped", "not-json"])
def test_an_http_error_reply_keeps_its_status_and_typed_error(monkeypatch, status, raw, message, error):
    def answer(request, timeout):
        raise urllib.error.HTTPError(request.full_url, status, "refused", {}, io.BytesIO(raw))

    monkeypatch.delenv("AGENT_MONITOR_SPONSORED_KIMI_URL", raising=False)
    monkeypatch.setattr(client._opener, "open", answer)
    with pytest.raises(client.SponsoredKimiError) as caught:
        client._request("/internal/v1/chat/completions", method="POST", payload={"model": "kimi-k3"}, timeout=5)
    assert str(caught.value) == message
    assert caught.value.status == status and caught.value.error == error


def test_other_client_errors_carry_no_status():
    error = client.SponsoredKimiError("Sponsored Kimi service is temporarily unavailable")
    assert error.status is None and error.error is None and error.args == ("Sponsored Kimi service is temporarily unavailable",)


def test_the_last_failure_is_the_one_reported():
    # A soon-freed site quota is sent again; the resend's lost connection is what the job hears.
    quota = client.SponsoredKimiError(
        "Sponsored Kimi request failed: Public Kimi gateway hourly call quota reached", status=429,
        error={"message": "Public Kimi gateway hourly call quota reached", "type": "gateway_quota",
               "forwarded": False, "retry_after": 10})
    clock = Clock()
    fake = Recorder(quota, ConnectionResetError("reset"), reply('{"items": []}'))
    with pytest.raises(KimiTaskError) as caught:
        call(fake, clock)
    assert len(fake.bodies) == 2 and clock.sleeps == [10.0]
    assert (caught.value.kind, str(caught.value)) == ("unavailable", "Kimi is temporarily unavailable")
