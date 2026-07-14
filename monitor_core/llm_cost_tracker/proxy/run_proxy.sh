#!/usr/bin/env bash
# Start the LiteLLM proxy for LLM cost monitoring.
#
# The proxy handles:
#   - Routing (Bedrock / SGLang / OpenAI) based on model name
#   - Auto-injecting prompt cache control points for Claude models
#   - Logging every call to monitor/calls.jsonl via trace_logger.py
#
# Any application with an OpenAI-compatible client can use this proxy
# by setting OPENAI_BASE_URL=http://localhost:4000/v1
#
# Usage:
#   ./run_proxy.sh                    # start proxy on port 4000
#   PROXY_PORT=8000 ./run_proxy.sh    # custom port
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TRACKER_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PROXY_PORT="${PROXY_PORT:-4000}"
CONFIG="$SCRIPT_DIR/config.yaml"

echo "==> Starting LiteLLM proxy on http://localhost:${PROXY_PORT}"
echo "    Config: $CONFIG"
echo "    Log:    ${LLM_MONITOR_LOG:-monitor/calls.jsonl}"
echo ""
echo "    Point your client at:"
echo "      export OPENAI_BASE_URL=http://localhost:${PROXY_PORT}/v1"
echo "      export OPENAI_API_KEY=sk-tau-local-1234"
echo ""

# PYTHONPATH must include the Token_Tracking_Monitor root so that
# llm_cost_tracker.proxy.trace_logger resolves correctly.
export PYTHONPATH="${TRACKER_ROOT}:${PYTHONPATH:-}"

exec litellm --config "$CONFIG" --port "$PROXY_PORT"
