# Standalone public Kimi deployment

This is a separate anonymous proof site. It must never be implemented by
turning public mode on for the private ProvingConsole server. The public site
uses its own hostname, port, systemd unit, dynamic user, state directory,
anonymous sessions, run database, UI, and provider credential.

## Fixed HTTP and model contract

The public server exposes only:

- `GET /`
- `GET /static/public_kimi.css`
- `GET /static/public_kimi.js`
- `GET /api/config`
- `POST /api/runs` with exactly `{"engine": "...", "problem": "..."}`
- `GET /api/runs/<unguessable-id>`
- `POST /api/runs/<unguessable-id>/stop`
- `GET /healthz`

There is no run-list, login, registration, settings, key, library, file-edit,
continuation, Lean, DAG, upload, or arbitrary-download route. Unknown fields,
engines, paths, and methods fail closed.

The model is always `kimi-k3` with `high` reasoning effort. The only public
engine is `plain`:

- Plain is response-only and performs one model call, plus at most one clean
  Markdown repair if the first response contains prohibited pseudo-tool markup.

All shell/tool-capable harnesses are excluded. The shared `jobs.start_job`
orchestrator is used only behind mandatory public guards: the automatic
Lean/DAG pipeline, user library, skills/persona injection, persistent memory,
pending feedback, subagents, and supplemental audit paths are disabled.
Both the jobs launcher and auto-pipeline entrypoint enforce the no-sidecar flag.

## Anonymous ownership and browser boundary

A random 256-bit host-only cookie owns each visitor's runs. It is
`HttpOnly; SameSite=Strict; Max-Age=86400`, and production sets `Secure`.
Caddy must preserve this cookie; removing it makes every status/stop request
lose ownership. Run IDs are also unguessable and there is no listing endpoint.

The page renders every model/status value with DOM `textContent`; it does not
interpret model output as HTML or Markdown. CSP, no-store, no-referrer,
frame-denial, and same-origin POST checks are enforced by both the application
and the separate Caddy host. Do not submit secrets or personal data: prompts
and proof artifacts exist in isolated public storage for up to 24 hours.

## Credential boundary

The real upstream Kimi key is supplied only through systemd
`LoadCredential=`. The standalone server reads it before importing the shared
job code, starts an in-process loopback gateway, removes the credential
directory and every inherited provider key/token, account home, proxy, and
runner command override from the environment, then installs only a random
loopback token and base URL for workers.

The gateway accepts only `POST /v1/chat/completions`, substitutes the real
key, forces `kimi-k3`, clamps output, and never returns provider error bodies.
The public browser remains Plain-only. The separately authenticated loopback
session API may preserve bounded tools and streaming for compatible harnesses
and Formal/Lean DAG; Caddy never exposes those internal paths. Public model
calls use one upstream attempt: ambiguous timeouts, 5xx responses, or empty
responses are not replayed, avoiding silent duplicate billing.

This urgent v1 keeps the gateway and the browser's response-only Plain worker
in the same hardened service. Tool-capable original-console harnesses run in a
different service and receive only expiring loopback proxy tokens, not the
credential mount. Within the public service, environment scrubbing is not an
OS-level credential boundary: compromised trusted Plain runner code could still guess and
read the credential file, although the public model has no tool path. A separate
key-holding gateway over a fixed-schema Unix socket is required for true
worker/key isolation and remains the next hardening step.

Use a dedicated provider key with a provider-side hard spend cap. Rotate any
credential that has ever appeared in chat, logs, shell history, or source.

## Implemented v1 limits

- request body: 32 KiB
- problem: 12,000 characters
- active runs: one per anonymous session, one per client IP, two globally
- starts: five/hour per session and per IP, 20/hour globally
- gateway calls: four concurrently, 120/hour globally
- original-console sponsored sessions: 24 active and 20 starts/hour per
  logical client, enough for all eight compatible harnesses plus Formal/DAG
- output: at most 8,192 tokens per model call
- public model attempts: one physical upstream attempt per logical call
- internal run iteration value: fixed at 8; users cannot change it
- model client timeout: 240 seconds; gateway upstream timeout: 225 seconds
- whole harness job timeout: 600 seconds
- public metadata/artifact retention: 24 hours, checked at startup and at
  bounded intervals during requests

The admission and gateway counters are in memory and reset when the service
restarts. Persistent daily spend/token reservation, request-id deduplication,
a durable disk quota, and an external cleanup timer are future hardening, not
v1 guarantees. The provider-side key cap is therefore the authoritative cost
backstop.

## Immutable release staging

Build a staging tree as an unprivileged user. The top level may contain only
`public-kimi-release.json`, `agent_monitor/`, and `venv/`. The release
preparer rejects VCS/environment/cache/backup files, application tests and
starter skills, database/key/credential assets, and every web asset except
`public_kimi.html`, `public_kimi.css`, and `public_kimi.js`. The virtual
environment remains an opaque dependency tree after global secret/backup
checks; it must be built with copied executables and reviewed separately.

The manifest is exact:

```json
{
  "schema_version": 1,
  "python": "venv/bin/python",
  "module": "agent_monitor.public_kimi_server",
  "model": "kimi-k3",
  "engine_allowlist": ["plain"],
  "auto_pipeline": false,
  "source_revision": "reviewed build identity"
}
```

Measure without executing staging code:

```bash
deploy/public-kimi/prepare_release.py \
  --source /path/to/reviewed-staging \
  --measure-only
```

Review the SHA-256 through an independent channel, stop all writers, then
publish only the pinned tree:

```bash
sudo deploy/public-kimi/install.sh \
  --release-source /path/to/reviewed-staging \
  --expected-tree-sha256 APPROVED_LOWERCASE_SHA256 \
  --credential-file /root/private/kimi_api_key
```

Without `--activate`, the installer only publishes a root-owned immutable
release, root-only credentials, documentation, and the new unit. It does not
reload/start systemd, modify Caddy, or touch `agent-monitor.service`.
`--activate` reloads and starts only `proving-kimi-public.service`, then
checks `http://127.0.0.1:4610/healthz`.

## Service and proxy

The unit runs `python -P -m agent_monitor.public_kimi_server` from the
content-addressed release with `DynamicUser=yes`, a private
`RuntimeDirectory=proving-kimi-public` whose anonymous run data is cleared on
service restart, strict filesystem/home/device/process
protections, no capabilities, `NoNewPrivileges=yes`, syscall/resource limits,
and no private console environment file or data path. It needs outbound HTTPS
because the credential gateway is in-process; it therefore does not claim
`PrivateNetwork=yes`.

`Caddyfile.example` defines a separate hostname and exact public route/method
allowlist, preserves the anonymous cookie, strips inbound credential headers,
overwrites the trusted client-IP header, applies a 32 KiB request limit and
strict browser headers, and has no fallback to the private console. Merge it
only after `caddy adapt`/validation succeeds. Never restart or rewrite the
existing private service as part of this deployment.
