"""Each turn's proof, kept so the Sandbox can show one proof per turn.

A proof run's ``proof.md`` is rewritten by every turn (the first run, then
each follow-up), so the Sandbox could only ever show the latest one.  Each
turn now leaves a copy of the proof as it was when the turn ended, under
the hidden ``.proof_turns/`` folder of the run's workspace (a dot-folder:
never in the run's file list or export):

- ``.proof_turns/index.json``: ``{"turns": [{"n", "started_at",
  "finished_at", "status", "file", "chars"}]}``, one entry per turn in
  order; a turn still running has ``finished_at`` null and status
  ``running``.
- ``.proof_turns/<n>.md`` (or ``<n>.tex`` for a LaTeX-only proof): the copy.

The console reads the index from the run record (``proof_turns``) and each
copy through the workspace file endpoint.  Everything here is best-effort:
a failure to record a turn never fails the turn itself.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DIR = ".proof_turns"
INDEX = "index.json"
MAX_TURNS = 200
MAX_CHARS = 400_000

_LOCK = threading.Lock()


def _dir(ws: Path) -> Path:
    return Path(ws) / DIR


def _load(ws: Path) -> list[dict[str, Any]]:
    path = _dir(ws) / INDEX
    try:
        if path.is_symlink() or not path.is_file():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    turns = data.get("turns") if isinstance(data, dict) else None
    return [t for t in turns if isinstance(t, dict)] if isinstance(turns, list) else []


def _folder(ws: Path) -> Path:
    """The turns' folder, made if missing; never one that is a symlink."""
    folder = _dir(ws)
    if folder.is_symlink():
        raise OSError("the turns' folder is a symlink")
    folder.mkdir(exist_ok=True)
    return folder


def _write(folder: Path, name: str, text: str) -> None:
    """``folder/name`` holds ``text``, written through a new file of its own
    (``mkstemp`` never follows a link) moved into place: a symlink left at
    either name by an engine is replaced, never written through."""
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, folder / name)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _save(ws: Path, turns: list[dict[str, Any]]) -> None:
    _write(_folder(ws), INDEX, json.dumps({"turns": turns[-MAX_TURNS:]}, ensure_ascii=False))


# Not named like a proof ("proof", "solution", "answer"): jobs._promote_proof_artifact
# would copy such a file over proof.md.
ANSWER_FILE = ".turn_reply.md"


def _read_proof(ws: Path, source: str) -> str:
    """The kept part of ``ws/source`` ('' when missing, a symlink or blank).
    Only that part is read: a runaway proof is never loaded whole (a UTF-8
    character is at most 4 bytes)."""
    path = ws / source
    if not path.is_file() or path.is_symlink():
        return ""
    with path.open("rb") as fh:
        raw = fh.read(MAX_CHARS * 4)
    text = raw.decode("utf-8", errors="replace")[:MAX_CHARS]
    return text if text.strip() else ""


def _earlier_turn(ws: Path, started_at: str) -> dict[str, Any] | None:
    """The proof a run already has when its first recorded turn is a
    follow-up (a run from before turns were kept, or whose first turn went
    unrecorded): kept as turn 1, "earlier", so the thread still reads one
    proof per turn.  Its time is when that proof was last written (never
    after the follow-up starts)."""
    for source, suffix in (("proof.md", ".md"), ("proof.tex", ".tex")):
        text = _read_proof(ws, source)
        if not text:
            continue
        try:
            written = datetime.fromtimestamp((ws / source).stat().st_mtime, timezone.utc)
        except (OSError, OverflowError, ValueError):
            written = None
        try:
            start = datetime.fromisoformat(started_at)
            start = start if start.tzinfo else start.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            start = None
        if written is None or (start is not None and written >= start):
            written = start
        at = written.isoformat() if written is not None else str(started_at or "")
        name = f"1{suffix}"
        _write(_folder(ws), name, text)
        return {"n": 1, "started_at": at, "finished_at": at, "status": "finished",
                "file": f"{DIR}/{name}", "chars": len(text), "earlier": True}
    return None


def begin(
    ws: Path | str, *, started_at: str, answer_only: bool = False, follow_up: bool = False
) -> int | None:
    """Record a turn starting; returns its number (1 for the first run).

    An ``answer_only`` turn (an easy question about a proof Ansatz CGM wrote:
    easy_route) answers without touching proof.md: what it keeps is its
    answer (``ANSWER_FILE``), not the proof.  A ``follow_up`` on a run with
    no turn recorded first keeps the proof the run already has as turn 1
    (``_earlier_turn``)."""
    ws = Path(ws)
    try:
        if not ws.is_dir():
            return None
        with _LOCK:
            turns = _load(ws)
            if follow_up and not turns:
                earlier = _earlier_turn(ws, started_at)
                if earlier:
                    turns.append(earlier)
            # A turn left running (a restart lost its worker) ended unrecorded.
            for turn in turns:
                if turn.get("finished_at") is None:
                    turn["status"] = "interrupted"
                    turn["finished_at"] = started_at
            n = int(turns[-1].get("n") or len(turns)) + 1 if turns else 1
            turns.append({"n": n, "started_at": started_at, "finished_at": None,
                          "status": "running", "file": "", "chars": 0,
                          **({"answer_only": True} if answer_only else {})})
            if answer_only:
                stale = ws / ANSWER_FILE
                if stale.is_file() and not stale.is_symlink():
                    stale.unlink()
            _save(ws, turns)
            return n
    except (OSError, ValueError, TypeError):
        return None


def _last_kept_proof(ws: Path, turns: list[dict[str, Any]], n: int) -> str | None:
    """The proof the last turn before ``n`` kept (None when none did)."""
    for turn in reversed(turns):
        try:
            number = int(turn.get("n"))
        except (TypeError, ValueError):
            continue
        if number >= n or turn.get("answer_only") or not turn.get("file"):
            continue
        path = ws / str(turn.get("file"))
        try:
            if path.is_symlink() or not path.is_file() or path.parent.name != DIR:
                return None
            return path.read_text(encoding="utf-8")
        except OSError:
            return None
    return None


def end(ws: Path | str, n: int | None, *, status: str, finished_at: str) -> None:
    """Record turn ``n`` ending with ``status``, keeping a copy of its proof."""
    if not n:
        return
    ws = Path(ws)
    try:
        if not ws.is_dir():
            return
        with _LOCK:
            turns = _load(ws)
            turn = next((t for t in turns if t.get("n") == n), None)
            if turn is None or turn.get("finished_at") is not None:
                return  # unknown, or ended already (a turn ends once)
            name, text = "", ""
            sources = ((ANSWER_FILE, ".md"),) if turn.get("answer_only") else (("proof.md", ".md"), ("proof.tex", ".tex"))
            for source, suffix in sources:
                text = _read_proof(ws, source)
                if text:
                    name = f"{n}{suffix}"
                    break
            if name and not turn.get("answer_only") and text == _last_kept_proof(ws, turns, n):
                # The proof as the turn before left it (a turn stopped or
                # failed before writing one, or that wrote the same proof):
                # nothing of this turn's own to keep, so no card repeats it.
                name, text = "", ""
            if name:
                _write(_folder(ws), name, text)
            if turn.get("answer_only"):
                answer = ws / ANSWER_FILE
                if answer.is_file() and not answer.is_symlink():
                    answer.unlink()
            turn.update({"finished_at": finished_at, "status": str(status or "finished"),
                         "file": f"{DIR}/{name}" if name else "", "chars": len(text)})
            _save(ws, turns)
    except (OSError, ValueError, TypeError):
        return


def summary(ws: Path | str | None) -> list[dict[str, Any]]:
    """The turns as the console shows them (no proof text)."""
    if not ws:
        return []
    out = []
    for turn in _load(Path(ws)):
        try:
            n = int(turn.get("n"))
        except (TypeError, ValueError):
            continue
        out.append({
            "n": n,
            "started_at": str(turn.get("started_at") or ""),
            "finished_at": turn.get("finished_at") or None,
            "status": str(turn.get("status") or ""),
            "file": str(turn.get("file") or ""),
            "chars": int(turn.get("chars") or 0),
            "answer_only": bool(turn.get("answer_only")),
            "earlier": bool(turn.get("earlier")),
        })
    return out
