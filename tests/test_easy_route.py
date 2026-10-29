"""Easy requests on Plain: the judge, the start, each follow-up, and the key's isolation."""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from agent_monitor import easy_route, jobs, subprocess_env

KEY = "sk-or-v1-" + "0" * 64  # a placeholder, never a real key


def _decision(choice: str, easy: float | None = None, confidence: float = 1.0) -> dict:
    """The Decisions API's answer to the judge's "difficulty" question."""
    easy = (1.0 if choice == "easy" else 0.0) if easy is None else easy
    return {"model": "typesafe/jev-1.13-20260917", "id": "gen-dec-1",
            "answers": {"difficulty": {"type": "choice", "choice": choice,
                                       "probabilities": {"easy": easy, "hard": round(1 - easy, 6)}, "confidence": confidence}},
            "usage": {"input_tokens": 401, "output_tokens": 31, "cost": 0.000017}}


def _reply(content):
    """A response whose body is ``content``: a decision (a choice word, or a
    dict) or raw bytes."""
    if isinstance(content, str) and content in {"easy", "hard"}:
        content = _decision(content)
    body = content if isinstance(content, bytes) else json.dumps(content).encode()

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return Resp(body)


class _Opener:
    def __init__(self, result):
        self.result = result
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append((req, timeout))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class JudgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(patch.dict(os.environ, {easy_route.KEY_ENV: KEY}, clear=False))
        os.environ.pop(easy_route.SWITCH_ENV, None)
        os.environ.pop(easy_route.MODEL_ENV, None)

    def judge(self, result, text="Prove that 2 + 2 = 4.", **kw):
        opener = _Opener(result)
        with patch("urllib.request.build_opener", return_value=opener):
            return easy_route.judge(text, **kw), opener

    def test_on_only_with_the_key_and_not_switched_off(self) -> None:
        self.assertTrue(easy_route.enabled())
        with patch.dict(os.environ, {easy_route.SWITCH_ENV: "0"}):
            self.assertFalse(easy_route.enabled())
        with patch.dict(os.environ, {easy_route.KEY_ENV: "  "}):
            self.assertFalse(easy_route.enabled())
        self.assertEqual(easy_route.model(), "typesafe/jev-1.13")
        with patch.dict(os.environ, {easy_route.MODEL_ENV: "vendor/other"}):
            self.assertEqual(easy_route.model(), "vendor/other")

    def test_an_easy_verdict_and_the_request_sent(self) -> None:
        verdict, opener = self.judge(_reply(_decision("easy", 0.97, 0.94)))
        self.assertEqual((verdict["verdict"], verdict["reason"], verdict["model"], verdict["error"]),
                         ("easy", "easy with probability 0.97, confidence 0.94", "typesafe/jev-1.13", ""))
        req, timeout = opener.requests[0]
        self.assertEqual(req.full_url, "https://openrouter.ai/api/alpha/decisions")
        self.assertEqual(req.get_header("Authorization"), f"Bearer {KEY}")
        sent = json.loads(req.data)
        self.assertEqual(sent["model"], "typesafe/jev-1.13")
        self.assertEqual(sent["state"], ["REQUEST:\nProve that 2 + 2 = 4."])
        question = sent["questions"]["difficulty"]
        self.assertEqual((question["type"], sorted(question["criteria"])), ("choice", ["easy", "hard"]))
        self.assertNotIn("messages", sent)
        self.assertLessEqual(timeout, 15)

    def test_a_follow_up_is_rated_with_its_problem(self) -> None:
        _, opener = self.judge(_reply("hard"), text="keep trying!", problem="Prove the Riemann hypothesis.")
        self.assertEqual(json.loads(opener.requests[0][0].data)["state"],
                         ["ORIGINAL PROBLEM OF THE SESSION:\nProve the Riemann hypothesis.", "REQUEST (this turn):\nkeep trying!"])

    def test_anything_but_a_clear_easy_is_hard(self) -> None:
        for content, want in [
            (_decision("hard"), "hard"),
            (_decision("easy", 0.68, 0.36), "easy"),
            (_decision("easy", 0.6), "easy"),
            (_decision("easy", 0.55, 0.1), "hard"),  # chosen, but not clearly
            (_decision("EASY"), "hard"),  # only the option named "easy"
            ({"answers": {"difficulty": {"type": "choice", "choice": "easy"}}}, "easy"),  # no probabilities given
            ({"answers": {"difficulty": {"type": "choice", "choice": "easy", "probabilities": {"easy": "x"}}}}, "hard"),
            ({"answers": {"difficulty": {"type": "choice", "choice": "medium"}}}, "hard"),
            ({"answers": {"other": {"choice": "easy"}}}, "hard"),
            ({"answers": []}, "hard"),
            ({}, "hard"),
            ([], "hard"),
        ]:
            verdict, _ = self.judge(_reply(content))
            self.assertEqual(verdict["verdict"], want, content)

    def test_failures_are_hard_with_an_error_and_never_the_key_or_the_text(self) -> None:
        for result, error in [
            (urllib.error.HTTPError("u", 401, "no", {}, io.BytesIO(b"")), "judge HTTP 401"),
            (TimeoutError("slow"), "judge unavailable (TimeoutError)"),
            (urllib.error.URLError("down"), "judge unavailable (URLError)"),
            (_reply_garbage := io.BytesIO(b"not json"), None),
        ]:
            if error is None:
                class R(io.BytesIO):
                    def __enter__(self):
                        return self

                    def __exit__(self, *a):
                        return False
                result = R(b"not json")
                error = "judge unavailable (JSONDecodeError)"
            verdict, _ = self.judge(result, text="secret problem text")
            self.assertEqual((verdict["verdict"], verdict["error"]), ("hard", error))
            self.assertNotIn(KEY, json.dumps(verdict))
            self.assertNotIn("secret problem text", json.dumps(verdict))

    def test_no_key_never_calls_out(self) -> None:
        with patch.dict(os.environ, {easy_route.KEY_ENV: ""}):
            verdict, opener = self.judge(_reply("easy"))
        self.assertEqual((verdict["verdict"], verdict["error"], opener.requests), ("hard", "no judge key", []))

    def test_long_text_is_cut(self) -> None:
        _, opener = self.judge(_reply("hard"), text="x" * 50_000)
        self.assertLess(len(json.loads(opener.requests[0][0].data)["state"][0]), 9_000)

    def test_the_judges_address(self) -> None:
        self.assertEqual(easy_route.url(), easy_route.URL)
        for value, want in [("https://proxy.example/api/alpha/decisions", "https://proxy.example/api/alpha/decisions"),
                            ("http://127.0.0.1:8123/judge", "http://127.0.0.1:8123/judge"),
                            ("http://evil.example/judge", easy_route.URL), ("file:///etc/passwd", easy_route.URL)]:
            with patch.dict(os.environ, {easy_route.URL_ENV: value}):
                self.assertEqual(easy_route.url(), want, value)

    def test_asking_for_cgm(self) -> None:
        for text in ["use Ansatz CGM", "Please run it with CGM.", "use the full harness", "ANSATZ CGM please"]:
            self.assertTrue(easy_route.asks_for_cgm(text), text)
        for text in ["Prove that 1+1=2", "the cgmx lemma", "keep trying!"]:
            self.assertFalse(easy_route.asks_for_cgm(text), text)


class StateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_turns_are_recorded_with_their_home(self) -> None:
        easy_route.note_turn(self.ws, home="math_harness", engine="plain",
                             verdict={"verdict": "easy", "reason": "short", "model": "m", "error": ""}, why="judged")
        easy_route.note_turn(self.ws, home="ignored", engine="math_harness", verdict=None, why="asked")
        state = easy_route.load(self.ws)
        self.assertEqual(easy_route.home(self.ws), "math_harness")
        self.assertEqual([(t["engine"], t["why"], t.get("verdict")) for t in state["turns"]],
                         [("plain", "judged", "easy"), ("math_harness", "asked", None)])

    def test_no_workspace_a_bad_file_or_a_symlink_records_nothing(self) -> None:
        easy_route.note_turn(self.ws / "missing", home="math_harness", engine="plain", verdict=None, why="x")
        (self.ws / easy_route.STATE_FILE).write_text("{bad", encoding="utf-8")
        self.assertEqual(easy_route.load(self.ws), {})
        self.assertEqual(easy_route.home(None), "")
        other = Path(self.enterContext(tempfile.TemporaryDirectory())) / "target.json"
        other.write_text("{}", encoding="utf-8")
        (self.ws / easy_route.STATE_FILE).unlink()
        (self.ws / easy_route.STATE_FILE).symlink_to(other)
        easy_route.note_turn(self.ws, home="math_harness", engine="plain", verdict=None, why="x")
        self.assertEqual(other.read_text(encoding="utf-8"), "{}")

    def test_the_notes(self) -> None:
        note = easy_route.plain_note({"model": "typesafe/jev-1.13", "reason": "A routine identity."})
        self.assertIn("this turn runs on Plain", note)
        self.assertIn("typesafe/jev-1.13: A routine identity.", note)
        self.assertIn("use Ansatz CGM", note)
        self.assertEqual(easy_route.cgm_note("asked"), "This turn runs on Ansatz CGM again, as asked.")
        self.assertIn("rated not easy by m", easy_route.cgm_note("judged", {"model": "m"}))


class StartTest(unittest.TestCase):
    """start_job_routed: which engine a start gets."""

    ADMIN = {"id": 1, "is_admin": True}
    MEMBER = {"id": 7, "is_admin": False}

    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(jobs, "RUNS_DIR", root / "runs"))
        self.enterContext(patch.dict(os.environ, {easy_route.KEY_ENV: KEY}))
        os.environ.pop(easy_route.SWITCH_ENV, None)
        self.calls = []
        self.chat = []
        self.fail_plain = None
        self.serial = 0

        def start_job(**kwargs):
            self.calls.append(kwargs)
            if kwargs["engine"] == "plain" and self.fail_plain:
                raise self.fail_plain
            self.serial += 1
            rid = f"{kwargs['engine']}_adhoc_{self.serial}"
            (root / "runs" / "workspaces" / rid).mkdir(parents=True)
            return {"run_id": rid, "engine": kwargs["engine"]}

        self.enterContext(patch.object(jobs, "start_job", side_effect=start_job))
        self.enterContext(patch.object(jobs, "_append_chat", side_effect=lambda rid, role, text: self.chat.append((rid, role, text))))
        self.root = root

    def start(self, verdict="easy", user=None, **kw):
        judge = patch.object(easy_route, "judge", return_value={"verdict": verdict, "reason": "Short.", "model": "typesafe/jev-1.13", "error": ""})
        with judge as judged:
            job = jobs.start_job_routed(engine=kw.pop("engine", "math_harness"), problem_text=kw.pop("problem_text", "Show 1+1=2."),
                                        user=user or self.ADMIN, model="gpt-6.1-sol", auth_route="codex_subscription", **kw)
        return job, judged

    def state(self, job):
        return easy_route.load(self.root / "runs" / "workspaces" / job["run_id"])

    def test_easy_starts_plain_with_the_same_request_and_says_so(self) -> None:
        job, judged = self.start("easy")
        self.assertEqual(job["engine"], "plain")
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertEqual((call["engine"], call["easy_route"], call["model"], call["auth_route"], call["problem_text"]),
                         ("plain", True, "gpt-6.1-sol", "codex_subscription", "Show 1+1=2."))
        judged.assert_called_once_with("Show 1+1=2.")
        self.assertEqual(self.state(job)["home"], "math_harness")
        self.assertEqual(self.state(job)["turns"][0]["engine"], "plain")
        self.assertEqual(len(self.chat), 1)
        self.assertIn("this turn runs on Plain", self.chat[0][2])

    def test_hard_starts_cgm_and_remembers_its_home(self) -> None:
        job, _ = self.start("hard")
        self.assertEqual(job["engine"], "math_harness")
        self.assertEqual([c["engine"] for c in self.calls], ["math_harness"])
        self.assertNotIn("easy_route", self.calls[0])
        self.assertEqual(self.state(job)["turns"][0]["engine"], "math_harness")
        self.assertEqual(self.chat, [])

    def test_a_plain_start_refused_falls_back_to_cgm(self) -> None:
        for error in (ValueError("Plain would run on the sponsored Kimi service"),
                      jobs.account_policy.ProjectPolicyError("tool-free")):
            self.calls.clear()
            self.chat.clear()
            self.fail_plain = error
            job, _ = self.start("easy")
            self.assertEqual([c["engine"] for c in self.calls], ["plain", "math_harness"])
            self.assertEqual(job["engine"], "math_harness")
            self.assertEqual(self.chat, [])

    def test_other_errors_are_not_swallowed(self) -> None:
        self.fail_plain = RuntimeError("disk full")
        with self.assertRaises(RuntimeError):
            self.start("easy")

    def test_asking_for_cgm_or_no_text_skips_the_judge(self) -> None:
        job, judged = self.start("easy", problem_text="Use Ansatz CGM: prove 1+1=2.")
        judged.assert_not_called()
        self.assertEqual(job["engine"], "math_harness")
        self.assertEqual(self.state(job)["turns"][0]["why"], "asked")
        job, judged = self.start("easy", problem_text="", problem_id="erdos_224")
        judged.assert_not_called()
        self.assertEqual(job["engine"], "math_harness")

    def test_unrouted_starts_are_start_job_unchanged(self) -> None:
        for kw in ({"engine": "plain"}, {"engine": "claude"}, {"engine": "math_harness", "run_kind": "analyze"}):
            self.calls.clear()
            user = kw.pop("user", None)
            job, judged = self.start("easy", user=user, **kw)
            judged.assert_not_called()
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(self.state(job), {})
        with patch.dict(os.environ, {easy_route.SWITCH_ENV: "0"}):
            self.calls.clear()
            job, judged = self.start("easy")
            judged.assert_not_called()
            self.assertEqual(job["engine"], "math_harness")
        from agent_monitor import cgm_max

        for kw in ({"auth_route": cgm_max.ROUTE}, {"model": cgm_max.MODEL_ID}, {"model": cgm_max.API_MODEL_ID}):
            self.calls.clear()
            judge = patch.object(easy_route, "judge")
            with judge as judged:
                job = jobs.start_job_routed(engine="math_harness", problem_text="Show 1+1=2.", user=self.ADMIN,
                                            **{"model": "gpt-6.1-sol", "auth_route": "codex_subscription", **kw})
            judged.assert_not_called()
            self.assertEqual([c["engine"] for c in self.calls], ["math_harness"], kw)

    def test_a_member_not_allowed_plain_or_another_model_stays_on_cgm(self) -> None:
        with patch.object(jobs.account_policy, "member_run_error", return_value="Plain is not available"):
            job, _ = self.start("easy", user=self.MEMBER)
        self.assertEqual([c["engine"] for c in self.calls], ["math_harness"])
        self.calls.clear()
        with patch.object(jobs.account_policy, "member_run_error", return_value=None), \
                patch.object(jobs.account_policy, "member_run_model", return_value="kimi-k3"):
            job, _ = self.start("easy", user=self.MEMBER)
        self.assertEqual([c["engine"] for c in self.calls], ["math_harness"])
        self.calls.clear()
        with patch.object(jobs.account_policy, "member_run_error", return_value=None), \
                patch.object(jobs.account_policy, "member_run_model", side_effect=lambda u, e, m: m):
            job, _ = self.start("easy", user=self.MEMBER)
        self.assertEqual([c["engine"] for c in self.calls], ["plain"])


class FollowUpTest(unittest.TestCase):
    """_easy_route_follow_up: the engine of each follow-up turn."""

    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(jobs, "RUNS_DIR", root / "runs"))
        self.enterContext(patch.object(jobs, "CACHE_DIR", root / "cache"))
        self.enterContext(patch.dict(os.environ, {easy_route.KEY_ENV: KEY}))
        os.environ.pop(easy_route.SWITCH_ENV, None)
        self.run_id = "math_harness_adhoc_fu"
        self.ws = root / "runs" / "workspaces" / self.run_id
        self.ws.mkdir(parents=True)
        jobs._write_run({"run_id": self.run_id, "engine": "math_harness", "status": "finished",
                         "problem_text": "Prove the theorem.", "problems": [{"text": "Prove the theorem.", "source": "initial"}],
                         "agents": [], "edges": []})
        easy_route.note_turn(self.ws, home="math_harness", engine="math_harness", verdict=None, why="judged")
        self.chat = []
        self.enterContext(patch.object(jobs, "_append_chat", side_effect=lambda rid, role, text: self.chat.append(text)))
        self.plain_ok_patch = patch.object(jobs, "_easy_plain_follow_up_ok", return_value=True)
        self.plain_ok = self.plain_ok_patch.start()
        self.addCleanup(patch.stopall)

    def turn(self, message, verdict="easy", **kw):
        with patch.object(easy_route, "judge", return_value={"verdict": verdict, "reason": "Short.", "model": "typesafe/jev-1.13", "error": ""}) as judged:
            data = jobs._easy_route_follow_up(self.run_id, jobs._load_run_record(self.run_id), self.ws,
                                              message=message, user={"id": 1, "is_admin": True}, model="gpt-6.1-sol", **kw)
        return data, judged

    def engine(self):
        return jobs._load_run_record(self.run_id)["engine"]

    def test_easy_hard_and_asked_turns_switch_the_engine_and_say_so(self) -> None:
        data, judged = self.turn("What is 2+2?", "easy")
        self.assertEqual((data["engine"], self.engine()), ("plain", "plain"))
        judged.assert_called_once_with("What is 2+2?", problem="Prove the theorem.")
        self.assertIn("this turn runs on Plain", self.chat[-1])
        data, _ = self.turn("keep trying!", "hard")
        self.assertEqual((data["engine"], self.engine()), ("math_harness", "math_harness"))
        self.assertIn("Ansatz CGM again, rated not easy", self.chat[-1])
        self.turn("2+3?", "easy")
        data, judged = self.turn("Now use Ansatz CGM on it", "easy")
        judged.assert_not_called()
        self.assertEqual(self.engine(), "math_harness")
        self.assertEqual(self.chat[-1], "This turn runs on Ansatz CGM again, as asked.")
        self.assertEqual([t["engine"] for t in easy_route.load(self.ws)["turns"]],
                         ["math_harness", "plain", "math_harness", "plain", "math_harness"])

    def test_the_same_engine_again_says_nothing(self) -> None:
        self.turn("keep trying!", "hard")
        self.assertEqual(self.chat, [])
        self.assertEqual(self.engine(), "math_harness")

    def test_continue_files_and_unrouted_runs_keep_their_engine(self) -> None:
        for message, kw in [("", {}), (None, {}), (jobs._BARE_CONTINUE_PREFIX + " please", {}), ("What is 2+2?", {"has_files": True})]:
            data, judged = self.turn(message, "easy", **kw)
            judged.assert_not_called()
            self.assertEqual(data["engine"], "math_harness")
        (self.ws / easy_route.STATE_FILE).unlink()
        data, judged = self.turn("What is 2+2?", "easy")
        judged.assert_not_called()
        self.assertEqual(self.engine(), "math_harness")
        self.assertEqual(self.chat, [])

    def test_plain_not_possible_keeps_cgm(self) -> None:
        self.plain_ok.return_value = False
        data, _ = self.turn("What is 2+2?", "easy")
        self.assertEqual((data["engine"], self.engine()), ("math_harness", "math_harness"))
        self.assertEqual(self.chat, [])

    def test_switched_off_or_failing_never_stops_the_turn(self) -> None:
        with patch.dict(os.environ, {easy_route.SWITCH_ENV: "off"}):
            data, judged = self.turn("What is 2+2?", "easy")
        judged.assert_not_called()
        with patch.object(easy_route, "judge", side_effect=RuntimeError("boom")):
            data = jobs._easy_route_follow_up(self.run_id, jobs._load_run_record(self.run_id), self.ws,
                                              message="What is 2+2?", user=None, model=None)
        self.assertEqual(data["engine"], "math_harness")

    def test_plain_on_the_runs_own_route_only(self) -> None:
        self.plain_ok_patch.stop()
        record = jobs._load_run_record(self.run_id)
        from agent_monitor import cgm_max

        for extra in ({"route_plan": {"advisor": {}, "worker": {}}}, {"auth_route": cgm_max.ROUTE},
                      {"live": {"auth_mode": cgm_max.ROUTE}}, {"model": cgm_max.API_MODEL_ID}):
            self.assertFalse(jobs._easy_plain_follow_up_ok(self.run_id, {**record, **extra}, None, "gpt-6.1-sol"), extra)
        for max_model in (cgm_max.MODEL_ID, cgm_max.API_MODEL_ID):
            self.assertFalse(jobs._easy_plain_follow_up_ok(self.run_id, record, None, max_model), max_model)
        with patch.object(jobs, "_continuation_auth_environment", side_effect=ValueError("route")):
            self.assertFalse(jobs._easy_plain_follow_up_ok(self.run_id, record, None, "gpt-6.1-sol"))
        with patch.object(jobs, "_continuation_auth_environment", return_value=({}, "codex_subscription")), \
                patch.object(jobs, "_tool_free_follow_up", return_value=False):
            self.assertTrue(jobs._easy_plain_follow_up_ok(self.run_id, record, {"id": 1, "is_admin": True}, "gpt-6.1-sol"))
            with patch.object(jobs, "_member_continuation_model", side_effect=lambda u, e, r, m: "kimi-k3" if e == "plain" else m):
                self.assertFalse(jobs._easy_plain_follow_up_ok(self.run_id, record, None, "gpt-6.1-sol"))


class KeyIsolationTest(unittest.TestCase):
    def test_the_judges_key_never_reaches_a_child_process(self) -> None:
        with patch.dict(os.environ, {easy_route.KEY_ENV: KEY}):
            self.assertNotIn(easy_route.KEY_ENV, subprocess_env.child_process_env())
            self.assertNotIn(easy_route.KEY_ENV, subprocess_env.child_process_env(extra={easy_route.KEY_ENV: KEY}))
        env = {easy_route.KEY_ENV: KEY, easy_route.URL_ENV: "http://127.0.0.1:1/x", easy_route.MODEL_ENV: "m",
               easy_route.SWITCH_ENV: "1", "PATH": "/bin"}
        subprocess_env.scrub_control_plane_env(env)
        self.assertEqual(env, {"PATH": "/bin"})

    def test_it_is_not_a_provider_key_anything_else_reads(self) -> None:
        import subprocess

        root = Path(__file__).resolve().parents[1] / "agent_monitor"
        hits = subprocess.run(["grep", "-rl", "--include=*.py", "--include=*.js", "--include=*.html", easy_route.KEY_ENV, str(root)],
                              capture_output=True, text=True).stdout.split()
        self.assertEqual(sorted(Path(h).name for h in hits), ["easy_route.py", "subprocess_env.py"])

    def test_latex_compiles_without_server_secrets(self) -> None:
        import inspect

        from agent_monitor import console_server

        source = inspect.getsource(console_server._compile_workspace_pdf)
        self.assertEqual(source.count("env=tex_env"), 2)
        self.assertIn("tex_env = child_process_env()", source)


class WiringTest(unittest.TestCase):
    def test_the_proof_start_and_both_follow_up_paths_route(self) -> None:
        import inspect

        from agent_monitor import console_server

        self.assertIn("job_manager.start_job_routed(", inspect.getsource(console_server))
        claimed = inspect.getsource(jobs._continue_claimed_run)
        with_message = inspect.getsource(jobs._continue_with_message)
        for source in (claimed, with_message):
            self.assertLess(source.index("_easy_route_follow_up("), source.index('engine = run_data.get("engine")'))
        self.assertIn("has_files=bool(user_entry.get(\"attachments\"))", with_message)
        start = inspect.getsource(jobs.start_job)
        self.assertLess(start.index("if easy_route and sponsored_kimi and easy_route_home != \"kimi\":"), start.index("_launch_run(") if "_launch_run(" in start else len(start))


if __name__ == "__main__":
    unittest.main()


class SponsoredRefusalTest(unittest.TestCase):
    """The real start_job: an easy Plain start never lands on the sponsored service."""

    def test_refused_before_anything_is_written(self) -> None:
        from agent_monitor import sponsored_kimi_client

        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(jobs, "RUNS_DIR", root / "runs"))
        self.enterContext(patch.object(jobs, "CACHE_DIR", root / "cache"))
        self.enterContext(patch.object(sponsored_kimi_client, "configured", return_value=True))
        self.enterContext(patch.object(sponsored_kimi_client, "supports_engine", return_value=True))
        self.enterContext(patch.object(sponsored_kimi_client, "endpoint", return_value="http://127.0.0.1:9/v1"))
        for name in ("KIMI_API_KEY", "KIMI_API_BASE", "KIMI_BASE_URL"):
            self.enterContext(patch.dict(os.environ, {name: ""}))
        admin = {"id": 1, "is_admin": True, "email": "admin@example.test"}
        with patch.object(jobs.threading, "Thread") as thread:
            with self.assertRaisesRegex(ValueError, "sponsored Kimi service"):
                jobs.start_job(engine="plain", problem_text="Show 1+1=2.", model="kimi-k3", user=admin,
                               auth_route="api_key", easy_route=True)
            thread.assert_not_called()
        self.assertFalse((root / "runs" / "workspaces").exists() and any((root / "runs" / "workspaces").iterdir()))


class AnswerOnlyTest(unittest.TestCase):
    """A Plain turn on a run whose proof Ansatz CGM wrote answers and keeps the proof."""

    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(jobs, "RUNS_DIR", root / "runs"))
        self.enterContext(patch.object(jobs, "CACHE_DIR", root / "cache"))
        self.run_id = "math_harness_adhoc_ans"
        self.ws = root / "runs" / "workspaces" / self.run_id
        self.ws.mkdir(parents=True)
        self.cgm_proof = "# Claim\n\nThe proof Ansatz CGM wrote.\n"
        (self.ws / "proof.md").write_text(self.cgm_proof, encoding="utf-8")
        self.addCleanup(jobs._STOP_EVENTS.pop, self.run_id, None)
        jobs._stop_event(self.run_id).clear()
        jobs._write_run({"run_id": self.run_id, "engine": "plain", "status": "finished", "problem_id": "adhoc",
                         "problem_text": "Prove it.", "problems": [{"text": "Prove it.", "source": "initial"}],
                         "agents": [{"trace_id": "t1", "output": "first", "status": "finished"}], "edges": []})

    def state(self, *engines):
        for engine in engines:
            easy_route.note_turn(self.ws, home="math_harness", engine=engine, verdict=None, why="judged")

    def test_when_a_plain_turn_is_answer_only(self) -> None:
        record = jobs._load_run_record(self.run_id)
        self.state("math_harness", "plain")
        self.assertTrue(jobs._easy_answer_only(self.ws, record))
        self.assertFalse(jobs._easy_answer_only(self.ws, {**record, "engine": "math_harness"}))
        (self.ws / easy_route.STATE_FILE).unlink()
        self.state("plain", "plain")  # never on Ansatz CGM: Plain may write the proof
        self.assertFalse(jobs._easy_answer_only(self.ws, record))
        (self.ws / easy_route.STATE_FILE).unlink()
        self.state("math_harness", "plain")
        (self.ws / "proof.md").write_text("", encoding="utf-8")
        self.assertFalse(jobs._easy_answer_only(self.ws, record))
        (self.ws / easy_route.STATE_FILE).unlink()
        self.assertFalse(jobs._easy_answer_only(self.ws, record))

    def test_the_turn_answers_keeps_the_proof_and_keeps_its_answer(self) -> None:
        from agent_monitor import auto_pipeline, proof_turns

        seen = {}

        def engine(**kwargs):
            seen.update(kwargs)
            # What plain_runner does in answer-only mode.
            (self.ws / proof_turns.ANSWER_FILE).write_text("Step 3 holds because $x^2 \\ge 0$.\n", encoding="utf-8")
            return {"run_id": self.run_id, "status": "finished", "edges": [], "totals": {},
                    "agents": [{"trace_id": "t2", "output": "answered", "status": "finished"}]}

        job_id = "job_ans"
        jobs._JOBS[job_id] = {"job_id": job_id, "run_id": self.run_id, "status": "running", "engine": "plain"}
        self.addCleanup(jobs._JOBS.pop, job_id, None)
        with patch.object(jobs, "_run_cli_engine_with_openclaude_retry", side_effect=engine), \
                patch.object(jobs, "_launch_pending_feedback"), patch.object(auto_pipeline, "start") as pipeline:
            jobs._execute_continue(job_id=job_id, run_id=self.run_id, engine="plain", message="Explain step 3.",
                                   problem_text="Prove it.", model="gpt-6.1-sol", max_iterations=2,
                                   workspace=str(self.ws), answer_only=True)
        self.assertEqual(seen["extra_env"].get("AGENT_MONITOR_PLAIN_ANSWER_ONLY"), "1")
        self.assertIn("Do NOT rewrite, repeat or replace the proof", seen["problem_text"])
        self.assertIn("Explain step 3.", seen["problem_text"])
        self.assertIn("The proof Ansatz CGM wrote.", seen["problem_text"])
        pipeline.assert_not_called()
        self.assertEqual((self.ws / "proof.md").read_text(encoding="utf-8"), self.cgm_proof)
        turn = proof_turns.summary(self.ws)[-1]
        self.assertEqual((turn["answer_only"], turn["status"], turn["file"]), (True, "finished", ".proof_turns/1.md"))
        self.assertEqual((self.ws / turn["file"]).read_text(encoding="utf-8"), "Step 3 holds because $x^2 \\ge 0$.\n")
        self.assertFalse((self.ws / proof_turns.ANSWER_FILE).exists())

    def test_answer_only_is_plains_alone(self) -> None:
        seen = {}

        def engine(**kwargs):
            seen.update(kwargs)
            return {"run_id": self.run_id, "status": "finished", "edges": [], "totals": {},
                    "agents": [{"trace_id": "t2", "output": "x", "status": "finished"}]}

        from agent_monitor import auto_pipeline, proof_turns

        job_id = "job_cgm"
        jobs._JOBS[job_id] = {"job_id": job_id, "run_id": self.run_id, "status": "running", "engine": "kimi"}
        self.addCleanup(jobs._JOBS.pop, job_id, None)
        with patch.object(jobs, "_run_cli_engine_with_openclaude_retry", side_effect=engine), \
                patch.object(jobs, "_launch_pending_feedback"), patch.object(auto_pipeline, "start"):
            jobs._execute_continue(job_id=job_id, run_id=self.run_id, engine="kimi", message="Explain step 3.",
                                   problem_text="Prove it.", model="kimi-k3", max_iterations=2,
                                   workspace=str(self.ws), answer_only=True)
        self.assertNotIn("AGENT_MONITOR_PLAIN_ANSWER_ONLY", seen["extra_env"])
        self.assertFalse(proof_turns.summary(self.ws)[-1]["answer_only"])

    def test_a_reply_or_a_kept_copy_is_never_promoted_to_the_proof(self) -> None:
        from agent_monitor import proof_turns
        from agent_monitor.runners import plain_runner

        self.assertEqual(plain_runner.ANSWER_FILE, proof_turns.ANSWER_FILE)
        for word in ("proof", "solution", "answer"):
            self.assertNotIn(word, proof_turns.ANSWER_FILE)
        import time as _time

        _time.sleep(0.01)
        (self.ws / proof_turns.ANSWER_FILE).write_text("A reply about the proof, long enough to be a candidate.\n", encoding="utf-8")
        (self.ws / ".proof_turns").mkdir()
        (self.ws / ".proof_turns" / "proof-answer.md").write_text("A kept copy, long enough to be a candidate too.\n", encoding="utf-8")
        self.assertIn(jobs._promote_proof_artifact(self.ws), (None, self.ws / "proof.md"))
        self.assertEqual((self.ws / "proof.md").read_text(encoding="utf-8"), self.cgm_proof)
        # With a proof long enough to count, it stays the one promoted.
        long_proof = self.cgm_proof + "More of the proof, so that it is a candidate.\n"
        (self.ws / "proof.md").write_text(long_proof, encoding="utf-8")
        _time.sleep(0.01)
        (self.ws / proof_turns.ANSWER_FILE).write_text("A newer reply, long enough to be a candidate.\n", encoding="utf-8")
        self.assertEqual(jobs._promote_proof_artifact(self.ws), self.ws / "proof.md")
        self.assertEqual((self.ws / "proof.md").read_text(encoding="utf-8"), long_proof)

    def test_the_plain_runner_in_answer_only_mode(self) -> None:
        from agent_monitor.runners import plain_runner

        cwd = os.getcwd()
        os.chdir(self.ws)
        self.addCleanup(os.chdir, cwd)
        events = []
        with patch.object(plain_runner, "emit_item", side_effect=events.append), \
                patch.dict(os.environ, {plain_runner.ANSWER_ONLY_ENV: "1"}):
            self.assertFalse(plain_runner._save_result("prompt", "Step 3 holds."))
            with self.assertRaises(ValueError):
                plain_runner._save_result("prompt", "   ")
        self.assertEqual((self.ws / "proof.md").read_text(encoding="utf-8"), self.cgm_proof)
        self.assertEqual((self.ws / plain_runner.ANSWER_FILE).read_text(encoding="utf-8").strip(), "Step 3 holds.")
        self.assertEqual(events[0]["text"], "Answer written; proof.md left unchanged.")
        with patch.object(plain_runner, "emit_item"), patch.dict(os.environ, {plain_runner.ANSWER_ONLY_ENV: ""}):
            self.assertTrue(plain_runner._save_result("prompt", "# New proof"))
        self.assertIn("New proof", (self.ws / "proof.md").read_text(encoding="utf-8"))

    def test_the_turn_records_of_answer_only_turns(self) -> None:
        from agent_monitor import proof_turns

        (self.ws / proof_turns.ANSWER_FILE).write_text("stale", encoding="utf-8")
        n = proof_turns.begin(self.ws, started_at="t1", answer_only=True)
        self.assertFalse((self.ws / proof_turns.ANSWER_FILE).exists())  # a stale answer never counts
        proof_turns.end(self.ws, n, status="failed", finished_at="t2")  # no answer written
        self.assertEqual((proof_turns.summary(self.ws)[-1]["file"], proof_turns.summary(self.ws)[-1]["answer_only"]), ("", True))
        self.assertEqual((self.ws / "proof.md").read_text(encoding="utf-8"), self.cgm_proof)
        m = proof_turns.begin(self.ws, started_at="t3")
        proof_turns.end(self.ws, m, status="finished", finished_at="t4")
        self.assertEqual((proof_turns.summary(self.ws)[-1]["file"], proof_turns.summary(self.ws)[-1]["answer_only"]), (".proof_turns/2.md", False))


class BudgetAndSettingsTest(unittest.TestCase):
    def test_a_slow_judge_is_cut_at_the_budget(self) -> None:
        import threading

        release = threading.Event()

        class Slow:
            def open(self, req, timeout=None):
                release.wait(5)
                return _reply("easy")

        self.addCleanup(release.set)
        with patch.dict(os.environ, {easy_route.KEY_ENV: KEY}), patch.object(easy_route, "TIMEOUT_S", 0.2), \
                patch("urllib.request.build_opener", return_value=Slow()):
            verdict = easy_route.judge("Prove 1+1=2.")
        self.assertEqual((verdict["verdict"], verdict["error"]), ("hard", "judge unavailable (TimeoutError)"))
        self.assertLess(verdict["seconds"], 2)

    def test_the_launcher_is_told_whether_the_judge_is_on_never_its_key(self) -> None:
        from agent_monitor import settings

        with patch.dict(os.environ, {easy_route.KEY_ENV: KEY}):
            os.environ.pop(easy_route.SWITCH_ENV, None)
            os.environ.pop(easy_route.MODEL_ENV, None)
            self.assertEqual(settings._easy_route_setting({"id": 1}), {"enabled": True, "model": "typesafe/jev-1.13"})
            # Guests too: their starts are rated (Auto, mathematics only, easy requests).
            self.assertEqual(settings._easy_route_setting({"id": 2, "guest": True}), {"enabled": True, "model": "typesafe/jev-1.13"})
            self.assertEqual(settings._easy_route_setting(None), {"enabled": False, "model": ""})
            self.assertNotIn(KEY, json.dumps(settings._easy_route_setting({"id": 1})))
        with patch.dict(os.environ, {easy_route.KEY_ENV: ""}):
            self.assertEqual(settings._easy_route_setting({"id": 1}), {"enabled": False, "model": ""})
        import inspect

        self.assertIn('"easy_route": _easy_route_setting(user)', inspect.getsource(settings.get_settings))


class KimiHomeTest(unittest.TestCase):
    """Ansatz CGM · Kimi K3 (``kimi``) is rated too; its easy turns run on Plain on its own route."""

    def test_a_kimi_start_routes_and_remembers_its_home(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(jobs, "RUNS_DIR", root / "runs"))
        self.enterContext(patch.dict(os.environ, {easy_route.KEY_ENV: KEY}))
        os.environ.pop(easy_route.SWITCH_ENV, None)
        calls = []

        def start_job(**kwargs):
            calls.append(kwargs)
            rid = f"{kwargs['engine']}_adhoc_{len(calls)}"
            (root / "runs" / "workspaces" / rid).mkdir(parents=True)
            return {"run_id": rid, "engine": kwargs["engine"]}

        self.enterContext(patch.object(jobs, "start_job", side_effect=start_job))
        self.enterContext(patch.object(jobs, "_append_chat"))
        for verdict, want in (("easy", "plain"), ("hard", "kimi")):
            calls.clear()
            with patch.object(easy_route, "judge", return_value={"verdict": verdict, "reason": "", "model": "m", "error": ""}):
                job = jobs.start_job_routed(engine="kimi", problem_text="What is 2+3?", model="kimi-k3",
                                            auth_route="api_key", user={"id": 1, "is_admin": True})
            self.assertEqual(job["engine"], want)
            if want == "plain":
                self.assertEqual((calls[0]["easy_route"], calls[0]["easy_route_home"], calls[0]["model"]), (True, "kimi", "kimi-k3"))
            state = easy_route.load(root / "runs" / "workspaces" / job["run_id"])
            self.assertEqual((state["home"], state["turns"][0]["engine"]), ("kimi", want))

    def test_the_sponsored_route_is_kimis_own_so_plain_may_take_it(self) -> None:
        from agent_monitor import sponsored_kimi_client

        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(jobs, "RUNS_DIR", root / "runs"))
        self.enterContext(patch.object(jobs, "CACHE_DIR", root / "cache"))
        self.enterContext(patch.object(sponsored_kimi_client, "configured", return_value=True))
        self.enterContext(patch.object(sponsored_kimi_client, "supports_engine", return_value=True))
        self.enterContext(patch.object(sponsored_kimi_client, "endpoint", return_value="http://127.0.0.1:9/v1"))
        for name in ("KIMI_API_KEY", "KIMI_API_BASE", "KIMI_BASE_URL"):
            self.enterContext(patch.dict(os.environ, {name: ""}))
        admin = {"id": 1, "is_admin": True, "email": "admin@example.test"}
        # The real start_job below registers a job (its worker never runs):
        # it is removed again, so no later test sees account 1 with a running job.
        before = set(jobs._JOBS)
        self.addCleanup(lambda: [jobs._JOBS.pop(key, None) for key in set(jobs._JOBS) - before])
        # Home Ansatz CGM (math_harness): refused before anything is written.
        with patch.object(jobs.threading, "Thread") as thread:
            with self.assertRaisesRegex(ValueError, "sponsored Kimi service"):
                jobs.start_job(engine="plain", problem_text="Show 1+1=2.", model="kimi-k3", user=admin,
                               auth_route="api_key", easy_route=True, easy_route_home="math_harness")
            thread.assert_not_called()
        # Home Ansatz CGM · Kimi K3: that refusal never applies (whatever else start_job then checks).
        with patch.object(jobs.threading, "Thread"):
            try:
                jobs.start_job(engine="plain", problem_text="Show 1+1=2.", model="kimi-k3", user=admin,
                               auth_route="api_key", easy_route=True, easy_route_home="kimi")
            except ValueError as exc:
                self.assertNotIn("Plain would run on the sponsored Kimi service", str(exc))

    def test_a_kimi_runs_follow_ups_go_back_to_kimi(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(jobs, "RUNS_DIR", root / "runs"))
        self.enterContext(patch.object(jobs, "CACHE_DIR", root / "cache"))
        self.enterContext(patch.dict(os.environ, {easy_route.KEY_ENV: KEY}))
        os.environ.pop(easy_route.SWITCH_ENV, None)
        run_id = "plain_adhoc_kimihome"
        ws = root / "runs" / "workspaces" / run_id
        ws.mkdir(parents=True)
        (ws / "proof.md").write_text("# Proof\n\nA proof long enough to count as one.\n", encoding="utf-8")
        jobs._write_run({"run_id": run_id, "engine": "plain", "status": "finished", "problem_text": "Prove it.",
                         "problems": [{"text": "Prove it.", "source": "initial"}], "agents": [], "edges": []})
        easy_route.note_turn(ws, home="kimi", engine="plain", verdict=None, why="judged")
        self.enterContext(patch.object(jobs, "_append_chat"))
        self.enterContext(patch.object(jobs, "_easy_plain_follow_up_ok", return_value=True))
        with patch.object(easy_route, "judge", return_value={"verdict": "hard", "reason": "", "model": "m", "error": ""}):
            data = jobs._easy_route_follow_up(run_id, jobs._load_run_record(run_id), ws, message="keep trying!", user=None, model="kimi-k3")
        self.assertEqual(data["engine"], "kimi")
        with patch.object(easy_route, "judge", return_value={"verdict": "easy", "reason": "", "model": "m", "error": ""}):
            data = jobs._easy_route_follow_up(run_id, jobs._load_run_record(run_id), ws, message="what is 2+3?", user=None, model="kimi-k3")
        self.assertEqual(data["engine"], "plain")
        # After a kimi turn, an easy one only answers (the proof is Ansatz CGM · Kimi K3's).
        self.assertTrue(jobs._easy_answer_only(ws, data))


class PlainApiAnswerTest(unittest.TestCase):
    def test_an_answer_needs_no_heading_but_never_carries_tool_markup(self) -> None:
        from agent_monitor.runners import plain_runner

        with patch.dict(os.environ, {plain_runner.ANSWER_ONLY_ENV: ""}):
            self.assertEqual(plain_runner._api_artifact_issues("Step 3 holds."), ["missing level-one final heading"])
            self.assertEqual(plain_runner._api_artifact_issues("# Proof\n\nok"), [])
        with patch.dict(os.environ, {plain_runner.ANSWER_ONLY_ENV: "1"}):
            self.assertEqual(plain_runner._api_artifact_issues("Step 3 holds because $x^2 \\ge 0$."), [])
            self.assertEqual(plain_runner._api_artifact_issues("Sure. <function_calls><invoke name='x'>"), ["<function_calls>", "<invoke "])


def _answers(**questions) -> dict:
    """A Decisions API answer: ``topic=("not_math", 0.95)`` style choices."""
    out = {}
    for name, (choice, p) in questions.items():
        out[name] = {"type": "choice", "choice": choice, "probabilities": {choice: p}, "confidence": p}
    return {"answers": out}


class AutoModeTest(unittest.TestCase):
    """Auto (rate_request) and the mathematics-only refusal (topic_refusal)."""

    def setUp(self) -> None:
        self.enterContext(patch.dict(os.environ, {easy_route.KEY_ENV: KEY}))
        os.environ.pop(easy_route.SWITCH_ENV, None)

    def rate(self, reply, text="Prove that 2+2=4.", source=False):
        opener = _Opener(_reply(reply))
        with patch("urllib.request.build_opener", return_value=opener):
            return easy_route.rate_request(text, has_source=source), opener

    def test_the_request_asks_topic_and_formalization_and_with_a_manuscript_the_task(self) -> None:
        _, opener = self.rate(_answers(topic=("math", 1.0), formalize=("formal", 1.0)))
        sent = json.loads(opener.requests[0][0].data)
        self.assertEqual(sorted(sent["questions"]), ["formalize", "topic"])
        self.assertEqual(sorted(sent["questions"]["topic"]["criteria"]), ["math", "not_math"])
        _, opener = self.rate(_answers(topic=("math", 1.0), formalize=("formal", 1.0), task=("analyze", 1.0)), source=True)
        sent = json.loads(opener.requests[0][0].data)
        self.assertEqual(sorted(sent["questions"]["task"]["criteria"]), ["analyze", "prove", "readability"])

    def test_modes(self) -> None:
        cases = [
            (_answers(topic=("math", 1.0), formalize=("formal", 0.9)), False, "formal"),
            (_answers(topic=("math", 1.0), formalize=("informal", 0.9)), False, "informal"),
            (_answers(topic=("math", 1.0), formalize=("informal", 0.55)), False, "formal"),  # not clearly informal
            (_answers(topic=("math", 1.0), formalize=("formal", 1.0), task=("analyze", 0.9)), True, "analyze"),
            (_answers(topic=("math", 1.0), formalize=("formal", 1.0), task=("readability", 0.8)), True, "readability"),
            (_answers(topic=("math", 1.0), formalize=("informal", 1.0), task=("prove", 0.9)), True, "informal"),
            (_answers(topic=("math", 1.0), formalize=("formal", 1.0), task=("readability", 0.4)), True, "analyze"),
            ({}, True, "analyze"),
            ({}, False, "formal"),
        ]
        for reply, source, want in cases:
            rated, _ = self.rate(reply, source=source)
            self.assertEqual((rated["math"], rated["mode"]), (True, want), (reply, source))

    def test_only_a_clear_not_math_is_refused(self) -> None:
        rated, _ = self.rate(_answers(topic=("not_math", 0.97), formalize=("informal", 1.0)), text="Write a poem.")
        self.assertEqual((rated["math"], rated["mode"]), (False, ""))
        self.assertIn("Ansätze answers mathematics only", rated["reason"])
        rated, _ = self.rate(_answers(topic=("not_math", 0.7), formalize=("formal", 1.0)), text="Write a poem.")
        self.assertTrue(rated["math"])
        for failure in (urllib.error.HTTPError("u", 500, "x", {}, io.BytesIO(b"")), TimeoutError("slow")):
            opener = _Opener(failure)
            with patch("urllib.request.build_opener", return_value=opener):
                rated = easy_route.rate_request("Write a poem.", has_source=False)
            self.assertEqual((rated["math"], rated["mode"]), (True, "formal"))

    def test_topic_refusal(self) -> None:
        def refusal(reply, text="Tell me a joke.", **kw):
            with patch("urllib.request.build_opener", return_value=_Opener(_reply(reply))):
                return easy_route.topic_refusal(text, **kw)

        self.assertIn("mathematics only", refusal(_answers(topic=("not_math", 0.95))))
        self.assertEqual(refusal(_answers(topic=("not_math", 0.6))), "")
        self.assertEqual(refusal(_answers(topic=("math", 1.0)), text="keep trying!", problem="Prove RH."), "")
        self.assertEqual(refusal({}), "")
        self.assertEqual(refusal(_answers(topic=("not_math", 1.0)), text="   "), "")  # nothing to rate
        with patch.dict(os.environ, {easy_route.SWITCH_ENV: "0"}):
            self.assertEqual(refusal(_answers(topic=("not_math", 1.0))), "")
        with patch("urllib.request.build_opener", return_value=_Opener(_reply(_answers(topic=("math", 1.0))))) as built:
            easy_route.topic_refusal("Explain step 3.", problem="Prove RH.")
        sent = json.loads(built.return_value.requests[0][0].data)
        self.assertEqual(sent["state"], ["ORIGINAL PROBLEM OF THE SESSION:\nProve RH.", "REQUEST (this turn):\nExplain step 3."])


class ServerAutoTest(unittest.TestCase):
    def test_refusal_is_for_accounts_and_guests_with_text_only(self) -> None:
        from agent_monitor import console_server

        with patch.dict(os.environ, {easy_route.KEY_ENV: KEY}), \
                patch.object(easy_route, "topic_refusal", return_value="no") as refuse:
            os.environ.pop(easy_route.SWITCH_ENV, None)
            self.assertEqual(console_server._not_math_refusal({"id": 1}, "Write a poem."), "no")
            self.assertEqual(console_server._not_math_refusal({"id": 2, "guest": True}, "Write a poem."), "no")
            self.assertEqual(console_server._not_math_refusal({"id": 1}, "   "), "")
            self.assertEqual(console_server._not_math_refusal({}, "x"), "")
            self.assertEqual(refuse.call_count, 2)
        with patch.dict(os.environ, {easy_route.KEY_ENV: ""}):
            self.assertEqual(console_server._not_math_refusal({"id": 1}, "Write a poem."), "")

    def test_the_auto_note(self) -> None:
        from agent_monitor import console_server

        notes = []
        with patch.object(console_server.job_manager, "_append_chat", side_effect=lambda rid, role, text: notes.append((rid, role, text))):
            console_server._note_auto_mode({"run_id": "r1"}, {"auto_mode": "formal"})
            console_server._note_auto_mode({"run_id": "r2"}, {"auto_mode": "bogus"})
            console_server._note_auto_mode({"run_id": "r3"}, {})
            console_server._note_auto_mode({}, {"auto_mode": "analyze"})
        self.assertEqual(notes, [("r1", "system", "Auto mode: Informal + formalization (chosen by typesafe/jev-1.13).")])

    def test_wiring(self) -> None:
        import inspect

        from agent_monitor import console_server

        source = inspect.getsource(console_server)
        self.assertIn('if path == "/api/route-request":', source)
        self.assertEqual(source.count("_note_auto_mode(job, body)"), 3)  # proof, Analyze, Readability starts
        self.assertIn('refusal = _not_math_refusal(user, body.get("problem_text"))', source)
        self.assertIn('refusal = _not_math_refusal(user, body.get("message"), run_id=run_id)', source)
        # The follow-up refusal comes before any run start is reserved.
        chat = source[source.index('refusal = _not_math_refusal(user, body.get("message")'):]
        self.assertLess(chat.index("refusal"), chat.index("_admit_member_message(user)"))


class GuestTest(unittest.TestCase):
    def test_a_guest_start_is_routed_when_plain_is_a_guest_engine(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(jobs, "RUNS_DIR", root / "runs"))
        self.enterContext(patch.dict(os.environ, {easy_route.KEY_ENV: KEY}))
        os.environ.pop(easy_route.SWITCH_ENV, None)
        calls = []

        def start_job(**kwargs):
            calls.append(kwargs)
            rid = f"{kwargs['engine']}_adhoc_{len(calls)}"
            (root / "runs" / "workspaces" / rid).mkdir(parents=True)
            return {"run_id": rid, "engine": kwargs["engine"]}

        self.enterContext(patch.object(jobs, "start_job", side_effect=start_job))
        self.enterContext(patch.object(jobs, "_append_chat"))
        guest = {"id": 9, "guest": True}
        from agent_monitor import console_server

        for allowed, want in ((frozenset({"kimi", "plain"}), "plain"), (frozenset({"kimi"}), "kimi")):
            calls.clear()
            with patch.object(console_server, "guest_engine_allowlist", return_value=allowed), \
                    patch.object(easy_route, "judge", return_value={"verdict": "easy", "reason": "", "model": "m", "error": ""}):
                job = jobs.start_job_routed(engine="kimi", problem_text="What is 2+3?", model="kimi-k3",
                                            auth_route="api_key", user=guest, max_output_tokens=4096)
            self.assertEqual(job["engine"], want)
            self.assertEqual(calls[-1]["max_output_tokens"], 4096)  # the guest's limits go with it

    def test_a_guests_auto_ratings_are_bounded(self) -> None:
        from agent_monitor import console_server

        console_server._GUEST_RATINGS.clear()
        guest = {"id": 4242, "guest": True}
        with patch.object(console_server, "GUEST_RATINGS_PER_HOUR", 3):
            self.assertEqual([console_server._guest_rating_allowed(guest) for _ in range(5)], [True, True, True, False, False])
            other = {"id": 4243, "guest": True}
            self.assertTrue(console_server._guest_rating_allowed(other))  # per guest
            with patch.object(console_server.time, "time", return_value=__import__("time").time() + 3601):
                self.assertTrue(console_server._guest_rating_allowed(guest))  # an hour later
        self.assertFalse(console_server._guest_rating_allowed({"guest": True}))  # no id
        console_server._GUEST_RATINGS.clear()


class ReadableLinkTest(unittest.TestCase):
    def test_a_readable_version_is_linked_to_its_proof_run_by_its_owner_only(self) -> None:
        from agent_monitor import console_server

        notes = []

        handler_cls = next(v for v in vars(console_server).values() if isinstance(v, type) and hasattr(v, "_link_readable_version"))
        handler = handler_cls.__new__(handler_cls)
        owners = {"p1": True, "p2": False}
        handler._is_run_owner = lambda run_id, user: owners.get(run_id, False)
        with patch.object(console_server.job_manager, "_append_chat", side_effect=lambda rid, role, text: notes.append((rid, role, text))):
            handler._link_readable_version({"run_id": "readability_r1"}, {"parent_run_id": "p1"}, {"id": 1})
            handler._link_readable_version({"run_id": "readability_r2"}, {"parent_run_id": "p2"}, {"id": 1})
            handler._link_readable_version({"run_id": "readability_r3"}, {"parent_run_id": "../x"}, {"id": 1})
            handler._link_readable_version({"run_id": "readability_r4"}, {}, {"id": 1})
        self.assertEqual(notes, [("p1", "system", "Readable version: readability_r1")])
