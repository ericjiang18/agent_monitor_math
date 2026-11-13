"""Sponsored Kimi client contract: typed broker errors, deadline header,
retry only what never reached processing, and user-facing messages."""
from __future__ import annotations

import http.client
import os
import unittest
from unittest.mock import patch

from agent_monitor import kimi_k3, lean_verify, proof_graph
from agent_monitor.runners import api_backend


_OK = {"model": "kimi-k3", "choices": [{"message": {"content": "KIMI_OK"}}]}
_SPONSORED_BASE = "http://127.0.0.1:4610/internal/v1"


def _typed(kind: str, **fields: object) -> dict:
    error = {
        "message": "safe message",
        "type": kind,
        "forwarded": False,
        "upstream_status": None,
        "upstream_elapsed_ms": None,
        "retry_after": None,
    }
    error.update(fields)
    return {"error": error}


def _fast_503(**fields: object) -> dict:
    return _typed(
        "upstream_rejected",
        **{"forwarded": True, "upstream_status": 503, "upstream_elapsed_ms": 400, **fields},
    )


class _Clock:
    """A fake monotonic clock; sleeping advances it."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class SponsoredRetryableTests(unittest.TestCase):
    def test_truth_table(self) -> None:
        cases = [
            # Nothing could be sent to the loopback broker.
            (0, "connection refused", True),
            (0, None, True),
            # Typed refusals that never left the broker or gateway.
            (429, _typed("gateway_busy", retry_after=2), True),
            (502, _typed("gateway_unavailable"), True),
            (502, _typed("upstream_unavailable"), True),
            (429, _typed("gateway_quota", retry_after=30), True),
            (429, _typed("gateway_quota", retry_after=31), False),
            (429, _typed("gateway_quota"), False),
            (429, _typed("gateway_quota", retry_after=True), False),
            # The same kinds once forwarded (or without the flag) are ambiguous.
            (502, _typed("upstream_unavailable", forwarded=True), False),
            (429, _typed("gateway_busy", forwarded=None), False),
            (429, _typed("gateway_quota", forwarded=True, retry_after=2), False),
            # A fast upstream 429/503 was refused before any processing.
            (503, _fast_503(), True),
            (503, _fast_503(upstream_elapsed_ms=2000), True),
            (503, _fast_503(upstream_elapsed_ms=0), True),
            (429, _typed("upstream_rejected", forwarded=True, upstream_status=429,
                         upstream_elapsed_ms=10), True),
            (503, _fast_503(upstream_elapsed_ms=2001), False),
            (503, _fast_503(upstream_elapsed_ms=None), False),
            (503, _fast_503(upstream_elapsed_ms="100"), False),
            (503, _fast_503(upstream_elapsed_ms=True), False),
            (503, _fast_503(upstream_elapsed_ms=-1), False),
            (502, _typed("upstream_rejected", forwarded=True, upstream_status=502,
                         upstream_elapsed_ms=10), False),
            (500, _typed("upstream_rejected", forwarded=True, upstream_status=500,
                         upstream_elapsed_ms=10), False),
            (400, _typed("upstream_rejected", forwarded=True, upstream_status=400,
                         upstream_elapsed_ms=10), False),
            # Everything else may have been processed.
            (504, _typed("upstream_timeout", forwarded=True), False),
            (504, _typed("gateway_timeout", forwarded=True), False),
            (502, _typed("response_too_large", forwarded=True), False),
            (429, _typed("sponsor_quota", retry_after=60), False),
            (401, _typed("unauthorized"), False),
            (403, _typed("forbidden"), False),
            (400, _typed("bad_request"), False),
            (502, _typed("something_new"), False),
            # Untyped bodies (an older broker) are never retried.
            (503, {"error": {"message": "Kimi request was rejected"}}, False),
            (429, {"error": {"message": "public Kimi call concurrency reached"}}, False),
            (502, {"error": "Kimi upstream unavailable"}, False),
            (503, "Service Unavailable", False),
            (503, None, False),
            # Any 2xx, including an empty completion.
            (200, _OK, False),
            (200, {"choices": []}, False),
            (200, _typed("gateway_busy"), False),
            (204, None, False),
        ]
        for code, body, expected in cases:
            with self.subTest(code=code, body=body):
                self.assertIs(kimi_k3.sponsored_retryable(code, body), expected)

    def test_delays_are_deterministic_and_bounded(self) -> None:
        self.assertEqual(kimi_k3.sponsored_retry_delay(1, None), 5.0)
        self.assertEqual(kimi_k3.sponsored_retry_delay(2, None), 15.0)
        self.assertEqual(kimi_k3.sponsored_retry_delay(1, _fast_503()), 5.0)
        # A retry_after hint only lengthens the wait (to at most 30 s); a
        # shorter one is not followed: Kimi's provider has kept refusing for
        # several seconds after hinting 1-2 s.
        cases = ((0, 5.0, 15.0), (1, 5.0, 15.0), (2, 5.0, 15.0), (7, 7.0, 15.0),
                 (17, 17.0, 17.0), (30, 30.0, 30.0), (3600, 30.0, 30.0))
        for retry_after, first, second in cases:
            with self.subTest(retry_after=retry_after):
                body = _typed("gateway_busy", retry_after=retry_after)
                self.assertEqual(kimi_k3.sponsored_retry_delay(1, body), first)
                self.assertEqual(kimi_k3.sponsored_retry_delay(2, body), second)

    def test_timeout_header_value_rounds_down_to_whole_seconds(self) -> None:
        self.assertEqual(kimi_k3.timeout_header_value(630), "630")
        self.assertEqual(kimi_k3.timeout_header_value(629.99), "629")
        self.assertEqual(kimi_k3.timeout_header_value(0.2), "1")
        self.assertEqual(kimi_k3.timeout_header_value(float("nan")), "1")
        self.assertEqual(kimi_k3.timeout_header_value(float("inf")), "1")

    def test_sponsored_environment_flags(self) -> None:
        for name in (
            "AGENT_MONITOR_SPONSORED_KIMI",
            "AGENT_MONITOR_PUBLIC_KIMI",
            "AGENT_MONITOR_GUEST_DERIVED",
        ):
            self.assertTrue(kimi_k3.is_sponsored({name: "1"}))
        self.assertFalse(kimi_k3.is_sponsored({}))
        self.assertFalse(kimi_k3.is_sponsored({"AGENT_MONITOR_SPONSORED_KIMI": "0"}))


class SponsoredRequestTests(unittest.TestCase):
    def _run(self, replies, *, timeout=630.0, allowance=None, clock=None):
        clock = clock or _Clock()
        sent: list[float] = []
        queue = list(replies)

        def request(seconds: float):
            sent.append(seconds)
            reply = queue.pop(0)
            if isinstance(reply, BaseException):
                raise reply
            if callable(reply):
                return reply(clock)
            return reply

        code, body, failure = kimi_k3.sponsored_request(
            request, timeout=timeout, allowance=allowance, sleep=clock.sleep, clock=clock
        )
        return code, body, failure, sent, clock.slept

    def test_fast_refusals_are_retried_with_the_fixed_schedule(self) -> None:
        code, body, failure, sent, slept = self._run(
            [(503, _fast_503()), (503, _fast_503()), (200, _OK)]
        )
        self.assertEqual((code, body, failure), (200, _OK, None))
        self.assertEqual(slept, [5.0, 15.0])
        # The first attempt carries the whole timeout, later ones what is left.
        self.assertEqual(sent, [630.0, 625.0, 610.0])

    def test_a_short_retry_after_never_shortens_the_schedule(self) -> None:
        busy = (429, _typed("gateway_busy", retry_after=2))
        _code, _body, failure, _sent, slept = self._run([busy, busy, busy])
        self.assertEqual(slept, [5.0, 15.0])
        self.assertIsNotNone(failure)
        assert failure is not None
        self.assertEqual((failure.status, failure.kind, failure.attempts), (429, "gateway_busy", 3))
        self.assertTrue(failure.retryable)
        self.assertFalse(failure.permanent)

    def test_a_longer_retry_after_sets_the_delay(self) -> None:
        busy = (429, _typed("gateway_busy", retry_after=20))
        _code, _body, _failure, _sent, slept = self._run([busy, busy, busy])
        self.assertEqual(slept, [20.0, 20.0])

    def test_a_provider_refusing_after_a_short_hint_is_waited_out(self) -> None:
        # Kimi's provider answered 429 with Retry-After: 1 and kept refusing for
        # several seconds after a completed call: resends 2 s apart all failed.
        clock = _Clock()
        start = clock()

        def provider(now: _Clock):
            if now() - start < 10:
                return 429, _fast_503(upstream_status=429, upstream_elapsed_ms=150, retry_after=1)
            return 200, _OK

        code, body, failure, _sent, slept = self._run([provider] * 3, clock=clock)
        self.assertEqual((code, body, failure), (200, _OK, None))
        self.assertEqual(slept, [5.0, 15.0])

    def test_a_run_survives_a_refusal_before_every_call(self) -> None:
        # Kimi's provider has refused the first request after nearly every
        # completed call (one run turn met 9 refusals among its 11 calls); with
        # six retries per run, the seventh refused call failed outright.
        allowance = kimi_k3.RetryAllowance(kimi_k3.SPONSORED_RUN_RETRIES)
        refused = (429, _fast_503(upstream_status=429, upstream_elapsed_ms=150, retry_after=1))
        for call in range(12):
            with self.subTest(call=call):
                code, body, failure, sent, _slept = self._run([refused, (200, _OK)], allowance=allowance)
                self.assertEqual((code, body, failure, len(sent)), (200, _OK, None, 2))

    def test_at_most_three_attempts(self) -> None:
        refused = (0, "connection refused")
        _code, _body, failure, sent, slept = self._run([refused] * 3)
        self.assertEqual(len(sent), 3)
        self.assertEqual(slept, [5.0, 15.0])
        assert failure is not None
        self.assertEqual((failure.status, failure.attempts, failure.retryable), (0, 3, True))

    def test_ambiguous_and_successful_replies_are_never_replayed(self) -> None:
        for reply in (
            (504, _typed("upstream_timeout", forwarded=True)),
            (504, _typed("gateway_timeout", forwarded=True)),
            (503, {"error": {"message": "Kimi request was rejected"}}),
            (502, _typed("upstream_rejected", forwarded=True, upstream_status=502,
                          upstream_elapsed_ms=50)),
            (503, _fast_503(upstream_elapsed_ms=9000)),
            (429, _typed("sponsor_quota", retry_after=60)),
            (401, _typed("unauthorized")),
            (200, {"choices": []}),
            (200, _OK),
        ):
            with self.subTest(reply=reply):
                code, _body, failure, sent, slept = self._run([reply, (200, _OK)])
                self.assertEqual(code, reply[0])
                self.assertEqual(len(sent), 1)
                self.assertEqual(slept, [])
                self.assertEqual(failure is None, 200 <= reply[0] < 300)

    def test_transport_errors_after_sending_end_the_call(self) -> None:
        for error, kind in (
            (TimeoutError("timed out"), "client_timeout"),
            (ConnectionResetError("reset"), "connection_lost"),
            (http.client.RemoteDisconnected("closed"), "connection_lost"),
            (http.client.IncompleteRead(b""), "connection_lost"),
        ):
            with self.subTest(error=type(error).__name__):
                code, body, failure, sent, slept = self._run([error, (200, _OK)])
                self.assertEqual((code, body), (0, None))
                self.assertEqual(len(sent), 1)
                self.assertEqual(slept, [])
                assert failure is not None
                self.assertEqual(failure.kind, kind)
                self.assertFalse(failure.retryable)

    def test_retry_is_skipped_when_less_than_30_seconds_would_remain(self) -> None:
        def slow_refusal(clock: _Clock):
            clock.now += 4.0
            return 503, _fast_503()

        # 60 s: 4 s + 5 s leave 51 s, then 4 s + 15 s leave 32 s, so both
        # retries run (three attempts is the most there ever are).
        _code, _body, failure, sent, slept = self._run(
            [slow_refusal, slow_refusal, slow_refusal], timeout=60.0
        )
        self.assertEqual(slept, [5.0, 15.0])
        self.assertEqual(len(sent), 3)
        # 40 s: after 4 s + 5 s, 31 s remain (retry); after 4 s more, a 15 s
        # pause would leave 12 s, so the third attempt is skipped.
        _code, _body, failure, sent, slept = self._run(
            [slow_refusal, slow_refusal, slow_refusal], timeout=40.0
        )
        self.assertEqual(slept, [5.0])
        self.assertEqual(sent, [40.0, 31.0])
        assert failure is not None
        self.assertEqual(failure.attempts, 2)
        # 30 s: even the first retry would leave less than 30 s.
        _code, _body, failure, sent, slept = self._run([slow_refusal], timeout=30.0)
        self.assertEqual((slept, len(sent)), ([], 1))

    def test_the_run_allowance_caps_retries_across_calls(self) -> None:
        allowance = kimi_k3.RetryAllowance(3)
        refused = (503, _fast_503())
        _code, _body, _failure, sent, _slept = self._run(
            [refused] * 3, allowance=allowance
        )
        self.assertEqual(len(sent), 3)  # two retries spent
        _code, _body, failure, sent, slept = self._run(
            [refused, refused], allowance=allowance
        )
        self.assertEqual(len(sent), 2)  # the third and last retry of the run
        _code, _body, failure, sent, slept = self._run([refused], allowance=allowance)
        self.assertEqual((len(sent), slept), (1, []))
        assert failure is not None
        self.assertTrue(failure.retryable)
        self.assertEqual(kimi_k3.SPONSORED_RUN_RETRIES, 24)


class FailureMessageTests(unittest.TestCase):
    def _message(self, code: int, body: object, **kwargs: object) -> str:
        return kimi_k3.failure_message(kimi_k3.call_failure(code, body, **kwargs))

    def test_messages_say_what_happened_and_what_to_do(self) -> None:
        self.assertEqual(
            self._message(503, _fast_503()),
            "Kimi's provider is temporarily overloaded and refused the request "
            "(HTTP 503). No tokens were used. Please try again in a few minutes.",
        )
        self.assertEqual(
            self._message(504, _typed("upstream_timeout", forwarded=True), elapsed=600.4),
            "Kimi took longer than 10 minutes to answer, so the call was stopped "
            "(HTTP 504). Please try again later.",
        )
        self.assertIn(
            "took longer than 10 minutes",
            self._message(504, _typed("gateway_timeout", forwarded=True), elapsed=615),
        )
        for kind in ("gateway_busy", "gateway_quota"):
            self.assertEqual(
                self._message(429, _typed(kind, retry_after=2)),
                "Kimi is busy on this site right now (HTTP 429). "
                "Please try again in a few minutes.",
            )
        self.assertEqual(
            self._message(429, _typed("sponsor_quota", retry_after=60)),
            "This run used up its Kimi call budget (HTTP 429).",
        )
        slow = self._message(503, _fast_503(upstream_elapsed_ms=9000))
        self.assertIn("(HTTP 503)", slow)
        self.assertNotIn("No tokens were used", slow)
        self.assertIn("No tokens were used", self._message(0, "refused"))
        self.assertIn("No tokens were used", self._message(502, _typed("gateway_unavailable")))
        # Advice that holds for a CGM run and a Formal/DAG action alike.
        self.assertEqual(
            self._message(401, _typed("unauthorized")),
            "The included Kimi access has expired (HTTP 401). Please try again.",
        )
        timeout = kimi_k3.failure_message(
            kimi_k3.CallFailure(status=0, kind="client_timeout", retryable=False, elapsed=630.2)
        )
        self.assertIn("took longer than 10 minutes", timeout)
        self.assertIn("630 s", timeout)
        # An untyped reply keeps the caller's own wording.
        self.assertEqual(self._message(503, {"error": {"message": "busy"}}), "")

    def test_no_tokens_is_claimed_only_for_a_refusal_before_processing(self) -> None:
        # A broker that had already sent the call on (forwarded) cannot know
        # whether Kimi processed it.
        forwarded = self._message(502, _typed("gateway_unavailable", forwarded=True))
        self.assertEqual(
            forwarded,
            "The connection to the included Kimi service failed while it was "
            "answering (HTTP 502). Please try again later.",
        )
        for code, body in (
            (502, _typed("upstream_unavailable", forwarded=True)),
            (504, _typed("upstream_timeout", forwarded=True)),
            (504, _typed("gateway_timeout", forwarded=True)),
            (503, _fast_503(upstream_elapsed_ms=9000)),
            (502, _typed("response_too_large", forwarded=True)),
        ):
            with self.subTest(body=body):
                self.assertNotIn("No tokens were used", self._message(code, body))
        # ``billed``: other calls the message speaks for may have been
        # processed (an earlier call of the same run), so it never claims it.
        for code, body in (
            (503, _fast_503()),
            (0, "refused"),
            (502, _typed("gateway_unavailable")),
            (502, _typed("upstream_unavailable")),
        ):
            with self.subTest(billed=body):
                failure = kimi_k3.call_failure(code, body)
                self.assertTrue(failure.retryable)
                message = kimi_k3.failure_message(failure, billed=True)
                self.assertNotIn("No tokens were used", message)
                self.assertIn("Please try again in a few minutes.", message)
        self.assertEqual(
            kimi_k3.failure_message(kimi_k3.call_failure(503, _fast_503()), billed=True),
            "Kimi's provider is temporarily overloaded and refused the request "
            "(HTTP 503). Please try again in a few minutes.",
        )

    def test_a_long_site_quota_wait_is_reported(self) -> None:
        self.assertEqual(
            self._message(429, _typed("gateway_quota", retry_after=3000)),
            "This site's hourly Kimi limit is reached (HTTP 429). "
            "Please try again in about 50 minutes.",
        )
        self.assertIn(
            "about 6 minutes", self._message(429, _typed("gateway_quota", retry_after=301))
        )
        for seconds in (30, 300):
            with self.subTest(retry_after=seconds):
                self.assertEqual(
                    self._message(429, _typed("gateway_quota", retry_after=seconds)),
                    "Kimi is busy on this site right now (HTTP 429). "
                    "Please try again in a few minutes.",
                )

    def test_refusal_reasons_name_this_site_or_the_provider(self) -> None:
        def reason(code: int, body: object) -> str:
            return kimi_k3.refusal_reason(kimi_k3.call_failure(code, body))

        self.assertEqual(reason(503, _fast_503()), "Kimi's provider is busy")
        self.assertEqual(reason(429, _fast_503(upstream_status=429)), "Kimi's provider is busy")
        self.assertEqual(
            reason(0, "connection refused"), "the included Kimi service could not be reached"
        )
        self.assertEqual(
            reason(502, _typed("gateway_unavailable")),
            "the included Kimi service could not be reached",
        )
        for kind in ("gateway_busy", "gateway_quota"):
            self.assertEqual(reason(429, _typed(kind, retry_after=2)), "Kimi is busy on this site")
        self.assertEqual(
            reason(502, _typed("upstream_unavailable")), "Kimi's provider could not be reached"
        )
        self.assertEqual(kimi_k3.refusal_reason(None), "Kimi's provider is busy")

    def test_permanent_failures(self) -> None:
        permanent = [
            (429, _typed("sponsor_quota", retry_after=60)),
            (429, _typed("gateway_quota", retry_after=1800)),
            (401, _typed("unauthorized")),
            (403, _typed("forbidden")),
            (400, _typed("bad_request")),
            (400, _typed("upstream_rejected", forwarded=True, upstream_status=400,
                         upstream_elapsed_ms=10)),
            (502, _typed("upstream_rejected", forwarded=True, upstream_status=302,
                         upstream_elapsed_ms=10)),
            (401, {"error": {"message": "Sponsored Kimi session expired"}}),
        ]
        for code, body in permanent:
            with self.subTest(code=code, body=body):
                self.assertTrue(kimi_k3.call_failure(code, body).permanent)
        transient = [
            (0, None),
            (503, _fast_503()),
            (503, _fast_503(upstream_elapsed_ms=9000)),
            (504, _typed("upstream_timeout", forwarded=True)),
            (502, _typed("upstream_unavailable", forwarded=True)),
            (429, {"error": {"message": "public Kimi call concurrency reached"}}),
            (503, {"error": {"message": "busy"}}),
        ]
        for code, body in transient:
            with self.subTest(code=code, body=body):
                self.assertFalse(kimi_k3.call_failure(code, body).permanent)


def _sponsored_env(**extra: str) -> dict[str, str]:
    return {
        "KIMI_API_KEY": "sponsored-loopback-token",
        "KIMI_API_BASE": _SPONSORED_BASE,
        "AGENT_MONITOR_SPONSORED_KIMI": "1",
        **extra,
    }


class ApiChatSponsoredTests(unittest.TestCase):
    def _call(self, replies, *, env=None, timeout=630, **kwargs):
        sent: list[dict] = []
        queue = list(replies)

        def fake_http(url, *, method, headers, payload, timeout):
            sent.append({"url": url, "headers": dict(headers), "timeout": timeout})
            reply = queue.pop(0)
            if isinstance(reply, BaseException):
                raise reply
            return reply

        error = None
        result = None
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("agent_monitor.settings._http_json", side_effect=fake_http),
            patch("agent_monitor.kimi_k3.time.sleep") as sleep,
        ):
            try:
                result = api_backend.api_chat(
                    "system",
                    "problem",
                    model="kimi-k3",
                    timeout=timeout,
                    env=env if env is not None else _sponsored_env(),
                    **kwargs,
                )
            except api_backend.APIBackendError as exc:
                error = exc
        return result, error, sent, [call.args[0] for call in sleep.call_args_list]

    def test_a_typed_fast_503_is_retried_and_succeeds(self) -> None:
        result, error, sent, slept = self._call(
            [(503, _fast_503()), (503, _fast_503()), (200, _OK)]
        )
        self.assertIsNone(error)
        assert result is not None
        self.assertEqual(result.text, "KIMI_OK")
        self.assertEqual(len(sent), 3)
        self.assertEqual(slept, [5.0, 15.0])
        self.assertEqual(sent[0]["headers"][kimi_k3.TIMEOUT_HEADER], "630")
        self.assertEqual(sent[0]["timeout"], 630.0)
        for call in sent[1:]:
            self.assertLessEqual(float(call["headers"][kimi_k3.TIMEOUT_HEADER]), 630)
            self.assertLessEqual(call["timeout"], 630)
        self.assertEqual(sent[0]["headers"]["Authorization"], "Bearer sponsored-loopback-token")

    def test_the_header_carries_the_timeout_actually_used(self) -> None:
        _result, _error, sent, _slept = self._call([(200, _OK)], timeout=118)
        self.assertEqual(sent[0]["headers"][kimi_k3.TIMEOUT_HEADER], "118")
        self.assertEqual(sent[0]["timeout"], 118.0)

    def test_exhausted_fast_refusals_report_the_overload(self) -> None:
        _result, error, sent, _slept = self._call([(503, _fast_503())] * 3)
        self.assertEqual(len(sent), 3)
        assert error is not None
        self.assertEqual(
            str(error),
            "Kimi's provider is temporarily overloaded and refused the request "
            "(HTTP 503). No tokens were used. Please try again in a few minutes.",
        )
        self.assertTrue(error.failure.retryable)
        self.assertEqual(error.failure.attempts, 3)
        self.assertNotIn("sponsored-loopback-token", str(error))

    def test_ambiguous_failures_are_never_replayed(self) -> None:
        for reply, fragment in (
            ((504, _typed("upstream_timeout", forwarded=True)), "took longer than"),
            ((504, _typed("gateway_timeout", forwarded=True)), "took longer than"),
            ((503, {"error": {"message": "busy"}}), "HTTP 503"),
            ((429, _typed("sponsor_quota", retry_after=60)), "call budget (HTTP 429)"),
            ((200, {"choices": []}), "empty response for kimi-k3"),
            (TimeoutError("timed out"), "took longer than"),
        ):
            with self.subTest(reply=reply):
                _result, error, sent, slept = self._call([reply, (200, _OK)])
                self.assertEqual(len(sent), 1)
                self.assertEqual(slept, [])
                assert error is not None
                self.assertIn(fragment, str(error))

    def test_the_run_allowance_bounds_transport_retries(self) -> None:
        allowance = kimi_k3.RetryAllowance(1)
        _result, error, sent, slept = self._call(
            [(503, _fast_503())] * 3, retry_allowance=allowance
        )
        self.assertEqual(len(sent), 2)
        self.assertEqual(slept, [5.0])
        assert error is not None
        self.assertTrue(error.failure.retryable)

    def test_an_empty_sponsored_completion_keeps_its_usage(self) -> None:
        empty = {
            "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 900, "completion_tokens": 8192},
        }
        _result, error, sent, _slept = self._call([(200, empty)])
        self.assertEqual(len(sent), 1)
        assert error is not None
        self.assertEqual(error.usage["input_tokens"], 900)
        self.assertEqual(error.usage["output_tokens"], 8192)
        self.assertIsNone(error.failure)

    def test_user_key_kimi_keeps_its_retries_and_bills_every_reply(self) -> None:
        empty = {
            "choices": [{"message": {"content": ""}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }
        ok = {
            "choices": [{"message": {"content": "KIMI_OK"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        user_env = {"KIMI_API_KEY": "user-key", "KIMI_API_BASE": "https://kimi.invalid/v1"}
        result, error, sent, _slept = self._call(
            [(503, {"error": {"message": "busy"}}), (200, empty), (200, ok)], env=user_env
        )
        self.assertIsNone(error)
        assert result is not None
        self.assertEqual(len(sent), 3)
        self.assertNotIn(kimi_k3.TIMEOUT_HEADER, sent[0]["headers"])
        self.assertEqual(sent[0]["timeout"], 630)
        self.assertEqual(result.usage["input_tokens"], 110)
        self.assertEqual(result.usage["output_tokens"], 55)


class DerivedSponsoredCallTests(unittest.TestCase):
    def _call_llm(self, replies, env, *, timeout=180):
        sent: list[dict] = []
        queue = list(replies)

        def fake_http(url, *, method, headers, payload, timeout):
            sent.append({
                "headers": dict(headers),
                "timeout": timeout,
                # The payload is changed in place between attempts.
                "effort": payload.get("reasoning_effort"),
            })
            reply = queue.pop(0)
            if isinstance(reply, BaseException):
                raise reply
            return reply

        booked: list[dict] = []
        with (
            patch("agent_monitor.settings._http_json", side_effect=fake_http),
            patch("agent_monitor.kimi_k3.time.sleep"),
            patch.object(
                proof_graph.usage_ledger,
                "record_usage",
                side_effect=lambda **kwargs: booked.append(kwargs),
            ),
        ):
            try:
                outcome = proof_graph._call_llm(env, "kimi-k3", "prompt", timeout=timeout)
            except ValueError as exc:
                outcome = exc
        return outcome, sent, booked

    def test_sponsored_derived_calls_wait_630_seconds_and_send_the_header(self) -> None:
        for env in (
            _sponsored_env(),
            {**_sponsored_env(), "AGENT_MONITOR_GUEST_DERIVED": "1"},
        ):
            with self.subTest(env=sorted(env)):
                outcome, sent, _booked = self._call_llm([(200, _OK)], env)
                self.assertEqual(outcome[1], "KIMI_OK")
                self.assertEqual(sent[0]["timeout"], 630.0)
                self.assertEqual(sent[0]["headers"][kimi_k3.TIMEOUT_HEADER], "630")

    def test_ambiguous_502_and_504_are_not_replayed(self) -> None:
        for reply in (
            (502, _typed("upstream_unavailable", forwarded=True)),
            (504, _typed("upstream_timeout", forwarded=True)),
            (504, _typed("gateway_timeout", forwarded=True)),
            (502, {"error": {"message": "Sponsored Kimi gateway unavailable"}}),
            (200, {"choices": []}),
            TimeoutError("timed out"),
        ):
            with self.subTest(reply=reply):
                outcome, sent, _booked = self._call_llm([reply, (200, _OK)], _sponsored_env())
                self.assertIsInstance(outcome, ValueError)
                self.assertEqual(len(sent), 1)
                self.assertNotIn("sponsored-loopback-token", str(outcome))

    def test_not_processed_refusals_are_retried(self) -> None:
        outcome, sent, booked = self._call_llm(
            [(429, _typed("gateway_busy", retry_after=2)), (503, _fast_503()), (200, _OK)],
            _sponsored_env(),
        )
        self.assertEqual(outcome[1], "KIMI_OK")
        self.assertEqual(len(sent), 3)
        self.assertEqual(len(booked), 1)

    def test_exhausted_refusals_explain_the_overload(self) -> None:
        outcome, sent, _booked = self._call_llm([(503, _fast_503())] * 3, _sponsored_env())
        self.assertEqual(len(sent), 3)
        self.assertIn("temporarily overloaded and refused the request (HTTP 503)", str(outcome))

    _CUT_AT_LIMIT = {
        "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
        "usage": {
            "prompt_tokens": 900,
            "completion_tokens": 8192,
            "completion_tokens_details": {"reasoning_tokens": 8192},
        },
    }

    def test_a_reply_that_reasoned_to_the_limit_is_asked_once_more_at_lower_effort(self) -> None:
        # The broker's credentials set KIMI_REASONING_EFFORT=high.
        env = {**_sponsored_env(KIMI_REASONING_EFFORT="high"), "AGENT_MONITOR_GUEST_DERIVED": "1"}
        with self.assertLogs("agent_monitor.proof_graph", level="WARNING") as logs:
            outcome, sent, booked = self._call_llm(
                [(200, self._CUT_AT_LIMIT), (200, _OK), (200, _OK)], env
            )
        self.assertEqual(outcome[1], "KIMI_OK")
        self.assertEqual([call["effort"] for call in sent], ["high", "low"])
        # A new call with its own full budget, not the rest of the first one.
        self.assertEqual([call["timeout"] for call in sent], [630.0, 630.0])
        self.assertEqual(
            [call["headers"][kimi_k3.TIMEOUT_HEADER] for call in sent], ["630", "630"]
        )
        # Both replies were billed, so both are booked.
        self.assertEqual(len(booked), 2)
        self.assertEqual(booked[0]["usage"]["output_tokens"], 8192)
        self.assertTrue(all(entry["billing"] == "sponsored" for entry in booked))
        self.assertIn("asking once more at reasoning_effort=low", "\n".join(logs.output))
        self.assertIn("finish_reason=length", "\n".join(logs.output))

    def test_the_default_max_effort_steps_down_to_high(self) -> None:
        outcome, sent, _booked = self._call_llm(
            [(200, self._CUT_AT_LIMIT), (200, _OK)], _sponsored_env()
        )
        self.assertEqual(outcome[1], "KIMI_OK")
        self.assertEqual([call["effort"] for call in sent], ["max", "high"])

    def test_a_second_reply_that_reasoned_to_the_limit_ends_the_call(self) -> None:
        env = _sponsored_env(KIMI_REASONING_EFFORT="high")
        outcome, sent, booked = self._call_llm(
            [(200, self._CUT_AT_LIMIT), (200, self._CUT_AT_LIMIT), (200, _OK)], env
        )
        self.assertIsInstance(outcome, ValueError)
        self.assertIn("empty response for kimi-k3", str(outcome))
        self.assertIn("finish_reason=length", str(outcome))
        self.assertEqual([call["effort"] for call in sent], ["high", "low"])
        self.assertEqual(len(booked), 2)

    def test_other_empty_replies_are_never_replayed(self) -> None:
        cases = {
            "no choices": ({"choices": []}, "high"),
            "stopped without text": (
                {"choices": [{"message": {"content": ""}, "finish_reason": "stop"}]}, "high"
            ),
            "no finish reason": ({"choices": [{"message": {"content": " "}}]}, "high"),
            "filtered": (
                {"choices": [{"message": {"content": ""}, "finish_reason": "content_filter"}]},
                "high",
            ),
            # Already at the lowest effort: the same request would end the same way.
            "at low effort": (self._CUT_AT_LIMIT, "low"),
        }
        for name, (reply, effort) in cases.items():
            with self.subTest(name):
                outcome, sent, booked = self._call_llm(
                    [(200, reply), (200, _OK)], _sponsored_env(KIMI_REASONING_EFFORT=effort)
                )
                self.assertIsInstance(outcome, ValueError)
                self.assertIn("empty response for kimi-k3", str(outcome))
                self.assertEqual(len(sent), 1)
                self.assertEqual(len(booked), 1)

    def test_a_reply_cut_at_the_limit_after_some_text_is_used_as_it_is(self) -> None:
        partial = {"choices": [{"message": {"content": "theorem t : True := by"}, "finish_reason": "length"}]}
        outcome, sent, _booked = self._call_llm(
            [(200, partial), (200, _OK)], _sponsored_env(KIMI_REASONING_EFFORT="high")
        )
        self.assertEqual(outcome[1], "theorem t : True := by")
        self.assertEqual(len(sent), 1)

    def test_the_second_request_keeps_the_sponsored_refusal_rules(self) -> None:
        env = _sponsored_env(KIMI_REASONING_EFFORT="high")
        outcome, sent, booked = self._call_llm(
            [(200, self._CUT_AT_LIMIT), (503, _fast_503()), (200, _OK)], env
        )
        self.assertEqual(outcome[1], "KIMI_OK")
        self.assertEqual([call["effort"] for call in sent], ["high", "low", "low"])
        self.assertEqual(len(booked), 2)
        for reply in (
            (504, _typed("upstream_timeout", forwarded=True)),
            TimeoutError("timed out"),
        ):
            with self.subTest(reply=reply):
                outcome, sent, _booked = self._call_llm(
                    [(200, self._CUT_AT_LIMIT), reply, (200, _OK)], env
                )
                self.assertIsInstance(outcome, ValueError)
                self.assertIn("took longer than", str(outcome))
                self.assertEqual(len(sent), 2)

    def test_a_refused_resend_never_claims_that_no_tokens_were_used(self) -> None:
        env = _sponsored_env(KIMI_REASONING_EFFORT="high")
        # A refusal of a first request used no tokens and says so.
        outcome, _sent, _booked = self._call_llm([(503, _fast_503())] * 3, env)
        self.assertIn("No tokens were used.", str(outcome))
        # After a billed reply that reasoned to the limit it must not.
        for refusal in (
            (503, _fast_503()),
            (503, _typed("gateway_unavailable", retry_after=2)),
            (0, "connection refused"),  # the broker could not be reached
        ):
            with self.subTest(refusal=refusal):
                outcome, sent, booked = self._call_llm(
                    [(200, self._CUT_AT_LIMIT)] + [refusal] * 3, env
                )
                self.assertIsInstance(outcome, ValueError)
                self.assertNotIn("No tokens were used", str(outcome))
                self.assertEqual(len(booked), 1)
        outcome, sent, _booked = self._call_llm(
            [(200, self._CUT_AT_LIMIT)] + [(503, _fast_503())] * 3, env
        )
        self.assertIn("temporarily overloaded and refused the request (HTTP 503)", str(outcome))
        self.assertEqual(len(sent), 4)

    def test_a_call_is_asked_again_at_most_once(self) -> None:
        # From max, high could still step down to low: one resend all the same.
        outcome, sent, booked = self._call_llm(
            [(200, self._CUT_AT_LIMIT), (200, self._CUT_AT_LIMIT), (200, _OK)],
            _sponsored_env(),
        )
        self.assertIsInstance(outcome, ValueError)
        self.assertIn("empty response for kimi-k3", str(outcome))
        self.assertEqual([call["effort"] for call in sent], ["max", "high"])
        self.assertEqual(len(booked), 2)

    def test_every_billed_user_key_reply_is_booked(self) -> None:
        empty = {
            "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }
        ok = {"choices": [{"message": {"content": "KIMI_OK"}}], "usage": {"prompt_tokens": 7}}
        user_env = {"KIMI_API_KEY": "user-key", "KIMI_API_BASE": "https://kimi.invalid/v1"}
        outcome, sent, booked = self._call_llm([(200, empty), (200, ok)], user_env, timeout=180)
        self.assertEqual(outcome[1], "KIMI_OK")
        self.assertEqual(sent[0]["timeout"], 180)
        self.assertNotIn(kimi_k3.TIMEOUT_HEADER, sent[0]["headers"])
        self.assertEqual(
            [entry["usage"]["input_tokens"] for entry in booked], [100.0, 7.0]
        )

    def test_a_console_holding_an_older_kimi_k3_keeps_the_previous_path(self) -> None:
        # A long-running console imports settings (and so kimi_k3) at start
        # and proof_graph lazily: after an update and before its restart it
        # may combine this proof_graph with a kimi_k3 lacking the new API.
        new_api = (
            "is_sponsored", "sponsored_request", "failure_message", "call_failure",
            "timeout_header_value", "TIMEOUT_HEADER", "SPONSORED_TIMEOUT_SECONDS",
        )
        saved = {name: getattr(kimi_k3, name) for name in new_api}
        for name in new_api:
            delattr(kimi_k3, name)
        try:
            outcome, sent, booked = self._call_llm([(200, _OK)], _sponsored_env(), timeout=180)
        finally:
            for name, value in saved.items():
                setattr(kimi_k3, name, value)
        self.assertEqual(outcome[1], "KIMI_OK")
        self.assertEqual(sent[0]["timeout"], 180)
        self.assertNotIn("X-Ansatze-Timeout", sent[0]["headers"])
        self.assertEqual(len(booked), 1)

    def test_lean_chat_uses_the_sponsored_rules(self) -> None:
        sent: list[dict] = []

        def fake_http(url, *, method, headers, payload, timeout):
            sent.append({"headers": dict(headers), "timeout": timeout})
            return 504, _typed("upstream_timeout", forwarded=True)

        env = {**_sponsored_env(), "AGENT_MONITOR_GUEST_DERIVED": "1"}
        with (
            patch("agent_monitor.settings._http_json", side_effect=fake_http),
            patch("agent_monitor.kimi_k3.time.sleep"),
        ):
            with self.assertRaisesRegex(ValueError, r"took longer than .* \(HTTP 504\)"):
                lean_verify._chat(env, "kimi-k3", "prove it")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["timeout"], 630.0)
        self.assertEqual(sent[0]["headers"][kimi_k3.TIMEOUT_HEADER], "630")

    def test_a_guest_lean_call_that_reasoned_to_the_limit_gets_its_lean(self) -> None:
        # What a guest's Verify met: high effort used the whole 8,192 tokens.
        efforts: list[str] = []
        replies = [(200, self._CUT_AT_LIMIT), (200, {"choices": [{"message": {"content": "LEAN"}}]})]

        def fake_http(url, *, method, headers, payload, timeout):
            efforts.append(payload.get("reasoning_effort"))
            return replies.pop(0)

        env = {
            **_sponsored_env(KIMI_REASONING_EFFORT="high"),
            "AGENT_MONITOR_GUEST_DERIVED": "1",
            "AGENT_MONITOR_LLM_MAX_OUTPUT": "8192",
        }
        with (
            patch("agent_monitor.settings._http_json", side_effect=fake_http),
            patch("agent_monitor.kimi_k3.time.sleep"),
            patch.object(proof_graph.usage_ledger, "record_usage"),
            self.assertLogs("agent_monitor.proof_graph", level="WARNING"),
        ):
            _chosen, content = lean_verify._chat(env, "kimi-k3", "formalize it")
        self.assertEqual(content, "LEAN")
        self.assertEqual(efforts, ["high", "low"])

    def test_a_lean_session_stopped_meanwhile_is_not_asked_again(self) -> None:
        # The run is deleted while the first request is out: the stop check
        # before the second request ends the session, as before any call.
        efforts: list[str] = []
        replies = [(200, self._CUT_AT_LIMIT), (200, _OK)]

        def fake_http(url, *, method, headers, payload, timeout):
            efforts.append(payload.get("reasoning_effort"))
            return replies.pop(0)

        checks: list[bool] = []

        def run_gone() -> bool:
            checks.append(bool(efforts))
            return bool(efforts)

        env = {**_sponsored_env(KIMI_REASONING_EFFORT="high"), "AGENT_MONITOR_GUEST_DERIVED": "1"}
        token = lean_verify.stop_when(run_gone)
        try:
            with (
                patch("agent_monitor.settings._http_json", side_effect=fake_http),
                patch("agent_monitor.kimi_k3.time.sleep"),
                patch.object(proof_graph.usage_ledger, "record_usage") as booked,
            ):
                with self.assertRaises(lean_verify.SessionStopped):
                    lean_verify._chat(env, "kimi-k3", "formalize it")
        finally:
            lean_verify.stop_reset(token)
        self.assertEqual(efforts, ["high"])
        self.assertEqual(checks, [False, True])
        self.assertEqual(booked.call_count, 1)


class SponsoredClientHeaderTests(unittest.TestCase):
    def test_extra_headers_never_replace_the_fixed_ones(self) -> None:
        from agent_monitor import sponsored_kimi_client as client

        captured: dict = {}

        class Response:
            headers: dict = {}

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def read(self, _limit):
                return b"{}"

        def fake_open(request, timeout):
            captured.update(dict(request.header_items()))
            return Response()

        with (
            patch.dict(os.environ, {"AGENT_MONITOR_SPONSORED_KIMI_URL": "http://127.0.0.1:4610"}),
            patch.object(client._opener, "open", side_effect=fake_open),
        ):
            client._request(
                "/internal/v1/chat/completions",
                method="POST",
                payload={},
                bearer="token-value",
                extra_headers={"X-ansatze-timeout": "90", "Authorization": "Bearer other"},
            )
        self.assertEqual(captured["X-ansatze-timeout"], "90")
        self.assertEqual(captured["Authorization"], "Bearer token-value")


if __name__ == "__main__":
    unittest.main()
