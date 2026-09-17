"""Remove transport wrappers from response-only Markdown documents.

This is presentation cleanup, never a judgment about a proof's correctness.
Only a complete outer Markdown fence and a narrowly recognized file-saving
preface are removed. Mathematical qualifications and incomplete replies stay.
"""
from __future__ import annotations

import re


_OUTER_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*(?:markdown|md)[ \t]*$", re.I)
_HEADING = re.compile(r"^#{1,6}[ \t]+\S")
_FILE = r"`?(?:\./)?proof\.md`?"
_SAVE = (
    r"(?:so[ ,]+|but[ ,]+)?(?:please\s+)?(?:save|copy)\s+"
    r"(?:the following|this)(?:\s+(?:verbatim|content|Markdown|text))*\s+"
    r"(?:as|into|to)\s+" + _FILE
    + r"(?:\s+(?:at|in|to)\s+(?:the\s+)?(?:requested|specified)\s+(?:path|directory|location))?"
)
_INABILITY = (
    r"I\s+(?:cannot|can't|can’t|am unable to)\s+(?:directly\s+)?"
    r"(?:write\s+(?:to\s+)?(?:the\s+)?(?:filesystem|file system|files)|"
    r"(?:write|save|create)\s+(?:to\s+)?" + _FILE + r")"
    r"(?:\s+(?:directly|from here|here|in this (?:environment|chat(?: environment)?)))*"
)
_PRESENT = r"(?:but\s+)?here is the (?:complete\s+)?(?:content|proof)"
_PREFACE = re.compile(
    r"^(?:" + _INABILITY + r"[.,;:]?\s+(?:" + _SAVE + r"|" + _PRESENT + r")|" + _SAVE + r")[.!:]?$",
    re.I,
)
_SAVED_PREFACE = re.compile(
    r"^Since I cannot write files in this session, here is the complete, "
    r"self-contained " + _FILE
    + r" [—–-] it can be saved verbatim to the workspace path\.(?:\s+|$)",
    re.I,
)
_OUTCOME_FOOTER = re.compile(
    r"ProvingConsole outcome: (?:Solved|Counterexample|Known/Open Status|Partial Progress)",
    re.I,
)


def _unwrap_markdown_fence(text: str, *, allow_tail: bool = False) -> str:
    lines = text.splitlines()
    if len(lines) < 3:
        return text
    opening = _OUTER_FENCE.fullmatch(lines[0])
    if not opening:
        return text
    fence = opening.group(1)
    closing = re.compile(r"^ {0,3}" + re.escape(fence[0]) + r"{" + str(len(fence)) + r",}[ \t]*$")
    ambiguous_inner = re.compile(
        r"^ {0,3}" + re.escape(fence[0]) + r"{" + str(len(fence)) + r",}[ \t]*[^" + re.escape(fence[0]) + r"\s].*$"
    )
    # An earlier matching close means this is an example followed by prose,
    # not one fence around the entire document. Shorter inner fences are safe.
    for index in range(1, len(lines)):
        if ambiguous_inner.fullmatch(lines[index]):
            # Same-width nesting is ambiguous Markdown. Keep its delimiters
            # rather than consume an inner close and turn proof prose into code.
            return text
        if closing.fullmatch(lines[index]):
            tail = "\n".join(lines[index + 1:]).strip()
            if not tail or allow_tail or _OUTCOME_FOOTER.fullmatch(tail):
                body = "\n".join(lines[1:index]).strip()
                return body + ("\n\n" + tail if tail else "")
            return text
    return text


def normalize_proof_markdown(text: str) -> str:
    """Return render-ready text without suppressing substantive model content."""
    original = text
    text = _unwrap_markdown_fence(text.strip())
    unchanged = original if text == original.strip() else text
    # A heading marks where the mathematical document begins. Require a whole
    # operational preface match, so a mixed paragraph's caveats cannot vanish.
    lines = text.splitlines()
    for index, line in enumerate(lines[1:], 1):
        if _HEADING.match(line) or _OUTER_FENCE.fullmatch(line):
            prefix = " ".join(part.strip() for part in lines[:index]).strip()
            saved_preface = _SAVED_PREFACE.match(prefix)
            if len(prefix) > 800 or not (_PREFACE.fullmatch(prefix) or saved_preface):
                return unchanged
            body = _unwrap_markdown_fence("\n".join(lines[index:]).strip(), allow_tail=True)
            if _HEADING.match(body):
                # Some replies append a status note to the operational sentence.
                # Keep that note, and any discussion after the fenced document.
                note = prefix[saved_preface.end():].strip() if saved_preface else ""
                return (note + "\n\n" if note else "") + body
            return unchanged
    return unchanged
