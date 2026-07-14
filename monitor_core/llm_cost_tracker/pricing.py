"""LLM token pricing for cost tracking.

Token pricing is fetched dynamically from LiteLLM's pricing JSON at startup.
Lookup strategy: exact match → strip region prefix (us./eu./apac./au.) → strip
provider prefix entirely (just the model slug). This avoids hardcoding prices
that drift as providers update rates.
"""

import json
import re
import urllib.request
from typing import Optional, Tuple


# ---------------------------------------------------------------------------
# Dynamic LLM pricing from LiteLLM's maintained JSON
# ---------------------------------------------------------------------------

_LITELLM_PRICING_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)

_pricing_cache: Optional[dict] = None


def _fetch_pricing() -> dict:
    """Fetch and cache the LiteLLM pricing JSON (once per process)."""
    global _pricing_cache
    if _pricing_cache is not None:
        return _pricing_cache
    try:
        resp = urllib.request.urlopen(_LITELLM_PRICING_URL, timeout=15)
        _pricing_cache = json.loads(resp.read())
        print(f"[pricing] Loaded pricing for {len(_pricing_cache)} models from LiteLLM")
    except Exception as e:
        print(f"[pricing] WARNING: Failed to fetch LiteLLM pricing ({e}), using empty cache")
        _pricing_cache = {}
    return _pricing_cache


# Region prefixes used by Bedrock cross-region inference profiles
_REGION_PREFIX_RE = re.compile(r"^(us|eu|apac|au)\.")

# Alias mapping: custom/proxy model names → LiteLLM pricing keys.
# All pricing is fetched dynamically from:
#   https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json
# Provider pricing pages for verification:
#   - DeepInfra Qwen: https://deepinfra.com/pricing (Qwen section)
#   - Nebius Qwen3-4B: https://nebius.com/token-factory/prices
#   - Azure OpenAI GPT: https://azure.microsoft.com/en-us/pricing/details/cognitive-services/openai-service/
#   - AWS Bedrock Claude: https://aws.amazon.com/bedrock/pricing/ (Anthropic section)
#   - Qwen cache pricing: https://www.alibabacloud.com/help/en/model-studio/user-guide/context-cache
_MODEL_ALIASES = {
    "sentinel-qwen3-4b-thinking-rl": "nebius/Qwen/Qwen3-4B",
    "Qwen3-4B-zeroshot": "nebius/Qwen/Qwen3-4B",
    "Qwen3-30B-A3B-Thinking-2507-tau2-consecutive-tool-penalty": "deepinfra/Qwen/Qwen3-30B-A3B",
    "Qwen3-30B-A3B-Thinking-2507": "deepinfra/Qwen/Qwen3-30B-A3B",
    "Qwen3-30B-A3B-zeroshot": "deepinfra/Qwen/Qwen3-30B-A3B",
    # Azure OpenAI GPT models (proxy model_name → LiteLLM key)
    "gpt-5-chat": "azure/gpt-5-chat",
    "gpt-5.2": "azure/gpt-5.2",
    "gpt-5.2-chat": "azure/gpt-5.2-chat",
    "gpt-5.2-codex": "azure/gpt-5.2-codex",
    "gpt-5.4": "azure/gpt-5.4",
    "gpt-5.5": "openai/gpt-5.5",
    "gpt-5.5-pro": "openai/gpt-5.5",
    "gpt-5.2-pro": "openai/gpt-5.2-pro",
    # Bedrock Claude (filename-extracted names → Bedrock pricing keys)
    "claude-3-5-haiku-20241022-v1": "us.anthropic.claude-3-5-haiku-20241022-v1:0",
    "claude-haiku-4-5-20251001-v1": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "claude-sonnet-4-5-20250929-v1": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
}


def _lookup_model(model: str) -> Optional[dict]:
    """Look up a model in the pricing data with fallback key strategies.

    Strategy:
      1. Exact match (e.g. "us.anthropic.claude-3-5-haiku-20241022-v1:0")
      2. Strip region prefix (e.g. "anthropic.claude-3-5-haiku-20241022-v1:0")
      3. Strip region + provider prefix, bare slug (e.g. "claude-3-5-haiku-20241022")
      4. Family match: strip date/version, find any key sharing the same family
    """
    data = _fetch_pricing()
    if not data:
        return None

    # Strategy 0: alias mapping for custom finetuned models
    if model in _MODEL_ALIASES:
        alias_key = _MODEL_ALIASES[model]
        if alias_key in data:
            return data[alias_key]

    # Strategy 1: exact
    if model in data:
        return data[model]

    # Strategy 2: strip region prefix
    stripped = _REGION_PREFIX_RE.sub("", model)
    if stripped != model and stripped in data:
        return data[stripped]

    # Strategy 3: strip provider prefix entirely and version suffix
    parts = stripped.split(".", 1)
    if len(parts) == 2:
        slug = parts[1]
        if slug in data:
            return data[slug]
        bare = re.sub(r"-v\d+:\d+$", "", slug)
        if bare in data:
            return data[bare]
    else:
        bare = re.sub(r"-v\d+:\d+$", "", stripped)

    # Strategy 4: family match — strip date stamp, find a key with same family
    family = re.sub(r"-\d{8}.*$", "", bare if len(parts) == 2 else re.sub(r"-v\d+:\d+$", "", stripped))
    if family:
        family_re = re.compile(re.escape(family) + r"(-\d|$)")
        region_prefix = _REGION_PREFIX_RE.match(model)
        prefix = region_prefix.group(0) if region_prefix else ""
        for key in data:
            if family_re.search(key) and key.startswith(prefix) and key != model:
                return data[key]
        for key in data:
            if family_re.search(key) and "anthropic." in key and "/" not in key:
                return data[key]

    return None


def get_token_price(model: str) -> Tuple[float, float]:
    """Return (input_price_per_1M, output_price_per_1M) for a model.

    Prices are fetched from LiteLLM's pricing data. Returns (0, 0) only if
    the model is completely unknown.
    """
    info = _lookup_model(model)
    if info is None:
        print(f"[pricing] WARNING: No pricing found for '{model}', costs will be 0")
        return (0.0, 0.0)
    inp = (info.get("input_cost_per_token") or 0) * 1_000_000
    out = (info.get("output_cost_per_token") or 0) * 1_000_000
    return (round(inp, 4), round(out, 4))


def get_cache_pricing(model: str) -> Tuple[float, float]:
    """Return (cache_read_per_1M, cache_write_per_1M) for a model.

    Uses LiteLLM's per-model cache pricing fields directly. If a field is missing,
    falls back to the input price (flat rate — no cache discount for that field).
    Returns (0, 0) only if the model is completely unknown.
    """
    info = _lookup_model(model)
    if info is None:
        return (0.0, 0.0)
    inp = info.get("input_cost_per_token") or 0
    read = (info.get("cache_read_input_token_cost") or inp) * 1_000_000
    write = (info.get("cache_creation_input_token_cost") or inp) * 1_000_000
    return (round(read, 4), round(write, 4))


def has_real_pricing(model: str) -> bool:
    """Return True if the model has real per-token pricing from a cloud API.

    Returns False for self-hosted models where dollar costs would be fabricated.
    """
    info = _lookup_model(model)
    if info is None:
        return False
    return info.get("input_cost_per_token") is not None
