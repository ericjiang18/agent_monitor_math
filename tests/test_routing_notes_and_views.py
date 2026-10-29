"""The chat a page reads never has the routing notes; the run's pipeline state
says whether its last pass took up the proof the run has now."""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import console_server, easy_route, jobs


class RoutingNotesTest(unittest.TestCase):
    def setUp(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(jobs, "RUNS_DIR", root / "runs"))
        self.enterContext(patch.object(jobs, "CACHE_DIR", root / "cache"))
        self.run_id = "plain_adhoc_notes"
        (root / "runs" / "workspaces" / self.run_id).mkdir(parents=True)
        jobs._write_run({"run_id": self.run_id, "engine": "plain", "status": "finished", "problem_id": "adhoc"})

    def test_the_notes_stay_in_the_chat_file_and_never_reach_a_page(self) -> None:
        verdict = {"verdict": "easy", "reason": "easy with probability 0.95, confidence 0.90", "model": "typesafe/jev-1.13"}
        jobs._append_chat(self.run_id, "user", "What is 1+1?")
        jobs._append_chat(self.run_id, "system", easy_route.plain_note(verdict))
        jobs._append_chat(self.run_id, "system", "Auto mode: Informal only (chosen by typesafe/jev-1.13).")
        jobs._append_chat(self.run_id, "system", easy_route.cgm_note("judged", verdict))
        jobs._append_chat(self.run_id, "system", easy_route.cgm_note("asked"))
        jobs._append_chat(self.run_id, "system", "Continuing proof with your feedback…")
        jobs._append_chat(self.run_id, "assistant", "1 + 1 = 2.")
        # A user may write anything, a note's words too: only the system's notes are left out.
        jobs._append_chat(self.run_id, "user", "Auto mode: please prove it")
        shown = [(m["role"], m["content"]) for m in jobs.list_chat(self.run_id)]
        self.assertEqual(shown, [
            ("user", "What is 1+1?"),
            ("system", "Continuing proof with your feedback…"),
            ("assistant", "1 + 1 = 2."),
            ("user", "Auto mode: please prove it"),
        ])
        # The operator still has them in the run's chat file.
        kept = [json.loads(line)["content"] for line in jobs._chat_file(self.run_id).read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(kept), 8)
        self.assertTrue(kept[1].startswith("Easy request: this turn runs on Plain"))
        self.assertTrue(kept[3].startswith("This turn runs on Ansatz CGM again"))

    def test_every_note_the_server_writes_is_one_it_leaves_out(self) -> None:
        verdict = {"verdict": "easy", "reason": "", "model": "typesafe/jev-1.13"}
        for note in (easy_route.plain_note(verdict), easy_route.cgm_note("judged", verdict),
                     easy_route.cgm_note("judged"), easy_route.cgm_note("asked")):
            self.assertTrue(jobs._routing_note({"role": "system", "content": note}), note)
        for label in console_server._AUTO_MODE_LABELS.values():
            self.assertTrue(jobs._routing_note({"role": "system", "content": f"Auto mode: {label} (chosen by x)."}))
        self.assertFalse(jobs._routing_note({"role": "system", "content": "✓ run finished"}))
        self.assertFalse(jobs._routing_note("Auto mode: x"))


class ProofCurrentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.ws = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.record = {"run_id": "r", "engine": "kimi", "status": "running"}

    def _sig(self, text: str) -> str:
        return "proof.md:" + hashlib.sha256(text.encode()).hexdigest()

    def test_whether_the_last_pass_took_up_the_proof_there_now(self) -> None:
        (self.ws / "proof.md").write_text("# Turn 1\n\nProof one.\n", encoding="utf-8")
        state = {"status": "done", "source_signature": self._sig("# Turn 1\n\nProof one.\n"), "stages": {}}
        self.assertIs(console_server._with_proof_current(state, self.ws, self.record)["proof_current"], True)
        # Turn 2 rewrote the proof: the pass's views are of the earlier one.
        (self.ws / "proof.md").write_text("# Turn 2\n\nProof two.\n", encoding="utf-8")
        shown = console_server._with_proof_current(state, self.ws, self.record)
        self.assertIs(shown["proof_current"], False)
        # The stored state is not changed.
        self.assertNotIn("proof_current", state)

    def test_without_a_pass_source_the_state_is_as_it_was(self) -> None:
        (self.ws / "proof.md").write_text("x\n", encoding="utf-8")
        for state in (None, {}, {"status": "running", "stages": {}}, {"source_signature": ""}):
            self.assertEqual(console_server._with_proof_current(state, self.ws, self.record), state)

    def test_a_failure_to_read_the_source_leaves_the_state_alone(self) -> None:
        state = {"source_signature": "proof.md:abc"}
        from agent_monitor import auto_pipeline

        with patch.object(auto_pipeline, "_source_signature", side_effect=OSError("gone")):
            self.assertEqual(console_server._with_proof_current(state, self.ws, self.record), state)

    def test_the_run_record_carries_it(self) -> None:
        import inspect

        source = inspect.getsource(console_server)
        self.assertIn('data["auto_pipeline"] = _with_proof_current(', source)


if __name__ == "__main__":
    unittest.main()
