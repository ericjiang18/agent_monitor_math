"""Fixes from a real-model bug hunt of the console: routing words never
reach the engine, Lean only when asked, a manuscript Jev cannot read is never
refused, Continue after a Plain answer resumes Ansatz CGM, the CGM graph after
a Plain answer, no duplicate turn copies, off stages stay off, and Plain's
audit line only when an audit is asked for."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import auto_pipeline, console_server, easy_route, engine_graph, jobs, proof_turns
from agent_monitor.runners import plain_runner

KEY = "sk-or-v1-" + "0" * 64


class StripDirectiveTest(unittest.TestCase):
    def test_the_words_that_ask_for_ansatz_cgm_are_not_part_of_the_problem(self) -> None:
        cases = {
            "Now prove that n^3 - n is divisible by 6 for every integer n. use Ansatz CGM":
                "Now prove that n^3 - n is divisible by 6 for every integer n.",
            "Use Ansatz CGM: prove that 2+2=4.": "prove that 2+2=4.",
            "Using Ansatz CGM, prove that there are infinitely many primes.":
                "prove that there are infinitely many primes.",
            "Prove the lemma. Run it on Ansatz CGM again.": "Prove the lemma.",
            "Ansatz CGM — prove that sqrt 2 is irrational": "prove that sqrt 2 is irrational",
            "Prove that x - y = -(y - x). use Ansatz CGM": "Prove that x - y = -(y - x).",
        }
        for text, want in cases.items():
            self.assertEqual(easy_route.strip_cgm_directive(text), want, text)
            self.assertTrue(easy_route.asks_for_cgm(text), text)

    def test_mathematics_and_a_bare_directive_stay_as_they_are(self) -> None:
        # A bare "CGM" may be mathematics (the conjugate gradient method); a message
        # that is only the directive keeps its words.
        for text in (
            "Explain the CGM inequality in step 3.",
            "Prove the convergence of the iterates with CGM for symmetric positive definite A.",
            "Bound the error of the approximation using CGM after k steps.",
            "Show the estimate holds on the CGM subspace.",
            "Use CGM to solve Ax = b and bound the residual.",
            "use Ansatz CGM", "Use Ansatz CGM on this.", "please use CGM instead", "Use CGM.",
            "Run it with CGM please.", "",
        ):
            self.assertEqual(easy_route.strip_cgm_directive(text), text.strip(), text)

    def test_a_bare_cgm_directive_is_stripped_only_as_a_sentence_of_its_own(self) -> None:
        self.assertEqual(easy_route.strip_cgm_directive("Prove X. Use CGM."), "Prove X.")
        self.assertEqual(easy_route.strip_cgm_directive("Prove X.\nUse CGM again"), "Prove X.")
        self.assertEqual(easy_route.strip_cgm_directive("Prove it again with the full harness please."), "Prove it again")
        self.assertEqual(easy_route.strip_cgm_directive("Prove the bound; run it on the full harness please."), "Prove the bound")
        # Text the directive never touched keeps its own ending.
        self.assertEqual(easy_route.strip_cgm_directive("Prove the following:"), "Prove the following:")


class FormalizationTest(unittest.TestCase):
    def test_lean_only_when_asked_and_not_turned_down(self) -> None:
        cases = {
            "Prove in Lean 4 that n + 0 = n.": True,
            "prove in lean4 that n+0=n": True,
            "Formalize this in Mathlib.": True,
            "Please formalise the proof": True,
            "what is the LEAN theorem prover": True,
            "Give an informal proof (no Lean), then formalize the key lemma in Lean": True,
            "Prove that sqrt 2 is irrational. Do not use Lean.": False,
            "Don't formalize, just prove it": False,
            "without Lean please, prove x": False,
            "prove it, no formalization needed": False,
            "I lean towards induction; prove 1+...+n = n(n+1)/2": False,
            "A lean proof of the lemma": False,
        }
        for text, want in cases.items():
            self.assertEqual(easy_route.asks_for_formalization(text), want, text)


class ManuscriptRatingTest(unittest.TestCase):
    """rate_request: a request whose manuscript the judge cannot read is never refused."""

    def rate(self, text: str, **kw):
        not_math = {"answers": {easy_route.TOPIC: {"choice": "not_math", "probabilities": {"not_math": 0.99}},
                                "task": {"choice": "readability", "probabilities": {"readability": 0.9}}}}
        with patch.dict(os.environ, {easy_route.KEY_ENV: KEY}), \
                patch.object(easy_route, "_decide", return_value=(not_math, "", 0.1)):
            return easy_route.rate_request(text, **kw)

    def test_files_or_a_link_are_never_refused_pasted_text_and_no_source_are(self) -> None:
        self.assertEqual(self.rate("Make this more readable", has_source=True)["mode"], "readability")
        self.assertTrue(self.rate("", has_source=True)["math"])
        self.assertFalse(self.rate("Make this more readable", has_source=True, source_seen=True)["math"])
        self.assertFalse(self.rate("Write a poem.", has_source=False)["math"])

    def test_pasted_text_is_a_manuscript_only_when_the_judge_is_fairly_sure(self) -> None:
        def rated(task, p):
            answers = {"answers": {easy_route.TOPIC: {"choice": "math", "probabilities": {"math": 1.0}},
                                   "task": {"choice": task, "probabilities": {task: p}}}}
            with patch.dict(os.environ, {easy_route.KEY_ENV: KEY}), \
                    patch.object(easy_route, "_decide", return_value=(answers, "", 0.1)) as decide:
                out = easy_route.rate_request("Claim 1. ...", has_source=True, source_seen=True)
            return out["mode"], decide.call_args[0][1]["task"]["instructions"]
        mode, instructions = rated("readability", 0.9)
        self.assertEqual(mode, "readability")
        self.assertIn("or simply a long problem to prove", instructions)
        self.assertEqual(rated("analyze", 0.55)[0], "informal")
        self.assertEqual(rated("prove", 0.9)[0], "informal")
        # The judge failing: a proof (pasted text is no manuscript by default).
        with patch.dict(os.environ, {easy_route.KEY_ENV: KEY}), \
                patch.object(easy_route, "_decide", return_value=(None, "timeout", 6.0)):
            self.assertEqual(easy_route.rate_request("Claim 1. ...", has_source=True, source_seen=True)["mode"], "informal")
            self.assertEqual(easy_route.rate_request("", has_source=True)["mode"], "analyze")

    def test_the_page_names_the_source_kind(self) -> None:
        import inspect

        self.assertIn('source_seen=str(body.get("source") or "") == "text"', inspect.getsource(console_server))


class EngineReadsTest(unittest.TestCase):
    """What the engine reads: problem.txt and a follow-up's prompt, without the directive."""

    def setUp(self) -> None:
        self.ws = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_problem_txt_and_problems_json_leave_the_directive_out(self) -> None:
        jobs._write_session_problems(self.ws, [
            {"text": "Prove that 2+2=4.", "source": "initial"},
            {"text": "Now prove that 3+3=6. use Ansatz CGM", "source": "human"},
        ])
        self.assertNotIn("Ansatz CGM", (self.ws / "problem.txt").read_text(encoding="utf-8"))
        self.assertEqual(json.loads((self.ws / "problems.json").read_text(encoding="utf-8"))[1]["text"],
                         "Now prove that 3+3=6.")

    def test_a_follow_ups_prompt_and_a_start_leave_it_out(self) -> None:
        import inspect

        follow = inspect.getsource(jobs._execute_continue)
        self.assertIn("message_for_engine = easy_route.strip_cgm_directive(message)", follow)
        self.assertIn("HUMAN FEEDBACK — address this now:\\n{message_for_engine}", follow)
        self.assertIn("message=message_for_engine,", follow)
        self.assertNotIn("HUMAN FEEDBACK — address this now:\\n{message}\\n", follow)
        start = inspect.getsource(jobs.start_job_routed)
        self.assertIn('kwargs = {**kwargs, "problem_text": easy_route.strip_cgm_directive(text)}', start)

    def test_a_start_that_asks_for_ansatz_cgm_runs_without_the_words(self) -> None:
        seen = {}
        with patch.dict(os.environ, {easy_route.KEY_ENV: KEY}), \
                patch.object(jobs, "start_job", side_effect=lambda **kw: seen.update(kw) or {"run_id": ""}):
            jobs.start_job_routed(engine="math_harness", run_kind="prove", model="gpt-6.1-sol", auth_route="codex_subscription",
                                  problem_text="Prove that 1+1=2. Use Ansatz CGM.", user={"id": 1, "is_admin": True})
        self.assertEqual((seen["engine"], seen["problem_text"]), ("math_harness", "Prove that 1+1=2."))


class ContinueAfterAnswerTest(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(jobs, "RUNS_DIR", root / "runs"))
        self.enterContext(patch.object(jobs, "CACHE_DIR", root / "cache"))
        self.enterContext(patch.dict(os.environ, {easy_route.KEY_ENV: KEY}))
        os.environ.pop(easy_route.SWITCH_ENV, None)
        self.run_id = "math_harness_adhoc_cont"
        self.ws = root / "runs" / "workspaces" / self.run_id
        self.ws.mkdir(parents=True)
        (self.ws / "proof.md").write_text("# Claim\n\nThe proof Ansatz CGM wrote.\n", encoding="utf-8")
        jobs._write_run({"run_id": self.run_id, "engine": "plain", "status": "finished",
                         "problem_text": "Prove it.", "problems": [{"text": "Prove it.", "source": "initial"}],
                         "agents": [], "edges": []})
        for engine in ("math_harness", "plain"):
            easy_route.note_turn(self.ws, home="math_harness", engine=engine, verdict=None, why="judged")
        self.enterContext(patch.object(jobs, "_append_chat"))

    def route(self, message, **kw):
        return jobs._easy_route_follow_up(self.run_id, jobs._load_run_record(self.run_id), self.ws,
                                          message=message, user={"id": 1, "is_admin": True}, model="gpt-6.1-sol", **kw)

    def test_a_bare_continue_after_a_plain_answer_goes_on_on_ansatz_cgm(self) -> None:
        self.assertEqual(self.route(None)["engine"], "math_harness")
        self.assertEqual(easy_route.load(self.ws)["turns"][-1]["why"], "continue")

    def test_files_or_a_plain_written_proof_keep_plain(self) -> None:
        self.assertEqual(self.route(None, has_files=True)["engine"], "plain")
        # A run Plain alone has worked on: Plain goes on with its own proof.
        (self.ws / easy_route.STATE_FILE).unlink()
        easy_route.note_turn(self.ws, home="math_harness", engine="plain", verdict=None, why="judged")
        self.assertEqual(self.route(None)["engine"], "plain")


class GraphAfterAnswerTest(unittest.TestCase):
    def test_a_plain_answer_keeps_the_ansatz_cgm_graph_once_ansatz_cgm_ran(self) -> None:
        ws = Path(self.enterContext(tempfile.TemporaryDirectory()))
        record = {"run_id": "r", "engine": "plain", "status": "finished", "agents": []}
        seen = []
        with patch.object(engine_graph, "_math_harness_graph", side_effect=lambda w: seen.append("cgm") or {"graph": {"nodes": []}}), \
                patch.object(engine_graph, "_math_harness_workflow_graph", return_value=None), \
                patch.object(engine_graph, "_execution_graph", side_effect=lambda r: seen.append("trace") or {"trace": True}):
            easy_route.note_turn(ws, home="math_harness", engine="plain", verdict=None, why="judged")
            engine_graph.build(ws, record, view="facts")
            self.assertEqual(seen, ["trace"])  # Ansatz CGM never ran here: Plain's own trace
            receipt = ws / "math_harness" / "project" / "engine_receipt.json"
            receipt.parent.mkdir(parents=True)
            receipt.write_text("{}", encoding="utf-8")
            seen.clear()
            engine_graph.build(ws, record, view="facts")
            self.assertEqual(seen[0], "cgm")


class NoDuplicateTurnCopyTest(unittest.TestCase):
    def test_a_turn_that_left_the_proof_as_it_was_keeps_no_copy(self) -> None:
        ws = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (ws / "proof.md").write_text("# Proof one\n", encoding="utf-8")
        n = proof_turns.begin(ws, started_at="t1")
        proof_turns.end(ws, n, status="finished", finished_at="t2")
        m = proof_turns.begin(ws, started_at="t3")
        proof_turns.end(ws, m, status="stopped", finished_at="t4")
        turns = proof_turns.summary(ws)
        self.assertEqual([(t["file"], t["status"]) for t in turns], [(".proof_turns/1.md", "finished"), ("", "stopped")])
        self.assertFalse((ws / ".proof_turns" / "2.md").exists())
        # A new proof is kept again.
        (ws / "proof.md").write_text("# Proof three\n", encoding="utf-8")
        k = proof_turns.begin(ws, started_at="t5")
        proof_turns.end(ws, k, status="finished", finished_at="t6")
        self.assertEqual(proof_turns.summary(ws)[-1]["file"], ".proof_turns/3.md")

    def test_an_answer_is_always_kept(self) -> None:
        ws = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (ws / "proof.md").write_text("Same.\n", encoding="utf-8")
        proof_turns.end(ws, proof_turns.begin(ws, started_at="t1"), status="finished", finished_at="t2")
        n = proof_turns.begin(ws, started_at="t3", answer_only=True)
        (ws / proof_turns.ANSWER_FILE).write_text("Same.\n", encoding="utf-8")
        proof_turns.end(ws, n, status="finished", finished_at="t4")
        self.assertEqual(proof_turns.summary(ws)[-1]["file"], ".proof_turns/2.md")


class TurnCopiesNeverFollowLinksTest(unittest.TestCase):
    def test_a_symlink_planted_in_the_turns_folder_is_replaced_not_written_through(self) -> None:
        ws = Path(self.enterContext(tempfile.TemporaryDirectory()))
        outside = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (ws / ".proof_turns").mkdir()
        for name in ("1.md", "index.json", "index.json.tmp"):
            (outside / name).write_text("untouched", encoding="utf-8")
            (ws / ".proof_turns" / name).symlink_to(outside / name)
        (ws / "proof.md").write_text("# Proof\n", encoding="utf-8")
        n = proof_turns.begin(ws, started_at="t1")
        proof_turns.end(ws, n, status="finished", finished_at="t2")
        for name in ("1.md", "index.json", "index.json.tmp"):
            self.assertEqual((outside / name).read_text(encoding="utf-8"), "untouched", name)
        self.assertFalse((ws / ".proof_turns" / "1.md").is_symlink())
        self.assertEqual((ws / ".proof_turns" / "1.md").read_text(encoding="utf-8"), "# Proof\n")
        self.assertEqual([t["n"] for t in proof_turns.summary(ws)], [1])
        self.assertEqual([p.name for p in (ws / ".proof_turns").iterdir() if p.name.startswith(".tmp-")], [])


class FailedTurnPipelineTest(unittest.TestCase):
    """A failed follow-up that left the proof as it was derives nothing again."""

    def setUp(self) -> None:
        self.run_id = "kimi_failed_follow_up"
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(jobs, "RUNS_DIR", root / "runs"))
        self.enterContext(patch.object(jobs, "CACHE_DIR", root / "cache"))
        self.ws = root / "runs" / "workspaces" / self.run_id
        self.ws.mkdir(parents=True)
        # At least 40 bytes: a proof the pipeline would derive (jobs._root_proof_artifact).
        (self.ws / "proof.md").write_text("# Proof\n\nTurn one: the whole argument, line by line.\n", encoding="utf-8")
        self.addCleanup(jobs._STOP_EVENTS.pop, self.run_id, None)
        jobs._stop_event(self.run_id).clear()
        jobs._write_run({"run_id": self.run_id, "engine": "kimi", "problem_id": "adhoc", "status": "finished",
                         "problem_text": "Prove it.", "problems": [{"text": "Prove it.", "source": "initial"}],
                         "agents": [{"trace_id": f"{self.run_id}::turn-1", "output": "first", "status": "finished"}],
                         "edges": []})

    def follow_up(self, write: str | None) -> int:
        job_id = "job_failed_turn"
        jobs._JOBS[job_id] = {"job_id": job_id, "run_id": self.run_id, "status": "running", "engine": "kimi"}
        self.addCleanup(jobs._JOBS.pop, job_id, None)

        def engine(*args, **kwargs):
            if write is not None:
                (self.ws / "proof.md").write_text(write, encoding="utf-8")
            return {"run_id": self.run_id, "status": "failed", "error": "provider call failed", "edges": [],
                    "totals": {}, "agents": [{"trace_id": f"{self.run_id}::turn-2", "output": "", "status": "failed"}]}

        with patch.object(jobs, "_run_cli_engine_with_openclaude_retry", side_effect=engine), \
                patch.object(jobs, "_launch_pending_feedback"), patch.object(auto_pipeline, "start") as start:
            jobs._execute_continue(job_id=job_id, run_id=self.run_id, engine="kimi", message="keep trying!",
                                   problem_text="Prove it.", model="kimi-k3", max_iterations=2, workspace=str(self.ws))
        return start.call_count

    def test_no_pass_when_the_failed_turn_changed_nothing(self) -> None:
        self.assertEqual(self.follow_up(None), 0)

    def test_a_pass_when_it_changed_the_proof(self) -> None:
        self.assertEqual(self.follow_up("# Proof\n\nA partial turn two, cut short by the provider.\n"), 1)


class OldTuningTest(unittest.TestCase):
    def test_a_profile_the_model_no_longer_offers_never_blocks_a_follow_up(self) -> None:
        import inspect

        source = inspect.getsource(jobs._continuation_auth_environment)
        self.assertIn("tuning = apply_recorded_tuning(configured, run_data) or {}\n    except ValueError:", source)
        from agent_monitor import run_tuning

        with self.assertRaises(ValueError):
            run_tuning.from_record({}, {"model": "kimi-k3", "auth_route": "sponsored_kimi", "reasoning_effort": "high"})


class BackfilledProblemTimeTest(unittest.TestCase):
    def test_a_backfilled_problem_is_timed_by_the_run(self) -> None:
        problems = jobs.ensure_problems({"problem_text": "Analyze this.", "created_at": "2026-10-16T02:20:00+00:00"})
        self.assertEqual(problems[0]["ts"], "2026-10-16T02:20:00+00:00")


class StaleKeepsOffTest(unittest.TestCase):
    def test_an_edit_marks_only_the_stages_that_ran_stale(self) -> None:
        ws = Path(self.enterContext(tempfile.TemporaryDirectory()))
        auto_pipeline._write_state(ws, {"status": "done", "stages": {
            "informal_dag": {"status": "done"}, "lean": {"status": "off"},
            "formal_dag": {"status": "off"}, "coverage": {"status": "off"}}})
        auto_pipeline.mark_stale(ws)
        stages = auto_pipeline.load_state(ws)["stages"]
        self.assertEqual({k: v["status"] for k, v in stages.items()},
                         {"informal_dag": "stale", "lean": "off", "formal_dag": "off", "coverage": "off"})


class PlainAuditLineTest(unittest.TestCase):
    def test_only_an_audit_request_adds_the_line(self) -> None:
        proof = "# Result\n\n2 + 2 = 4."
        listing = "Skills: _agent/skills/research-proof-audit/SKILL.md, _agent/skills/x/SKILL.md"
        self.assertEqual(plain_runner._ensure_audit_disclosure(listing, proof), proof)
        self.assertIn("degraded audit:", plain_runner._ensure_audit_disclosure("CANDIDATE AUDIT GATE: audit it", proof))


class FileListRaceTest(unittest.TestCase):
    def test_a_file_that_goes_while_it_is_listed_is_left_out(self) -> None:
        ws = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (ws / "proof.md").write_text("x", encoding="utf-8")
        (ws / "gone.md").write_text("y", encoding="utf-8")
        real_stat, real_is_file = Path.stat, Path.is_file

        def stat(path, *a, **kw):
            # Gone between the walk and the listing's own stat (an lstat still answers).
            if path.name == "gone.md" and kw.get("follow_symlinks", True):
                raise FileNotFoundError(path)
            return real_stat(path, *a, **kw)

        def is_file(path, *a, **kw):
            return True if path.name == "gone.md" else real_is_file(path, *a, **kw)

        with patch.object(Path, "stat", stat), patch.object(Path, "is_file", is_file):
            listed = console_server._list_workspace_files(ws)
        self.assertEqual([f["path"] for f in listed], ["proof.md"])

    def test_a_workspace_that_goes_while_it_is_walked_lists_nothing(self) -> None:
        ws = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with patch.object(Path, "rglob", side_effect=FileNotFoundError("gone")):
            self.assertEqual(console_server._list_workspace_files(ws), [])


if __name__ == "__main__":
    unittest.main()
