from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from evals import promote_research_prompt


def _write_inputs(root: Path) -> tuple[Path, Path, Path, str]:
    candidate_s = root / "candidate-s.md"
    delta = root / "candidate-t-delta.md"
    live = root / "SOUL.md"
    candidate_s.write_text("# Candidate S\n\nBase prompt.\n", encoding="utf-8")
    delta.write_text(
        "# Candidate T delta\n\n"
        + promote_research_prompt.DEFAULT_MARKER
        + ".\n",
        encoding="utf-8",
    )
    live.write_text("Base prompt.\n", encoding="utf-8")
    expected = (
        "Base prompt.\n\n"
        + promote_research_prompt.DEFAULT_MARKER
        + ".\n"
    )
    return candidate_s, delta, live, expected


def test_prompt_promotion_dry_run_and_atomic_apply(tmp_path: Path) -> None:
    candidate_s, delta, live, expected = _write_inputs(tmp_path)
    expected_hash = sha256(expected.encode("utf-8")).hexdigest()

    dry_run = promote_research_prompt.promote(
        candidate_s_path=candidate_s,
        candidate_t_delta_path=delta,
        live_path=live,
        expected_output_sha256=expected_hash,
    )
    assert dry_run["status"] == "dry-run"
    assert live.read_text(encoding="utf-8") == "Base prompt.\n"

    applied = promote_research_prompt.promote(
        candidate_s_path=candidate_s,
        candidate_t_delta_path=delta,
        live_path=live,
        apply=True,
        expected_output_sha256=expected_hash,
    )
    assert applied["status"] == "applied"
    assert live.read_text(encoding="utf-8") == expected
    assert not list(tmp_path.glob(".*.promote-*.tmp"))

    with pytest.raises(ValueError, match="compare-and-swap refused"):
        promote_research_prompt.promote(
            candidate_s_path=candidate_s,
            candidate_t_delta_path=delta,
            live_path=live,
            apply=True,
        )


def test_prompt_promotion_rejects_wrong_output_hash_and_symlink(tmp_path: Path) -> None:
    candidate_s, delta, live, _ = _write_inputs(tmp_path)
    with pytest.raises(ValueError, match="hash mismatch"):
        promote_research_prompt.promote(
            candidate_s_path=candidate_s,
            candidate_t_delta_path=delta,
            live_path=live,
            expected_output_sha256="0" * 64,
        )

    link = tmp_path / "linked-SOUL.md"
    link.symlink_to(live)
    with pytest.raises(ValueError, match="non-symlink"):
        promote_research_prompt.promote(
            candidate_s_path=candidate_s,
            candidate_t_delta_path=delta,
            live_path=link,
        )
