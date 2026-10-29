"""Ansatz CGM "Max (API)": a Claude API advisor and an OpenAI API worker, both
at maximum effort, billed to API keys (the console side: plan, start,
records, follow-ups, policy, tuning and settings)."""
from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from agent_monitor import account_policy, auto_pipeline, cgm_max, jobs, run_tuning, usage_ledger

_HARNESS = {
    "id": "math_harness",
    "label": "Ansatz CGM harness",
    "available": True,
    "auth_modes": ["codex_subscription", "claude_subscription", "api_key"],
}
_ADMIN = {"id": 17, "email": "owner@example.com", "is_admin": True}
_FULL = {"id": 18, "email": "researcher@example.edu", "full_access": True}
_RESTRICTED = {"id": 19, "email": "student@example.edu"}
_GUEST = {"id": 20, "email": "guest+x@public.local", "guest": True}
_KEYS = {"ANTHROPIC_API_KEY": "sk-ant-test", "OPENAI_API_KEY": "sk-openai-test"}

_API_RECORD = {
    "run_id": "math_harness_max_api_1",
    "engine": "math_harness",
    "owner_id": 17,
    "auth_route": "api_key",
    "model": "max-api",
    "requested_model": "max-api",
    "reasoning_effort": "max",
    "speed_mode": "standard",
    "live": {"auth_mode": "api_key", "model": "max-api"},
    "route_plan": {
        "mode": "api",
        "advisor": {"route": "api_key", "provider": "anthropic", "model": "claude-fable-5-1"},
        "worker": {"route": "api_key", "provider": "openai", "model": "gpt-6-astra"},
    },
}


def _key_sources(server=None, own=None):
    """plan_for_api reads the administrator's resolved env (server keys) and a
    member's own stored keys."""
    return (
        patch(
            "agent_monitor.settings.resolved_user_env",
            return_value=dict(_KEYS if server is None else server),
        ),
        patch(
            "agent_monitor.auth.get_user_env",
            return_value=dict(_KEYS if own is None else own),
        ),
    )


class _FakeThread:
    captured: dict = {}

    def __init__(self, *, kwargs=None, **_unused):
        type(self).captured = dict(kwargs or {})

    def start(self) -> None:
        pass

    def is_alive(self) -> bool:  # start_job launches derived work only for real threads
        return False


class MaxApiPlanTests(unittest.TestCase):
    def test_the_administrator_runs_the_most_capable_pair_on_its_keys(self) -> None:
        server, own = _key_sources()
        with server, own:
            plan = cgm_max.plan_for_api(_ADMIN)
        self.assertTrue(plan["available"], plan["reason"])
        self.assertEqual(plan["mode"], "api")
        self.assertEqual(
            plan["advisor"],
            {
                "route": "api_key",
                "provider": "anthropic",
                "model": "claude-fable-5-1",
                "effort": "max",
                "roles": ["strategist", "verifier", "curator"],
            },
        )
        self.assertEqual(
            (plan["worker"]["provider"], plan["worker"]["model"], plan["worker"]["effort"]),
            ("openai", "gpt-6-astra", "max"),
        )

    def test_a_full_access_member_pays_with_its_own_keys_only(self) -> None:
        server, own = _key_sources()
        with server, own:
            self.assertTrue(cgm_max.plan_for_api(_FULL)["available"])
        # Server keys (as public mode lends them) never stand in for its own.
        server, own = _key_sources(own={"ANTHROPIC_API_KEY": "sk-ant-own"})
        with server, own:
            plan = cgm_max.plan_for_api(_FULL)
        self.assertFalse(plan["available"])
        self.assertIn("add your OpenAI key in Settings", plan["reason"])

    def test_one_key_is_never_enough(self) -> None:
        for missing, named in (("ANTHROPIC_API_KEY", "Anthropic"), ("OPENAI_API_KEY", "OpenAI")):
            keys = {name: value for name, value in _KEYS.items() if name != missing}
            server, own = _key_sources(server=keys)
            with self.subTest(missing), server, own:
                plan = cgm_max.plan_for_api(_ADMIN)
                self.assertFalse(plan["available"])
                self.assertIn(f"add your {named} key in Settings", plan["reason"])
        server, own = _key_sources(server={})
        with server, own:
            self.assertIn("add your Anthropic and OpenAI keys in Settings", cgm_max.plan_for_api(_ADMIN)["reason"])

    def test_restricted_members_guests_and_ownerless_runs_are_refused(self) -> None:
        server, own = _key_sources()
        with server, own:
            for user, reason in (
                (_RESTRICTED, "limited to the site administrator and full-access accounts"),
                (_GUEST, "guests can't start it"),
                (None, "needs a signed-in account"),
            ):
                with self.subTest(user=user):
                    plan = cgm_max.plan_for_api(user)
                    self.assertFalse(plan["available"])
                    self.assertIn(reason, plan["reason"])

    def test_the_operator_may_pin_another_allowlisted_model_per_side(self) -> None:
        settings = {
            cgm_max.API_ADVISOR_MODEL_SETTING: "claude-opus-5-5",
            cgm_max.API_WORKER_MODEL_SETTING: "gpt-6.1-sol",
        }
        with patch.dict("os.environ", settings):
            self.assertEqual(cgm_max.api_models(), ("claude-opus-5-5", "gpt-6.1-sol"))
        unknown = {
            cgm_max.API_ADVISOR_MODEL_SETTING: "claude-3-opus",
            cgm_max.API_WORKER_MODEL_SETTING: "kimi-k3",
        }
        with patch.dict("os.environ", unknown):
            self.assertEqual(cgm_max.api_models(), ("claude-fable-5-1", "gpt-6-astra"))

    def test_a_stored_plan_is_read_back_only_within_the_allowlists(self) -> None:
        self.assertEqual(
            (lambda p: (p["advisor"]["model"], p["worker"]["model"]))(
                cgm_max.record_plan_api(_API_RECORD)
            ),
            ("claude-fable-5-1", "gpt-6-astra"),
        )
        tampered = {
            "route_plan": {
                "mode": "api",
                "advisor": {"model": "claude-opus-5-5"},
                "worker": {"model": "gpt-4o; rm -rf /"},
            }
        }
        plan = cgm_max.record_plan_api(tampered)
        self.assertEqual((plan["advisor"]["model"], plan["worker"]["model"]), ("claude-opus-5-5", "gpt-6-astra"))
        self.assertNotIn("available", plan)

    def test_the_child_env_names_both_models_and_never_a_subscription(self) -> None:
        env: dict[str, str] = {}
        cgm_max.apply_models_api(env, cgm_max.record_plan_api(_API_RECORD))
        self.assertEqual(
            env,
            {
                "AGENT_MONITOR_CGM_ROUTE_MODE": "max_api",
                "AGENT_MONITOR_SELECTED_MODEL": "max-api",
                "AGENT_MONITOR_CGM_ADVISOR_MODEL": "claude-fable-5-1",
                "AGENT_MONITOR_CGM_WORKER_MODEL": "gpt-6-astra",
                "AGENT_MONITOR_CGM_API_TIMEOUT": "1800",
                "AGENT_MONITOR_CGM_MAX_SECONDS": "10800",
                "AGENT_MONITOR_API_MAX_TOKENS": "64000",
            },
        )
        self.assertEqual(cgm_max.route_mode(env), "max_api")
        self.assertEqual(cgm_max.route_mode({"AGENT_MONITOR_CGM_ROUTE_MODE": "max"}), "max")
        self.assertIsNone(cgm_max.route_mode({"AGENT_MONITOR_CGM_ROUTE_MODE": "maximum"}))
        # A request's output ceiling never caps the maximum-effort budget.
        capped = {"AGENT_MONITOR_MAX_OUTPUT_TOKENS": "4096"}
        cgm_max.apply_models_api(capped, cgm_max.record_plan_api(_API_RECORD))
        self.assertNotIn("AGENT_MONITOR_MAX_OUTPUT_TOKENS", capped)

    def test_records_tell_the_two_maxes_apart(self) -> None:
        self.assertTrue(cgm_max.is_max_api_record(_API_RECORD))
        self.assertTrue(cgm_max.is_max_api_record({"model": "max-api"}))
        self.assertTrue(cgm_max.is_max_api_record({"route_plan": {"mode": "api"}}))
        subscription = {"auth_route": "dual_subscription", "model": "max"}
        self.assertFalse(cgm_max.is_max_api_record(subscription))
        self.assertFalse(cgm_max.is_max_record(_API_RECORD))
        self.assertFalse(cgm_max.is_max_api_record({"auth_route": "api_key", "model": "gpt-6-astra"}))

    def test_derived_work_runs_on_the_worker_api_model_at_high_effort(self) -> None:
        view = cgm_max.formal_view(_API_RECORD)
        self.assertEqual(
            (view["auth_route"], view["model"], view["requested_model"], view["reasoning_effort"]),
            ("api_key", "gpt-6-astra", "gpt-6-astra", "high"),
        )
        self.assertEqual(view["live"]["auth_mode"], "api_key")
        self.assertEqual(cgm_max.formal_model(_API_RECORD, "max-api"), "gpt-6-astra")
        # The derived route accepts that profile (Lean and DAG work start).
        self.assertEqual(
            run_tuning.normalize(view["model"], view["auth_route"], reasoning_effort="high")["reasoning_effort"],
            "high",
        )


class MaxApiJobTests(unittest.TestCase):
    def _start(self, tmp: Path, *, user=_ADMIN, engine="math_harness", model="max-api",
               projected=None, server=None, own=None, **kwargs):
        written: list[dict] = []
        workspace = tmp / "workspace"
        workspace.mkdir(exist_ok=True)
        projected = {"HOME": "/srv/home", **_KEYS} if projected is None else projected
        with ExitStack() as stack:
            for item in (
                patch("agent_monitor.engines_registry.all_engine_ids", return_value={"kimi", "math_harness", "plain"}),
                patch("agent_monitor.engines_registry.list_engines", return_value=[dict(_HARNESS)]),
                patch.object(jobs, "workspace_dir", return_value=workspace),
                patch.object(jobs, "_write_run", side_effect=lambda run: written.append(dict(run))),
                patch("agent_monitor.jobs.threading.Thread", _FakeThread),
                *_key_sources(server=server, own=own),
            ):
                stack.enter_context(item)
            extra_env = stack.enter_context(
                patch.object(jobs, "_user_extra_env", return_value=dict(projected))
            )
            pipeline = stack.enter_context(patch("agent_monitor.auto_pipeline.start"))
            stack.enter_context(patch.dict("os.environ", {"AGENT_MONITOR_DISABLE_AUTO_PIPELINE": ""}))
            job = jobs.start_job(
                engine=engine,
                problem_text="Prove that there are infinitely many primes.",
                model=model,
                user=user,
                **kwargs,
            )
        return job, written, _FakeThread.captured, pipeline, extra_env

    def test_max_api_starts_on_both_keys_at_max_effort(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            job, written, captured, pipeline, extra_env = self._start(
                Path(raw), engine="kimi", reasoning_effort="low", speed_mode="fast"
            )
        # The request's engine, effort and speed never weaken Max (API).
        self.assertEqual(job["engine"], "math_harness")
        record = written[0]
        self.assertEqual((record["model"], record["requested_model"]), ("max-api", "max-api"))
        self.assertEqual(record["auth_route"], "api_key")
        self.assertEqual(record["live"]["auth_mode"], "api_key")
        self.assertEqual(record["credential_source"], "user")
        self.assertEqual((record["reasoning_effort"], record["speed_mode"]), ("max", "standard"))
        self.assertEqual(record["route_plan"]["mode"], "api")
        self.assertEqual(record["route_plan"]["advisor"]["model"], "claude-fable-5-1")
        self.assertEqual(record["route_plan"]["worker"]["model"], "gpt-6-astra")
        self.assertEqual(usage_ledger.billing_class(record), "api")
        # Both providers' keys are projected for the run: by the two models.
        self.assertEqual(extra_env.call_args.args[1:], ("claude-fable-5-1", "gpt-6-astra"))
        env = captured["extra_env"]
        self.assertEqual(env["AGENT_MONITOR_CGM_ROUTE_MODE"], "max_api")
        self.assertEqual(env["AGENT_MONITOR_SELECTED_MODEL"], "max-api")
        self.assertEqual(env["AGENT_MONITOR_CGM_ADVISOR_MODEL"], "claude-fable-5-1")
        self.assertEqual(env["AGENT_MONITOR_CGM_WORKER_MODEL"], "gpt-6-astra")
        self.assertEqual(env["AGENT_MONITOR_CGM_API_TIMEOUT"], "1800")
        self.assertEqual(env["AGENT_MONITOR_CGM_MAX_SECONDS"], "10800")
        self.assertEqual(env["AGENT_MONITOR_API_MAX_TOKENS"], "64000")
        self.assertEqual(env["AGENT_MONITOR_RUN_REASONING_EFFORT"], "max")
        self.assertEqual(env["AGENT_MONITOR_AUTH_MODE"], "api_key")
        self.assertEqual((env["ANTHROPIC_API_KEY"], env["OPENAI_API_KEY"]), ("sk-ant-test", "sk-openai-test"))
        for name in (
            "AGENT_MONITOR_CODEX_SUBSCRIPTION", "AGENT_MONITOR_CLAUDE_SUBSCRIPTION",
            "CODEX_HOME", "CLAUDE_CONFIG_DIR", "AGENT_MONITOR_CLAUDE_EFFORT",
        ):
            self.assertNotIn(name, env)
        # The OpenAI worker gets reasoning effort max from the run profile.
        self.assertEqual(
            run_tuning.openai_payload_options(env, "gpt-6-astra").get("reasoning"), {"effort": "max"}
        )
        # The outer watchdog leaves room for the 3-hour pipeline, and the
        # model projection keeps the mode.
        self.assertGreaterEqual(jobs._cli_timeout_seconds("math_harness", env), 10_800 + 300)
        projected = dict(env)
        jobs._apply_selected_model_env(projected, engine="math_harness", model="max-api")
        self.assertEqual(projected["AGENT_MONITOR_SELECTED_MODEL"], "max-api")
        # Lean and graph work after the run use the worker model.
        self.assertEqual(pipeline.call_args.kwargs["model"], "gpt-6-astra")

    def test_a_full_access_member_runs_it_on_its_own_keys(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            _job, written, _captured, _pipeline, extra_env = self._start(Path(raw), user=_FULL)
        self.assertEqual(written[0]["route_plan"]["mode"], "api")
        self.assertIs(extra_env.call_args.args[0], _FULL)

    def test_operator_timeouts_never_cut_a_max_api_run_short(self) -> None:
        from agent_monitor.subprocess_env import child_process_env

        with tempfile.TemporaryDirectory() as raw:
            _job, _written, captured, *_ = self._start(Path(raw))
        extra = captured["extra_env"]
        self.assertIsNone(jobs._cgm_operator_timeout_seconds("math_harness", extra))
        for operator in ({"AGENT_MONITOR_CLI_TIMEOUT": "3600"}, {"AGENT_MONITOR_MATH_HARNESS_TIMEOUT": "3600"}):
            with patch.dict("os.environ", operator):
                watchdog = jobs._cli_timeout_seconds("math_harness", child_process_env(extra=extra))
            self.assertGreaterEqual(watchdog, cgm_max.RUN_SECONDS + 300, operator)

    def test_max_api_is_refused_without_both_keys_or_for_other_engines_users_and_projects(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            tmp = Path(raw)
            # The plan sees both keys, but only one reaches the run.
            with self.assertRaisesRegex(ValueError, "both an Anthropic and an OpenAI API key"):
                self._start(tmp, projected={"HOME": "/srv/home", "ANTHROPIC_API_KEY": "sk-ant-test"})
            with self.assertRaisesRegex(ValueError, "add your OpenAI key in Settings"):
                self._start(tmp, server={"ANTHROPIC_API_KEY": "sk-ant-test"})
            with self.assertRaisesRegex(ValueError, "only for Ansatz CGM"):
                self._start(tmp, engine="plain")
            with self.assertRaisesRegex(ValueError, "limited to the site administrator and full-access"):
                self._start(tmp, user=_RESTRICTED)
            with self.assertRaisesRegex(ValueError, "guests can't start it"):
                self._start(tmp, user=_GUEST)
            with self.assertRaises(account_policy.ProjectPolicyError):
                self._start(tmp, tool_free=True)

    def test_a_saved_default_is_never_max_api(self) -> None:
        with patch("agent_monitor.settings.resolved_user_env", return_value={"AGENT_MONITOR_MODEL": "max-api"}):
            self.assertIsNone(jobs._saved_default_model(_ADMIN, engine="math_harness", auth_route="api_key"))


class MaxApiContinuationTests(unittest.TestCase):
    def _configure(self, env: dict[str, str], user=_ADMIN):
        with patch("agent_monitor.engines_registry.list_engines", return_value=[dict(_HARNESS)]):
            return jobs._continuation_auth_environment(
                run_data=dict(_API_RECORD), engine="math_harness", model="max-api", user=user, env=env
            )

    def test_a_follow_up_stays_max_api_on_both_keys(self) -> None:
        self.assertEqual(jobs._continuation_model(_API_RECORD, "gpt-6.1-sol"), "max-api")
        self.assertTrue(jobs._recorded_route_runs(_ADMIN, "math_harness", _API_RECORD, "max-api"))
        plain_api = {"auth_route": "api_key", "model": "gpt-6-astra"}
        self.assertFalse(jobs._recorded_route_runs(_ADMIN, "math_harness", plain_api, "max-api"))
        subscription = {"auth_route": "dual_subscription", "model": "max"}
        self.assertFalse(jobs._recorded_route_runs(_ADMIN, "math_harness", subscription, "max-api"))
        configured, route = self._configure({"HOME": "/srv/home", **_KEYS})
        self.assertEqual(route, "api_key")
        self.assertEqual(configured["AGENT_MONITOR_CGM_ROUTE_MODE"], "max_api")
        self.assertEqual(configured["AGENT_MONITOR_CGM_ADVISOR_MODEL"], "claude-fable-5-1")
        self.assertEqual(configured["AGENT_MONITOR_CGM_WORKER_MODEL"], "gpt-6-astra")
        self.assertEqual(configured["AGENT_MONITOR_RUN_REASONING_EFFORT"], "max")
        self.assertEqual(configured["OPENAI_API_KEY"], "sk-openai-test")
        self.assertNotIn("AGENT_MONITOR_CLAUDE_SUBSCRIPTION", configured)

    def test_a_follow_up_without_both_keys_stops(self) -> None:
        with self.assertRaisesRegex(ValueError, "needs both an Anthropic and an OpenAI API key"):
            self._configure({"HOME": "/srv/home", "OPENAI_API_KEY": "sk-openai-test"})

    def test_a_follow_up_projects_both_providers(self) -> None:
        self.assertEqual(
            jobs._run_provider_models(_API_RECORD, "max-api", None),
            ("claude-fable-5-1", "gpt-6-astra"),
        )
        self.assertEqual(
            jobs._run_provider_models({"model": "gpt-6-astra"}, "gpt-6-astra", "gpt-6.1-sol"),
            ("gpt-6-astra", "gpt-6.1-sol"),
        )


class MaxApiPolicyTests(unittest.TestCase):
    def test_member_checks_name_the_plan_reason(self) -> None:
        server, own = _key_sources()
        with server, own:
            self.assertIn(
                "full-access accounts",
                account_policy.member_model_error(_RESTRICTED, "math_harness", "max-api"),
            )
            self.assertIsNone(account_policy.member_model_error(_FULL, "math_harness", "max-api"))

    def test_a_restricted_member_cannot_continue_a_max_api_run(self) -> None:
        self.assertIn(
            "Max (API)",
            account_policy.member_continuation_error(_RESTRICTED, "api_key", record=_API_RECORD),
        )
        self.assertIsNone(
            account_policy.member_continuation_error(
                _RESTRICTED, "api_key", record={"auth_route": "api_key", "model": "gpt-6-astra"}
            )
        )
        self.assertIsNone(account_policy.member_continuation_error(_FULL, "api_key", record=_API_RECORD))

    def test_projects_keep_max_api_off(self) -> None:
        self.assertEqual(
            account_policy.project_run_error("math_harness", model="max-api", auth_route="api_key"),
            account_policy.PROJECT_TOOL_FREE_ERROR,
        )


class MaxApiTuningAndPipelineTests(unittest.TestCase):
    def test_max_api_runs_only_at_max_effort_on_the_api_route(self) -> None:
        caps = run_tuning.capabilities("max-api", "api_key", "math_harness")
        self.assertEqual([item["id"] for item in caps["reasoning"]], ["max"])
        self.assertEqual([item["id"] for item in caps["speed"]], ["standard"])
        with self.assertRaisesRegex(ValueError, "does not support reasoning effort"):
            run_tuning.normalize("max-api", "api_key", reasoning_effort="low")
        with self.assertRaisesRegex(ValueError, "does not support reasoning effort"):
            run_tuning.normalize("max-api", "claude_subscription", reasoning_effort="max")
        env: dict[str, str] = {}
        run_tuning.from_record(env, _API_RECORD)
        self.assertEqual(env["AGENT_MONITOR_RUN_REASONING_EFFORT"], "max")
        self.assertNotIn("AGENT_MONITOR_CLAUDE_EFFORT", env)

    def test_derived_views_wait_for_a_max_api_run_past_its_own_watchdog(self) -> None:
        with patch("agent_monitor.jobs._load_run_record", return_value=dict(_API_RECORD)):
            wait = auto_pipeline._max_run_wait_seconds(_API_RECORD["run_id"])
        self.assertEqual(wait, auto_pipeline.max_api_run_wait_seconds())
        with patch("agent_monitor.jobs._load_run_record", return_value={"auth_route": "dual_subscription", "model": "max"}):
            self.assertEqual(auto_pipeline._max_run_wait_seconds("max"), auto_pipeline.max_run_wait_seconds())
        with patch("agent_monitor.jobs._load_run_record", return_value={"auth_route": "api_key", "model": "gpt-6-astra"}):
            self.assertIsNone(auto_pipeline._max_run_wait_seconds("other"))
        with patch("agent_monitor.jobs._load_run_record", side_effect=FileNotFoundError):
            self.assertIsNone(auto_pipeline._max_run_wait_seconds("gone"))
        # At least the watchdog of a real Max (API) start, plus the record write
        # (OPERATIONS.md), with the written-proof round on or off.
        for writeup in ("1", "0"):
            with self.subTest(writeup=writeup), tempfile.TemporaryDirectory() as raw, \
                    patch.dict("os.environ", {"AGENT_MONITOR_CGM_WRITEUP": writeup}):
                _job, _written, captured, *_ = MaxApiJobTests()._start(Path(raw))
                watchdog = jobs._cli_timeout_seconds("math_harness", captured["extra_env"])
                self.assertGreaterEqual(auto_pipeline.max_api_run_wait_seconds(), watchdog + 300)
                # The pipeline waits that long for such a run.
                waited: list[float] = []

                def wait_for_source(_run_id, _workspace, *, timeout):
                    waited.append(timeout)
                    return "", {"status": "failed"}

                with (
                    patch("agent_monitor.jobs._load_run_record", return_value=dict(_API_RECORD)),
                    patch.object(auto_pipeline, "_run_record", return_value=dict(_API_RECORD)),
                    patch.object(auto_pipeline, "_wait_for_source", side_effect=wait_for_source),
                ):
                    try:
                        auto_pipeline._run(_API_RECORD["run_id"], Path(raw), 17, "gpt-6-astra", "math_harness")
                    except Exception:  # noqa: BLE001 - only the wait matters here
                        pass
                self.assertEqual(waited, [auto_pipeline.max_api_run_wait_seconds()])


class MaxApiEnvironmentTests(unittest.TestCase):
    def test_runs_never_inherit_anthropic_custom_headers(self) -> None:
        # The Anthropic SDK adds them to every request, where they could
        # replace the run's own key.
        from agent_monitor.subprocess_env import child_process_env

        source = {"PATH": "/usr/bin", "ANTHROPIC_CUSTOM_HEADERS": "X-Api-Key: someone-else"}
        self.assertNotIn("ANTHROPIC_CUSTOM_HEADERS", child_process_env(source))
        self.assertEqual(child_process_env(source)["PATH"], "/usr/bin")


if __name__ == "__main__":
    unittest.main()
