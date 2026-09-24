"""Conservative normalization for the machine-readable proof outcome footer."""
from __future__ import annotations

import re
from pathlib import Path


CANONICAL_LABELS = {
    "solved": "Solved",
    "counterexample": "Counterexample",
    "known/open status": "Known/Open Status",
    "partial progress": "Partial Progress",
}
_LABEL_PATTERN = r"(Solved|Counterexample|Known/Open Status|Partial Progress)"
_TRAILING_PUNCTUATION = r"(?:[.!?][ \t]*)?"

OUTCOME_LINE_RE = re.compile(
    r"(?im)^[ \t]*\*{0,2}ProvingConsole[ \t]+outcome[ \t]*:[ \t]*"
    + _LABEL_PATTERN
    + r"[ \t]*"
    + _TRAILING_PUNCTUATION
    + r"\*{0,2}(?:[ \t]*[.!?])?[ \t]*\r?$"
)
LEGACY_OUTCOME_LINE_RE = re.compile(
    r"(?im)^[ \t]*\*{0,2}ProvingConsole(?:[ \t]+outcome)?[ \t]*:[ \t]*"
    + _LABEL_PATTERN
    + r"[ \t]*"
    + _TRAILING_PUNCTUATION
    + r"\*{0,2}(?:[ \t]*[.!?])?[ \t]*\r?$"
)
OUTCOME_HEADING_RE = re.compile(
    r"(?im)^[ \t]*#{1,6}[ \t]*ProvingConsole[ \t]+outcome[ \t]*\r?$"
    r"(?:\r?\n[ \t]*)+\*{0,2}"
    + _LABEL_PATTERN
    + r"[ \t]*"
    + _TRAILING_PUNCTUATION
    + r"\*{0,2}(?:[ \t]*[.!?])?[ \t]*\r?$"
)
_OUTCOME_MARKER_LINE_RE = re.compile(
    r"(?im)^[ \t]*\*{0,2}ProvingConsole[ \t]+outcome[ \t]*:[^\r\n]*\r?$"
)
_LEGACY_OUTCOME_MARKER_LINE_RE = re.compile(
    r"(?im)^[ \t]*\*{0,2}ProvingConsole(?:[ \t]+outcome)?[ \t]*:[^\r\n]*\r?$"
)
_OUTCOME_HEADING_MARKER_RE = re.compile(
    r"(?im)^[ \t]*#{1,6}[ \t]*ProvingConsole[ \t]+outcome[ \t]*\r?$"
)


def outcome_requested(prompt: str) -> bool:
    """Return whether the task explicitly requests the footer contract."""
    return "provingconsole outcome:" in (prompt or "").lower()


def recognized_labels(
    text: str,
    *,
    allow_legacy_marker: bool = False,
) -> set[str]:
    """Return distinct canonical labels from recognized standalone variants."""
    line_re = LEGACY_OUTCOME_LINE_RE if allow_legacy_marker else OUTCOME_LINE_RE
    labels: set[str] = set()
    for match in [*line_re.finditer(text), *OUTCOME_HEADING_RE.finditer(text)]:
        raw = match.group(1)
        canonical = CANONICAL_LABELS.get(raw.casefold())
        if canonical is not None:
            labels.add(canonical)
    return labels


def _marker_counts(
    text: str,
    *,
    allow_legacy_marker: bool,
) -> tuple[int, int]:
    """Return (all footer-like markers, fully recognized markers)."""
    line_re = LEGACY_OUTCOME_LINE_RE if allow_legacy_marker else OUTCOME_LINE_RE
    marker_re = (
        _LEGACY_OUTCOME_MARKER_LINE_RE
        if allow_legacy_marker
        else _OUTCOME_MARKER_LINE_RE
    )
    all_markers = len(list(marker_re.finditer(text))) + len(
        list(_OUTCOME_HEADING_MARKER_RE.finditer(text))
    )
    recognized = len(list(line_re.finditer(text))) + len(
        list(OUTCOME_HEADING_RE.finditer(text))
    )
    return all_markers, recognized


def strip_outcome_markers(
    text: str,
    *,
    allow_legacy_marker: bool = False,
) -> str:
    """Remove standalone valid, duplicate, conflicting, or malformed markers."""
    marker_re = (
        _LEGACY_OUTCOME_MARKER_LINE_RE
        if allow_legacy_marker
        else _OUTCOME_MARKER_LINE_RE
    )
    cleaned = OUTCOME_HEADING_RE.sub("", text)
    cleaned = marker_re.sub("", cleaned)
    cleaned = _OUTCOME_HEADING_MARKER_RE.sub("", cleaned)
    return cleaned.rstrip()


def canonicalize_outcome_text(
    text: str,
    *,
    allow_legacy_marker: bool = False,
) -> str:
    """Return exactly one footer, failing closed on missing/conflicting labels.

    Harmless punctuation and repeated copies of one label preserve that label.
    Distinct labels, malformed labels, and missing labels all resolve to
    ``Partial Progress``. This function never infers a stronger outcome from
    ordinary prose.
    """
    labels = recognized_labels(text, allow_legacy_marker=allow_legacy_marker)
    marker_count, recognized_count = _marker_counts(
        text,
        allow_legacy_marker=allow_legacy_marker,
    )
    all_markers_valid = marker_count > 0 and marker_count == recognized_count
    label = (
        next(iter(labels))
        if len(labels) == 1 and all_markers_valid
        else "Partial Progress"
    )
    cleaned = strip_outcome_markers(
        text,
        allow_legacy_marker=allow_legacy_marker,
    )
    return cleaned + f"\n\nProvingConsole outcome: {label}\n"


def ensure_requested_outcome(prompt: str, text: str) -> str:
    """Normalize the artifact only when its task requests the footer."""
    if not outcome_requested(prompt):
        return text
    return canonicalize_outcome_text(text)


def normalize_requested_outcome_file(path: Path, prompt: str) -> bool:
    """Canonicalize one regular artifact in place; return whether it changed."""
    if not outcome_requested(prompt) or path.is_symlink() or not path.is_file():
        return False
    try:
        original = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    rendered = ensure_requested_outcome(prompt, original)
    if rendered == original:
        return False
    try:
        path.write_text(rendered, encoding="utf-8")
    except OSError:
        return False
    return True
