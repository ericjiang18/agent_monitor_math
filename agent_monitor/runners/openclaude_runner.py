#!/usr/bin/env python3
"""Select OpenClaude's subscription or API-key provider at run time."""
from __future__ import annotations

import os
import shutil
import sys


def main() -> int:
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
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
    ]
    if os.environ.get("AGENT_MONITOR_CODEX_SUBSCRIPTION") == "1":
        # `codexplan` selects OpenClaude's Codex transport, which can re-read
        # the authenticated Codex CLI home instead of consuming an API key.
        os.environ["CLAUDE_CODE_USE_OPENAI"] = "1"
        os.environ.pop("OPENAI_API_KEY", None)
        os.environ.pop("OPENAI_API_KEYS", None)
        os.environ.pop("CODEX_API_KEY", None)
        os.environ["OPENAI_MODEL"] = os.environ.get("AGENT_MONITOR_CODEX_MODEL", "codexplan")
        os.environ.setdefault(
            "OPENCLAUDE_CONFIG_DIR",
            os.path.join(os.environ["CODEX_HOME"], "openclaude"),
        )
        os.environ["CODEX_AUTH_JSON_PATH"] = os.path.join(
            os.environ["CODEX_HOME"], "auth.json"
        )
        argv = [*common, "--model", os.environ.get("AGENT_MONITOR_CODEX_MODEL", "codexplan"), prompt]
    else:
        argv = [
            *common,
            "--provider",
            "openai",
            "--model",
            os.environ.get("AGENT_MONITOR_OPENCLAUDE_MODEL", "gpt-5.2"),
            prompt,
        ]
    os.execv(binary, argv)
    return 127


if __name__ == "__main__":
    raise SystemExit(main())
