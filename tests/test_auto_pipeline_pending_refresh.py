from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_monitor import auto_pipeline


class AutoPipelinePendingRefreshTests(unittest.TestCase):
    def test_continuation_during_final_signature_starts_one_latest_refresh(self) -> None:
        first_final_started = threading.Event()
        release_first_final = threading.Event()
        second_finish = threading.Event()
        source_calls = 0
        wait_calls = 0
        batch_signatures: list[str] = []
        original_finish = auto_pipeline._finish

        def wait_for_source(*_args, **_kwargs):
            nonlocal wait_calls
            wait_calls += 1
            signature = "proof.md:old" if wait_calls == 1 else "proof.md:new"
            return signature, {"status": "finished", "engine": "math_harness"}

        def source_signature(*_args, **_kwargs):
            nonlocal source_calls
            source_calls += 1
            if source_calls == 1:
                first_final_started.set()
                self.assertTrue(release_first_final.wait(3))
                return "proof.md:old"
            return "proof.md:new"

        def run_batch(workspace, *_args, **_kwargs):
            state = auto_pipeline.load_state(workspace) or {}
            batch_signatures.append(str(state.get("source_signature") or ""))

        def finish(workspace):
            original_finish(workspace)
            if len(batch_signatures) >= 2:
                second_finish.set()

        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            key = str(workspace.resolve())
            try:
                with (
                    patch.object(auto_pipeline, "_wait_for_source", side_effect=wait_for_source),
                    patch.object(
                        auto_pipeline,
                        "_run_record",
                        return_value={"status": "finished", "engine": "math_harness"},
                    ),
                    patch.object(
                        auto_pipeline, "_source_signature", side_effect=source_signature
                    ),
                    patch.object(auto_pipeline, "_run_batch", side_effect=run_batch),
                    patch.object(auto_pipeline, "_finish", side_effect=finish),
                ):
                    auto_pipeline.start(
                        run_id="race-run",
                        workspace=workspace,
                        owner_id=7,
                        model="kimi-k3",
                        engine="math_harness",
                    )
                    self.assertTrue(first_final_started.wait(3))

                    queued = auto_pipeline.start(
                        run_id="race-run",
                        workspace=workspace,
                        owner_id=7,
                        model="kimi-k3",
                        engine="math_harness",
                    )
                    self.assertIs(queued.get("pending_refresh"), True)
                    release_first_final.set()
                    self.assertTrue(second_finish.wait(5))

                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        with auto_pipeline._ACTIVE_LOCK:
                            if key not in auto_pipeline._ACTIVE:
                                break
                        time.sleep(0.01)

                state = auto_pipeline.load_state(workspace) or {}
                self.assertEqual(batch_signatures, ["proof.md:old", "proof.md:new"])
                self.assertEqual(state.get("source_signature"), "proof.md:new")
                self.assertEqual(state.get("rerun_count"), 1)
                self.assertNotIn("pending_refresh", state)
                with auto_pipeline._ACTIVE_LOCK:
                    self.assertNotIn(key, auto_pipeline._ACTIVE)
                    self.assertNotIn(key, auto_pipeline._PENDING_START)
            finally:
                release_first_final.set()
                with auto_pipeline._ACTIVE_LOCK:
                    active = auto_pipeline._ACTIVE.pop(key, None)
                    auto_pipeline._PENDING_START.pop(key, None)
                if active is not None:
                    active.join(timeout=3)

    def test_completed_internal_rerun_consumes_matching_pending_refresh(self) -> None:
        final_signature_started = threading.Event()
        release_final_signature = threading.Event()
        finished = threading.Event()
        batch_signatures: list[str] = []
        original_finish = auto_pipeline._finish

        def source_signature(*_args, **_kwargs):
            final_signature_started.set()
            self.assertTrue(release_final_signature.wait(3))
            return "proof.md:new"

        def run_batch(workspace, *_args, **_kwargs):
            state = auto_pipeline.load_state(workspace) or {}
            batch_signatures.append(str(state.get("source_signature") or ""))

        def finish(workspace):
            original_finish(workspace)
            finished.set()

        with tempfile.TemporaryDirectory() as raw:
            workspace = Path(raw)
            key = str(workspace.resolve())
            try:
                with (
                    patch.object(
                        auto_pipeline,
                        "_wait_for_source",
                        return_value=(
                            "proof.md:old",
                            {"status": "finished", "engine": "math_harness"},
                        ),
                    ),
                    patch.object(
                        auto_pipeline,
                        "_run_record",
                        return_value={"status": "finished", "engine": "math_harness"},
                    ),
                    patch.object(
                        auto_pipeline, "_source_signature", side_effect=source_signature
                    ),
                    patch.object(auto_pipeline, "_run_batch", side_effect=run_batch),
                    patch.object(auto_pipeline, "_finish", side_effect=finish),
                ):
                    auto_pipeline.start(
                        run_id="coalesced-run",
                        workspace=workspace,
                        owner_id=7,
                        model="kimi-k3",
                        engine="math_harness",
                    )
                    self.assertTrue(final_signature_started.wait(3))
                    queued = auto_pipeline.start(
                        run_id="coalesced-run",
                        workspace=workspace,
                        owner_id=7,
                        model="kimi-k3",
                        engine="math_harness",
                    )
                    self.assertIs(queued.get("pending_refresh"), True)
                    release_final_signature.set()
                    self.assertTrue(finished.wait(5))

                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        with auto_pipeline._ACTIVE_LOCK:
                            if key not in auto_pipeline._ACTIVE:
                                break
                        time.sleep(0.01)

                state = auto_pipeline.load_state(workspace) or {}
                self.assertEqual(batch_signatures, ["proof.md:old", "proof.md:new"])
                self.assertEqual(state.get("source_signature"), "proof.md:new")
                self.assertEqual(state.get("rerun_count"), 1)
                self.assertNotIn("pending_refresh", state)
                with auto_pipeline._ACTIVE_LOCK:
                    self.assertNotIn(key, auto_pipeline._ACTIVE)
                    self.assertNotIn(key, auto_pipeline._PENDING_START)
            finally:
                release_final_signature.set()
                with auto_pipeline._ACTIVE_LOCK:
                    active = auto_pipeline._ACTIVE.pop(key, None)
                    auto_pipeline._PENDING_START.pop(key, None)
                if active is not None:
                    active.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
