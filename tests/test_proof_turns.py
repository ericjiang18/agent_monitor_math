"""Each turn's proof is kept (proof_turns), for one proof card per turn."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import jobs, proof_turns


class ProofTurnsModuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_a_turn_keeps_its_proof_as_it_was_when_it_ended(self) -> None:
        (self.ws / "proof.md").write_text("# Claim\n\nFirst proof.\n", encoding="utf-8")
        n = proof_turns.begin(self.ws, started_at="2026-10-28T10:00:00+00:00")
        self.assertEqual(n, 1)
        self.assertEqual(proof_turns.summary(self.ws)[0]["status"], "running")
        self.assertIsNone(proof_turns.summary(self.ws)[0]["finished_at"])
        proof_turns.end(self.ws, n, status="finished", finished_at="2026-10-28T10:05:00+00:00")
        # The next turn rewrites proof.md; the first turn's copy stays.
        (self.ws / "proof.md").write_text("# Claim\n\nSecond proof.\n", encoding="utf-8")
        m = proof_turns.begin(self.ws, started_at="2026-10-28T10:06:00+00:00")
        proof_turns.end(self.ws, m, status="failed", finished_at="2026-10-28T10:09:00+00:00")
        turns = proof_turns.summary(self.ws)
        self.assertEqual([t["n"] for t in turns], [1, 2])
        self.assertEqual([t["status"] for t in turns], ["finished", "failed"])
        self.assertEqual([t["file"] for t in turns], [".proof_turns/1.md", ".proof_turns/2.md"])
        self.assertEqual((self.ws / ".proof_turns" / "1.md").read_text(encoding="utf-8"), "# Claim\n\nFirst proof.\n")
        self.assertEqual((self.ws / ".proof_turns" / "2.md").read_text(encoding="utf-8"), "# Claim\n\nSecond proof.\n")
        self.assertEqual(turns[0]["chars"], len("# Claim\n\nFirst proof.\n"))
        self.assertEqual(turns[0]["finished_at"], "2026-10-28T10:05:00+00:00")

    def test_a_turn_ends_once(self) -> None:
        (self.ws / "proof.md").write_text("A\n", encoding="utf-8")
        n = proof_turns.begin(self.ws, started_at="t1")
        proof_turns.end(self.ws, n, status="finished", finished_at="t2")
        (self.ws / "proof.md").write_text("B\n", encoding="utf-8")
        proof_turns.end(self.ws, n, status="failed", finished_at="t3")
        turn = proof_turns.summary(self.ws)[0]
        self.assertEqual((turn["status"], turn["finished_at"]), ("finished", "t2"))
        self.assertEqual((self.ws / ".proof_turns" / "1.md").read_text(encoding="utf-8"), "A\n")

    def test_a_turn_without_a_proof_keeps_none_and_latex_is_kept_as_latex(self) -> None:
        n = proof_turns.begin(self.ws, started_at="t1")
        proof_turns.end(self.ws, n, status="failed", finished_at="t2")
        self.assertEqual(proof_turns.summary(self.ws)[0]["file"], "")
        (self.ws / "proof.md").write_text("   \n", encoding="utf-8")
        (self.ws / "proof.tex").write_text("\\begin{proof}x\\end{proof}", encoding="utf-8")
        m = proof_turns.begin(self.ws, started_at="t3")
        proof_turns.end(self.ws, m, status="finished", finished_at="t4")
        self.assertEqual(proof_turns.summary(self.ws)[1]["file"], ".proof_turns/2.tex")

    def test_a_turn_left_running_by_a_restart_is_interrupted_when_the_next_starts(self) -> None:
        proof_turns.begin(self.ws, started_at="t1")
        proof_turns.begin(self.ws, started_at="t2")
        turns = proof_turns.summary(self.ws)
        self.assertEqual([(t["n"], t["status"], t["finished_at"]) for t in turns], [(1, "interrupted", "t2"), (2, "running", None)])

    def test_no_workspace_or_a_bad_index_records_nothing_and_breaks_nothing(self) -> None:
        self.assertIsNone(proof_turns.begin(self.ws / "missing", started_at="t"))
        proof_turns.end(self.ws / "missing", 1, status="finished", finished_at="t")
        self.assertEqual(proof_turns.summary(None), [])
        (self.ws / ".proof_turns").mkdir()
        (self.ws / ".proof_turns" / "index.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(proof_turns.summary(self.ws), [])
        self.assertEqual(proof_turns.begin(self.ws, started_at="t"), 1)

    def test_the_index_keeps_the_latest_turns_only(self) -> None:
        with patch.object(proof_turns, "MAX_TURNS", 3):
            for i in range(5):
                n = proof_turns.begin(self.ws, started_at=f"s{i}")
                proof_turns.end(self.ws, n, status="finished", finished_at=f"f{i}")
        self.assertEqual([t["n"] for t in proof_turns.summary(self.ws)], [3, 4, 5])

    def test_only_the_part_kept_of_a_long_proof_is_read(self) -> None:
        # A runaway proof.md is never loaded whole: only the prefix kept is
        # read, and a character cut by that prefix never reaches the copy.
        text = ("é" * 7 + "😀" * 5 + "x") * 400
        (self.ws / "proof.md").write_text(text, encoding="utf-8")
        real_read_text = Path.read_text

        def read_text(path, *args, **kwargs):
            if path.name == "proof.md":
                raise AssertionError("proof.md was read whole")
            return real_read_text(path, *args, **kwargs)

        with patch.object(proof_turns, "MAX_CHARS", 101), patch.object(Path, "read_text", read_text):
            n = proof_turns.begin(self.ws, started_at="t1")
            proof_turns.end(self.ws, n, status="finished", finished_at="t2")
        kept = (self.ws / ".proof_turns" / "1.md").read_text(encoding="utf-8")
        self.assertEqual(kept, text[:101])
        self.assertEqual(proof_turns.summary(self.ws)[0]["chars"], 101)

    def test_a_symlinked_folder_is_never_written_through(self) -> None:
        outside = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (self.ws / ".proof_turns").symlink_to(outside, target_is_directory=True)
        self.assertIsNone(proof_turns.begin(self.ws, started_at="t"))
        self.assertEqual(list(outside.iterdir()), [])

    def test_the_folder_is_hidden_from_the_run_file_list(self) -> None:
        from agent_monitor import console_server

        (self.ws / "proof.md").write_text("A\n", encoding="utf-8")
        n = proof_turns.begin(self.ws, started_at="t1")
        proof_turns.end(self.ws, n, status="finished", finished_at="t2")
        paths = [f["path"] for f in console_server._list_workspace_files(self.ws)]
        self.assertEqual(paths, ["proof.md"])
        # ...but served by the workspace file endpoint, which the card reads.
        self.assertIsNotNone(console_server._workspace_target(self.ws, ".proof_turns/1.md"))


class EarlierProofTest(unittest.TestCase):
    """A follow-up on a run with no turn recorded keeps the proof it has as turn 1."""

    def setUp(self) -> None:
        self.ws = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def _proof(self, text: str, when: str, name: str = "proof.md") -> None:
        import os
        from datetime import datetime

        (self.ws / name).write_text(text, encoding="utf-8")
        stamp = datetime.fromisoformat(when).timestamp()
        os.utime(self.ws / name, (stamp, stamp))

    def test_the_proof_an_older_run_has_is_turn_one_and_the_follow_up_turn_two(self) -> None:
        self._proof("# Even\n\nThe sum of two even integers is even.\n", "2026-09-27T20:26:20+00:00")
        n = proof_turns.begin(self.ws, started_at="2026-10-29T06:14:24.510000+00:00", follow_up=True)
        self.assertEqual(n, 2)
        earlier, live = proof_turns.summary(self.ws)
        self.assertEqual(
            (earlier["n"], earlier["status"], earlier["earlier"], earlier["file"], earlier["chars"]),
            (1, "finished", True, ".proof_turns/1.md", len("# Even\n\nThe sum of two even integers is even.\n")),
        )
        # Its time is when that proof was written: the card goes before the follow-up.
        self.assertEqual(earlier["started_at"], "2026-09-27T20:26:20+00:00")
        self.assertEqual(earlier["finished_at"], "2026-09-27T20:26:20+00:00")
        self.assertEqual((self.ws / ".proof_turns" / "1.md").read_text(encoding="utf-8"), "# Even\n\nThe sum of two even integers is even.\n")
        self.assertEqual((live["n"], live["status"], live["earlier"], live["finished_at"]), (2, "running", False, None))
        # The follow-up's own proof is kept when it ends; the earlier one stays.
        (self.ws / "proof.md").write_text("# P vs NP\n\nOpen.\n", encoding="utf-8")
        proof_turns.end(self.ws, n, status="finished", finished_at="2026-10-29T06:15:45+00:00")
        self.assertEqual((self.ws / ".proof_turns" / "2.md").read_text(encoding="utf-8"), "# P vs NP\n\nOpen.\n")
        self.assertIn("sum of two even", (self.ws / ".proof_turns" / "1.md").read_text(encoding="utf-8"))
        # Later follow-ups number on.
        self.assertEqual(proof_turns.begin(self.ws, started_at="2026-10-29T07:00:00+00:00", follow_up=True), 3)
        self.assertEqual([t["n"] for t in proof_turns.summary(self.ws)], [1, 2, 3])

    def test_its_time_is_never_after_the_follow_up_starts(self) -> None:
        self._proof("A proof.\n", "2026-10-29T08:00:00+00:00")
        proof_turns.begin(self.ws, started_at="2026-10-29T07:00:00+00:00", follow_up=True)
        earlier = proof_turns.summary(self.ws)[0]
        self.assertEqual((earlier["started_at"], earlier["finished_at"]), ("2026-10-29T07:00:00+00:00",) * 2)

    def test_a_latex_proof_is_kept_as_latex(self) -> None:
        self._proof("\\begin{proof}x\\end{proof}", "2026-10-28T10:00:00+00:00", name="proof.tex")
        proof_turns.begin(self.ws, started_at="2026-10-29T07:00:00+00:00", follow_up=True)
        self.assertEqual(proof_turns.summary(self.ws)[0]["file"], ".proof_turns/1.tex")

    def test_an_answer_only_follow_up_keeps_the_proof_as_turn_one_too(self) -> None:
        self._proof("Kept proof.\n", "2026-10-28T10:00:00+00:00")
        n = proof_turns.begin(self.ws, started_at="2026-10-29T07:00:00+00:00", follow_up=True, answer_only=True)
        self.assertEqual(n, 2)
        turns = proof_turns.summary(self.ws)
        self.assertEqual([(t["n"], t["earlier"], t["answer_only"]) for t in turns], [(1, True, False), (2, False, True)])

    def test_nothing_more_is_kept_for_a_first_run_a_run_with_turns_or_no_proof(self) -> None:
        # A first run: proof.md there already (a run started from one) is not an earlier turn.
        self._proof("Seed.\n", "2026-10-28T10:00:00+00:00")
        self.assertEqual(proof_turns.begin(self.ws, started_at="2026-10-29T07:00:00+00:00"), 1)
        self.assertEqual([t["earlier"] for t in proof_turns.summary(self.ws)], [False])
        # Turns recorded already: a follow-up is just the next one.
        proof_turns.end(self.ws, 1, status="finished", finished_at="2026-10-29T07:01:00+00:00")
        self.assertEqual(proof_turns.begin(self.ws, started_at="2026-10-29T07:02:00+00:00", follow_up=True), 2)
        self.assertEqual([t["earlier"] for t in proof_turns.summary(self.ws)], [False, False])
        # No proof (blank, or missing): the follow-up is turn 1.
        other = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (other / "proof.md").write_text("  \n", encoding="utf-8")
        self.assertEqual(proof_turns.begin(other, started_at="2026-10-29T07:00:00+00:00", follow_up=True), 1)
        self.assertEqual(len(proof_turns.summary(other)), 1)

    def test_a_symlinked_proof_is_never_read(self) -> None:
        outside = Path(self.enterContext(tempfile.TemporaryDirectory())) / "secret.md"
        outside.write_text("secret\n", encoding="utf-8")
        (self.ws / "proof.md").symlink_to(outside)
        self.assertEqual(proof_turns.begin(self.ws, started_at="2026-10-29T07:00:00+00:00", follow_up=True), 1)
        self.assertFalse((self.ws / ".proof_turns" / "1.md").exists())


class ProofTurnsWorkerTest(unittest.TestCase):
    """A follow-up turn records itself, and ends before queued feedback starts."""

    def setUp(self) -> None:
        self.run_id = "kimi_proof_turns"
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(jobs, "RUNS_DIR", root / "runs"))
        self.enterContext(patch.object(jobs, "CACHE_DIR", root / "cache"))
        self.workspace = root / "runs" / "workspaces" / self.run_id
        self.workspace.mkdir(parents=True)
        (self.workspace / "proof.md").write_text("# Proof\n\nTurn one.\n", encoding="utf-8")
        self.addCleanup(jobs._STOP_EVENTS.pop, self.run_id, None)
        jobs._stop_event(self.run_id).clear()
        jobs._write_run({
            "run_id": self.run_id, "engine": "kimi", "problem_id": "adhoc", "status": "finished",
            "problem_text": "Prove it.", "problems": [{"text": "Prove it.", "source": "initial"}],
            "agents": [{"trace_id": f"{self.run_id}::turn-1", "output": "first", "status": "finished"}],
            "edges": [],
        })

    def _follow_up(self, engine, launched) -> None:
        from agent_monitor import auto_pipeline

        job_id = "job_turn_2"
        jobs._JOBS[job_id] = {"job_id": job_id, "run_id": self.run_id, "status": "running", "engine": "kimi"}
        self.addCleanup(jobs._JOBS.pop, job_id, None)
        with (
            patch.object(jobs, "_run_cli_engine_with_openclaude_retry", side_effect=engine),
            patch.object(jobs, "_launch_pending_feedback", side_effect=launched),
            patch.object(auto_pipeline, "start"),
        ):
            jobs._execute_continue(
                job_id=job_id, run_id=self.run_id, engine="kimi", message="keep trying!",
                problem_text="Prove it.", model="kimi-k3", max_iterations=2,
                workspace=str(self.workspace),
            )

    def test_a_follow_up_keeps_its_proof_and_ends_before_queued_feedback_starts(self) -> None:
        seen = {}

        def engine(*args, **kwargs):
            seen["during"] = proof_turns.summary(self.workspace)
            (self.workspace / "proof.md").write_text("# Proof\n\nTurn two.\n", encoding="utf-8")
            return {"run_id": self.run_id, "status": "finished", "edges": [], "totals": {},
                    "agents": [{"trace_id": f"{self.run_id}::turn-2", "output": "second", "status": "finished"}]}

        def launched(*args, **kwargs):
            seen["at_launch"] = proof_turns.summary(self.workspace)

        self._follow_up(engine, launched)
        # A run with a proof and no turn recorded (an older run): its proof is kept as turn 1 first.
        self.assertEqual([(t["n"], t["status"], t["earlier"]) for t in seen["during"]], [(1, "finished", True), (2, "running", False)])
        self.assertEqual([(t["n"], t["status"]) for t in seen["at_launch"]], [(1, "finished"), (2, "finished")])
        earlier, kept = proof_turns.summary(self.workspace)
        self.assertEqual((self.workspace / earlier["file"]).read_text(encoding="utf-8"), "# Proof\n\nTurn one.\n")
        self.assertTrue(kept["finished_at"])
        self.assertEqual((self.workspace / kept["file"]).read_text(encoding="utf-8"), (self.workspace / "proof.md").read_text(encoding="utf-8"))

    def test_a_failed_follow_up_is_recorded_failed(self) -> None:
        def engine(*args, **kwargs):
            raise RuntimeError("provider call failed")

        self._follow_up(engine, lambda *a, **k: None)
        # The run's earlier proof (turn 1), then the follow-up that failed.
        self.assertEqual([t["status"] for t in proof_turns.summary(self.workspace)], ["finished", "failed"])

    def test_an_error_while_keeping_the_turn_never_keeps_the_worker(self) -> None:
        # _proof_turn_end runs in the worker's finally just before _end_worker:
        # an error of any kind there must not escape, or the worker would stay
        # registered (the run left running, no Continue able to start).
        def engine(*args, **kwargs):
            return {"run_id": self.run_id, "status": "finished", "edges": [], "totals": {},
                    "agents": [{"trace_id": f"{self.run_id}::turn-2", "output": "second", "status": "finished"}]}

        launched = []
        with patch.object(proof_turns, "end", side_effect=RuntimeError("index unreadable")):
            self._follow_up(engine, lambda *a, **k: launched.append(True))
        self.assertEqual(launched, [True])
        self.assertNotIn("job_turn_2", jobs._WORKERS)
        self.assertEqual(jobs._load_run_record(self.run_id)["status"], "finished")

    def test_the_turn_hooks_never_raise(self) -> None:
        for error in (RuntimeError("x"), MemoryError(), RecursionError(), AttributeError("x"), KeyError("x")):
            with patch.object(proof_turns, "begin", side_effect=error):
                self.assertIsNone(jobs._proof_turn_begin(self.run_id, self.workspace))
            with patch.object(proof_turns, "end", side_effect=error):
                jobs._proof_turn_end(self.run_id, self.workspace, 1)
            with patch.object(jobs, "_load_run_record", side_effect=error):
                jobs._proof_turn_end(self.run_id, self.workspace, 1)

    def test_an_analysis_follow_up_records_no_turn(self) -> None:
        record = jobs._load_run_record(self.run_id)
        record["run_kind"] = "analyze"
        jobs._write_run(record)

        def engine(*args, **kwargs):
            return {"run_id": self.run_id, "status": "finished", "edges": [], "totals": {},
                    "agents": [{"trace_id": f"{self.run_id}::turn-2", "output": "second", "status": "finished"}]}

        self._follow_up(engine, lambda *a, **k: None)
        self.assertFalse((self.workspace / ".proof_turns").exists())

    def test_a_deleted_run_gets_no_turn_folder(self) -> None:
        with patch.object(jobs, "_run_deleted", return_value=True):
            self.assertIsNone(jobs._proof_turn_begin(self.run_id, self.workspace))
            jobs._proof_turn_end(self.run_id, self.workspace, 1)
        self.assertFalse((self.workspace / ".proof_turns").exists())


class ProofTurnsWiringTest(unittest.TestCase):
    def test_first_runs_and_follow_ups_both_record_their_turn_and_the_api_lists_them(self) -> None:
        import inspect

        from agent_monitor import console_server

        execute = inspect.getsource(jobs._execute_job)
        body = inspect.getsource(jobs._execute_job_body)
        follow = inspect.getsource(jobs._execute_continue)
        # The first run: only a proof run records turns; the body ends the turn before its queued feedback starts.
        self.assertIn('turn = _proof_turn_begin(run_id, ws) if run_kind == "prove" else None', execute)
        self.assertLess(body.index("_proof_turn_end(run_id, ws, turn)"), body.index("_launch_pending_feedback("))
        self.assertLess(follow.index("_proof_turn_end(run_id, ws, turn)"), follow.index("_launch_pending_feedback("))
        self.assertIn('data["proof_turns"] = proof_turns.summary(ws)', inspect.getsource(console_server))


if __name__ == "__main__":
    unittest.main()
