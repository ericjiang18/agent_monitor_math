"""Agent persona management — system prompt, skills, memory.

The files live in HERMES_HOME (``data/hermes/``) and apply to **every** engine:

- ``SOUL.md``                 — agent identity, injected as slot #1 of the
                                system prompt (``load_soul_identity=True``).
- ``skills/<name>/SKILL.md``  — skill packs, indexed into the system prompt.
                                A skill directory renamed to ``<name>.disabled``
                                is excluded from the index.
- ``memories/MEMORY.md``      — the agent's persistent notes.
- ``memories/USER.md``        — what the agent knows about the user.
  Memory injection is gated by ``memory.memory_enabled`` in
  ``HERMES_HOME/config.yaml``.

Hermes reads these natively. Engines that cannot (Codex, OpenClaude, OpenHands,
DeepAgents, …) receive the same content through :func:`persona_preamble`, which
is prepended to their task prompt.
"""
from __future__ import annotations

import re
import shutil
from typing import Any

from agent_monitor import HERMES_HOME

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,63}")
_MEMORY_FILES = ("MEMORY.md", "USER.md")
_DISABLED_SUFFIX = ".disabled"
_SEED_MARKER = ".seeded"

# Budget for skill bodies rendered into a portable prompt. Beyond this the
# remaining skills degrade to name + description only, so a large skill library
# can never crowd out the actual problem statement.
_PREAMBLE_SKILL_BUDGET = 9000
# Generous ceiling on the identity block — normal SOUL.md files are far smaller;
# this only stops a runaway file from dominating the prompt.
_PREAMBLE_IDENTITY_LIMIT = 20000


def _skills_dir():
    d = HERMES_HOME / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _memories_dir():
    d = HERMES_HOME / "memories"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_seeded() -> None:
    """Populate a fresh agent home with starter skills and memory scaffolds.

    Runs once — the marker file means skills the user deleted on purpose are
    never resurrected.
    """
    marker = _skills_dir() / _SEED_MARKER
    if marker.exists():
        return
    from agent_monitor.skill_seeds import MEMORY_SCAFFOLDS, STARTER_SKILLS

    try:
        for name, content in STARTER_SKILLS.items():
            d = _skills_dir() / name
            if not d.exists() and not (_skills_dir() / f"{name}{_DISABLED_SUFFIX}").exists():
                d.mkdir(parents=True, exist_ok=True)
                (d / "SKILL.md").write_text(content, encoding="utf-8")
        for fname, content in MEMORY_SCAFFOLDS.items():
            path = _memories_dir() / fname
            if not path.exists():
                path.write_text(content, encoding="utf-8")
        marker.write_text("starter skills seeded\n", encoding="utf-8")
    except OSError:
        pass  # A read-only home just means no seeds; not fatal.


def _check_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME_RE.fullmatch(name) or ".." in name or "/" in name:
        raise ValueError(f"Invalid name: {name!r}")
    return name


# ── config.yaml (memory toggle) ──────────────────────────────────────────

def _config_path():
    return HERMES_HOME / "config.yaml"


def _read_config() -> dict[str, Any]:
    path = _config_path()
    if not path.exists():
        return {}
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _write_config(cfg: dict[str, Any]) -> None:
    import yaml

    _config_path().parent.mkdir(parents=True, exist_ok=True)
    _config_path().write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def memory_enabled() -> bool:
    mem = _read_config().get("memory") or {}
    return bool(mem.get("memory_enabled"))


def set_memory_enabled(enabled: bool) -> None:
    cfg = _read_config()
    mem = cfg.get("memory")
    if not isinstance(mem, dict):
        mem = {}
    mem["memory_enabled"] = bool(enabled)
    mem["user_profile_enabled"] = bool(enabled)
    cfg["memory"] = mem
    _write_config(cfg)


# ── system prompt (SOUL.md) ──────────────────────────────────────────────

def get_system_prompt() -> str:
    path = HERMES_HOME / "SOUL.md"
    try:
        return path.read_text(encoding="utf-8") if path.exists() else ""
    except OSError:
        return ""


def save_system_prompt(text: str) -> None:
    HERMES_HOME.mkdir(parents=True, exist_ok=True)
    path = HERMES_HOME / "SOUL.md"
    text = text or ""
    if text.strip():
        path.write_text(text, encoding="utf-8")
    elif path.exists():
        path.unlink()  # empty → fall back to the built-in default identity


# ── skills ───────────────────────────────────────────────────────────────

def list_skills() -> list[dict[str, Any]]:
    ensure_seeded()
    out: list[dict[str, Any]] = []
    for d in sorted(_skills_dir().iterdir()):
        if not d.is_dir() or d.name.startswith("."):
            continue
        enabled = not d.name.endswith(_DISABLED_SUFFIX)
        name = d.name[: -len(_DISABLED_SUFFIX)] if not enabled else d.name
        skill_md = d / "SKILL.md"
        content = ""
        if skill_md.exists():
            try:
                content = skill_md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                content = ""
        desc = next(
            (ln.strip().lstrip("#").strip() for ln in content.splitlines() if ln.strip()),
            "",
        )
        out.append(
            {"name": name, "enabled": enabled, "description": desc[:160], "content": content}
        )
    return out


def _skill_dir(name: str, *, must_exist: bool = False):
    name = _check_name(name)
    base = _skills_dir()
    for cand in (base / name, base / f"{name}{_DISABLED_SUFFIX}"):
        if cand.is_dir():
            return cand
    if must_exist:
        raise FileNotFoundError(f"Unknown skill: {name}")
    return base / name


def save_skill(name: str, content: str) -> None:
    d = _skill_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(content or "", encoding="utf-8")


def set_skill_enabled(name: str, enabled: bool) -> None:
    d = _skill_dir(name, must_exist=True)
    is_enabled = not d.name.endswith(_DISABLED_SUFFIX)
    if is_enabled == bool(enabled):
        return
    # Enable: strip the .disabled suffix; disable: append it.
    if enabled:
        target = d.parent / d.name[: -len(_DISABLED_SUFFIX)]
    else:
        target = d.parent / f"{d.name}{_DISABLED_SUFFIX}"
    d.rename(target)


def delete_skill(name: str) -> None:
    d = _skill_dir(name, must_exist=True)
    shutil.rmtree(d)


# ── memory files ─────────────────────────────────────────────────────────

def list_memory() -> list[dict[str, Any]]:
    ensure_seeded()
    out = []
    for fname in _MEMORY_FILES:
        path = _memories_dir() / fname
        content = ""
        if path.exists():
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                content = ""
        out.append({"name": fname, "content": content})
    return out


def save_memory(name: str, content: str) -> None:
    if name not in _MEMORY_FILES:
        raise ValueError(f"Unknown memory file: {name!r} (use MEMORY.md or USER.md)")
    path = _memories_dir() / name
    content = content or ""
    if content.strip():
        path.write_text(content, encoding="utf-8")
    elif path.exists():
        path.unlink()


# ── portable persona (engines that can't read HERMES_HOME) ───────────────

def persona_preamble(*, include_memory: bool | None = None) -> str:
    """Render identity + enabled skills + memory as a prompt preamble.

    Hermes loads all of this natively, so this is for the CLI engines (Codex,
    OpenClaude, OpenHands, OpenClaw, DeepAgents, …) which only ever see a task
    prompt. Returns "" when there is nothing configured.
    """
    ensure_seeded()
    blocks: list[str] = []

    identity = get_system_prompt().strip()
    if identity:
        if len(identity) > _PREAMBLE_IDENTITY_LIMIT:
            identity = identity[:_PREAMBLE_IDENTITY_LIMIT] + "\n…(identity truncated)"
        blocks.append("=== AGENT IDENTITY ===\n" + identity)

    skills = [s for s in list_skills() if s["enabled"] and (s.get("content") or "").strip()]
    if skills:
        index = "\n".join(f"- {s['name']}: {s['description']}" for s in skills)
        parts = [f"=== SKILLS ({len(skills)}) ===\nAvailable skills:\n{index}"]
        budget = _PREAMBLE_SKILL_BUDGET
        omitted: list[str] = []
        for s in skills:
            body = s["content"].strip()
            if len(body) <= budget:
                parts.append(f"--- skill: {s['name']} ---\n{body}")
                budget -= len(body)
            else:
                omitted.append(s["name"])
        if omitted:
            parts.append(
                "(Skills listed but not expanded here — ask for them if needed: "
                + ", ".join(omitted)
                + ")"
            )
        blocks.append("\n\n".join(parts))

    want_memory = memory_enabled() if include_memory is None else include_memory
    if want_memory:
        mem = [m for m in list_memory() if (m.get("content") or "").strip()]
        if mem:
            body = "\n\n".join(f"--- {m['name']} ---\n{m['content'].strip()}" for m in mem)
            blocks.append("=== MEMORY (carried from earlier runs) ===\n" + body)

    if not blocks:
        return ""
    return (
        "The operator configured the following persona for you. Follow it for "
        "this task.\n\n" + "\n\n".join(blocks) + "\n\n=== END PERSONA ===\n"
    )


# ── aggregate ────────────────────────────────────────────────────────────

def get_agent_config() -> dict[str, Any]:
    return {
        "system_prompt": get_system_prompt(),
        "memory_enabled": memory_enabled(),
        "memory": list_memory(),
        "skills": list_skills(),
    }
