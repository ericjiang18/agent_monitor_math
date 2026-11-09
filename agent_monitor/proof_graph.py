"""Proof-graph generation — distill a run's proof into a logical DAG.

A small/cheap LLM reads the problem plus the proof write-up and emits a
strict-JSON graph: nodes are logical units (assumptions, lemmas, claims,
steps, contradiction, conclusion) and edges are "supports" arrows from
premise to consequence. The result is cached in the run workspace as
``proof_graph.json`` so it only costs one model call per (re)generation.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import logging
import math
import os
import re
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote

from agent_monitor import usage_ledger

logger = logging.getLogger(__name__)

GRAPH_FILENAME = "proof_graph.json"
LEAN_GRAPH_FILENAME = "lean_proof_graph.json"
MAX_GRAPH_CACHE_BYTES = 4_000_000
MAX_SOURCE_BYTES = 2_000_000
MAX_PROBLEM_BYTES = 1_000_000
MAX_GRAPH_NODES = 256
MAX_GRAPH_EDGES = 4096
MAX_GRAPH_ID_LENGTH = 160
MAX_LEAN_DECLARATIONS = 256

CLAUDE_DERIVED_TIMEOUT_ENV = "AGENT_MONITOR_CLAUDE_DERIVED_TIMEOUT"
DEFAULT_CLAUDE_DERIVED_TIMEOUT = 600.0
MIN_CLAUDE_DERIVED_TIMEOUT = 60.0
MAX_CLAUDE_DERIVED_TIMEOUT = 3_600.0


def _claude_derived_timeout(env: Mapping[str, str]) -> float:
    """Resolve one bounded response-only timeout without exposing its input."""
    raw = str(env.get(CLAUDE_DERIVED_TIMEOUT_ENV) or "").strip()
    if not raw:
        return DEFAULT_CLAUDE_DERIVED_TIMEOUT
    try:
        requested = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{CLAUDE_DERIVED_TIMEOUT_ENV} must be a finite number of seconds "
            "greater than zero"
        ) from exc
    if not math.isfinite(requested) or requested <= 0:
        raise ValueError(
            f"{CLAUDE_DERIVED_TIMEOUT_ENV} must be a finite number of seconds "
            "greater than zero"
        )
    return max(
        MIN_CLAUDE_DERIVED_TIMEOUT,
        min(requested, MAX_CLAUDE_DERIVED_TIMEOUT),
    )

_KINDS = {
    "assumption", "definition", "lemma", "claim", "case",
    "step", "contradiction", "conclusion",
}

# Kept as a module constant so it can be tuned/replaced easily (the user may
# supply their own prompt template later).  It follows the CGM fact
# discipline: every node is a self-contained mathematical statement, exactly
# one conclusion, named results cited, dependencies acyclic.  A run with a
# verified CGM fact graph never uses it (see ``graph_from_facts``).
GRAPH_PROMPT = """You are a mathematical proof analyst. Read the PROBLEM and the PROOF below and record the proof as a fact graph in the CGM style: a directed acyclic graph (DAG) whose nodes are self-contained mathematical statements and whose edges say which earlier statements each one depends on.

Respond with STRICT JSON only — no markdown fences, no commentary. Schema:
{
  "title": "short name of the theorem being proved",
  "nodes": [
    {
      "id": "n1",
      "kind": "assumption | definition | lemma | claim | case | step | contradiction | conclusion",
      "label": "very short title, at most 8 words",
      "statement": "precise 1-3 sentence statement of this logical unit; LaTeX with $...$ is allowed",
      "citations": ["what this step relies on: a citation marker like [2] used in the proof, a named theorem such as Euclid's lemma, or \"self-derived\""]
    }
  ],
  "edges": [
    {"from": "n1", "to": "n2", "label": "implies | uses | case of | contradicts | derives"}
  ]
}

Rules:
- 6 to 20 nodes. Each node is ONE fact: a statement a reader could verify on its own from the statements of its incoming edges, not a paragraph dump or a narrative step such as "now we compute".
- Statements are self-contained: restate every variable, hypothesis and quantifier they need (no "this", "the above", or "by the previous step"). Keep the proof's own notation.
- Exactly one node with kind "conclusion"; its statement is the claim the proof establishes, stated exactly as proved (never stronger than the proof supports). If the proof leaves a gap, keep the gap visible as a "claim" node rather than silently closing it.
- Edges point from a premise to the statement that uses it (assumptions at the top, the conclusion at the bottom). Every node must reach the conclusion. The graph must be acyclic.
- Include every hypothesis, definition and known fact the proof actually uses. For proofs by contradiction, include the negated-assumption node (kind "assumption") and the contradiction node (kind "contradiction"); for case splits, one "case" node per case.
- "citations" is required on every node: copy the citation markers the proof uses where present, name the classical result when the proof invokes one (e.g. "Cauchy–Schwarz inequality", "Euclid's lemma"), and use "self-derived" for statements the proof establishes itself. Never invent a source.

PROBLEM:
{problem}

PROOF:
{proof}
"""

LEAN_GRAPH_PROMPT = """You are a Lean 4 proof analyst. Read the PROBLEM and the Lean 4 SOURCE below (Lean 4.14 + Mathlib), then extract the logical structure of the *formal* proof as a directed acyclic graph (DAG).

Respond with STRICT JSON only — no markdown fences, no commentary. Schema:
{
  "title": "Lean identifier of the main theorem, e.g. parabolic_upper_bound",
  "nodes": [
    {
      "id": "n1",
      "kind": "assumption | definition | lemma | claim | case | step | contradiction | conclusion",
      "lean_kind": "variable | axiom | def | abbrev | lemma | theorem | have | obtain | refine | sorry",
      "label": "exact Lean name / binder, e.g. hneumann or hopf_strict_comparison",
      "statement": "Lean code snippet only — the type signature or `have` line, NOT English prose",
      "citations": ["Lean name used, Mathlib lemma, or self-derived"]
    }
  ],
  "edges": [
    {"from": "n1", "to": "n2", "label": "uses | applies | cases | rw | exact | intro"}
  ]
}

Rules — this graph is for *Lean code*, not natural language:
- Only describe what is in the SOURCE. Never invent a declaration, hypothesis or
  lemma that does not occur there, and never repair or complete the proof.
- `label` MUST be a Lean identifier (declaration name, hypothesis name, or `have` binder). Never an English title.
- `statement` MUST be Lean syntax copied/abbreviated from the source, e.g.
  `theorem main : ∀ x ∈ closure Ω, ∀ t > 0, u x t ≤ M` or `have h : u x t ≤ M + ε * t := by …`
  Do NOT rewrite into English sentences. Do NOT use LaTeX prose.
- Prefer the type after `:` (proposition/type) for theorems/lemmas; for `have`/`obtain` include the binder and type.
- Nodes: every top-level declaration, plus the `have`/`obtain` steps that carry the argument
  inside long proofs. Skip imports and trivial `let`.
- Mark each remaining `sorry` as its own node with lean_kind "sorry" and an edge to the step it leaves open.
- Map kinds: axiom/opaque/variable → assumption; def/abbrev → definition; lemma → lemma; supporting theorem → claim; main theorem → conclusion; local have → step.
- Edges go premise → dependent and must reflect actual use in the source (a name appearing in a proof or statement).
- Exactly one `conclusion`. 3–24 nodes. Acyclic.

PROBLEM:
{problem}

LEAN SOURCE:
{lean}
"""


def _read_workspace_text(
    workspace: Path, name: str, *, limit: int
) -> str:
    parts = Path(name).parts
    if (
        not parts or Path(name).is_absolute()
        or any(part in {"", ".", ".."} for part in parts)
    ):
        return ""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = -1
    fd = -1
    try:
        directory = os.open(
            Path(workspace).resolve(),
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | nofollow,
        )
        for component in parts[:-1]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | nofollow,
                dir_fd=directory,
            )
            os.close(directory)
            directory = child
        fd = os.open(
            parts[-1], os.O_RDONLY | os.O_CLOEXEC | nofollow,
            dir_fd=directory,
        )
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            state = os.fstat(handle.fileno())
            if not stat.S_ISREG(state.st_mode) or state.st_size > limit:
                return ""
            raw = handle.read(limit + 1)
    except OSError:
        return ""
    finally:
        if fd >= 0:
            os.close(fd)
        if directory >= 0:
            os.close(directory)
    if len(raw) > limit:
        return ""
    return raw.decode(errors="replace")


def _proof_source(workspace: Path, run_record: dict[str, Any] | None) -> tuple[str, str]:
    """Return (source_label, proof_text). Prefers workspace write-ups."""
    for name in ("proof.md", "proof.tex"):
        text = _read_workspace_text(
            workspace, name, limit=MAX_SOURCE_BYTES
        ).strip()
        if text:
            return name, text
    # Fallback: the final agent output recorded on the run
    agents = (run_record or {}).get("agents") or []
    for a in reversed(agents):
        out = str(a.get("output") or "").strip()
        if len(out) > 200:
            return "final agent output", out[:MAX_SOURCE_BYTES]
    return "", ""


# How a provider is named in an error message.
_PROVIDER_LABELS = {
    "anthropic": "Anthropic",
    "deepseek": "DeepSeek",
    "google": "Google",
    "groq": "Groq",
    "kimi": "Kimi",
    "openai": "OpenAI",
    "openrouter": "OpenRouter",
    "together": "Together",
    "xai": "xAI",
}


def provider_label(provider: str) -> str:
    """A provider's name as people write it ("OpenAI", not "Openai")."""
    return _PROVIDER_LABELS.get(provider, provider.title())


def key_defect(key: str) -> bool:
    """Whether an API key holds a character no API key has: a space, a line
    break or another control character, or one outside ASCII (a paste can
    carry a non-breaking or zero-width space).  An HTTP library refuses such
    a key and quotes it in its error."""
    return any(not 33 <= ord(char) <= 126 for char in key)


def key_defect_message(provider: str) -> str:
    return (
        f"The {provider_label(provider)} API key contains a space, a line break or "
        "another character API keys never have; paste it again in Settings"
    )


def _resolve_llm_provider(
    env: dict[str, str], model: str | None
) -> tuple[str, str, str, str]:
    """Resolve a selected model to its native provider, endpoint base and key."""
    requested = (
        model
        or env.get("AGENT_MONITOR_GRAPH_MODEL")
        or env.get("AGENT_MONITOR_MODEL")
        or ""
    ).strip()
    if not requested:
        if env.get("OPENAI_API_KEY"):
            requested = "gpt-5-mini-2025-08-07"
        elif env.get("ANTHROPIC_API_KEY"):
            requested = "claude-haiku-4-5"
        elif env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY"):
            requested = "gemini-3.6-flash"
        elif env.get("DEEPSEEK_API_KEY"):
            requested = "deepseek-v4-flash"
        elif env.get("KIMI_API_KEY"):
            requested = "kimi-k3"
        elif env.get("XAI_API_KEY"):
            requested = "grok-4.5"
        elif any(
            env.get(name)
            for name in (
                "OPENROUTER_API_KEY",
                "GROQ_API_KEY",
                "TOGETHERAI_API_KEY",
            )
        ):
            raise ValueError(
                "No model selected; use an explicit provider:model id in Settings"
            )
        else:
            requested = "gpt-5-mini-2025-08-07"

    specs = {
        "openai": (
            env.get("OPENAI_API_KEY"),
            env.get("OPENAI_BASE_URL") or "https://api.openai.com/v1",
        ),
        "kimi": (
            env.get("KIMI_API_KEY"),
            env.get("KIMI_API_BASE")
            or "https://2qg3r7w8aefiukbds.flashflame.ai/v1",
        ),
        "anthropic": (
            env.get("ANTHROPIC_API_KEY"),
            env.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com",
        ),
        "google": (
            env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY"),
            env.get("GEMINI_API_BASE") or "https://generativelanguage.googleapis.com",
        ),
        "openrouter": (
            env.get("OPENROUTER_API_KEY"),
            env.get("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1",
        ),
        "deepseek": (
            env.get("DEEPSEEK_API_KEY"),
            env.get("DEEPSEEK_API_BASE") or "https://api.deepseek.com",
        ),
        "groq": (
            env.get("GROQ_API_KEY"),
            env.get("GROQ_API_BASE") or "https://api.groq.com/openai/v1",
        ),
        "together": (
            env.get("TOGETHERAI_API_KEY"),
            env.get("TOGETHERAI_API_BASE") or "https://api.together.xyz/v1",
        ),
        "xai": (
            env.get("XAI_API_KEY"),
            env.get("XAI_API_BASE") or "https://api.x.ai/v1",
        ),
    }

    # A colon-qualified model is an explicit billing/provider route.  Do not
    # strip it before resolving: doing so lets another configured key win the
    # name-based fallback (for example ``deepseek:custom`` used to select
    # OpenRouter when both keys were present).  OpenRouter's native model ids
    # use ``vendor/model`` and remain intact; direct custom providers use an
    # unambiguous colon prefix instead.
    provider_aliases = {
        "openai": "openai",
        "kimi": "kimi",
        "anthropic": "anthropic",
        "claude": "anthropic",
        "google": "google",
        "gemini": "google",
        "openrouter": "openrouter",
        "deepseek": "deepseek",
        "groq": "groq",
        "together": "together",
        "togetherai": "together",
        "together_ai": "together",
        "xai": "xai",
    }
    explicit_provider: str | None = None
    chosen = requested
    prefix, separator, remainder = requested.partition(":")
    colon_provider = (
        provider_aliases.get(prefix.strip().lower()) if separator else None
    )
    if colon_provider:
        chosen = remainder.strip()
        if not chosen:
            raise ValueError(f"Model id is empty after provider prefix {prefix!r}")
        explicit_provider = colon_provider

    lowered = chosen.lower()
    if explicit_provider:
        order = [explicit_provider]
    elif lowered.startswith("kimi-"):
        order = ["kimi"]
    elif lowered.startswith("claude-"):
        order = ["anthropic"]
    elif lowered.startswith("gemini-"):
        order = ["google"]
    elif lowered.startswith("deepseek-"):
        order = ["deepseek"]
    elif lowered.startswith("grok-"):
        order = ["xai"]
    elif "/" in chosen:
        order = ["openrouter"]
    elif lowered == "chat-latest" or lowered.startswith(("gpt-", "o1", "o3", "o4")):
        order = ["openai"]
    else:
        raise ValueError(
            f"Model {chosen} has no unambiguous API provider; "
            "use an explicit provider:model id in Settings"
        )

    for provider in order:
        key, base = specs[provider]
        clean_key = str(key or "").strip()
        if clean_key:
            return provider, str(base).strip().rstrip("/"), clean_key, chosen
    expected = {
        "anthropic": "ANTHROPIC_API_KEY",
        "google": "GEMINI_API_KEY or GOOGLE_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "groq": "GROQ_API_KEY",
        "together": "TOGETHERAI_API_KEY",
        "xai": "XAI_API_KEY",
        "openai": "OPENAI_API_KEY",
        "kimi": "KIMI_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
    }.get(order[0], "a matching provider API key")
    raise ValueError(
        f"Model {chosen} requires {expected}; add and verify it in Settings"
    )


def _resolve_llm(env: dict[str, str], model: str | None) -> tuple[str, str, str]:
    """Backward-compatible resolver returning (base_url, api_key, model)."""
    _provider, base, key, chosen = _resolve_llm_provider(env, model)
    return base, key, chosen


def _api_error(body: Any) -> str:
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("type") or error)
        if error:
            return str(error)
        return str(body.get("message") or body)
    return str(body)


def _echoes_provider_errors(base: str) -> bool:
    """Whether a provider error body from ``base`` may be shown to the caller.

    Public HTTPS endpoints and the attested sponsored loopback broker
    (``http://<loopback>:<port>/internal/v1``) may be quoted; any other
    plain-HTTP, loopback, private, link-local or single-label host could be an
    internal service reached through a stored custom base URL.
    """
    import ipaddress
    from urllib.parse import urlsplit

    parsed = urlsplit(str(base or "").strip())
    host = (parsed.hostname or "").strip().lower()
    try:
        port = parsed.port
    except ValueError:
        return False
    if not host or parsed.username or parsed.password:
        return False
    loopback = host in {"localhost", "127.0.0.1", "::1"}
    if loopback and parsed.scheme == "http" and port:
        return parsed.path.rstrip("/") == "/internal/v1" and not parsed.query
    if parsed.scheme != "https" or loopback or "." not in host.strip("."):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return not host.endswith((".local", ".internal", ".localhost"))
    return address.is_global


def _openai_response_text(body: dict[str, Any]) -> str:
    direct = body.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    parts: list[str] = []
    for item in body.get("output") or []:
        if not isinstance(item, dict):
            continue
        for block in item.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") in {"output_text", "text"} and block.get("text"):
                parts.append(str(block["text"]))
    return "\n".join(parts)


class ObservedModel(str):
    """The requested routing ID, carrying the provider-reported identity.

    Repairs must keep routing to the model the caller selected, while
    authorship evidence may only cite what the provider said it actually ran.
    Subclassing ``str`` keeps every existing caller unaffected.
    """

    observed_model: str

    def __new__(cls, requested: str, observed: object = None) -> "ObservedModel":
        value = super().__new__(cls, requested)
        value.observed_model = observed.strip() if isinstance(observed, str) else ""
        return value


def _stopped_at_output_limit(body: object) -> bool:
    """Whether a Chat Completions reply says it stopped at its output limit."""
    try:
        return str(body["choices"][0].get("finish_reason") or "") == "length"  # type: ignore[index]
    except (KeyError, IndexError, TypeError, AttributeError):
        return False


def _call_llm(
    env: dict[str, str],
    model: str | None,
    prompt: str,
    *,
    timeout: int = 240,
    before_resend: Callable[[], None] | None = None,
) -> tuple[str, str, str]:
    """Call OpenAI, Anthropic, Gemini, or an OpenAI-compatible provider natively.

    A server-sponsored Kimi call (guest or member) waits
    ``kimi_k3.SPONSORED_TIMEOUT_SECONDS`` whatever ``timeout`` says, since the
    broker answers within it; it sends that budget as ``X-Ansatze-Timeout``
    and retries only a failure that certainly never reached processing
    (``kimi_k3.sponsored_request``), never an ambiguous timeout, 5xx or empty
    reply.  The one exception is a reply that stopped at its output limit
    with no text: it is asked once more at the next lower reasoning effort,
    after ``before_resend`` (a Lean session's stop check) lets it.
    Every 2xx reply's usage is booked, including an empty completion a retry
    replaced.
    """
    from agent_monitor.settings import _http_json

    if env.get("AGENT_MONITOR_GUEST_DERIVED") == "1":
        model = "kimi-k3"
    provider, base, key, chosen = _resolve_llm_provider(env, model)
    if key and key_defect(key):
        # Never sent: the request would fail with the key quoted.
        raise ValueError(key_defect_message(provider))
    max_output = max(1024, min(int(env.get("AGENT_MONITOR_LLM_MAX_OUTPUT") or 32768), 128000))
    headers: dict[str, str]
    payload: dict[str, Any]
    if provider == "openai":
        url = f"{base}/responses"
        headers = {"Authorization": f"Bearer {key}"}
        payload = {
            "model": chosen,
            "input": prompt,
            "max_output_tokens": max_output,
            "store": False,
        }
        from agent_monitor.run_tuning import openai_payload_options

        payload.update(openai_payload_options(env, chosen))
    elif provider == "kimi":
        from agent_monitor import kimi_k3

        url = f"{base}/chat/completions"
        headers = {"Authorization": f"Bearer {key}"}
        payload = kimi_k3.chat_payload(
            user=prompt,
            env=env,
            max_tokens=max_output,
        )
    elif provider == "anthropic":
        api_base = base if base.endswith("/v1") else f"{base}/v1"
        url = f"{api_base}/messages"
        headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        }
        payload = {
            "model": chosen,
            "max_tokens": max_output,
            "messages": [{"role": "user", "content": prompt}],
        }
    elif provider == "google":
        api_base = base if base.endswith("/v1beta") else f"{base}/v1beta"
        # The id is quoted: a stray space would make an invalid URL, whose
        # error quotes it with the key.
        url = f"{api_base}/models/{quote(chosen)}:generateContent?key={key}"
        headers = {}
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_output},
        }
    else:
        url = f"{base}/chat/completions"
        headers = {"Authorization": f"Bearer {key}"}
        payload = {
            "model": chosen,
            "messages": [{"role": "user", "content": prompt}],
        }

    from agent_monitor import kimi_k3

    # (getattr: a console that loaded an older kimi_k3 before this module was
    # updated keeps the previous handling until it restarts.)
    is_sponsored = getattr(kimi_k3, "is_sponsored", None)
    sponsored = provider == "kimi" and is_sponsored is not None and is_sponsored(env)
    if sponsored:
        # The broker answers within this budget (its gateway deadline plus
        # 15 s); a shorter client wait would abandon healthy slow replies.
        timeout = kimi_k3.SPONSORED_TIMEOUT_SECONDS
    billing = (
        "sponsored"
        if provider == "kimi" and env.get("AGENT_MONITOR_GUEST_DERIVED") == "1"
        else None
    )

    def request(seconds: float | None = None) -> tuple[int, object]:
        limit = timeout if seconds is None else seconds
        call_headers = headers
        if sponsored:
            call_headers = {
                **headers,
                kimi_k3.TIMEOUT_HEADER: kimi_k3.timeout_header_value(limit),
            }
        try:
            code, body = _http_json(
                url,
                method="POST",
                headers=call_headers,
                payload=payload,
                timeout=limit,
            )
        except (ValueError, http.client.InvalidURL) as exc:
            # A request the HTTP library refused (an invalid URL or header
            # value) quotes it, key included; nothing was sent.
            from agent_monitor.runners.api_backend import _safe_error

            raise ValueError(
                f"{provider_label(provider)} model request was refused for {chosen}: "
                f"{_safe_error(exc, key)}"
            ) from None
        if 200 <= code < 300 and isinstance(body, dict):
            # Every 2xx reply was billed, including an empty completion that
            # a retry below replaces: book each one, not only the last.
            usage_ledger.record_usage(
                model=str(body.get("model") or chosen),
                usage=usage_ledger.http_usage(provider, body),
                billing=billing,
            )
        return code, body

    if sponsored:
        # Sponsored calls are admitted and billed per logical call: only a
        # failure that never reached processing is retried; an ambiguous
        # timeout, 5xx or empty reply is never replayed.
        code, body, failure = kimi_k3.sponsored_request(request, timeout=timeout)
        # One exception: a reply that stopped at the output limit with no text
        # (K3 spent the whole 8,192-token budget on reasoning) is not
        # ambiguous: it was processed and booked, and the same request would
        # end the same way. It is asked once more, as a new call at the next
        # lower effort (high -> low), and that reply is booked too.
        lower_effort = getattr(kimi_k3, "lower_effort_after_empty", None)
        spent_effort = payload.get("reasoning_effort")
        resent = False
        if (
            failure is None
            and lower_effort is not None
            and _stopped_at_output_limit(body)
            and lower_effort(payload, code, body)
        ):
            if before_resend is not None:
                # A Lean session stopped meanwhile (its run was deleted)
                # makes no further model call.
                before_resend()
            summarize = getattr(kimi_k3, "empty_completion_summary", None)
            logger.warning(
                "Kimi spent the whole output budget on reasoning for %s (%s; "
                "reasoning_effort=%s); asking once more at reasoning_effort=%s",
                chosen,
                summarize(body) if summarize is not None else "",
                spent_effort,
                payload.get("reasoning_effort"),
            )
            resent = True
            code, body, failure = kimi_k3.sponsored_request(request, timeout=timeout)
        # The first reply of a resent call was billed: a refused resend must
        # not say that no tokens were used.
        message = (
            kimi_k3.failure_message(failure, billed=resent) if failure is not None else ""
        )
        if message:
            # Built from typed status fields only, never the reply body; an
            # untyped reply (older broker) is reported as before.
            raise ValueError(message)
    elif provider == "kimi":
        # An empty completion usually means reasoning used up the output
        # budget; retry it with less reasoning rather than identically.
        # (getattr: a long-running process may still hold an older kimi_k3.)
        lower_effort = getattr(kimi_k3, "lower_effort_after_empty", None)
        if lower_effort is None:
            code, body = kimi_k3.request_with_retry(request)
        else:
            code, body = kimi_k3.request_with_retry(
                request,
                before_retry=lambda failed_code, failed_body: lower_effort(
                    payload, failed_code, failed_body
                ),
            )
    else:
        code, body = request()
    if code != 200 or not isinstance(body, dict):
        if not _echoes_provider_errors(base):
            # SSRF guard: a private, loopback or plain-HTTP endpoint could be
            # an internal service; report only the status, never its body.
            raise ValueError(
                f"{provider_label(provider)} model call failed for {chosen} (HTTP {code}); "
                "the response of this non-public endpoint is not shown"
            )
        detail = str(_api_error(body) or "unknown error")
        if key:
            detail = detail.replace(key, "[redacted]")
        raise ValueError(
            f"{provider_label(provider)} model call failed for {chosen} (HTTP {code}): "
            f"{detail[-1600:]}"
        )
    try:
        if provider == "openai":
            content = _openai_response_text(body)
        elif provider == "kimi":
            from agent_monitor import kimi_k3

            content = kimi_k3.response_text(body)
        elif provider == "anthropic":
            content = "\n".join(
                str(block.get("text") or "")
                for block in body.get("content") or []
                if isinstance(block, dict) and block.get("type") == "text"
            )
        elif provider == "google":
            content = "\n".join(
                str(part.get("text") or "")
                for candidate in body.get("candidates") or []
                if isinstance(candidate, dict)
                for part in ((candidate.get("content") or {}).get("parts") or [])
                if isinstance(part, dict) and part.get("text")
            )
        else:
            content = str(body["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError) as exc:
        raise ValueError(
            f"{provider_label(provider)} returned an unexpected response shape for {chosen}"
        ) from exc
    if not content.strip():
        detail = ""
        if provider == "kimi":
            from agent_monitor import kimi_k3

            # finish_reason and token counts only: never prompt or key text.
            summarize = getattr(kimi_k3, "empty_completion_summary", None)
            detail = summarize(body) if summarize is not None else ""
            logger.warning(
                "Kimi returned an empty completion for %s (%s; last reasoning_effort=%s)",
                chosen,
                detail,
                payload.get("reasoning_effort"),
            )
        raise ValueError(
            f"{provider_label(provider)} returned an empty response for {chosen}"
            + (f" ({detail})" if detail else "")
        )
    return ObservedModel(chosen, body.get("model")), content, provider

_AMBIGUOUS_LATEX_ESCAPE = re.compile(
    r'\\u(?![0-9A-Fa-f]{4})'
    r'|\\(?:bar|begin|beta|big(?:l|r)?|Big(?:l|r)?|binom|bmod|boldsymbol|boxed|bra|brack'
    r'|forall|frac'
    r'|nabla|natural|ne|neg|neq|nexists|nleq|nmid|not|notin|nu'
    r'|rangle|rceil|ref|rfloor|rho|right|rightarrow|rm|root'
    r'|tan|tau|text|tfrac|therefore|theta|times|to|top|triangle)\b'
    r'|\\(?!["\\/bfnrtu])'
)


def _repair_latex_json_escapes(blob: str) -> str:
    """Escape unpaired TeX slashes inside JSON strings.

    Already valid ``\\alpha`` JSON must stay untouched. A global regex can
    accidentally match the second slash in that pair and turn it into three
    slashes, producing ``Invalid \\escape``. Count each slash run instead:
    even runs are already paired; only an odd final slash is considered for
    TeX repair. Structural JSON defects remain errors.
    """
    out: list[str] = []
    index = 0
    in_string = False
    while index < len(blob):
        char = blob[index]
        if not in_string:
            out.append(char)
            if char == '"':
                in_string = True
            index += 1
            continue
        if char == '"':
            out.append(char)
            in_string = False
            index += 1
            continue
        if char != "\\":
            out.append(char)
            index += 1
            continue

        end = index
        while end < len(blob) and blob[end] == "\\":
            end += 1
        slash_count = end - index
        following = blob[end] if end < len(blob) else ""
        if slash_count % 2 == 0:
            out.append("\\" * slash_count)
            index = end
            continue

        candidate = "\\" + blob[end:]
        needs_tex_escape = bool(_AMBIGUOUS_LATEX_ESCAPE.match(candidate))
        if needs_tex_escape:
            out.append("\\" * (slash_count + 1))
            index = end
        elif following in {'"', '/'} or following in "bfnrt":
            out.append("\\" * slash_count + following)
            index = end + 1
        elif following == "u" and re.match(r"u[0-9A-Fa-f]{4}", blob[end : end + 5]):
            out.append("\\" * slash_count + blob[end : end + 5])
            index = end + 5
        else:
            out.append("\\" * (slash_count + 1))
            index = end
    return "".join(out)


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    # Strip a ```json (or bare) fence if the model added one anyway. A fence
    # tagged with another language (```lean inside a Formal DAG statement,
    # ```latex in an informal one) is part of a JSON string, not the payload.
    for m in re.finditer(r"```([A-Za-z0-9_+-]*)(.*?)```", text, re.S):
        if m.group(1).lower() in {"", "json"} and "{" in m.group(2):
            text = m.group(2).strip()
            break
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("model response contained no JSON object")
    blob = text[start : end + 1]
    # Models routinely emit LaTeX without JSON-escaping backslashes. Commands
    # such as `\forall` and `\neq` are especially dangerous: JSON accepts
    # their leading `\f` / `\n` and silently produces control characters,
    # so an exception-only retry is too late. The repair regex is intentionally
    # limited to known TeX commands and preserves legitimate escapes such as a
    # newline followed by `next`. Malformed commas/braces still fail normally.
    return json.loads(_repair_latex_json_escapes(blob))


def _sanitize(graph: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(graph, dict):
        raise ValueError("model returned a non-object graph")
    raw_nodes = graph.get("nodes") or []
    raw_edges = graph.get("edges") or []
    if not isinstance(raw_nodes, list) or len(raw_nodes) > MAX_GRAPH_NODES:
        raise ValueError("proof graph has too many or invalid nodes")
    if not isinstance(raw_edges, list) or len(raw_edges) > MAX_GRAPH_EDGES:
        raise ValueError("proof graph has too many or invalid edges")
    nodes = []
    seen: set[str] = set()
    for n in raw_nodes:
        if not isinstance(n, dict):
            continue
        nid = str(n.get("id") or "").strip()
        if not nid or len(nid) > MAX_GRAPH_ID_LENGTH or nid in seen:
            continue
        seen.add(nid)
        kind = str(n.get("kind") or "step").strip().lower()
        raw_cites = n.get("citations")
        if isinstance(raw_cites, str):
            raw_cites = [raw_cites]
        cites = [
            str(c)[:120]
            for c in (raw_cites or [])
            if isinstance(c, (str, int, float)) and str(c).strip()
        ][:6]
        entry = {
            "id": nid,
            "kind": kind if kind in _KINDS else "step",
            "label": str(n.get("label") or nid)[:120],
            "statement": str(n.get("statement") or "")[:1200],
            "citations": cites,
        }
        lk = str(n.get("lean_kind") or "").strip().lower()
        if lk:
            entry["lean_kind"] = lk[:40]
        nodes.append(entry)
    ids = {n["id"] for n in nodes}
    edges = []
    edge_seen: set[tuple[str, str, str]] = set()
    adjacency: dict[str, set[str]] = {nid: set() for nid in ids}

    def reaches(start: str, target: str) -> bool:
        pending = [start]
        visited: set[str] = set()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current in visited:
                continue
            visited.add(current)
            pending.extend(adjacency.get(current, ()))
        return False

    for e in raw_edges:
        if not isinstance(e, dict):
            continue
        src, dst = str(e.get("from") or ""), str(e.get("to") or "")
        label = str(e.get("label") or "")[:40]
        if (
            src in ids
            and dst in ids
            and src != dst
            and (src, dst, label) not in edge_seen
            and not reaches(dst, src)
        ):
            edge_seen.add((src, dst, label))
            adjacency[src].add(dst)
            edges.append({"from": src, "to": dst, "label": label})
    if not nodes:
        raise ValueError("model returned an empty graph")
    return {"title": str(graph.get("title") or "")[:200], "nodes": nodes, "edges": edges}


_CLAUDE_ACCOUNT_MODELS = {"sonnet", "opus", "fable", "claude-haiku-4-5"}
_CLAUDE_DAG_SYSTEM = (
    "You are a mathematical proof analyst. Follow the requested strict JSON "
    "schema exactly and return JSON only, without markdown fences or commentary."
)
_CODEX_DAG_SYSTEM = (
    "You are a mathematical proof analyst. Follow the requested strict JSON "
    "schema exactly and return JSON only, without markdown fences or commentary."
)


def _claude_account_ready(user: dict[str, Any] | None) -> bool:
    if not user or user.get("id") is None:
        return False
    from agent_monitor import claude_login
    from agent_monitor.auth import may_use_own_subscriptions

    # Only the administrator (or an account given full access) may use a
    # subscription, so another member's sign-in from before that policy never
    # pulls its DAG work off the API route.
    return may_use_own_subscriptions(user["id"]) and claude_login.account_login_ready(
        claude_login.account_home(user["id"])
    )


def _run_claude_prompt(
    user: dict[str, Any] | None,
    prompt: str,
    *,
    model: str | None = None,
    timeout: float | None = None,
    source_env: dict[str, str] | None = None,
) -> tuple[str, str]:
    from agent_monitor.jobs import _configure_auth_route
    from agent_monitor.runners.claude_backend import ClaudeBackendError, claude_exec
    from agent_monitor.settings import restrict_account_base_urls, resolved_user_env

    from agent_monitor.subprocess_env import child_process_env

    env = child_process_env(
        extra=restrict_account_base_urls(
            {
                **{key: value for key, value in (source_env or {}).items() if value},
                **{k: v for k, v in (resolved_user_env(user) or {}).items() if v},
            },
            user,
        )
    )
    env = _configure_auth_route(
        env,
        route="claude_subscription",
        user_id=(user or {}).get("id"),
    )
    from agent_monitor.jobs import _canonical_claude_continuation_model

    # Records carry resolved names (claude-opus-5-5): keep the run's model.
    selected = _canonical_claude_continuation_model(model) or "sonnet"
    effective_timeout = (
        _claude_derived_timeout(env) if timeout is None else timeout
    )
    try:
        result = claude_exec(
            _CLAUDE_DAG_SYSTEM,
            prompt,
            model=selected,
            timeout=effective_timeout,
            source_env=env,
        )
    except ClaudeBackendError as exc:
        raise ValueError(str(exc)) from exc
    from agent_monitor import usage_ledger

    usage_ledger.record_usage(
        model=str(result.model or selected),
        usage=usage_ledger.cli_usage(getattr(result, "usage", None), input_includes_cache=False),
        billing="subscription",
        cost_usd=getattr(result, "cost_usd", None),
    )
    content = str(result.text or "").strip()
    if not content:
        raise ValueError("Claude Code returned an empty proof graph response")
    return str(result.model or selected), content


def _codex_account_ready(user: dict[str, Any] | None) -> bool:
    if not user or user.get("id") is None:
        return False
    from agent_monitor import codex_login
    from agent_monitor.auth import may_use_own_subscriptions

    # As for Claude: an API failure never falls back to a restricted member's sign-in.
    return may_use_own_subscriptions(user["id"]) and codex_login.account_login_ready(
        codex_login.account_home(user["id"])
    )


def _run_codex_prompt_env(
    env: dict[str, str],
    workspace: Path,
    prompt: str,
    *,
    model: str | None = None,
    timeout: int = 240,
) -> str:
    """One-shot response pinned to the configured Codex subscription env."""
    from agent_monitor.runners.codex_backend import (
        CodexBackendError,
        codex_subscription_exec,
    )

    scratch = workspace / ".graph_scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    requested = str(model or "").strip()
    if requested.lower() in {"codex", "codex-cli", "codex cli", "codex subscription"}:
        requested = ""
    try:
        result = codex_subscription_exec(
            _CODEX_DAG_SYSTEM,
            prompt,
            source_env=env,
            model=requested or None,
            timeout=timeout,
            workspace=scratch,
        )
    except CodexBackendError as exc:
        raise ValueError(str(exc)) from exc
    from agent_monitor import usage_ledger

    usage_ledger.record_usage(
        model=requested or "codex",
        usage=usage_ledger.cli_usage(getattr(result, "usage", None), input_includes_cache=True),
        billing="subscription",
    )
    return str(result.text or "").strip()


def _run_codex_prompt(
    user: dict[str, Any] | None,
    workspace: Path,
    prompt: str,
    *,
    model: str | None = None,
    owner_id: int | None = None,
    timeout: int = 240,
    source_env: dict[str, str] | None = None,
) -> str:
    """One-shot answer billed only to the caller's connected Codex account."""
    from agent_monitor.jobs import _configure_auth_route
    from agent_monitor.settings import restrict_account_base_urls, resolved_user_env

    session_owner = (user or {}).get("id")
    if owner_id is not None and session_owner is not None:
        if int(owner_id) != int(session_owner):
            raise ValueError("The run owner does not match the signed-in account")
    route_owner = owner_id if owner_id is not None else session_owner
    from agent_monitor.subprocess_env import child_process_env

    env = child_process_env(
        extra=restrict_account_base_urls(
            {
                **{key: value for key, value in (source_env or {}).items() if value},
                **{k: v for k, v in (resolved_user_env(user) or {}).items() if v},
            },
            user,
        )
    )
    env = _configure_auth_route(
        env,
        route="codex_subscription",
        user_id=int(route_owner) if route_owner is not None else None,
    )
    return _run_codex_prompt_env(
        env, workspace, prompt, model=model, timeout=timeout
    )


def _sanitize_cached_graph(raw: Any) -> dict[str, Any]:
    graph = _sanitize(raw)
    provenance = raw.get("provenance") if isinstance(raw, dict) else None
    if isinstance(provenance, dict):
        clean: dict[str, Any] = {}
        for key, limit in (
            ("kind", 40), ("producer", 80), ("toolchain", 200),
            ("mathlib_fingerprint", 64),
        ):
            clean[key] = str(provenance.get(key) or "")[:limit]
        for key in (
            "matched_declarations", "proof_edges", "statement_edges",
            "omitted_proof_edges", "omitted_statement_edges",
            "dependency_truncated_count", "sorry_dependent_declarations",
        ):
            value = provenance.get(key, 0)
            clean[key] = (
                value
                if isinstance(value, int)
                and not isinstance(value, bool)
                and 0 <= value <= MAX_GRAPH_EDGES
                else 0
            )
        graph["provenance"] = clean
    return graph


def load_cached(workspace: Path, *, kind: str = "informal") -> dict[str, Any] | None:
    name = LEAN_GRAPH_FILENAME if str(kind).lower() == "formal" else GRAPH_FILENAME
    p = Path(workspace).resolve() / name
    try:
        fd = os.open(
            p,
            os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
        )
        with os.fdopen(fd, "rb") as handle:
            state = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(state.st_mode)
                or state.st_size > MAX_GRAPH_CACHE_BYTES
            ):
                return None
            raw = handle.read(MAX_GRAPH_CACHE_BYTES + 1)
        if len(raw) > MAX_GRAPH_CACHE_BYTES:
            return None
        value = json.loads(raw.decode())
    except (
        json.JSONDecodeError, OSError, UnicodeDecodeError, RecursionError,
        ValueError,
    ):
        return None
    if not isinstance(value, dict) or not isinstance(value.get("graph"), dict):
        return None
    try:
        value = dict(value)
        value["graph"] = _sanitize_cached_graph(value["graph"])
        if "compiler_graph" in value:
            value["compiler_graph"] = _sanitize_cached_graph(
                value["compiler_graph"]
            )
    except ValueError:
        return None
    return value


def _write_cached(
    workspace: Path, name: str, result: dict[str, Any]
) -> None:
    root = Path(workspace).resolve()
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    if len(payload.encode()) > MAX_GRAPH_CACHE_BYTES:
        raise ValueError("proof graph cache exceeds its size limit")
    fd, raw = tempfile.mkstemp(prefix=".proof-graph-", suffix=".tmp", dir=root)
    tmp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, root / name)
    finally:
        tmp.unlink(missing_ok=True)


def _lean_source(workspace: Path) -> str:
    return _read_workspace_text(
        workspace, "lean/Proof.lean", limit=MAX_SOURCE_BYTES
    )


def _lean_sig(body: str) -> str:
    """Best-effort Lean signature / have-line for display (no English prose)."""
    text = (body or "").strip()
    if not text:
        return ""
    # Drop leading attributes / doc comments already stripped by decl parser.
    # Prefer the part before `:=` / `where`, keep binders + type.
    for sep in (":=", " where\n", " where "):
        if sep in text:
            text = text.split(sep, 1)[0].strip()
            break
    # Collapse whitespace but keep it reading like code.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n+", "\n", text).strip()
    return text[:900]


def graph_from_lean(
    lean: str, *, kg_snapshot: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build a structural DAG, preferring exact compiler dependencies.

    The optional snapshot is observational metadata produced by lean_kg. A
    missing/invalid snapshot retains the legacy
    text parser, and metadata can never change Lean verification status.
    """
    from agent_monitor import lean_kg
    from agent_monitor.lean_verify import _decls_with_docs, _lean_executable_text

    if len((lean or "").encode()) > MAX_SOURCE_BYTES:
        raise ValueError("Lean source exceeds the Formal DAG limit")
    decls = _decls_with_docs(lean)
    if len(decls) > MAX_LEAN_DECLARATIONS:
        raise ValueError("Lean source has too many declarations for the DAG")
    if not decls:
        return {"title": "Lean proof", "nodes": [], "edges": []}
    lines = (lean or "").splitlines()
    # Comments and strings blanked, line for line: a name that only appears in
    # a comment (the next declaration's `Cites:` doc line, a `-- by foo`
    # remark) is not a use.
    code_lines = _lean_executable_text(lean or "").splitlines()
    kind_map = {
        "axiom": "assumption",
        "opaque": "assumption",
        "def": "definition",
        "abbrev": "definition",
        "structure": "definition",
        "inductive": "definition",
        "instance": "definition",
        "lemma": "lemma",
        "theorem": "claim",
        "example": "step",
    }
    main_idx = next(
        (i for i, d in enumerate(decls) if d.get("kind") == "theorem" and str(d.get("name") or "").lower() == "main"),
        None,
    )
    if main_idx is None:
        main_idx = next(
            (i for i in range(len(decls) - 1, -1, -1) if decls[i].get("kind") in {"theorem", "lemma"}),
            None,
        )

    nodes: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for i, d in enumerate(decls):
        raw_name = str(d.get("name") or "").strip() or f"{d.get('kind')}_{i+1}"
        nid = re.sub(r"[^A-Za-z0-9_]+", "_", raw_name) or f"n{i+1}"
        base, n = nid, 2
        while nid in used_ids:
            nid = f"{base}_{n}"
            n += 1
        used_ids.add(nid)
        start = max(0, int(d.get("line") or 1) - 1)
        end = decls[i + 1]["line"] - 1 if i + 1 < len(decls) else len(lines)
        # The slice up to the next declaration also holds that declaration's
        # doc comment; end at the last line with code instead.
        while end > start + 1 and not (
            code_lines[end - 1] if end - 1 < len(code_lines) else ""
        ).strip():
            end -= 1
        body = "\n".join(lines[start:end]).strip()
        code_body = "\n".join(code_lines[start:end])
        lean_kind = str(d.get("kind") or "def")
        statement = _lean_sig(body) or f"{lean_kind} {raw_name}"
        cites = []
        for ln in (d.get("doc") or "").splitlines():
            m = re.match(r"(?i)^\s*cites?\s*[:：]\s*(.+)$", ln)
            if m:
                cites.append(m.group(1).strip()[:120])
        kind = kind_map.get(lean_kind, "step")
        if i == main_idx:
            kind = "conclusion"
        nodes.append(
            {
                "id": nid,
                "kind": kind,
                "lean_kind": lean_kind,
                "label": raw_name,
                "statement": statement,
                "citations": cites,
                "_body": code_body,
            }
        )

    edges: list[dict[str, str]] = []
    names = [
        (str(declaration.get("name") or ""), nodes[index]["id"])
        for index, declaration in enumerate(decls)
    ]
    raw_name_counts: dict[str, int] = {}
    for raw_name, _node_id in names:
        if raw_name:
            raw_name_counts[raw_name] = raw_name_counts.get(raw_name, 0) + 1
    node_by_line = {
        int(decl.get("line") or 0): nodes[index]["id"]
        for index, decl in enumerate(decls)
    }
    index_by_node = {node["id"]: index for index, node in enumerate(nodes)}
    compiler_targets: set[str] = set()
    qualified_to_node: dict[str, str] = {}
    kg_ok = bool(
        isinstance(kg_snapshot, dict)
        and kg_snapshot.get("schema_version") == lean_kg.SCHEMA_VERSION
        and kg_snapshot.get("producer") == lean_kg.PRODUCER
        and kg_snapshot.get("status") == "ok"
        and kg_snapshot.get("sandbox") in lean_kg.TRUSTED_SANDBOXES
        and kg_snapshot.get("source_sha256")
        == hashlib.sha256(lean.encode()).hexdigest()
        and kg_snapshot.get("normalized_source_sha256")
        == lean_kg.normalized_source_sha256(lean)
    )
    kg_declarations = (
        kg_snapshot.get("declarations") or [] if kg_ok else []
    )
    sorry_dependent: set[str] = set()
    for item in kg_declarations:
        if not isinstance(item, dict) or not item.get("matched"):
            continue
        target = node_by_line.get(int(item.get("line") or 0))
        qualified = str(item.get("qualified_name") or "")
        if target and qualified:
            compiler_targets.add(target)
            qualified_to_node[qualified] = target
            if item.get("uses_sorry") is True:
                sorry_dependent.add(target)
    for node in nodes:
        if node["id"] in sorry_dependent:
            # Compiler evidence: this declaration (or something it uses)
            # still depends on `sorry`; it is structure, not a proof.
            node["citations"] = ["depends on sorry (unproved)", *node["citations"]]
    for item in kg_declarations:
        if not isinstance(item, dict) or not item.get("matched"):
            continue
        target = node_by_line.get(int(item.get("line") or 0))
        if not target:
            continue
        proof_uses = sorted(
            {
                str(name)
                for name in (item.get("local_proof_uses") or [])
                if str(name).strip()
            }
        )
        statement_mentions = sorted(
            {
                str(name)
                for name in (item.get("local_statement_mentions") or [])
                if str(name).strip()
            }
        )
        linked: set[tuple[str, str]] = set()
        for dependency in proof_uses:
            source = qualified_to_node.get(dependency)
            edge_key = (source or "", "proof uses")
            if (
                source
                and index_by_node[source] < index_by_node[target]
                and edge_key not in linked
            ):
                edges.append(
                    {"from": source, "to": target, "label": "proof uses"}
                )
                linked.add(edge_key)
        for dependency in statement_mentions:
            source = qualified_to_node.get(dependency)
            edge_key = (source or "", "type mentions")
            if (
                source
                and index_by_node[source] < index_by_node[target]
                and edge_key not in linked
            ):
                edges.append(
                    {"from": source, "to": target, "label": "type mentions"}
                )
                linked.add(edge_key)

    for i, node in enumerate(nodes):
        body = node.pop("_body", "")
        if node["id"] in compiler_targets:
            continue
        linked_fallback: set[str] = set()
        current_name = names[i][0]
        for j in range(i):
            nm, src = names[j]
            if (
                not nm
                or not src
                or len(nm) < 2
                or raw_name_counts.get(nm, 0) != 1
                or nm == current_name
                or src in linked_fallback
            ):
                continue
            if re.search(r"\b" + re.escape(nm) + r"\b", body):
                edges.append({"from": src, "to": node["id"], "label": "uses"})
                linked_fallback.add(src)
    title = nodes[main_idx]["label"] if main_idx is not None and main_idx < len(nodes) else "Lean proof"
    graph = _sanitize({"title": title, "nodes": nodes, "edges": edges})
    if compiler_targets:
        retained_proof_edges = sum(
            edge.get("label") == "proof uses" for edge in graph["edges"]
        )
        retained_statement_edges = sum(
            edge.get("label") == "type mentions" for edge in graph["edges"]
        )
        graph["provenance"] = {
            "kind": "lean-compiler",
            "producer": str(kg_snapshot.get("producer") or "")[:80],
            "toolchain": str(kg_snapshot.get("toolchain") or "")[:200],
            "mathlib_fingerprint": str(
                kg_snapshot.get("mathlib_fingerprint") or ""
            )[:64],
            "matched_declarations": len(compiler_targets),
            "proof_edges": retained_proof_edges,
            "statement_edges": retained_statement_edges,
            "omitted_proof_edges": max(
                0,
                int(kg_snapshot.get("proof_edge_count") or 0)
                - retained_proof_edges,
            ),
            "omitted_statement_edges": max(
                0,
                int(kg_snapshot.get("statement_edge_count") or 0)
                - retained_statement_edges,
            ),
            "dependency_truncated_count": int(
                kg_snapshot.get("dependency_truncated_count") or 0
            ),
            "sorry_dependent_declarations": len(sorry_dependent),
        }
    return graph


def graph_from_text(text: str) -> dict[str, Any]:
    """Deterministic fallback used by the automatic derived-tool pipeline.

    It deliberately avoids launching a second agent harness when a direct DAG
    model is unavailable. The result is structural rather than semantic, but it
    keeps the selected engine as the run's sole harness.
    """
    src = str(text or "").strip()
    if not src:
        raise ValueError("proof text is empty")
    blocks = [
        re.sub(r"(?m)^#{1,6}\s+", "", part).strip()
        for part in re.split(r"\n\s*\n+", src)
        if part.strip()
    ]
    if len(blocks) < 2:
        blocks = [
            part.strip()
            for part in re.split(r"(?<=[.!?])\s+(?=[A-Z])", src)
            if part.strip()
        ]
    blocks = [block for block in blocks if len(block) > 12][:16] or [src[:4000]]
    nodes: list[dict[str, Any]] = []
    for index, block in enumerate(blocks):
        plain = re.sub(r"[#*_~`]+", " ", block)
        plain = re.sub(r"\s+", " ", plain).strip()
        lowered = plain.lower()
        if index == len(blocks) - 1:
            kind = "conclusion"
        elif index == 0 and any(word in lowered for word in ("assume", "given", "let ", "suppose")):
            kind = "assumption"
        elif any(word in lowered for word in ("lemma", "claim", "we show", "it remains")):
            kind = "claim"
        elif "contradict" in lowered:
            kind = "contradiction"
        else:
            kind = "step"
        label_words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'_-]*", plain)[:8]
        nodes.append(
            {
                "id": f"n{index + 1}",
                "kind": kind,
                "label": " ".join(label_words)[:100] or f"Step {index + 1}",
                "statement": plain[:1500],
                "citations": ["structural parse"],
            }
        )
    edges = [
        {"from": nodes[index - 1]["id"], "to": nodes[index]["id"], "label": "supports"}
        for index in range(1, len(nodes))
    ]
    return _sanitize({"title": nodes[-1]["label"], "nodes": nodes, "edges": edges})


FACT_GRAPH_MODEL = "cgm-fact-graph"
FACT_GRAPH_ATTESTED_BASIS = "model-verifier accepted (not a Lean certificate)"
FACT_GRAPH_UNATTESTED_BASIS = "unattested CGM facts (no valid verifier receipt)"


def _load_fact_dag(workspace: Path) -> list[dict[str, Any]] | None:
    """The run's validated CGM fact DAG (``engine_graph.load_fact_dag``)."""
    try:
        from agent_monitor import engine_graph

        loader = getattr(engine_graph, "load_fact_dag", None)
        facts = loader(Path(workspace)) if callable(loader) else None
    except Exception:  # noqa: BLE001 - an unreadable fact graph means "none"
        return None
    if not isinstance(facts, list) or not facts:
        return None
    return facts


def _load_proof_labels(workspace: Path) -> dict[str, str] | None:
    """The labels the run's proof.md names its lemmas by
    (``engine_graph.load_proof_labels``); ``None`` when it numbers them."""
    try:
        from agent_monitor import engine_graph

        loader = getattr(engine_graph, "load_proof_labels", None)
        labels = loader(Path(workspace)) if callable(loader) else None
    except Exception:  # noqa: BLE001 - unreadable labels mean numbered lemmas
        return None
    return labels if isinstance(labels, dict) and labels else None


def _facts_digest(
    facts: list[dict[str, Any]], labels: Mapping[str, str] | None = None
) -> str:
    canonical: Any = [
        {
            "fact_id": str(fact.get("fact_id") or ""),
            "statement": str(fact.get("statement") or ""),
            "predecessors": [str(p) for p in fact.get("predecessors") or []],
            "is_target": bool(fact.get("is_target")),
            "attested": bool(fact.get("attested")),
        }
        for fact in facts
        if isinstance(fact, Mapping)
    ]
    if labels is not None:
        # The lemma names are part of the graph; numbered runs keep their digest.
        canonical = {"facts": canonical, "labels": dict(labels)}
    return hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def graph_from_facts(
    facts: list[dict[str, Any]], labels: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Informal DAG built deterministically from a CGM fact DAG (no model).

    One node per fact (the target is the ``conclusion``, every other fact a
    ``lemma``), node id = fact id (so Lean's ``Informal: <fact_id>`` doc lines
    and the coverage cross-linker line up), statements verbatim within the
    sanitizer bounds, and one ``uses`` edge per predecessor.  Lemmas are named
    as proof.md names them: "Lemma <label>" from ``labels`` (a
    ``verified-closure@2`` proof), else "Lemma k" in the facts' order.
    Acceptance is a model verifier's, never a Lean certificate.
    """
    if not isinstance(facts, list) or not facts:
        raise ValueError("fact graph is empty")
    if len(facts) > MAX_GRAPH_NODES:
        raise ValueError("fact graph exceeds the proof graph node limit")
    targets = [fact for fact in facts if isinstance(fact, Mapping) and fact.get("is_target")]
    if len(targets) != 1:
        # A verifier-partial run has lemmas but no proved target; its facts
        # do not form an informal proof with one conclusion.
        raise ValueError("fact graph has no single target conclusion")
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    lemma_index = 0
    for fact in facts:
        if not isinstance(fact, Mapping):
            raise ValueError("fact graph item is not an object")
        fact_id = str(fact.get("fact_id") or "").strip()
        if not fact_id or len(fact_id) > MAX_GRAPH_ID_LENGTH:
            raise ValueError("fact graph item has an invalid id")
        is_target = bool(fact.get("is_target"))
        if is_target:
            label = "Target theorem"
        else:
            lemma_index += 1
            name = labels[fact_id] if labels and fact_id in labels else lemma_index
            label = f"Lemma {name}"
        citations = [f"cgm fact {fact_id}"[:120]]
        citations.append(
            FACT_GRAPH_ATTESTED_BASIS
            if fact.get("attested") is True
            else "unattested fact"
        )
        nodes.append(
            {
                "id": fact_id,
                "kind": "conclusion" if is_target else "lemma",
                "label": label,
                "statement": str(fact.get("statement") or "").strip(),
                "citations": citations,
            }
        )
        for predecessor in dict.fromkeys(
            str(item) for item in fact.get("predecessors") or []
        ):
            edges.append({"from": predecessor, "to": fact_id, "label": "uses"})
    title = " ".join(str(targets[0].get("statement") or "").split())[:200]
    graph = _sanitize({"title": title, "nodes": nodes, "edges": edges})
    if len(graph["edges"]) != len(edges):
        # The fact DAG was validated upstream; a dropped edge means an unknown
        # predecessor or a cycle, so refuse rather than publish a wrong DAG.
        raise ValueError("fact graph edges do not form a DAG over its facts")
    return graph


def fact_graph_basis(result: Mapping[str, Any] | None) -> str:
    """Human label for what a ``cgm-fact-graph`` result rests on."""
    if isinstance(result, Mapping) and result.get("attested") is True:
        return FACT_GRAPH_ATTESTED_BASIS
    return FACT_GRAPH_UNATTESTED_BASIS


def fact_graph_result(workspace: Path) -> dict[str, Any] | None:
    """Cache-ready informal DAG result from the run's CGM facts, or ``None``.

    ``None`` also when the facts have no single target conclusion (a
    verifier-partial run); callers then fall back to the model-derived DAG.
    ``attested`` is true only for a valid ``harness-gate-receipt``.
    """
    facts = _load_fact_dag(workspace)
    if not facts:
        return None
    labels = _load_proof_labels(workspace)
    try:
        graph = graph_from_facts(facts, labels)
    except ValueError:
        return None
    attested = all(fact.get("attested") is True for fact in facts)
    result = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": FACT_GRAPH_MODEL,
        "source": "math_harness fact graph",
        "source_sha256": _facts_digest(facts, labels),
        "kind": "informal",
        "status": "derived",
        "derivation": "deterministic",
        "attested": attested,
        "certificate": False,
        "graph": graph,
    }
    result["basis"] = fact_graph_basis(result)
    return result


def _is_analysis_record(run_record: Mapping[str, Any] | None) -> bool:
    """An Analyze run: its informal DAG is the analysis graph's projection.

    Decided by the run record, never by workspace files (a proof agent could
    write an ``analysis/`` folder into its own workspace).
    """
    record = run_record or {}
    return str(record.get("run_kind") or "") == "analyze" or str(record.get("engine") or "") == "analyze"


def _is_readability_record(run_record: Mapping[str, Any] | None) -> bool:
    """A readability run (decided by the record, as for an analysis)."""
    record = run_record or {}
    return str(record.get("run_kind") or "") == "readability" or str(record.get("engine") or "") == "readability"


def analysis_projection(workspace: Path) -> dict[str, Any] | None:
    """The deterministic ``proof_graph.json`` envelope of an Analyze run, from
    ``analysis/analysis_graph.json`` (``None`` before it is published); kept
    in the cache only when it changed.  Never calls a model."""
    from agent_monitor.analyze import graph as analysis_graph

    graph = analysis_graph.load(workspace)
    if graph is None:
        return None
    result = analysis_graph.to_proof_graph(graph)
    cached = load_cached(workspace, kind="informal")
    if (
        cached
        and cached.get("model") == result.get("model")
        and cached.get("source_sha256") == result.get("source_sha256")
    ):
        return cached
    try:
        _write_cached(workspace, GRAPH_FILENAME, result)
    except (OSError, ValueError):
        pass
    return result


def load_or_build_informal(
    workspace: Path, run_record: Mapping[str, Any] | None = None
) -> dict[str, Any] | None:
    """Cached informal DAG, built deterministically from CGM facts if absent.

    Read paths never replace a model-derived DAG: Lean ``Informal: <node>``
    coverage links point at its node ids, so only an explicit refresh
    (``generate``) or the automatic pipeline switches such a run to the fact
    graph.  A missing cache, or an earlier fact graph whose facts changed, is
    (re)built and persisted.  Never calls a model.  An Analyze run's informal
    DAG is always the projection of its analysis graph.
    """
    if _is_analysis_record(run_record):
        return analysis_projection(workspace) or load_cached(workspace, kind="informal")
    cached = load_cached(workspace, kind="informal")
    if cached and cached.get("model") != FACT_GRAPH_MODEL:
        return cached
    facts = _load_fact_dag(workspace)
    if not facts:
        return cached
    if cached and cached.get("source_sha256") == _facts_digest(
        facts, _load_proof_labels(workspace)
    ):
        return cached
    result = fact_graph_result(workspace)
    if result is None:
        return cached
    try:
        _write_cached(workspace, GRAPH_FILENAME, result)
    except (OSError, ValueError):
        pass
    return result


def _compiler_snapshot_for_verified_source(
    workspace: Path, lean: str
) -> dict[str, Any] | None:
    """Load/extract KG only for the exact kernel-checked workspace source."""
    from agent_monitor import lean_kg, lean_verify

    checked = lean_verify.load_cached(workspace) or {}
    status = str(checked.get("status") or "")
    if status not in {"verified", "unfaithful", "incomplete"}:
        return None
    recorded = str(checked.get("lean") or "")
    if lean not in {recorded, recorded.rstrip() + "\n"}:
        return None
    # An incomplete file that compiled is bound by `checked_source_sha256`;
    # its compiler facts mark sorry-dependent declarations.
    bound_field = (
        "checked_source_sha256" if status == "incomplete" else "verified_source_sha256"
    )
    if str(checked.get(bound_field) or "") != hashlib.sha256(
        lean.encode()
    ).hexdigest():
        return None
    checked_toolchain = str(
        ((checked.get("toolchain") or {}).get("version") or "")
        if isinstance(checked.get("toolchain"), dict)
        else ""
    )
    if not checked_toolchain:
        return None
    toolchain = checked_toolchain
    snapshot = lean_kg.load_snapshot(
        workspace, source=lean, toolchain=toolchain
    )
    if snapshot is None:
        snapshot = lean_kg.extract_snapshot(
            workspace,
            source=lean,
            uses_mathlib=bool(checked.get("uses_mathlib")),
        )
    return snapshot


def load_or_parse_formal(workspace: Path) -> dict[str, Any] | None:
    """Cached current graph, else compiler-derived or structural Formal DAG."""
    from agent_monitor import lean_kg

    lean = _lean_source(workspace)
    if not lean.strip():
        return None
    source_hash = lean_kg.normalized_source_sha256(lean)
    cached = load_cached(workspace, kind="formal")
    if (
        cached
        and cached.get("graph")
        and cached.get("source_sha256") == source_hash
    ):
        result = dict(cached)
        result.pop("compiler_graph", None)
        result.pop("knowledge_graph", None)
        result.pop("provenance", None)
        cached_graph = result.get("graph")
        cached_provenance = (
            cached_graph.get("provenance")
            if isinstance(cached_graph, dict)
            else None
        )
        compiler_edge_labels = {"proof uses", "type mentions"}
        cached_was_compiler = bool(
            str(result.get("model") or "") == "lean-compiler"
            or (
                isinstance(cached_provenance, dict)
                and (
                    cached_provenance.get("kind") == "lean-compiler"
                    or str(cached_provenance.get("producer") or "").startswith(
                        "agent-monitor-lean-env-"
                    )
                )
            )
            or any(
                str(edge.get("label") or "") in compiler_edge_labels
                for edge in ((cached_graph or {}).get("edges") or [])
                if isinstance(edge, dict)
            )
        )
        if isinstance(cached_graph, dict):
            untrusted_graph = dict(cached_graph)
            untrusted_graph.pop("provenance", None)
            result["graph"] = untrusted_graph
        snapshot = _compiler_snapshot_for_verified_source(workspace, lean)
        result["knowledge_graph"] = lean_kg.snapshot_summary(snapshot)
        refreshed_graph = graph_from_lean(lean, kg_snapshot=snapshot)
        if cached_was_compiler:
            result["graph"] = refreshed_graph
            result["model"] = (
                "lean-compiler"
                if refreshed_graph.get("provenance")
                else "lean-parse"
            )
        elif refreshed_graph.get("provenance"):
            result["compiler_graph"] = refreshed_graph
        return result
    snapshot = _compiler_snapshot_for_verified_source(workspace, lean)
    graph = graph_from_lean(lean, kg_snapshot=snapshot)
    if not graph.get("nodes"):
        return {"status": "none", "reason": "no Lean declarations yet"}
    kg_summary = lean_kg.snapshot_summary(snapshot)
    compiler_graph = bool(graph.get("provenance"))
    return {
        "generated_at": None,
        "model": "lean-compiler" if compiler_graph else "lean-parse",
        "source": "Proof.lean",
        "source_sha256": source_hash,
        "kind": "formal",
        "status": "parsed",
        "knowledge_graph": kg_summary,
        "graph": graph,
    }


def _deterministic_formal_result(
    workspace: Path, lean: str
) -> dict[str, Any] | None:
    """Prefer an exact or deterministic Formal DAG over an LLM re-extraction.

    Primary graph: the declaration-level structure of Proof.lean. When the
    kernel-checked compiler snapshot is available its exact dependencies
    become the primary edges (relabelled so the structural Formal view keeps
    showing them) and the provenance-bearing graph stays in
    ``compiler_graph`` for the Compiler-facts view. Without compiler data the
    text parse is used only when it is informative (at least three
    declarations and two dependency edges); otherwise return ``None`` so the
    caller asks a model to expose the steps inside a long proof.
    """
    from agent_monitor import lean_kg

    try:
        snapshot = _compiler_snapshot_for_verified_source(workspace, lean)
    except (OSError, ValueError):
        snapshot = None
    try:
        structural = graph_from_lean(lean)
        compiler_graph = graph_from_lean(lean, kg_snapshot=snapshot) if snapshot else None
    except ValueError:
        return None
    if not structural.get("nodes"):
        return None
    compiler_ok = bool(compiler_graph and compiler_graph.get("provenance"))
    if compiler_ok:
        primary = dict(structural)
        primary["edges"] = [
            {
                "from": edge["from"],
                "to": edge["to"],
                "label": "uses" if edge.get("label") == "proof uses" else "statement uses",
            }
            for edge in compiler_graph.get("edges") or []
            if isinstance(edge, dict)
        ]
    elif len(structural.get("nodes") or []) >= 3 and len(structural.get("edges") or []) >= 2:
        primary = structural
    else:
        return None
    result: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": "lean-parse",
        "source": "Proof.lean",
        "source_sha256": hashlib.sha256(lean.strip().encode()).hexdigest(),
        "kind": "formal",
        "graph": _sanitize(primary),
        "knowledge_graph": lean_kg.snapshot_summary(snapshot),
    }
    if compiler_ok:
        result["compiler_graph"] = compiler_graph
    return result


@usage_ledger.attributed("dag")
def generate(
    *,
    workspace: Path,
    run_record: dict[str, Any] | None,
    user: dict[str, Any] | None,
    model: str | None = None,
    kind: str = "informal",
    allow_codex_fallback: bool = True,
) -> dict[str, Any]:
    """Generate and cache a proof graph.

    Manual generation may use the connected Codex account. The automatic
    cooperative pipeline disables that fallback so it never launches a hidden
    second harness when another engine was selected.
    """
    from agent_monitor.settings import resolved_user_env

    kind = "formal" if str(kind or "").strip().lower() == "formal" else "informal"
    problem = ""
    problem = _read_workspace_text(
        workspace, "problem.txt", limit=MAX_PROBLEM_BYTES
    ).strip()

    if kind == "formal":
        lean = _lean_source(workspace)
        if not lean.strip():
            raise ValueError("No Proof.lean yet — run Lean Verification first")
        source = "Proof.lean"
        structural_source = lean
        prompt = LEAN_GRAPH_PROMPT.replace("{problem}", problem[:6000]).replace("{lean}", lean[:60000])
        out_name = LEAN_GRAPH_FILENAME
        deterministic = _deterministic_formal_result(workspace, lean)
        if deterministic is not None:
            # The declaration-level graph is exact (compiler dependencies) or
            # at least deterministic; no model re-extraction is needed.
            _write_cached(workspace, out_name, deterministic)
            return deterministic
    else:
        if _is_readability_record(run_record):
            # A rewrite of someone's manuscript is never read as a proof; its
            # paper map is built from the manuscript structure.
            raise ValueError("A readability run has a paper map, not a proof DAG")
        if _is_analysis_record(run_record):
            # Regenerate guard: an analysis's DAG is its manuscript structure.
            # GRAPH_PROMPT never re-reads the report (or a model reply) as a proof.
            projection = analysis_projection(workspace)
            if projection is None:
                raise ValueError("The analysis has not published its graph yet")
            return projection
        fact_result = fact_graph_result(workspace)
        if fact_result is not None:
            # A verified CGM fact DAG *is* the informal structure; derive it
            # deterministically instead of asking a model to re-extract it.
            _write_cached(workspace, GRAPH_FILENAME, fact_result)
            return fact_result
        source, proof = _proof_source(workspace, run_record)
        if not proof:
            raise ValueError("No proof text yet — wait for the run to produce proof.md")
        structural_source = proof
        prompt = GRAPH_PROMPT.replace("{problem}", problem[:6000]).replace("{proof}", proof[:60000])
        out_name = GRAPH_FILENAME

    is_guest = bool((user or {}).get("guest") or (run_record or {}).get("guest"))
    if is_guest:
        from agent_monitor.lean_verify import _llm_environment

        env = _llm_environment(user, run_record)
        model = "kimi-k3"
        allow_codex_fallback = False
    else:
        from agent_monitor.settings import restrict_account_base_urls

        # A non-admin account never chooses where derived calls are sent.
        env = restrict_account_base_urls(dict(resolved_user_env(user) or {}), user)
    if (
        not is_guest
        and str((run_record or {}).get("credential_source") or "").strip()
        == "sponsored_kimi"
    ):
        from agent_monitor import sponsored_kimi_client

        owner_id = (user or {}).get("id") or (run_record or {}).get("owner_id")
        cache_key = str((run_record or {}).get("run_id") or workspace.name)
        env.update(
            sponsored_kimi_client.issue_credentials(
                engine="formal",
                client_id=owner_id,
                cache_key=cache_key,
                # Outlasts a sponsored call's 630 s (kimi_k3.SPONSORED_TIMEOUT_SECONDS),
                # or 1,260 s when its reply is asked again at lower effort.
                minimum_ttl_seconds=1_500,
            )
        )
    from agent_monitor.run_tuning import from_record as apply_recorded_tuning

    if not is_guest:
        apply_recorded_tuning(env, run_record)
    graph: dict[str, Any] | None = None
    compiler_snapshot: dict[str, Any] | None = None
    used_structural_fallback = False
    chosen_model = ""
    content = ""
    from agent_monitor.jobs import _route_from_record

    recorded_route = "api_key" if is_guest else _route_from_record(run_record)
    if recorded_route == "api_key" and not model:
        from agent_monitor.auto_pipeline import _api_model

        # A rebuild that names no model uses the run's own model, as the
        # automatic stages do, not the account's current default.
        model = _api_model(
            (run_record or {}).get("requested_model") or (run_record or {}).get("model")
        )
    requested = str(model or "").strip().lower()
    requested_codex = requested in {"codex", "codex-cli", "codex cli"}
    requested_claude = requested in _CLAUDE_ACCOUNT_MODELS
    from agent_monitor import account_policy

    # Only a legacy run (no recorded route) may pick a subscription by model
    # name or account preference, and never a member's: subscriptions are the
    # administrator's, even a sign-in a member linked before that policy.
    may_pick_subscription = recorded_route is None and not account_policy.is_restricted(user)
    use_codex = recorded_route == "codex_subscription" or (
        requested_codex and allow_codex_fallback and may_pick_subscription
    )
    if requested_codex and allow_codex_fallback and not use_codex:
        # Say why "codex" is refused rather than letting it fail as an API
        # model name: a member gets the note its Settings show, anyone else
        # the route the run was recorded on.
        route_name = {
            "api_key": "an API key",
            "claude_subscription": "a Claude subscription",
        }.get(str(recorded_route), str(recorded_route).replace("_", " "))
        raise ValueError(
            account_policy.member_route_error(user, "codex_subscription")
            or f"This run was recorded on {route_name}, so its DAG is rebuilt on "
            "that route, not on a Codex subscription; leave the model empty to "
            "use the run's own model"
        )
    use_claude = recorded_route == "claude_subscription"
    if may_pick_subscription and not use_claude:
        from agent_monitor.settings import claude_enabled

        use_claude = (
            (requested_claude or (allow_codex_fallback and claude_enabled(user, env=env)))
            and _claude_account_ready(user)
        )

    if use_claude:
        chosen_model, content = _run_claude_prompt(
            user,
            prompt,
            model=model
            or str(
                (run_record or {}).get("requested_model")
                or (run_record or {}).get("model")
                or ""
            ).strip()
            or None,
            source_env=env,
        )
    elif not use_codex:
        try:
            chosen_model, content, _provider = _call_llm(
                env, model, prompt, timeout=180
            )
        except ValueError:
            if allow_codex_fallback:
                # Manual DAG generation of a legacy run (no recorded route) may
                # use the user's connected Codex account, subject to their
                # preference; a run recorded on an API key stays on it.
                from agent_monitor.settings import codex_enabled

                if (
                    may_pick_subscription
                    and codex_enabled(user, env=env)
                    and _codex_account_ready(user)
                ):
                    use_codex = True
                else:
                    raise
            else:
                used_structural_fallback = True
                chosen_model = "lean-parse" if kind == "formal" else "structural-parse"
                if kind == "formal":
                    compiler_snapshot = _compiler_snapshot_for_verified_source(
                        workspace, structural_source
                    )
                    graph = graph_from_lean(
                        structural_source, kg_snapshot=compiler_snapshot
                    )
                else:
                    graph = graph_from_text(structural_source)

    if use_codex:
        codex_model = str(
            model or (run_record or {}).get("model") or ""
        ).strip() or None
        chosen_model = codex_model or "codex"
        content = _run_codex_prompt(
            user,
            workspace,
            prompt,
            model=codex_model,
            owner_id=(run_record or {}).get("owner_id"),
            source_env=env,
        )

    if graph is None:
        graph = _sanitize(_extract_json(str(content)))
    compiler_graph: dict[str, Any] | None = None
    kg_summary: dict[str, Any] | None = None
    if kind == "formal":
        from agent_monitor import lean_kg

        if compiler_snapshot is None:
            compiler_snapshot = _compiler_snapshot_for_verified_source(
                workspace, structural_source
            )
        kg_summary = lean_kg.snapshot_summary(compiler_snapshot)
        compiler_graph = graph_from_lean(
            structural_source, kg_snapshot=compiler_snapshot
        )
        if (
            used_structural_fallback
            and compiler_graph.get("provenance")
        ):
            graph = compiler_graph
            chosen_model = "lean-compiler"
    result = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "model": chosen_model,
        "source": source,
        "source_sha256": hashlib.sha256(
            structural_source.strip().encode()
        ).hexdigest(),
        "kind": kind,
        "graph": graph,
    }
    if kg_summary is not None:
        result["knowledge_graph"] = kg_summary
    if (
        compiler_graph is not None
        and compiler_graph.get("provenance")
        and compiler_graph is not graph
    ):
        result["compiler_graph"] = compiler_graph
    _write_cached(workspace, out_name, result)
    return result
