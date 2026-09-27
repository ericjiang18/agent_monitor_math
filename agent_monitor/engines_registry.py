"""Engine registry: built-in engines plus external CLI harnesses.

External harnesses (Codex CLI, OpenClaude, OpenHands) run as
subprocesses inside the per-run workspace. Command templates can be overridden
via environment variables so users can adapt to their local install.

Template placeholders: {prompt} {workspace} {problem_file} {model_args}
"""
from __future__ import annotations

import os
import shlex
import shutil
from pathlib import Path
from typing import Any

BUILTIN_ENGINES: list[dict[str, Any]] = [
    {
        "id": "hermes",
        "label": "Hermes",
        "vendor": "Nous Research",
        "description": "Vendored agent harness — tools, code, subagents",
        "kind": "builtin",
        "url": "https://github.com/NousResearch/hermes-agent",
    },
    {
        "id": "improof",
        "label": "IMProof",
        "vendor": "FirstProof",
        "description": "Author–Critic ProofStack workflow (batch-2)",
        "kind": "builtin",
        "url": "https://github.com/1stproof/batch-2",
    },
    {
        "id": "ucla",
        "label": "UCLA Harness",
        "vendor": "UCLA",
        "description": "Literature → advisor → solvers → verify",
        "kind": "builtin",
        "url": None,
    },
]

# ── External CLI harnesses ────────────────────────────────────────────────
# Each defines: availability check + default command template.
# Prompts instruct the agent to write proof.tex inside the workspace (cwd).

_PROOF_INSTRUCTION = (
    "You are solving an informal mathematics proof problem. "
    "Work inside the current directory. Maintain your evolving proof in "
    "./proof.md as a Markdown document (use $...$ / $$...$$ for math, headings "
    "for structure); update it as the proof develops. "
    "The problem statement is in ./problem.txt.\n\nPROBLEM:\n{problem}"
)

# Shared by every engine so a run's references are auditable and the console can
# extract them. Kept strict about not inventing sources — a fabricated citation
# is worse than an inline proof of the same fact.
CITATION_REQUIREMENTS = (
    "CITATIONS (required):\n"
    "- Justify each non-obvious step by a definition, a numbered lemma of your "
    "own, or a citation marker like [1].\n"
    "- End proof.md with a `## References` section listing every marker you "
    "used, one per line, formatted as: `[1] <identifier> — <what it gives you>`. "
    "Use resolvable identifiers where they exist (arXiv:2401.01234, "
    "doi:10.1007/..., book + theorem number, URL); for classical results the "
    "name is enough (e.g. `Euclid's lemma`).\n"
    "- Tag steps you proved yourself as `self-derived` rather than citing "
    "literature for them.\n"
    "- Never invent a reference. If you cannot locate a real source, write "
    "`[uncited: standard result]` and prove the fact inline instead.\n"
    "- Every listed reference must be cited somewhere in the text, and every "
    "marker in the text must appear in the list.\n"
)


def proof_prompt(
    problem_text: str,
    workspace: Path | None = None,
    preamble: str = "",
) -> str:
    """Task prompt for CLI engines.

    ``preamble`` carries the operator's persona (identity / skills / memory) for
    engines that cannot read the agent home themselves.
    """
    text = _PROOF_INSTRUCTION.format(problem=problem_text)
    if workspace is not None:
        # Embedded agents (e.g. openclaw) may run tools from their own home
        # workspace — pin the absolute path so proof.tex lands in the run dir.
        text = (
            f"Your working directory for this task is: {workspace} "
            f"(absolute path — write proof.tex THERE).\n" + text
        )
    text = f"{text}\n\n{CITATION_REQUIREMENTS}"
    if preamble.strip():
        text = f"{preamble.strip()}\n\n{text}"
    return text


CLI_ENGINES: dict[str, dict[str, Any]] = {
    "kimi": {
        "id": "kimi",
        "label": "Kimi Proof",
        "vendor": "Moonshot AI",
        "description": "Draft, critique, and refine a proof with Kimi",
        "kind": "cli",
        "url": "https://platform.moonshot.ai",
        "check_file": "agent_monitor/runners/kimi_runner.py",
        "cmd_env": "KIMI_PROOF_CMD",
        "default_cmd": "{root}/.venv/bin/python -u {root}/agent_monitor/runners/kimi_runner.py {prompt}",
        "install_hint": "Add a Kimi API key in Settings",
        "parser_style": "codex",
        "supported_models": ["kimi-k3"],
    },
    "claude": {
        "id": "claude",
        "label": "Claude Code",
        "vendor": "Anthropic",
        "description": "Claude Code proof workspace · sign in and connect an Anthropic key",
        "kind": "cli",
        "url": "https://code.claude.com/docs",
        "binary": "claude",
        "cmd_env": "CLAUDE_CODE_CMD",
        "default_cmd": "claude --bare -p --verbose --output-format stream-json --permission-mode acceptEdits --tools Read,Write,Edit {model_args} {prompt}",
        "install_hint": "Install Claude Code and add an Anthropic API key",
        "parser_style": "openclaude",
    },
    "codex": {
        "id": "codex",
        "label": "Codex CLI",
        "vendor": "OpenAI",
        "description": "Terminal coding agent (codex exec)",
        "kind": "cli",
        "url": "https://github.com/openai/codex",
        "binary": "codex",
        "binary_candidates": ["~/.npm-global/bin/codex"],
        "cmd_env": "CODEX_CMD",
        # --json emits JSONL events (incl. per-turn token usage) instead of TTY text.
        # Landlock (workspace-write) denies all writes on this kernel, so run
        # unsandboxed — each run already gets its own workspace directory.
        "default_cmd": 'codex exec --json --cd {workspace} --sandbox danger-full-access --skip-git-repo-check {model_args} {prompt}',
        "install_hint": "npm install -g @openai/codex  (or: brew install --cask codex)",
    },
    "openclaude": {
        "id": "openclaude",
        "label": "OpenClaude",
        "vendor": "Gitlawb",
        "description": "Open-source coding-agent CLI, multi-provider",
        "kind": "cli",
        "url": "https://github.com/Gitlawb/openclaude",
        "binary": "openclaude",
        "binary_candidates": ["~/.npm-global/bin/openclaude"],
        "cmd_env": "OPENCLAUDE_CMD",
        # The wrapper selects Codex subscription auth when CODEX_HOME is
        # present, otherwise preserving the OpenAI API-key path.
        "default_cmd": (
            "{root}/.venv/bin/python -u "
            "{root}/agent_monitor/runners/openclaude_runner.py {prompt}"
        ),
        "install_hint": "npm install -g @gitlawb/openclaude@latest",
    },
    "openhands": {
        "id": "openhands",
        "label": "OpenHands",
        "vendor": "OpenHands",
        "description": "AI-driven development agent (headless CLI)",
        "kind": "cli",
        "url": "https://github.com/OpenHands/openhands",
        "binary": "openhands",
        "binary_candidates": ["~/.local/bin/openhands"],
        "cmd_env": "OPENHANDS_CMD",
        # Wrapper uses OpenHands Codex ACP for subscription users and execs the
        # original headless API-key CLI otherwise.
        "default_cmd": (
            "{root}/.venv/bin/python -u "
            "{root}/agent_monitor/runners/openhands_subscription_runner.py {prompt}"
        ),
        "install_hint": "uv tool install openhands   (openhands.dev)",
    },
    "openclaw": {
        "id": "openclaw",
        "label": "OpenClaw",
        "vendor": "OpenClaw",
        "description": "Personal AI assistant agent (embedded local run)",
        "kind": "cli",
        "url": "https://github.com/openclaw/openclaw",
        "binary": "openclaw",
        "binary_candidates": ["~/.npm-global/bin/openclaw"],
        "cmd_env": "OPENCLAW_CMD",
        # Wrapper chooses OpenClaw's Codex app-server route for subscription
        # users and keeps the prior API-key route as fallback.
        "default_cmd": (
            "{root}/.venv/bin/python -u "
            "{root}/agent_monitor/runners/openclaw_runner.py {prompt} {ws_name}"
        ),
        # openclaw needs node >=24.15; setup.sh installs one to ~/.local/node24.
        "extra_path": ["~/.local/node24/bin", "~/.npm-global/bin"],
        "install_hint": "npm install -g --prefix ~/.npm-global openclaw@latest (needs Node >= 24.15)",
        "parser_style": "openclaw",
    },
    "deepagents": {
        "id": "deepagents",
        "label": "DeepAgents",
        "vendor": "LangChain",
        "description": "LangGraph deep agent — planning, filesystem, sub-agents",
        "kind": "cli",
        "url": "https://github.com/langchain-ai/deepagents",
        "check_file": "engines/deepagents/.venv/bin/python",
        "cmd_env": "DEEPAGENTS_CMD",
        "default_cmd": (
            "{root}/engines/deepagents/.venv/bin/python -u "
            "{root}/agent_monitor/runners/deepagents_runner.py {prompt}"
        ),
        "install_hint": "./setup.sh  (creates engines/deepagents/.venv)",
        "parser_style": "codex",
    },
    "plain": {
        "id": "plain",
        "label": "Plain (no harness)",
        "vendor": "baseline",
        "description": "Single LLM call — no scaffolding, the baseline",
        "kind": "cli",
        "url": None,
        "check_file": "agent_monitor/runners/plain_runner.py",
        "cmd_env": "PLAIN_CMD",
        "default_cmd": (
            "{root}/.venv/bin/python -u "
            "{root}/agent_monitor/runners/plain_runner.py {prompt}"
        ),
        "install_hint": "needs OPENAI_API_KEY",
        "parser_style": "codex",
    },
    "metaharness": {
        "id": "metaharness",
        "label": "Meta-Harness",
        "vendor": "Stanford IRIS",
        "description": "Harness-evolution loop: solver → evaluator → proposer",
        "kind": "cli",
        "url": "https://github.com/stanford-iris-lab/meta-harness",
        "check_file": "agent_monitor/runners/metaharness_runner.py",
        "cmd_env": "METAHARNESS_CMD",
        "default_cmd": (
            "{root}/.venv/bin/python -u "
            "{root}/agent_monitor/runners/metaharness_runner.py {prompt}"
        ),
        "install_hint": "./setup.sh  (needs OPENAI_API_KEY)",
        "parser_style": "codex",
    },
}

_ROOT = Path(__file__).resolve().parent.parent

CODEX_SUBSCRIPTION_ENGINES = frozenset(
    {
        "hermes",
        "improof",
        "codex",
        "openclaude",
        "openhands",
        "openclaw",
        "deepagents",
        "plain",
        "metaharness",
    }
)
ENGINE_AUTH_MODES: dict[str, list[str]] = {
    "kimi": ["api_key"],
    "claude": ["api_key"],
    "hermes": ["codex_subscription", "api_key"],
    "improof": ["codex_subscription", "api_key"],
    "ucla": ["api_key"],
    "codex": ["codex_subscription", "api_key"],
    "openclaude": ["codex_subscription", "api_key"],
    "openhands": ["codex_subscription", "api_key"],
    "openclaw": ["codex_subscription", "api_key"],
    "deepagents": ["codex_subscription", "api_key"],
    "plain": ["codex_subscription", "api_key"],
    "metaharness": ["codex_subscription", "api_key"],
}

_DEFAULT_EXTRA_PATHS = [
    "~/.local/node24/bin",
    "~/.npm-global/bin",
    "~/.local/bin",
    "~/.cargo/bin",
]


def tool_search_path() -> str:
    """PATH that includes user-level Node / npm / uv tool installs."""
    extras = [str(Path(p).expanduser()) for p in _DEFAULT_EXTRA_PATHS if Path(p).expanduser().is_dir()]
    extras.append(os.environ.get("PATH", ""))
    return os.pathsep.join(extras)


def which_tool(binary: str) -> str | None:
    return shutil.which(binary, path=tool_search_path())


def _cli_available(spec: dict[str, Any]) -> tuple[bool, str | None]:
    """Return (available, resolved_command_template)."""
    override = os.environ.get(spec.get("cmd_env") or "", "")
    if override:
        return True, override
    if spec.get("dir_env"):
        d = os.environ.get(spec["dir_env"], "")
        if not d or not Path(d).is_dir():
            return False, None
        if not spec.get("default_cmd"):
            return False, None
        return True, spec["default_cmd"]
    if spec.get("check_file"):
        if (_ROOT / spec["check_file"]).exists():
            return True, spec.get("default_cmd")
        return False, None
    binary = spec.get("binary")
    if binary and which_tool(binary):
        return True, spec.get("default_cmd")
    for cand in spec.get("binary_candidates") or []:
        if Path(cand).expanduser().exists():
            return True, spec.get("default_cmd")
    return False, None


def list_engines() -> list[dict[str, Any]]:
    """All engines with availability and authentication info for the UI."""
    out: list[dict[str, Any]] = []
    for e in BUILTIN_ENGINES:
        out.append(
            {
                **e,
                "available": True,
                "hint": None,
                "auth_modes": ENGINE_AUTH_MODES.get(e["id"], []),
                "subscription_supported": e["id"] in CODEX_SUBSCRIPTION_ENGINES,
            }
        )
    for spec in CLI_ENGINES.values():
        ok, _cmd = _cli_available(spec)
        out.append(
            {
                "id": spec["id"],
                "label": spec["label"],
                "vendor": spec["vendor"],
                "description": spec["description"],
                "kind": "cli",
                "url": spec["url"],
                "available": ok,
                "hint": None if ok else spec.get("install_hint"),
                "auth_modes": ENGINE_AUTH_MODES.get(spec["id"], []),
                "subscription_supported": spec["id"] in CODEX_SUBSCRIPTION_ENGINES,
                "supported_models": list(spec.get("supported_models") or []),
            }
        )
    return out


def all_engine_ids() -> set[str]:
    return {e["id"] for e in BUILTIN_ENGINES} | set(CLI_ENGINES.keys())


def supported_models(engine_id: str) -> list[str]:
    """Engine-specific allowlist; empty means the account catalog applies."""
    return list((CLI_ENGINES.get(engine_id) or {}).get("supported_models") or [])


def build_cli_command(
    engine_id: str,
    *,
    prompt: str,
    workspace: Path,
    problem_file: Path,
    model: str | None = None,
) -> list[str] | None:
    """Resolve a CLI engine's command as argv list, or None if unavailable."""
    spec = CLI_ENGINES.get(engine_id)
    if not spec:
        return None
    ok, template = _cli_available(spec)
    if not ok or not template:
        return None
    argv: list[str] = []
    for token in shlex.split(template):
        if token == "{model_args}":
            if model:
                argv.extend(["--model", model])
            continue
        token = token.replace("{root}", str(_ROOT))
        token = token.replace("{home}", str(Path.home()))
        token = token.replace("{workspace}", str(workspace))
        token = token.replace("{ws_name}", workspace.name)
        token = token.replace("{problem_file}", str(problem_file))
        if "{prompt}" in token:
            token = token.replace("{prompt}", prompt)
        argv.append(token)
    return argv


def engine_extra_path(engine_id: str) -> list[str]:
    """Expanded PATH prefixes an engine's subprocess needs (e.g. newer node)."""
    spec = CLI_ENGINES.get(engine_id) or {}
    extras = [str(Path(p).expanduser()) for p in spec.get("extra_path") or []]
    seen = set(extras)
    for p in _DEFAULT_EXTRA_PATHS:
        d = str(Path(p).expanduser())
        if d not in seen:
            extras.append(d)
            seen.add(d)
    return extras
