"""Engine registry: built-in engines plus external CLI harnesses.

External harnesses (Codex CLI, OpenClaude, OpenHands) run as
subprocesses inside the per-run workspace. Command templates can be overridden
via environment variables so users can adapt to their local install.

Template placeholders: {prompt} {workspace} {problem_file}
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


def proof_prompt(problem_text: str, workspace: Path | None = None) -> str:
    text = _PROOF_INSTRUCTION.format(problem=problem_text)
    if workspace is not None:
        # Embedded agents (e.g. openclaw) may run tools from their own home
        # workspace — pin the absolute path so proof.tex lands in the run dir.
        text = (
            f"Your working directory for this task is: {workspace} "
            f"(absolute path — write proof.tex THERE).\n" + text
        )
    return text


CLI_ENGINES: dict[str, dict[str, Any]] = {
    "codex": {
        "id": "codex",
        "label": "Codex CLI",
        "vendor": "OpenAI",
        "description": "Terminal coding agent (codex exec)",
        "kind": "cli",
        "url": "https://github.com/openai/codex",
        "binary": "codex",
        "cmd_env": "CODEX_CMD",
        # --json emits JSONL events (incl. per-turn token usage) instead of TTY text.
        "default_cmd": 'codex exec --json --cd {workspace} --sandbox workspace-write --skip-git-repo-check {prompt}',
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
        "cmd_env": "OPENCLAUDE_CMD",
        # Saved provider profiles may point at Opengateway (needs its own key),
        # so pin the provider to OpenAI which reads OPENAI_API_KEY from env.
        # stream-json --verbose emits JSONL events with usage + total_cost_usd.
        "default_cmd": (
            "openclaude --print --provider openai --model gpt-5.2 "
            "--output-format stream-json --verbose "
            "--dangerously-skip-permissions {prompt}"
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
        "cmd_env": "OPENHANDS_CMD",
        # --override-with-envs lets headless mode boot from LLM_MODEL/LLM_API_KEY
        # (injected by the job runner) instead of interactive settings.
        "default_cmd": (
            "openhands --headless --always-approve --exit-without-confirmation "
            "--override-with-envs -t {prompt}"
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
        # Embedded local agent (no gateway daemon needed); reads OPENAI_API_KEY
        # etc. from env. Session JSONL (parsed post-run) carries per-message
        # usage + cost. {ws_name} keeps sessions isolated per run.
        "default_cmd": (
            "{home}/.npm-global/bin/openclaw agent --local --json --agent main "
            "--session-key agent:main:{ws_name} "
            "--model openai/gpt-5.5 --timeout 3000 -m {prompt}"
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
    if binary and shutil.which(binary):
        return True, spec.get("default_cmd")
    for cand in spec.get("binary_candidates") or []:
        if Path(cand).expanduser().exists():
            return True, spec.get("default_cmd")
    return False, None


def list_engines() -> list[dict[str, Any]]:
    """All engines with availability info for the UI."""
    out: list[dict[str, Any]] = []
    for e in BUILTIN_ENGINES:
        out.append({**e, "available": True, "hint": None})
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
            }
        )
    return out


def all_engine_ids() -> set[str]:
    return {e["id"] for e in BUILTIN_ENGINES} | set(CLI_ENGINES.keys())


def build_cli_command(engine_id: str, *, prompt: str, workspace: Path, problem_file: Path) -> list[str] | None:
    """Resolve a CLI engine's command as argv list, or None if unavailable."""
    spec = CLI_ENGINES.get(engine_id)
    if not spec:
        return None
    ok, template = _cli_available(spec)
    if not ok or not template:
        return None
    argv: list[str] = []
    for token in shlex.split(template):
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
    return [str(Path(p).expanduser()) for p in spec.get("extra_path") or []]
