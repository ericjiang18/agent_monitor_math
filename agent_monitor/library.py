"""User library: persistent memory / skills / tools shared across all engines.

Items live in data/library/library.json and are injected into every run:
- a "USER LIBRARY" block is prepended to the problem prompt;
- files are materialized into the run workspace under _library/
  (MEMORY.md, SKILLS.md, tools/<name>.sh, tools.json) so agents with shell or
  file tools can read and execute them.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from agent_monitor import DATA_DIR

LIBRARY_DIR = DATA_DIR / "library"
LIBRARY_FILE = LIBRARY_DIR / "library.json"

VALID_TYPES = ("memory", "skill", "tool")

_LOCK = threading.Lock()


def _now() -> float:
    return time.time()


def _load() -> dict[str, Any]:
    if LIBRARY_FILE.exists():
        try:
            data = json.loads(LIBRARY_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("items", [])
                data.setdefault("settings", {})
                data["settings"].setdefault("auto_memory", True)
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"items": [], "settings": {"auto_memory": True}}


def _save(data: dict[str, Any]) -> None:
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    tmp = LIBRARY_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(LIBRARY_FILE)


def get_library() -> dict[str, Any]:
    with _LOCK:
        data = _load()
    items = sorted(data["items"], key=lambda i: -(i.get("updated_at") or 0))
    counts = {t: sum(1 for i in items if i.get("type") == t) for t in VALID_TYPES}
    return {"items": items, "counts": counts, "settings": data["settings"]}


def upsert_item(payload: dict[str, Any]) -> dict[str, Any]:
    itype = str(payload.get("type") or "").strip().lower()
    if itype not in VALID_TYPES:
        raise ValueError(f"type must be one of {VALID_TYPES}")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    content = str(payload.get("content") or "")
    if not content.strip():
        raise ValueError("content is required")

    item = {
        "id": str(payload.get("id") or "").strip() or f"{itype}_{uuid.uuid4().hex[:8]}",
        "type": itype,
        "name": name,
        "description": str(payload.get("description") or "").strip(),
        "content": content,
        "enabled": bool(payload.get("enabled", True)),
        "tags": [str(t) for t in (payload.get("tags") or [])],
        "source": str(payload.get("source") or "user"),
        "updated_at": _now(),
    }
    with _LOCK:
        data = _load()
        existing = next((i for i in data["items"] if i.get("id") == item["id"]), None)
        if existing:
            item["created_at"] = existing.get("created_at") or _now()
            data["items"] = [item if i.get("id") == item["id"] else i for i in data["items"]]
        else:
            item["created_at"] = _now()
            data["items"].append(item)
        _save(data)
    return item


def delete_item(item_id: str) -> bool:
    with _LOCK:
        data = _load()
        before = len(data["items"])
        data["items"] = [i for i in data["items"] if i.get("id") != item_id]
        if len(data["items"]) != before:
            _save(data)
            return True
    return False


def update_settings(settings: dict[str, Any]) -> dict[str, Any]:
    with _LOCK:
        data = _load()
        if "auto_memory" in settings:
            data["settings"]["auto_memory"] = bool(settings["auto_memory"])
        _save(data)
        return dict(data["settings"])


def enabled_items(itype: str | None = None) -> list[dict[str, Any]]:
    with _LOCK:
        data = _load()
    items = [i for i in data["items"] if i.get("enabled", True)]
    if itype:
        items = [i for i in items if i.get("type") == itype]
    return items


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:60] or "item"


def materialize(workspace: Path) -> dict[str, Any]:
    """Write enabled library items into <workspace>/_library/ for agent access."""
    memories = enabled_items("memory")
    skills = enabled_items("skill")
    tools = enabled_items("tool")
    if not (memories or skills or tools):
        return {"written": []}

    lib_dir = workspace / "_library"
    lib_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    if memories:
        lines = ["# Memory\n"]
        for m in memories:
            lines.append(f"## {m['name']}\n")
            if m.get("description"):
                lines.append(f"_{m['description']}_\n")
            lines.append(m["content"].rstrip() + "\n")
        (lib_dir / "MEMORY.md").write_text("\n".join(lines), encoding="utf-8")
        written.append("_library/MEMORY.md")

    if skills:
        lines = ["# Skills\n"]
        for s in skills:
            lines.append(f"## {s['name']}\n")
            if s.get("description"):
                lines.append(f"_{s['description']}_\n")
            lines.append(s["content"].rstrip() + "\n")
        (lib_dir / "SKILLS.md").write_text("\n".join(lines), encoding="utf-8")
        written.append("_library/SKILLS.md")

    if tools:
        tdir = lib_dir / "tools"
        tdir.mkdir(exist_ok=True)
        manifest = []
        for t in tools:
            fname = _safe_name(t["name"]) + ".sh"
            script = tdir / fname
            body = t["content"]
            if not body.startswith("#!"):
                body = "#!/bin/bash\n" + body
            script.write_text(body, encoding="utf-8")
            script.chmod(0o755)
            written.append(f"_library/tools/{fname}")
            manifest.append(
                {
                    "name": t["name"],
                    "description": t.get("description") or "",
                    "script": f"_library/tools/{fname}",
                }
            )
        (tdir.parent / "tools.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        written.append("_library/tools.json")

    return {"written": written}


def compose_context() -> str:
    """Build the USER LIBRARY prompt block (empty string when nothing enabled)."""
    memories = enabled_items("memory")
    skills = enabled_items("skill")
    tools = enabled_items("tool")
    if not (memories or skills or tools):
        return ""

    parts = ["===== USER LIBRARY (memory / skills / tools) ====="]
    if memories:
        parts.append("\n[MEMORY] Facts and context from previous work — take into account:")
        for m in memories:
            head = m["name"] + (f" — {m['description']}" if m.get("description") else "")
            parts.append(f"- {head}:\n{m['content'].strip()}")
    if skills:
        parts.append("\n[SKILLS] Methods/strategies you should apply when relevant:")
        for s in skills:
            head = s["name"] + (f" — {s['description']}" if s.get("description") else "")
            parts.append(f"- {head}:\n{s['content'].strip()}")
    if tools:
        parts.append(
            "\n[TOOLS] Executable helper scripts in the workspace under _library/tools/ "
            "(run with: bash _library/tools/<name>.sh [args]):"
        )
        for t in tools:
            fname = _safe_name(t["name"]) + ".sh"
            desc = t.get("description") or ""
            parts.append(f"- _library/tools/{fname}: {desc}")
    parts.append(
        "\nFull copies are in the workspace: _library/MEMORY.md, _library/SKILLS.md, _library/tools/."
    )
    parts.append("===== END USER LIBRARY =====\n")
    return "\n".join(parts)


def auto_memory_enabled() -> bool:
    with _LOCK:
        return bool(_load()["settings"].get("auto_memory", True))


def record_run_memory(
    *, run_id: str, engine: str, problem_id: str, status: str, summary: str
) -> dict[str, Any] | None:
    """Append a compact memory entry when a run finishes (if auto_memory on)."""
    if not auto_memory_enabled():
        return None
    return upsert_item(
        {
            "id": f"memory_run_{run_id}",
            "type": "memory",
            "name": f"Run {problem_id} ({engine})",
            "description": f"auto record · status {status}",
            "content": summary.strip()[:4000] or f"Run {run_id} finished with status {status}.",
            "enabled": False,  # auto entries start disabled; user can enable in UI
            "source": f"run:{run_id}",
        }
    )
