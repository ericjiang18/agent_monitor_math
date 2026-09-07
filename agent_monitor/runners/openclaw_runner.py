#!/usr/bin/env python3
"""Launch OpenClaw with an isolated API, Codex, or Claude account route."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import shlex
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator

if __package__:
    from .openclaw_claude_cli_bridge import (
        BINARY_ENV as _CLAUDE_BRIDGE_BINARY_ENV,
        CONFIG_HOME_ENV as _CLAUDE_BRIDGE_CONFIG_HOME_ENV,
        PROCESS_HOME_ENV as _CLAUDE_BRIDGE_PROCESS_HOME_ENV,
        PROVIDER_ENV_NAMES as _CLAUDE_PROVIDER_ENV_NAMES,
    )
else:
    # Keep direct registry execution package-correct and independent of cwd or
    # PYTHONPATH. This also prevents a same-named workspace module from being
    # imported in place of the trusted bridge.
    repository_root = Path(__file__).resolve().parents[2]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))
    from agent_monitor.runners.openclaw_claude_cli_bridge import (
        BINARY_ENV as _CLAUDE_BRIDGE_BINARY_ENV,
        CONFIG_HOME_ENV as _CLAUDE_BRIDGE_CONFIG_HOME_ENV,
        PROCESS_HOME_ENV as _CLAUDE_BRIDGE_PROCESS_HOME_ENV,
        PROVIDER_ENV_NAMES as _CLAUDE_PROVIDER_ENV_NAMES,
    )


_KIMI_MODEL_ID = "kimi-k3"
_KIMI_OPENCLAW_PROVIDER = "flashflame"
_KIMI_DEFAULT_BASE_URL = "https://2qg3r7w8aefiukbds.flashflame.ai/v1"
_KIMI_REASONING_EFFORTS = {"low", "high", "max"}
_CLAUDE_CLI_RUNTIME = "claude-cli"
_CLAUDE_MODEL_PROVIDER = "anthropic"
_LEAN_DECLARATION_RE = re.compile(r"(?m)^\s*(?:theorem|lemma|example)\b")
_LEAN_PLACEHOLDER_RE = re.compile(r"(?i)\b(?:sorry|admit)\b")
_MIN_LEAN_ARTIFACT_BYTES = 80
_MAX_LEAN_ARTIFACT_BYTES = 2_000_000


def _ensure_local_gateway_auth(config: dict) -> str | None:
    """Protect one isolated local gateway and return its bearer token.

    OpenClaw's parent agent can run without an explicit gateway credential, but
    subagent announce/history calls fail closed.  A random token scoped to the
    per-run state directory lets those local callbacks authenticate without
    sharing a long-lived operator secret.
    """
    gateway = config.setdefault("gateway", {})
    if not isinstance(gateway, dict):
        gateway = {}
        config["gateway"] = gateway
    gateway.setdefault("mode", "local")
    auth = gateway.setdefault("auth", {})
    if not isinstance(auth, dict):
        auth = {}
        gateway["auth"] = auth
    mode = str(auth.get("mode") or "token")
    auth["mode"] = mode
    if mode != "token":
        return None
    token = auth.get("token")
    if isinstance(token, str) and token.strip():
        return token.strip()
    if token is not None:
        # Preserve a configured SecretRef; OpenClaw resolves it itself.
        return None
    generated = secrets.token_urlsafe(32)
    auth["token"] = generated
    return generated


def _subscription_state_dir(
    account_home: Path,
    workspace: Path,
    workspace_name: str,
) -> Path:
    """Return a run-scoped OpenClaw state directory inside one account home."""
    safe_name = "".join(
        character if character.isalnum() or character in "-_" else "-"
        for character in workspace_name
    )[:80] or "run"
    workspace_hash = hashlib.sha256(str(workspace).encode()).hexdigest()[:12]
    return account_home / "openclaw" / "runs" / f"{safe_name}-{workspace_hash}"


def _claude_account_home(source: dict[str, str] | os._Environ[str]) -> Path:
    """Validate the already-selected per-user Claude account home."""
    raw_config = str(source.get("CLAUDE_CONFIG_DIR") or "").strip()
    raw_home = str(source.get("HOME") or "").strip()
    if not raw_config or not raw_home:
        raise ValueError("Claude subscription route requires an isolated account home")
    config_home = Path(raw_config).expanduser()
    process_home = Path(raw_home).expanduser()
    if (
        not config_home.is_absolute()
        or not process_home.is_absolute()
        or config_home.is_symlink()
        or process_home.is_symlink()
        or not config_home.is_dir()
        or not process_home.is_dir()
    ):
        raise ValueError("Claude subscription account home is invalid")
    config_home = config_home.resolve()
    process_home = process_home.resolve()
    if not config_home == process_home:
        raise ValueError("Claude config and process homes must identify one account")
    return config_home


def _selected_claude_model(source: dict[str, str] | os._Environ[str]) -> str:
    """Return the exact supported Claude model requested by the UI."""
    from agent_monitor.runners.claude_backend import MODEL_ALIASES

    selected = str(
        source.get("AGENT_MONITOR_CLAUDE_MODEL") or "sonnet"
    ).strip().lower()
    if selected not in MODEL_ALIASES:
        rendered = selected or "(empty)"
        raise ValueError(f"Unsupported Claude account model for OpenClaw: {rendered}")
    return selected


def _write_claude_subscription_config(
    state_dir: Path,
    workspace: Path,
    *,
    model_id: str,
    account_home: Path,
    claude_binary: Path,
) -> tuple[Path, str]:
    """Pin OpenClaw to its bundled native Claude CLI backend, with no fallback."""
    bridge = Path(__file__).with_name("openclaw_claude_cli_bridge.py").resolve()
    if not bridge.is_file() or not os.access(bridge, os.X_OK):
        raise ValueError("OpenClaw Claude bridge is missing or not executable")
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_dir.chmod(0o700)
    model_ref = f"{_CLAUDE_MODEL_PROVIDER}/{model_id}"
    config: dict[str, Any] = {
        "agents": {
            "defaults": {
                "model": {"primary": model_ref, "fallbacks": []},
                "models": {
                    model_ref: {"agentRuntime": {"id": _CLAUDE_CLI_RUNTIME}}
                },
                "cliBackends": {
                    _CLAUDE_CLI_RUNTIME: {
                        "command": str(bridge),
                        "env": {
                            _CLAUDE_BRIDGE_CONFIG_HOME_ENV: str(account_home),
                            _CLAUDE_BRIDGE_PROCESS_HOME_ENV: str(account_home),
                            _CLAUDE_BRIDGE_BINARY_ENV: str(claude_binary),
                        },
                        "clearEnv": sorted(_CLAUDE_PROVIDER_ENV_NAMES),
                    }
                },
                "workspace": str(workspace),
                "skipBootstrap": True,
            }
        },
        # Keep host command execution on OpenClaw's allowlist.  Claude still
        # receives the scoped MCP bridge and its non-exec workspace tools, but
        # neither model nor prompt can silently enable arbitrary host commands.
        "tools": {"exec": {"mode": "allowlist", "strictInlineEval": True}},
    }
    gateway_token = _ensure_local_gateway_auth(config)
    if gateway_token:
        os.environ["OPENCLAW_GATEWAY_TOKEN"] = gateway_token
    config_path = state_dir / "openclaw.json"
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    config_path.chmod(0o600)
    return config_path, model_ref


def _scrub_claude_provider_environment() -> None:
    """Remove non-subscription billing routes before launching OpenClaw."""
    for name in _CLAUDE_PROVIDER_ENV_NAMES:
        os.environ.pop(name, None)


def _is_kimi_api_model(model: str | None) -> bool:
    """Return whether one selected OpenClaw model is the Kimi K3 API model."""
    normalized = str(model or "").strip().lower().replace(":", "/")
    return normalized.rsplit("/", 1)[-1] == _KIMI_MODEL_ID


def _write_kimi_provider_config(state_dir: Path, workspace: Path) -> Path:
    """Materialize a per-run OpenAI-Completions provider without the API key.

    OpenClaw does not dynamically register unknown ``openai/*`` model ids.  A
    custom provider catalog entry is therefore required for Kimi K3.  The
    config stores only an environment reference; OpenClaw resolves the actual
    key from the child process environment at request time.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    config_path = state_dir / "openclaw.json"
    config: dict[str, Any] = {}
    if config_path.is_file():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                config = loaded
        except (OSError, json.JSONDecodeError):
            config = {}

    models = config.setdefault("models", {})
    if not isinstance(models, dict):
        models = {}
        config["models"] = models
    models.setdefault("mode", "merge")
    providers = models.setdefault("providers", {})
    if not isinstance(providers, dict):
        providers = {}
        models["providers"] = providers

    base_url = str(
        os.environ.get("KIMI_API_BASE") or _KIMI_DEFAULT_BASE_URL
    ).rstrip("/")
    effort = str(os.environ.get("KIMI_REASONING_EFFORT") or "max").lower()
    if effort not in _KIMI_REASONING_EFFORTS:
        effort = "max"
    providers[_KIMI_OPENCLAW_PROVIDER] = {
        "baseUrl": base_url,
        "apiKey": "${KIMI_API_KEY}",
        "api": "openai-completions",
        "timeoutSeconds": 300,
        "models": [
            {
                "id": _KIMI_MODEL_ID,
                "name": "Kimi K3",
                "reasoning": True,
                "input": ["text"],
                "contextWindow": 200_000,
                "maxTokens": 32_768,
                "compat": {
                    "supportsTools": True,
                    "supportedReasoningEfforts": ["low", "high", "max"],
                },
            }
        ],
    }

    agents = config.setdefault("agents", {})
    if not isinstance(agents, dict):
        agents = {}
        config["agents"] = agents
    defaults = agents.setdefault("defaults", {})
    if not isinstance(defaults, dict):
        defaults = {}
        agents["defaults"] = defaults
    selected = f"{_KIMI_OPENCLAW_PROVIDER}/{_KIMI_MODEL_ID}"
    defaults["model"] = {"primary": selected}
    defaults["workspace"] = str(workspace)
    defaults["skipBootstrap"] = True
    visible_models = defaults.setdefault("models", {})
    if not isinstance(visible_models, dict):
        visible_models = {}
        defaults["models"] = visible_models
    visible_models[selected] = {
        "params": {"extra_body": {"reasoning_effort": effort}}
    }

    gateway_token = _ensure_local_gateway_auth(config)
    if gateway_token:
        os.environ["OPENCLAW_GATEWAY_TOKEN"] = gateway_token
    config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    try:
        config_path.chmod(0o600)
    except OSError:
        pass
    return config_path


@contextlib.contextmanager
def _openclaw_api_model_runtime(
    workspace: Path,
    requested_model: str,
) -> Iterator[str]:
    """Select a per-run custom provider only for Kimi K3 API requests."""
    if not _is_kimi_api_model(requested_model):
        yield requested_model
        return

    previous_state = os.environ.get("OPENCLAW_STATE_DIR")
    previous_gateway_token = os.environ.get("OPENCLAW_GATEWAY_TOKEN")
    temporary: tempfile.TemporaryDirectory[str] | None = None
    if previous_state:
        state_dir = Path(previous_state).expanduser()
    else:
        temporary = tempfile.TemporaryDirectory(prefix="agent-monitor-openclaw-kimi-")
        state_dir = Path(temporary.name)
    os.environ["OPENCLAW_STATE_DIR"] = str(state_dir)
    try:
        _write_kimi_provider_config(state_dir, workspace)
        yield f"{_KIMI_OPENCLAW_PROVIDER}/{_KIMI_MODEL_ID}"
    finally:
        if previous_state is None:
            os.environ.pop("OPENCLAW_STATE_DIR", None)
        else:
            os.environ["OPENCLAW_STATE_DIR"] = previous_state
        if previous_gateway_token is None:
            os.environ.pop("OPENCLAW_GATEWAY_TOKEN", None)
        else:
            os.environ["OPENCLAW_GATEWAY_TOKEN"] = previous_gateway_token
        if temporary is not None:
            temporary.cleanup()


def _bind_workspace_tool_python(
    workspace: Path,
    executable: str | None = None,
    proof_tools_path: Path | None = None,
) -> list[Path]:
    """Bind exact trusted starter wrappers before OpenClaw sanitizes env vars."""
    python = shlex.quote(executable or sys.executable)
    proof_tools = shlex.quote(
        str(
            (
                proof_tools_path
                or Path(__file__).resolve().parents[1] / "proof_tools.py"
            ).resolve()
        )
    )
    replacements = {
        "exact-math-certificate.sh": (
            "exact-math-certificate",
            f'exec {python} {proof_tools} exact-math-certificate "${{1:-}}"',
        ),
        "bounded-counterexample-search.sh": (
            "bounded-counterexample-search",
            f'exec {python} {proof_tools} bounded-counterexample-search "${{1:-}}"',
        ),
        "statement-fidelity-audit.sh": (
            "statement-fidelity-audit",
            f'exec {python} {proof_tools} statement-fidelity-audit "${{1:-}}"',
        ),
        "audit-output-validator.sh": (
            "audit-output-validator",
            f'exec {python} {proof_tools} audit-output-validator "${{1:-}}"',
        ),
        "pdf-text-extract.sh": (
            None,
            f'{python} - "$resolved" "$start_page" "$end_page" "$max_chars"',
        ),
    }
    changed: list[Path] = []
    root = workspace / "_library" / "tools"
    for name, (command, new) in replacements.items():
        path = root / name
        try:
            body = path.read_text(encoding="utf-8")
        except OSError:
            continue
        updated = body
        if command is None:
            old = (
                '"${AGENT_MONITOR_PYTHON:-python3}" - "$resolved" '
                '"$start_page" "$end_page" "$max_chars"'
            )
            updated = body.replace(old, new, 1)
        else:
            expected_tail = [
                "-m",
                "agent_monitor.proof_tools",
                command,
                "${1:-}",
            ]
            lines = body.splitlines(keepends=True)
            for index, line in enumerate(lines):
                ending = "\n" if line.endswith("\n") else ""
                candidate = line[:-1] if ending else line
                try:
                    tokens = shlex.split(candidate)
                except ValueError:
                    continue
                # The executable token may be a placeholder or an already
                # bound absolute path. Everything else must match exactly.
                if (
                    len(tokens) == 6
                    and tokens[0] == "exec"
                    and tokens[2:] == expected_tail
                ):
                    lines[index] = new + ending
                    updated = "".join(lines)
                    break
        if updated == body:
            continue
        path.write_text(updated, encoding="utf-8")
        path.chmod(0o755)
        changed.append(path)
    return changed


_AUDIT_GATE_MARKER = "CANDIDATE AUDIT GATE:"
_AUDIT_CONTRACT_FILES = {
    "global.json",
    "decomposed.json",
    "verifier-output.json",
    "merge-map.json",
    "run.json",
}
_RECOGNIZED_PASS_STEMS = {
    "global",
    "global-pass",
    "global-audit",
    "global-rerun",
    "decomposed",
    "decomposed-pass",
    "decomposed-audit",
    "decomposed-rerun",
    "boundary",
    "boundary-pass",
    "boundary-audit",
    "boundary-clause-sweep",
    "boundary-rerun",
    "citation",
    "citation-pass",
    "citation-audit",
    "citation-rerun",
    "refuter",
    "refuter-pass",
    "refuter-audit",
    "refuter-rerun",
    "verifier-output",
    "merged",
    "merge",
}
_OUTCOME_RE = re.compile(
    r"(?im)^[ \t]*\*{0,2}ProvingConsole outcome:[ \t]*"
    r"(?:Solved|Counterexample|Known/Open Status|Partial Progress)"
    r"[ \t]*(?:[.!?][ \t]*)?\*{0,2}(?:[ \t]*[.!?])?[ \t]*$"
)
_OUTCOME_HEADING_RE = re.compile(
    r"(?im)^[ \t]*#{1,6}[ \t]*ProvingConsole outcome[ \t]*$"
    r"(?:\r?\n[ \t]*)+^[ \t]*\*{0,2}"
    r"(?:Solved|Counterexample|Known/Open Status|Partial Progress)"
    r"[ \t]*(?:[.!?][ \t]*)?\*{0,2}(?:[ \t]*[.!?])?[ \t]*$"
)
_OUTCOME_LABEL_RE = re.compile(
    r"(?i)\b(Solved|Counterexample|Known/Open Status|Partial Progress)\b"
)
_OUTCOME_CANONICAL_LABELS = {
    "solved": "Solved",
    "counterexample": "Counterexample",
    "known/open status": "Known/Open Status",
    "partial progress": "Partial Progress",
}
_AUDIT_LAST_MILE = """

OPENCLAW AUDIT LAST-MILE CHECK: When the candidate-audit gate activates, the
parent agent must not stop after child/global/decomposed/refuter outputs. Read
the merge protocol, validate every structured pass, and reconcile all
singleton and conflicting findings. After repairing proof.md, freeze its exact
bytes as audit/<run-id>/final-candidate.md, rerun global and decomposed passes
against it as global-rerun.json and decomposed-rerun.json, then rebuild
verifier-output.json and merge-map.json. run.json must include a
research-proof-audit.reconciliation.v1 object that binds the root proof hash,
final-candidate hash, and current reruns. Run the installed
validate_reconciliation.py on audit/<run-id> and do not edit proof.md
afterward. If that cannot be completed, explicitly record degraded audit and
the missing artifact; never represent unmerged child verdicts as a complete
audit.

LOCAL ADAPTER LIFECYCLE: Do not use sessions_spawn or sessions_yield for these
audit passes. In the local CLI adapter, yielding ends the parent process and
closes the gateway before child completion announcements can be merged. Run
the passes serially in the parent with separately frozen inputs, record
`degraded_independence` in run.json, and complete the required last-mile files
before returning.
""".strip()


def _augment_audit_prompt(prompt: str) -> str:
    if _AUDIT_GATE_MARKER not in prompt or _AUDIT_LAST_MILE in prompt:
        return prompt
    return f"{prompt.rstrip()}\n\n{_AUDIT_LAST_MILE}"


def _proof_sha256(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _validate_fresh_formal_lean_artifact(
    workspace: Path,
    *,
    prior_sha256: str | None,
) -> tuple[bool, str]:
    """Validate one new root Proof.lean without promoting an unchecked result."""
    root = workspace.resolve()
    proof = workspace / "Proof.lean"
    try:
        if proof.is_symlink():
            return False, "Proof.lean must be a regular file, not a symbolic link"
        if not proof.is_file():
            return False, "OpenClaw did not create root Proof.lean"
        if proof.resolve().parent != root:
            return False, "Proof.lean resolved outside the workspace root"
        size = proof.stat().st_size
        if not _MIN_LEAN_ARTIFACT_BYTES <= size <= _MAX_LEAN_ARTIFACT_BYTES:
            return False, "Proof.lean is empty, truncated, or unreasonably large"
        raw = proof.read_bytes()
        current_sha256 = hashlib.sha256(raw).hexdigest()
        if prior_sha256 is not None and current_sha256 == prior_sha256:
            return False, "OpenClaw left the previous Proof.lean unchanged"
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False, "Proof.lean could not be read as a regular UTF-8 file"
    if _LEAN_DECLARATION_RE.search(text) is None:
        return False, "Proof.lean contains no theorem, lemma, or example declaration"
    if _LEAN_PLACEHOLDER_RE.search(text) is not None:
        return False, "Proof.lean contains a forbidden sorry/admit placeholder"
    return True, "OpenClaw produced a fresh Proof.lean for the outer Lean checker"


def _emit_formal_lean_artifact_status(accepted: bool, detail: str) -> None:
    print(
        json.dumps(
            {
                "payloads": [{"text": detail}],
                "agentMonitorFormalLeanArtifactAccepted": accepted,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


def _root_candidate(workspace: Path) -> Path | None:
    """Return one bounded regular root proof without following links."""
    root = workspace.resolve()
    candidates: list[Path] = []
    for name in ("proof.md", "proof.tex"):
        raw = workspace / name
        try:
            resolved = raw.resolve()
            if (
                raw.is_symlink()
                or not raw.is_file()
                or resolved.parent != root
                or not 40 <= raw.stat().st_size <= 10_000_000
            ):
                continue
            candidates.append(raw)
        except OSError:
            continue
    return max(candidates, key=lambda path: path.stat().st_mtime) if candidates else None


def _validated_audit_contract_exists(workspace: Path) -> bool:
    """Trust native audit completion only after runner-side schema validation."""
    audit_root = workspace / "audit"
    if audit_root.is_symlink() or not audit_root.is_dir():
        return False
    from agent_monitor.runners.deepseek_harness_runner import (
        _valid_merge_map,
        _validate_verifier_document,
    )

    for directory in sorted(audit_root.iterdir())[:64]:
        if directory.is_symlink() or not directory.is_dir():
            continue
        documents: dict[str, Any] = {}
        for name in _AUDIT_CONTRACT_FILES:
            path = directory / name
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 5_000_000:
                break
            try:
                documents[name] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                break
        else:
            manifest = documents.get("run.json")
            if not isinstance(manifest, dict) or not manifest:
                continue
            if not _valid_merge_map(documents.get("merge-map.json")):
                continue
            validated = True
            recognized: dict[str, dict[str, Any]] = {
                "global.json": documents["global.json"],
                "decomposed.json": documents["decomposed.json"],
                "verifier-output.json": documents["verifier-output.json"],
            }
            for path in directory.iterdir():
                normalized = path.name.lower().replace("_", "-")
                stem = normalized.removesuffix(".json")
                if (
                    path.name in _AUDIT_CONTRACT_FILES
                    or not normalized.endswith(".json")
                    or (
                        stem not in _RECOGNIZED_PASS_STEMS
                        and not stem.endswith("-refuter")
                    )
                ):
                    continue
                try:
                    document = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    validated = False
                    break
                if not isinstance(document, dict):
                    validated = False
                    break
                recognized[path.name] = document
            for name, document in recognized.items():
                if not isinstance(document, dict):
                    validated = False
                    break
                label = "native-" + re.sub(r"[^a-z0-9-]+", "-", name.lower())
                valid, _detail = _validate_verifier_document(directory, label, document)
                if not valid:
                    validated = False
                    break
            if validated:
                from agent_monitor.research_audit import reconciliation_complete

                if not reconciliation_complete(workspace, directory):
                    continue
                return True
    return False


def _persist_terminal_candidate(
    workspace: Path,
    terminal: tuple[Path, str] | None,
) -> Path | None:
    """Persist a substantive terminal answer before supplemental auditing."""
    existing = _root_candidate(workspace)
    if existing is not None:
        return existing
    text = str(terminal[1] if terminal else "").strip()
    if len(text) < 40:
        return None
    target = workspace / "proof.md"
    if target.is_symlink():
        return None
    target.write_text(text.rstrip() + "\n", encoding="utf-8")
    return target


def _normalize_requested_outcome(workspace: Path, prompt: str) -> bool:
    """Write one exact footer line when the prompt requests the outcome contract."""
    if "provingconsole outcome:" not in (prompt or "").lower():
        return False
    candidate = _root_candidate(workspace)
    if candidate is None:
        return False
    try:
        text = candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    matches = [*_OUTCOME_RE.finditer(text), *_OUTCOME_HEADING_RE.finditer(text)]
    labels: set[str] = set()
    for match in matches:
        selected = _OUTCOME_LABEL_RE.search(match.group(0))
        if selected is not None:
            canonical = _OUTCOME_CANONICAL_LABELS.get(
                selected.group(1).casefold()
            )
            if canonical is not None:
                labels.add(canonical)
    label = "Partial Progress"
    # Repeated copies of one verdict are harmless. Conflicting verdicts and a
    # missing verdict both fail closed rather than promoting the candidate.
    if len(labels) == 1:
        label = next(iter(labels))
    cleaned = _OUTCOME_HEADING_RE.sub("", _OUTCOME_RE.sub("", text)).rstrip()
    rendered = cleaned + f"\n\nProvingConsole outcome: {label}\n"
    if rendered == text:
        return False
    try:
        candidate.write_text(rendered, encoding="utf-8")
    except OSError:
        return False
    return True


def _api_audit_call(system: str, user: str, *, model: str, emit_event=None):
    from agent_monitor.runners.api_backend import api_chat

    result = api_chat(system, user, model=model)
    callback = emit_event or (lambda _event: None)
    callback({"type": "turn.completed", "usage": result.usage})
    return result


def _claude_audit_call(system: str, user: str, *, model: str, emit_event=None):
    """Use the same isolated Claude subscription for structured audit passes."""
    from agent_monitor.runners.claude_backend import (
        ClaudeBackendError,
        claude_exec,
        forward_as_codex_event,
    )
    from agent_monitor.runners.codex_backend import CodexBackendError

    callback = emit_event or (lambda _event: None)

    def forward(event: dict) -> None:
        forward_as_codex_event(event, callback)

    try:
        return claude_exec(
            system,
            user,
            model=model,
            emit_event=forward,
            enable_tools=False,
            max_turns=1,
        )
    except ClaudeBackendError as exc:
        # The shared audit orchestrator's retry/fail-closed boundary is typed
        # around CodexBackendError; translate transport failure, never output.
        raise CodexBackendError(f"Claude supplemental audit failed: {exc}") from exc


def _run_supplemental_audit(workspace: Path, prompt: str, model: str) -> bool:
    """Complete an explicitly requested audit without replacing native reasoning."""
    if _AUDIT_GATE_MARKER.lower() not in prompt.lower():
        return False
    if _validated_audit_contract_exists(workspace):
        return False
    candidate_path = _root_candidate(workspace)
    if candidate_path is None:
        return False
    candidate = candidate_path.read_text(encoding="utf-8", errors="replace")
    from agent_monitor.runners.api_backend import APIBackendError
    from agent_monitor.runners.codex_backend import CodexBackendError, subscription_enabled
    from agent_monitor.runners.claude_backend import (
        subscription_enabled as claude_subscription_enabled,
    )
    from agent_monitor.runners.deepseek_harness_runner import (
        ensure_requested_outcome,
        run_codex_audit,
    )

    print(
        json.dumps(
            {
                "payloads": [
                    {
                        "text": "OpenClaw · trusted supplemental audit · "
                        "global, decomposed, refuter, merge, adjudication"
                    }
                ],
                "agentMonitorSupplementalAudit": True,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    if subscription_enabled():
        call = None
    elif claude_subscription_enabled():
        call = _claude_audit_call
    else:
        call = _api_audit_call
    try:
        final = run_codex_audit(
            prompt=prompt,
            candidate=candidate,
            model=model,
            call=call,
            adapter="openclaw-response-orchestrated-audit",
            runtime_label="OpenClaw",
            audit_prefix="openclaw",
        )
        final = ensure_requested_outcome(prompt, final)
        target = workspace / "proof.md"
        if target.is_symlink():
            raise OSError("refusing to overwrite linked proof.md")
        target.write_text(final.rstrip() + "\n", encoding="utf-8")
    except (APIBackendError, CodexBackendError, OSError, ValueError) as exc:
        cleaned = _OUTCOME_RE.sub("", candidate).rstrip()
        degraded = (
            cleaned
            + "\n\n## Audit status\n\n"
            + "degraded audit: OpenClaw's trusted supplemental structured audit "
            + f"did not complete ({type(exc).__name__}: {exc}).\n\n"
            + "ProvingConsole outcome: Partial Progress\n"
        )
        target = workspace / "proof.md"
        if not target.is_symlink():
            target.write_text(degraded, encoding="utf-8")
        print(
            json.dumps(
                {
                    "payloads": [{"text": f"OpenClaw supplemental audit failed: {exc}"}],
                    "agentMonitorSupplementalAudit": False,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return True
    return True


def _recover_checkpointed_proof(
    workspace: Path,
    prompt: str,
    returncode: int,
    *,
    prior_sha256: str | None = None,
) -> bool:
    """Preserve a substantive checkpoint after a late OpenClaw transport error.

    Recovery is deliberately fail-closed: it never promotes a claim, and any
    requested machine-readable outcome is normalized to Partial Progress.
    """
    if returncode == 0:
        return False
    proof = workspace / "proof.md"
    try:
        if proof.is_symlink() or not proof.is_file():
            return False
        text = proof.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    current_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if prior_sha256 is not None and current_sha256 == prior_sha256:
        return False
    if len(text.strip()) < 200:
        return False
    cleaned = _OUTCOME_RE.sub("", text).rstrip()
    notes = [
        "degraded run: OpenClaw transport ended after a substantive proof.md "
        "checkpoint; the checkpoint was preserved conservatively."
    ]
    if _AUDIT_GATE_MARKER in prompt:
        notes.append(
            "degraded audit: the parent transport ended before normal completion; "
            "treat every unvalidated or missing audit artifact as unresolved."
        )
    rendered = cleaned + "\n\n" + "\n\n".join(notes)
    if "provingconsole outcome:" in prompt.lower():
        rendered += "\n\nProvingConsole outcome: Partial Progress"
    proof.write_text(rendered.rstrip() + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "payloads": [
                    {
                        "text": "OpenClaw transport ended after proof.md was checkpointed; "
                        "preserved as Partial Progress."
                    }
                ],
                "agentMonitorCheckpointRecovery": True,
                "originalReturnCode": returncode,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return True


def _session_key(path: Path) -> str | None:
    """Resolve one transcript's OpenClaw session key across storage formats."""
    trajectory = path.with_suffix(".trajectory.jsonl")
    try:
        with trajectory.open("r", encoding="utf-8", errors="replace") as handle:
            for _ in range(12):
                line = handle.readline()
                if not line:
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                value = event.get("sessionKey")
                if isinstance(value, str) and value:
                    return value
    except OSError:
        pass

    # OpenClaw 2026.7 writes a trajectory pointer next to the runtime JSONL
    # and stores the session-key mapping in sessions.json. Keep the legacy
    # trajectory lookup above, then consult that bounded local index without
    # trusting a path from the index to select a different transcript.
    index = path.parent / "sessions.json"
    try:
        if (
            index.is_symlink()
            or not index.is_file()
            or index.stat().st_size > 10_000_000
        ):
            return None
        document = json.loads(index.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    session_id = path.stem
    resolved_path = path.resolve()
    for key, record in document.items():
        if not isinstance(key, str) or not isinstance(record, dict):
            continue
        if str(record.get("sessionId") or "") != session_id:
            continue
        indexed_file = str(record.get("sessionFile") or "").strip()
        if indexed_file:
            candidate = Path(indexed_file).expanduser()
            try:
                if (
                    not candidate.is_absolute()
                    or candidate.resolve() != resolved_path
                ):
                    continue
            except OSError:
                continue
        return key
    return None


def _session_key_matches(path: Path, expected: str | None) -> bool:
    """Match OpenClaw's normalized session keys without widening scope."""
    if expected is None:
        return True
    actual = _session_key(path)
    return actual is not None and actual.casefold() == expected.casefold()


def _session_offsets(
    state_dir: Path, session_key: str | None = None
) -> dict[Path, int]:
    """Snapshot existing transcripts so a continuation cannot reuse an old stop."""
    offsets: dict[Path, int] = {}
    for path in state_dir.glob("agents/*/sessions/*.jsonl"):
        if not _session_key_matches(path, session_key):
            continue
        try:
            offsets[path] = path.stat().st_size
        except OSError:
            continue
    return offsets


def _terminal_event_since(
    state_dir: Path, offsets: dict[Path, int], session_key: str | None = None
) -> tuple[Path, str] | None:
    """Return a newly appended, tool-free terminal assistant response."""
    for path in sorted(state_dir.glob("agents/*/sessions/*.jsonl")):
        if not _session_key_matches(path, session_key):
            continue
        try:
            size = path.stat().st_size
            start = offsets.get(path, 0)
            if start > size:
                start = 0
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(start)
                lines = handle.readlines()
        except OSError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = event.get("message") or {}
            if (
                event.get("type") != "message"
                or message.get("role") != "assistant"
                or message.get("stopReason") != "stop"
            ):
                continue
            text = "\n".join(
                str(block.get("text") or "").strip()
                for block in message.get("content") or []
                if isinstance(block, dict)
                and block.get("type") == "text"
                and str(block.get("text") or "").strip()
            ).strip()
            if text:
                return path, text
    return None


def _session_model_usage_since(
    state_dir: Path,
    offsets: dict[Path, int],
    session_key: str | None = None,
) -> tuple[tuple[str, str], ...]:
    """Read authoritative provider/model records appended by this run only."""
    records: list[tuple[str, str]] = []

    def add(provider: object, model: object) -> None:
        provider_text = str(provider or "").strip().lower()
        model_text = str(model or "").strip().lower()
        if model_text:
            records.append((provider_text, model_text))

    def add_model_usage(container: object) -> None:
        if not isinstance(container, dict):
            return
        usage = container.get("modelUsage")
        if not isinstance(usage, list):
            return
        for item in usage:
            if isinstance(item, dict):
                add(item.get("provider"), item.get("model"))

    for path in sorted(state_dir.glob("agents/*/sessions/*.jsonl")):
        if not _session_key_matches(path, session_key):
            continue
        try:
            size = path.stat().st_size
            start = offsets.get(path, 0)
            if start > size:
                start = 0
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(start)
                lines = handle.readlines()
        except OSError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            message = event.get("message")
            if isinstance(message, dict) and message.get("role") == "assistant":
                add(message.get("provider"), message.get("model"))
                add_model_usage(message)
                add_model_usage(message.get("usage"))
            add_model_usage(event)
            add_model_usage(event.get("usage"))
    return tuple(dict.fromkeys(records))


def _model_id_matches(requested: str, actual: str) -> bool:
    expected = requested.strip().lower().rsplit("/", 1)[-1]
    observed = actual.strip().lower().rsplit("/", 1)[-1]
    if expected.startswith("claude-"):
        return observed == expected or observed.startswith(expected + "-")
    return (
        observed == expected
        or observed.startswith(f"claude-{expected}-")
        or f"-{expected}-" in observed
    )


def _attest_claude_session_model(
    state_dir: Path,
    offsets: dict[Path, int],
    *,
    session_key: str,
    requested: str,
) -> tuple[tuple[str, str], ...]:
    """Fail closed unless new OpenClaw records attest the selected Claude model."""
    records = _session_model_usage_since(state_dir, offsets, session_key)
    if not records:
        raise ValueError("OpenClaw recorded no Claude model usage for this run")
    for provider, model in records:
        if provider not in {"anthropic", "claude-cli"}:
            rendered = provider or "unknown"
            raise ValueError(
                f"OpenClaw used unexpected provider {rendered} for Claude account run"
            )
        if not _model_id_matches(requested, model):
            raise ValueError(
                f"OpenClaw model mismatch: requested {requested}, recorded {model}"
            )
    return records


def _terminate_process_group(proc: subprocess.Popen[bytes], sig: int) -> None:
    try:
        os.killpg(proc.pid, sig)
    except (OSError, ProcessLookupError):
        try:
            proc.send_signal(sig)
        except OSError:
            pass


def _run_with_terminal_watchdog(
    argv: list[str],
    state_dir: Path,
    *,
    grace_s: float = 15.0,
    session_key: str | None = None,
) -> int:
    """Finish a subscription run if OpenClaw leaves Node alive after `stop`."""
    offsets = _session_offsets(state_dir, session_key)
    proc = subprocess.Popen(argv, start_new_session=True)
    prior_handlers: dict[int, object] = {}

    def forward(signum: int, _frame: object) -> None:
        _terminate_process_group(proc, signum)
        raise SystemExit(128 + signum)

    for signum in (signal.SIGTERM, signal.SIGINT):
        prior_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, forward)

    terminal: tuple[Path, str] | None = None
    terminal_at: float | None = None
    try:
        while proc.poll() is None:
            found = _terminal_event_since(state_dir, offsets, session_key)
            if found and terminal_at is None:
                terminal, terminal_at = found, time.monotonic()
            if terminal_at is not None and time.monotonic() - terminal_at >= grace_s:
                _terminate_process_group(proc, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    _terminate_process_group(proc, signal.SIGKILL)
                    proc.wait(timeout=5)
                assert terminal is not None
                print(
                    json.dumps(
                        {
                            "sessionFile": str(terminal[0]),
                            "payloads": [{"text": terminal[1]}],
                            "agentMonitorTerminalWatchdog": True,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                return 0
            time.sleep(0.5)
        return int(proc.returncode or 0)
    finally:
        for signum, handler in prior_handlers.items():
            signal.signal(signum, handler)


def main() -> int:
    prompt = _augment_audit_prompt(sys.argv[1] if len(sys.argv) > 1 else "")
    workspace_name = sys.argv[2] if len(sys.argv) > 2 else Path.cwd().name
    binary = shutil.which("openclaw")
    if not binary:
        print("openclaw is not installed", file=sys.stderr)
        return 127

    codex_subscription = os.environ.get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1"
    claude_subscription = os.environ.get("AGENT_MONITOR_CLAUDE_SUBSCRIPTION") == "1"
    formal_lean_mode = (
        os.environ.get("AGENT_MONITOR_FORMAL_LEAN_MODE") == "1"
        or (
            claude_subscription
            and os.environ.get("AGENT_MONITOR_CLAUDE_LEAN_MODE") == "1"
        )
    )
    if formal_lean_mode:
        prompt = (
            prompt.rstrip()
            + "\n\nOPENCLAW FORMAL LEAN FRESHNESS CONTRACT: You must write a "
            "fresh changed root Proof.lean and then compile that exact version. "
            "Even if the current source already compiles, safely revise its proof "
            "or explanatory doc comment; merely checking it is not completion."
        )
    if codex_subscription and claude_subscription:
        print(
            "OpenClaw requires exactly one account subscription route",
            file=sys.stderr,
        )
        return 78
    subscription = codex_subscription or claude_subscription
    state_dir: Path | None = None
    api_runtime: contextlib.AbstractContextManager[str] | None = None
    if codex_subscription:
        # Keep OpenClaw conversations/config isolated per web user while its
        # Codex app-server re-reads the existing CODEX_HOME login.
        codex_home = Path(os.environ["CODEX_HOME"])
        workspace = Path.cwd().resolve()
        _bind_workspace_tool_python(workspace)
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ.pop("OPENAI_API_KEYS", None)
        os.environ.pop("CODEX_API_KEY", None)
        safe_name = "".join(c if c.isalnum() or c in "-_" else "-" for c in workspace_name)[:80] or "run"
        workspace_hash = hashlib.sha256(str(workspace).encode()).hexdigest()[:12]
        default_state = codex_home / "openclaw" / "runs" / f"{safe_name}-{workspace_hash}"
        state_dir = Path(os.environ.get("OPENCLAW_STATE_DIR") or default_state)
        os.environ["OPENCLAW_STATE_DIR"] = str(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        try:
            state_dir.chmod(0o700)
        except OSError:
            pass
        model_id = os.environ.get("AGENT_MONITOR_CODEX_MODEL", "gpt-5.6-sol")
        if model_id.startswith("openai/"):
            model_id = model_id.split("/", 1)[1]
        model = f"openai/{model_id}"
        # Explicitly mark this model as the ChatGPT/Codex Responses transport.
        # Without this entry OpenClaw treats openai/* as Platform API models
        # and correctly refuses OAuth credentials.
        config_path = state_dir / "openclaw.json"
        config: dict = {}
        if config_path.is_file():
            try:
                loaded = json.loads(config_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    config = loaded
            except (OSError, json.JSONDecodeError):
                config = {}
        if not config:
            config = {
                "models": {
                    "mode": "merge",
                    "providers": {
                        "openai": {
                            "baseUrl": "https://chatgpt.com/backend-api/codex",
                            "api": "openai-chatgpt-responses",
                            "models": [
                                {
                                    "id": model_id,
                                    "name": model_id,
                                    "reasoning": True,
                                    "input": ["text"],
                                    "contextWindow": 272000,
                                    "maxTokens": 128000,
                                }
                            ],
                        }
                    },
                },
                "agents": {
                    "defaults": {
                        "model": {"primary": model},
                        "workspace": str(workspace),
                        "skipBootstrap": True,
                    }
                },
            }
        gateway_token = _ensure_local_gateway_auth(config)
        if gateway_token:
            os.environ["OPENCLAW_GATEWAY_TOKEN"] = gateway_token
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        try:
            config_path.chmod(0o600)
        except OSError:
            pass
    elif claude_subscription:
        workspace = Path.cwd().resolve()
        _bind_workspace_tool_python(workspace)
        try:
            account_home = _claude_account_home(os.environ)
            model_id = _selected_claude_model(os.environ)
        except ValueError as exc:
            print(f"OpenClaw Claude route refused to start: {exc}", file=sys.stderr)
            return 78
        claude_raw = shutil.which("claude")
        if not claude_raw:
            print("claude is not installed", file=sys.stderr)
            return 127
        try:
            claude_binary = Path(claude_raw).expanduser().resolve(strict=True)
        except OSError:
            print("claude executable cannot be resolved", file=sys.stderr)
            return 127
        if not claude_binary.is_file() or not os.access(claude_binary, os.X_OK):
            print("claude executable is not runnable", file=sys.stderr)
            return 127
        _scrub_claude_provider_environment()
        state_dir = _subscription_state_dir(
            account_home,
            workspace,
            workspace_name,
        )
        os.environ["OPENCLAW_STATE_DIR"] = str(state_dir)
        try:
            _config_path, model = _write_claude_subscription_config(
                state_dir,
                workspace,
                model_id=model_id,
                account_home=account_home,
                claude_binary=claude_binary,
            )
        except (OSError, ValueError) as exc:
            print(f"OpenClaw Claude route setup failed: {exc}", file=sys.stderr)
            return 78
    else:
        workspace = Path.cwd().resolve()
        requested_model = os.environ.get(
            "AGENT_MONITOR_OPENCLAW_MODEL", "openai/gpt-5.5"
        )
        api_runtime = _openclaw_api_model_runtime(workspace, requested_model)
        model = api_runtime.__enter__()

    argv = [
        binary,
        "agent",
        "--local",
        "--json",
        "--agent",
        "main",
        "--session-key",
        f"agent:main:{workspace_name}",
        "--model",
        model,
        "--timeout",
        "3000",
        "-m",
        prompt,
    ]
    if subscription and state_dir is not None:
        grace_s = max(
            2.0,
            min(
                float(os.environ.get("AGENT_MONITOR_OPENCLAW_EXIT_GRACE_S", "15")),
                120.0,
            ),
        )
        workspace = Path.cwd().resolve()
        prior_lean_sha256 = (
            _proof_sha256(workspace / "Proof.lean")
            if formal_lean_mode
            else None
        )
        prior_proof_sha256 = (
            None
            if formal_lean_mode
            else _proof_sha256(workspace / "proof.md")
        )
        session_key = f"agent:main:{workspace_name}"
        terminal_offsets = _session_offsets(state_dir, session_key)
        returncode = _run_with_terminal_watchdog(
            argv,
            state_dir,
            grace_s=grace_s,
            session_key=session_key,
        )
        if claude_subscription:
            try:
                model_records = _attest_claude_session_model(
                    state_dir,
                    terminal_offsets,
                    session_key=session_key,
                    requested=model_id,
                )
            except ValueError as exc:
                print(
                    json.dumps(
                        {
                            "payloads": [{"text": str(exc)}],
                            "agentMonitorClaudeModelAttested": False,
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                return 65
            print(
                json.dumps(
                    {
                        "agentMonitorClaudeModelAttested": True,
                        "modelUsage": [
                            {"provider": provider, "model": used_model}
                            for provider, used_model in model_records
                        ],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        if formal_lean_mode:
            if returncode != 0:
                _emit_formal_lean_artifact_status(
                    False,
                    "OpenClaw ended before completing the formal Lean run "
                    f"(exit {returncode}); Proof.lean was not promoted.",
                )
                return returncode
            accepted, detail = _validate_fresh_formal_lean_artifact(
                workspace,
                prior_sha256=prior_lean_sha256,
            )
            _emit_formal_lean_artifact_status(accepted, detail)
            return 0 if accepted else 65
        terminal = _terminal_event_since(state_dir, terminal_offsets, session_key)
        _persist_terminal_candidate(workspace, terminal)
        recovered = _recover_checkpointed_proof(
            workspace,
            prompt,
            returncode,
            prior_sha256=prior_proof_sha256,
        )
        effective_returncode = 0 if recovered else returncode
        if effective_returncode == 0:
            _normalize_requested_outcome(workspace, prompt)
            _run_supplemental_audit(workspace, prompt, model_id)
        return effective_returncode

    # Keep the wrapper alive for the trusted API-mode audit last mile. The
    # native child inherits this process group, so the outer job controller can
    # still stop the whole run without leaking a detached OpenClaw process.
    try:
        workspace = Path.cwd().resolve()
        prior_lean_sha256 = (
            _proof_sha256(workspace / "Proof.lean")
            if formal_lean_mode
            else None
        )
        returncode = subprocess.call(argv)
        if formal_lean_mode:
            if returncode != 0:
                _emit_formal_lean_artifact_status(
                    False,
                    "OpenClaw ended before completing the formal Lean run "
                    f"(exit {returncode}); Proof.lean was not promoted.",
                )
                return int(returncode)
            accepted, detail = _validate_fresh_formal_lean_artifact(
                workspace,
                prior_sha256=prior_lean_sha256,
            )
            _emit_formal_lean_artifact_status(accepted, detail)
            return 0 if accepted else 65
        if returncode == 0:
            _normalize_requested_outcome(workspace, prompt)
            _run_supplemental_audit(workspace, prompt, model)
        return int(returncode)
    finally:
        if api_runtime is not None:
            api_runtime.__exit__(*sys.exc_info())


if __name__ == "__main__":
    raise SystemExit(main())
