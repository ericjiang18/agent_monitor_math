#!/usr/bin/env bash
# One-time ChatGPT/OpenAI-account login for the Codex CLI engine.
#
# By default, `codex` runs bill against your OPENAI_API_KEY (metered API
# usage). This logs the Codex CLI in with your ChatGPT/OpenAI account instead
# (subscription usage, e.g. Plus/Pro/Team/Codex). Once this succeeds,
# agent_monitor/jobs.py's _ensure_codex_auth() automatically prefers this
# account login for every `codex` engine run — until you log out or set
# AGENT_MONITOR_CODEX_AUTH_MODE=apikey to force API-key billing again.
#
# Run from the repo root:  ./codex_account_login.sh
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v codex >/dev/null; then
  echo "codex CLI not found. Install it first, e.g.:" >&2
  echo "  npm install -g @openai/codex" >&2
  exit 1
fi

HOME_DIR="${AGENT_MONITOR_CODEX_ACCOUNT_HOME:-$(pwd)/data/codex_home/account}"
mkdir -p "$HOME_DIR"

echo "Logging Codex CLI in with your ChatGPT/OpenAI account…"
echo "(CODEX_HOME=$HOME_DIR — separate from any API-key logins)"
echo
echo "This is a headless server, so we use device-code auth: the CLI prints a"
echo "URL + short code below. Open that URL on ANY device with a browser"
echo "(phone, laptop — doesn't need to reach this machine), sign in, and enter"
echo "the code. No SSH tunnel / port forwarding needed."
echo

if CODEX_HOME="$HOME_DIR" codex login --device-auth; then
  :
else
  echo
  echo "device-auth failed or unsupported by this codex version (or your"
  echo "ChatGPT workspace has Device Code Authorization disabled)."
  echo "Falling back to normal browser login — this needs port forwarding:"
  echo "  ssh -L 1455:localhost:1455 <this-host>"
  echo "then re-run this script over that SSH session."
  echo
  CODEX_HOME="$HOME_DIR" codex login
fi

echo
echo "Done. Future 'codex' engine runs will use this account login automatically."
