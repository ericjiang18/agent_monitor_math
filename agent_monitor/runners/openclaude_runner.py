#!/usr/bin/env python3
"""Select OpenClaude's subscription or API-key provider at run time."""
from __future__ import annotations

import os
import shutil
import sys


AUDIT_LAST_MILE_MARKER = "OPENCLAUDE AUDIT LAST-MILE CONTRACT:"
AUDIT_LAST_MILE = """

OPENCLAUDE AUDIT LAST-MILE CONTRACT: This applies only because the prompt
explicitly activated CANDIDATE AUDIT GATE. Reserve the final five turns for a
single audit/<run-id>/ directory. Before the final answer, ensure that directory
contains schema-valid global.json, decomposed.json, verifier-output.json,
merge-map.json, and a nonempty run.json. run.json must record the run id,
executed passes, independence method, degraded_independence boolean, and any
repairs. After all repairs, freeze the exact final proof.md bytes as
final-candidate.md, rerun global and decomposed checks as global-rerun.json and
decomposed-rerun.json, and rebuild verifier-output.json and merge-map.json.
run.json must include a research-proof-audit.reconciliation.v1 object binding
the root proof hash, frozen final-candidate hash, and current reruns.
merge-map.json must use schema_version
ensemble-paper-audit-app.merge-map.v1 plus findings and counts with exactly
global_only, decomposed_only, and both. Actually run the locked
audit-output-validator on every verifier JSON, then run
`_agent/skills/research-proof-audit/scripts/validate_reconciliation.py
audit/<run-id>`. Do not edit proof.md afterward. If any required file or
validation remains missing, write `degraded audit:` and the exact gap in
proof.md; do not imply completion and do not let an audit verdict promote an
open-problem claim to Solved.
""".strip()


def _augment_audit_prompt(prompt: str) -> str:
    """Add a bounded artifact-completion reminder only to explicit audit runs."""
    lowered = prompt.lower()
    if "candidate audit gate:" not in lowered or AUDIT_LAST_MILE_MARKER.lower() in lowered:
        return prompt
    return prompt.rstrip() + "\n\n" + AUDIT_LAST_MILE


def _codex_adapter_model(requested: str | None) -> str:
    # The current OpenClaude OAuth path authenticates only through this
    # shortcut. It resolves to gpt-5.6-sol in the installed adapter.
    return "codexplan"


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    prompt = _augment_audit_prompt(prompt)
    binary = shutil.which("openclaude")
    if not binary:
        print("openclaude is not installed", file=sys.stderr)
        return 127

    common = [
        binary,
        "--print",
        "--output-format",
        "stream-json",
        "--verbose",
        "--dangerously-skip-permissions",
        "--no-session-persistence",
        "--bare",
        "--max-turns",
        # Source-sensitive proof audits routinely spend more than twenty turns
        # fetching and checking primary material before they can write the
        # requested artifact. OpenClaude exits at the cap without a final
        # answer, so keep enough headroom for the write/verify phase.
        os.environ.get("AGENT_MONITOR_OPENCLAUDE_MAX_TURNS", "60"),
    ]
    if os.environ.get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1":
        # `--provider openai` plus the `codexplan` alias selects OpenClaude's
        # Codex Responses transport. CLAUDE_CODE_USE_OPENAI must stay unset:
        # in v0.28 it means ordinary API-key compatibility mode and validates
        # OPENAI_API_KEY before the Codex shortcut is resolved.
        os.environ.pop("CLAUDE_CODE_USE_OPENAI", None)
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ.pop("OPENAI_API_KEYS", None)
        os.environ.pop("CODEX_API_KEY", None)
        # Provider settings saved for ordinary OpenAI API use must not bleed
        # into the Codex shortcut. In OpenClaude 0.28 a non-local base URL is
        # enough to make the preflight demand an API key before `codexplan`
        # can select OAuth transport.
        for name in (
            "OPENAI_BASE_URL",
            "OPENAI_API_BASE",
            "OPENAI_API_FORMAT",
            "OPENAI_AZURE_STYLE",
            "OPENAI_AUTH_HEADER",
            "OPENAI_AUTH_SCHEME",
            "OPENAI_AUTH_HEADER_VALUE",
        ):
            os.environ.pop(name, None)
        # The installed adapter explicitly maps the current Codex model set;
        # unknown or stale UI values safely fall back to its stable alias. Keep
        # OPENAI_MODEL on that shortcut so OpenClaude selects OAuth transport;
        # ``--model`` below remains the actual requested Codex model.
        requested_model = os.environ.get("AGENT_MONITOR_CODEX_MODEL", "")
        adapter_model = _codex_adapter_model(requested_model)
        os.environ["OPENAI_MODEL"] = "codexplan"
        os.environ["AGENT_MONITOR_REQUESTED_CODEX_MODEL"] = requested_model

        os.environ.setdefault(
            "OPENCLAUDE_CONFIG_DIR",
            os.path.join(os.environ["CODEX_HOME"], "openclaude"),
        )
        os.environ["CODEX_AUTH_JSON_PATH"] = os.path.join(
            os.environ["CODEX_HOME"], "auth.json"
        )
        argv = [*common, "--provider", "openai", "--model", adapter_model, prompt]
    else:
        selected_model = os.environ.get(
            "AGENT_MONITOR_OPENCLAUDE_MODEL", "kimi-k3"
        )
        os.environ["CLAUDE_CODE_USE_OPENAI"] = "1"
        os.environ["OPENAI_MODEL"] = selected_model
        argv = [
            *common,
            "--provider",
            "openai",
            "--model",
            selected_model,
            prompt,
        ]
    os.execv(binary, argv)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
