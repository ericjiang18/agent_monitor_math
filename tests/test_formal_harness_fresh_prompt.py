from __future__ import annotations

import unittest

from agent_monitor import lean_verify


class FormalHarnessFreshPromptTests(unittest.TestCase):
    def test_manual_harness_requires_changed_bytes_without_statement_drift(self) -> None:
        prompt = lean_verify.HARNESS_TASK
        self.assertIn("bytes differ from the file present", prompt)
        self.assertIn("compiling an unchanged file is", prompt)
        self.assertIn("Preserve the exact requested theorem statement", prompt)


if __name__ == "__main__":
    unittest.main()
