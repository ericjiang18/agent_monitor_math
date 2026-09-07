"""Agent persona management — system prompt, skills, memory.

The files live in HERMES_HOME (``data/hermes/``) and apply to **every** engine:

- ``SOUL.md``                 — agent identity, injected as slot #1 of the
                                system prompt (``load_soul_identity=True``).
- ``skills/<name>/SKILL.md``  — skill packs, indexed by path in portable prompts.
                                A skill directory renamed to ``<name>.disabled``
                                is excluded from the index.
- ``memories/MEMORY.md``      — the agent's persistent notes.
- ``memories/USER.md``        — what the agent knows about the user.
  Memory injection is gated by ``memory.memory_enabled`` in
  ``HERMES_HOME/config.yaml``.

Hermes reads these natively. Portable prompts copy each enabled skill package
into the run workspace and give tool-capable engines a relative path, so they
load only relevant instructions without multiplying every skill body across
every agent turn.
"""
from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path
from typing import Any

from agent_monitor import HERMES_HOME

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_. -]{0,63}")
_MEMORY_FILES = ("MEMORY.md", "USER.md")
_DISABLED_SUFFIX = ".disabled"
_SEED_MARKER = ".seeded"
_SEED_V2_MARKER = ".seeded-v4-research-proof-audit-reconciliation"
_LEGACY_RESEARCH_AUDIT_PACKAGE_DIGESTS = {
    # Deployed v3 built-in, excluding interpreter cache files.  An edited
    # package intentionally will not match and will never be overlaid.
    "b873b3a4fb50c147f6af0d45240763f806540dff68e440ad90cf407811e77453",
}

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


def _skill_package_digest(path: Path) -> str | None:
    """Fingerprint user-visible package files while ignoring Python caches."""
    try:
        digest = hashlib.sha256()
        files = sorted(
            item
            for item in path.rglob("*")
            if item.is_file()
            and not item.is_symlink()
            and "__pycache__" not in item.parts
        )
        for item in files:
            relative = item.relative_to(path).as_posix().encode("utf-8")
            content = item.read_bytes()
            digest.update(len(relative).to_bytes(4, "big"))
            digest.update(relative)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
        return digest.hexdigest()
    except (OSError, ValueError):
        return None


def ensure_seeded() -> None:
    """Seed fresh profiles and migrate new built-ins without overwriting users."""
    marker = _skills_dir() / _SEED_MARKER
    version_marker = _skills_dir() / _SEED_V2_MARKER
    fresh = not marker.exists()
    if not fresh and version_marker.exists():
        return
    from agent_monitor.skill_seeds import (
        MEMORY_SCAFFOLDS,
        STARTER_SKILL_PACKAGES,
        STARTER_SKILL_REFERENCES,
        STARTER_SKILLS,
    )

    try:
        names = STARTER_SKILLS if fresh else {
            name: STARTER_SKILLS[name] for name in STARTER_SKILL_PACKAGES
        }
        for name, content in names.items():
            d = _skills_dir() / name
            if d.exists() or (_skills_dir() / f"{name}{_DISABLED_SUFFIX}").exists():
                package = STARTER_SKILL_PACKAGES.get(name)
                if (
                    name == "research-proof-audit"
                    and d.is_dir()
                    and package is not None
                    and _skill_package_digest(d)
                    in _LEGACY_RESEARCH_AUDIT_PACKAGE_DIGESTS
                ):
                    shutil.copytree(package, d, dirs_exist_ok=True)
                continue
            package = STARTER_SKILL_PACKAGES.get(name)
            if package is not None:
                shutil.copytree(package, d)
            else:
                d.mkdir(parents=True, exist_ok=True)
                (d / "SKILL.md").write_text(content, encoding="utf-8")
        if fresh:
            for name, references in STARTER_SKILL_REFERENCES.items():
                skill_dir = _skills_dir() / name
                if not skill_dir.is_dir():
                    continue
                for filename, content in references.items():
                    target = skill_dir / "references" / filename
                    if not target.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(content, encoding="utf-8")
            for fname, content in MEMORY_SCAFFOLDS.items():
                path = _memories_dir() / fname
                if not path.exists():
                    path.write_text(content, encoding="utf-8")
            marker.write_text("starter skills seeded\n", encoding="utf-8")
        version_marker.write_text("research proof audit skills seeded\n", encoding="utf-8")
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
        desc = _skill_description(content)
        out.append(
            {"name": name, "enabled": enabled, "description": desc[:160], "content": content}
        )
    return out


def _skill_description(content: str) -> str:
    """Return frontmatter description, otherwise the first Markdown heading."""
    text = content or ""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end >= 0:
            for line in text[3:end].splitlines():
                key, sep, value = line.partition(":")
                if sep and key.strip().lower() == "description":
                    value = value.strip().strip("'\"")
                    if value:
                        return value[:160]
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()[:160]
    return ""


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

def _materialize_skills(workspace: Path, skills: list[dict[str, Any]]) -> dict[str, str]:
    """Copy enabled skill packages into a run-visible directory.

    Some harnesses expose only the workspace as their virtual filesystem. A
    host-absolute HERMES_HOME reference is therefore unusable even though the
    skill exists. Copy packages (including references/assets) and advertise a
    relative path only after the copy succeeds.
    """
    visible: dict[str, str] = {}
    root = workspace / "_agent" / "skills"
    for skill in skills:
        name = _check_name(str(skill.get("name") or ""))
        source = _skill_dir(name, must_exist=True)
        target = root / name
        try:
            for item in source.rglob("*"):
                if item.is_symlink():
                    continue
                relative = item.relative_to(source)
                destination = target / relative
                if item.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                elif item.is_file():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, destination)
            skill_md = target / "SKILL.md"
            if skill_md.is_file():
                visible[name] = f"_agent/skills/{name}/SKILL.md"
        except OSError:
            continue
    return visible


def materialize_enabled_skills(workspace: Path) -> dict[str, str]:
    """Expose enabled profile skill packages inside one run workspace.

    Hermes can normally discover profile skills through ``HERMES_HOME``.  Its
    Codex app-server compatibility route, however, is intentionally confined
    to the run workspace.  Keeping this small public entry point lets native
    and portable harnesses share the same stable ``_agent/skills`` paths
    without injecting duplicate identity or memory text into the prompt.
    """
    ensure_seeded()
    skills = [
        skill for skill in list_skills()
        if skill["enabled"] and (skill.get("content") or "").strip()
    ]
    return _materialize_skills(workspace, skills)


def persona_preamble(
    *, include_memory: bool | None = None, workspace: Path | None = None
) -> str:
    """Render identity + lazy skill references + memory as a prompt preamble.

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
        visible = _materialize_skills(workspace, skills) if workspace is not None else {}
        skill_root = _skills_dir()
        indexed = [s for s in skills if workspace is None or s["name"] in visible]
        index = "\n".join(
            f"- {s['name']}: {s['description']} "
            f"(source: {visible.get(s['name']) or (skill_root / s['name'] / 'SKILL.md')})"
            for s in indexed
        )
        if indexed:
            blocks.append(
                f"=== SKILLS ({len(indexed)}) ===\n"
                "Enabled skills are listed below. If a skill is relevant and you "
                "have a file-reading tool, read its source before using it; do not "
                "load unrelated skills.\n"
                + index
            )

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
