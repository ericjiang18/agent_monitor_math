from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from agent_monitor import jobs


class ContinuationSafeProofReadTests(unittest.TestCase):
    def test_regular_markdown_is_bounded_to_prompt_excerpt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            content = "# Proof\n\n" + "a" * 12_000
            (workspace / "proof.md").write_text(content)

            name, excerpt = jobs._continuation_proof_excerpt(workspace)

        self.assertEqual(name, "proof.md")
        self.assertEqual(excerpt, content[:8000])

    def test_markdown_precedes_regular_latex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "proof.md").write_text("markdown proof")
            (workspace / "proof.tex").write_text("latex proof")

            name, excerpt = jobs._continuation_proof_excerpt(workspace)

        self.assertEqual((name, excerpt), ("proof.md", "markdown proof"))

    def test_symlinked_proof_is_not_injected_into_continuation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            workspace = Path(tmp)
            secret = Path(outside) / "secret.txt"
            secret.write_text("must never be sent to a model")
            (workspace / "proof.md").symlink_to(secret)

            name, excerpt = jobs._continuation_proof_excerpt(workspace)

            self.assertEqual((name, excerpt), ("proof.md", ""))

    def test_oversized_proof_is_not_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            proof = workspace / "proof.md"
            with proof.open("wb") as handle:
                handle.truncate(2 * 1024 * 1024 + 1)

            name, excerpt = jobs._continuation_proof_excerpt(workspace)

        self.assertEqual((name, excerpt), ("proof.md", ""))


if __name__ == "__main__":
    unittest.main()
