# Hosted deployment map

This file documents how to discover the hosted Agent Monitor deployment. The
application repository alone is not authoritative for hostnames, TLS, process
paths, or secret mounts.

## Public entry points

- Homepage and documentation: <https://54-82-94-57.sslip.io/>
- Stable console entry: <https://54-82-94-57.sslip.io/sign-in>
- Intended canonical hostname: <https://moonshot.hailab.io/>

The EC2 public address changed from `54.235.45.210` to `54.82.94.57` on
September 10, 2026. The previous IP-based links are obsolete. Caddy now serves
the new hostname with valid HTTPS, and public sign-in links are relative.
Before sharing an IP-based URL after a host restart, confirm the instance's
current public address and test HTTPS with ordinary public DNS. A successful
loopback request using `--resolve` does not establish public reachability.

The canonical hostname currently passes through Cloudflare, whose routing is
configured outside this repository. Until that route is cut over to this
origin, use the `sslip.io` address to test the version running on this host.

The documentation site is mounted at `/`, so public pages use clean URLs such
as `/overview` rather than `/docs/overview`. During the initial migration,
Caddy continues serving `/docs` and `/docs/*` as non-indexable compatibility
aliases. Convert them to permanent root redirects only after old cached `/` to
`/docs/` responses have aged out, avoiding a redirect loop for prior visitors.
The historical `/docs/sign-in` variants remain direct, non-cacheable sign-in
redirects rather than static aliases.

The `/sign-in` route is deliberately stable. Caddy redirects it to the current
mounted console, which means the randomized mount can be rotated without
rebuilding the documentation site. The redirect destination is observable to
any visitor; authentication, not knowledge of that path, is the security
boundary.

The page can also deliberately create a restricted guest session. Guest access
is enabled by default in application code and can be disabled with
`AGENT_MONITOR_GUEST_ACCESS=0`. It is not equivalent to
`AGENT_MONITOR_PUBLIC=1`, which automatically creates guest sessions instead of
showing the sign-in/guest choice. Before enabling either mode on a hosted
instance, verify the guest engine allowlist, concurrency/session-creation
and rolling run/verification limits, output-token cap, and provider-key policy
from `.env.example`. Guests default to the tool-free **Kimi K3** harness with
three stages: draft, critique, and refine. Hosted runs can use the existing
sponsored Kimi broker; a configured dedicated Kimi credential or the visitor's
own Kimi key provides the direct route. The one-call `plain` baseline is also
eligible when enabled. Guests cannot set custom provider origins or inherit
unrelated server credentials. The allowance is **10 cumulative run starts** per
guest session, after which sign-in is required; deleting runs does not restore
starts. Claude Code and Codex require sign-in from the outset. A `PLAIN_CMD` or
`KIMI_PROOF_CMD` override disables the corresponding engine for guests because
arbitrary local commands are outside the built-in contract.
Guest Formal/Lean and informal/formal DAG views are enabled by the reviewed
update in `artifacts/guest-formal-dag-2026-09-15/`. Automatic derived work waits
for the final guest proof and uses only short-lived Kimi proxy credentials;
Lean checking always uses the isolated broker. Existing completed guest runs
start their missing pipeline once when opened. Direct Verify, Compile, Check,
Audit, and feedback actions are bounded separately from proof-run starts.
Coding-agent Formal harnesses still require sign-in.
Self-registration is closed unless `AGENT_MONITOR_REGISTRATION_OPEN=1` is
explicitly set, including during first-account bootstrap. The public Ansatz
deployment enables this setting at the owner's request through
`/etc/systemd/system/agent-monitor.service.d/95-public-signup.conf`; its account
page opens in Sign up mode. Enrollment changes require a daemon reload and
service restart after confirming no proof processes are active. Deployments
intended for restricted enrollment should close registration after their
enrollment window. Ordinary local harnesses do not isolate hostile prompts
from the host, so registration policy is an explicit access decision rather
than a workaround for guest limits.

## Research atlas and shared memory

The Open Problems catalogue at `/open-problems` contains 1,756 selected entries
across Erdős, Kourovka, Millennium, Ramanujan’s legacy, Hilbert, and Smale.
Eighteen highlights have original editorial statements and sourced research
progress; proof claims retain their reported status. Other entries link to the
original statements. The public page fetches `/research/open-problems.json`.
The maintained selection is `agent_monitor/data/open_problem_curation.json`;
`scripts/curate_open_problems.py` and `scripts/import_open_problems.py` rebuild
it. Review and publication evidence are in
`artifacts/open-problems-curation-2026-09-16/`. “Click to solve” preserves an
existing account or opens a guest workspace, loads source context, and waits
for the visitor to choose Prove. This update did not restart the console.

## News refresh

The News page at `/news` replaces Overview in public navigation. The combined
platform tour and getting-started guide are at `/quickstart`; `/overview` is
kept as a compatibility redirect.

`ansatze-news.timer` checks selected RSS/Atom sources on the hour and half-hour.
The `ansatze-news.service` worker runs as a dedicated `ansatze-news` system user
from an immutable release under `/opt/ansatze-news/releases/`. It stores its
feed and source-cache state under `/var/lib/ansatze-news/`. Caddy exposes only
`/var/lib/ansatze-news/news.json` at `/research/news.json`, independently of the
static website release. Rebuilding VitePress cannot replace the live feed with
an older snapshot. The page polls this read-only endpoint every three minutes.

The worker validates configured HTTPS sources, dates, redirects, article
links, duplicates, size limits, and relevance rules. Source failures preserve
last-good entries, and the page reports source-check and publication dates
separately. Source records and publication dates are not invented by a model.
Kimi K3 may summarize up to eight new records per refresh through the existing
loopback broker; no provider key enters the browser or updater configuration.
Source titles, URLs, dates, categories, and proof-claim labels stay immutable.
No model tools are enabled. Processed records are cached; pending or failed
summaries are retried with bounded backoff. Editorial seed records remain
unchanged by the model. Automatic arXiv entries show the source headline while
a readable summary is pending; their feed dates can represent revised-paper
announcements. Proof announcements do not become certified solutions. Eight
feeds are monitored; Anthropic is currently a curated source because a working
public feed was not available.

Maintained inputs: `agent_monitor/data/news_sources.json` and
`agent_monitor/data/news_seed.json`. Code: `agent_monitor/news.py`,
`agent_monitor/news_kimi.py`, and `scripts/update_math_news.py`. Review and
installation evidence: `artifacts/news-2026-09-16/`.

Check scheduler health with `systemctl status ansatze-news.timer` and
`systemctl status ansatze-news.service`; inspect worker results with
`journalctl -u ansatze-news.service`. Run a manual check with
`systemctl start ansatze-news.service`. To pause automatic collection, stop
`ansatze-news.timer`; the last published feed remains available. Public refresh
metadata shows whether source checks are current. Never put provider keys in
feed records, static files, command arguments, or service-unit environment.

## Research atlas

The default atlas at `/dag` displays an informal–formal bipartite graph imported
from the supplied `graph.json` and identical KG ZIP archives. It includes 24,950
informal entries, 16,682 real Lean declarations and 16,761 resolved connections.
Unresolved references are optional and visibly separate.
The **Show dependencies** toggle loads 54,128 informal references and 72 proof
links from `/research/bipartite-dependencies.json`, preserving the supplied
direction and labels. Formal-to-formal dependencies are absent from this input.
Dependency review evidence is in `artifacts/atlas-dependencies-2026-09-15/`.
Reproduce the public
index and lazy source details with `scripts/import_bipartite_atlas.py`; use
`--check` to verify all generated artifacts. Review evidence and the previous
static release are in `artifacts/bipartite-atlas-2026-09-15/`. Publish static
updates with the existing atomic `npm run docs:build`; no service restart is
needed. The source graph remains available at `/dag?view=source`.

The original informal research atlas imports eligible active tagged mathematical
statements from the pinned Stacks Project source without a node-count cap.
Disconnected statements remain visible; only actual source citations become
arrows. Coverage counts, deliberate exclusions, the pinned source, import method,
and licenses are documented at `/dag-sources`; reproduce the data with
`scripts/import_informal_atlas.py`. Caddy compresses `/research/*.json` on the
active public host. The catalogue is source literature, separate from model
memories. It is never labeled as model-generated work.

The expanded catalogue and independently checked source provenance are recorded
in `artifacts/atlas-expansion-2026-09-10/`. Updating its static backend index uses
the hash-bound `scripts/promote_atlas_index.py` without restarting the console;
the running backend does not load that catalogue into model memory.

Shared model memory currently requires explicit publication by the completed
run's owner. The final assistant output must have recorded response-model
identity from the eligible GPT-6, GPT-5.6 Sol, or Fable model list. Session
summaries and selected model names do not establish authorship. API Plain,
API Math Harness, and Claude response streams preserve this identity. Native
Codex CLI output currently omits it, so those results cannot be published as
shared memory. Existing runs without response provenance are also ineligible.
Registered harness runs retrieve relevant published memories automatically;
guest and sponsored public runs do not. Publication remains an owner action.

The hosted response-only Codex adapter is vetted against `codex-cli 0.153.4`.
Re-vet upgrades using the loopback response-only contract before changing its
exact version guard. Tests and narrow hosted-source patches for this release
are retained in `artifacts/dag10k-update-2026-09-10/`. The older workspace
backend is not a drop-in replacement for the hosted backend.

## Find the live wiring

On the host, inspect the effective configuration instead of guessing from a
checkout:

```bash
sudo systemctl cat agent-monitor.service
sudo systemctl show agent-monitor.service \
  -p FragmentPath -p DropInPaths -p WorkingDirectory -p ExecStart
sudo sed -n '1,240p' /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
```

The current deployment is transitional: the service imports application code
from `/home/ubuntu/agent_monitor_math`, while its virtual environment and data
directory are under `/home/repo/agent_monitor_math`. Caddy serves the generated
documentation from `/home/repo/agent_monitor_math/docs/.vitepress/dist`. This
split explains why editing an arbitrary checkout may not update the running
server. The systemd unit and Caddyfile are the source of truth until deployment
is consolidated into an immutable release.

## Publish documentation

Install dependencies once with `npm ci`, then publish with:

```bash
npm run docs:build
```

The build is rendered into a staging directory and atomically exchanged with
the directory Caddy serves, so readers do not see a half-built site.

After changing routing, validate before reloading Caddy and smoke-test the
public surface:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl reload caddy
curl -I https://54-82-94-57.sslip.io/
curl -I https://54-82-94-57.sslip.io/overview
curl -I https://54-82-94-57.sslip.io/sign-in
```

Do not put API keys, password-reset tokens, the console mount, or Cloudflare
credentials in this file. Production secrets are loaded through encrypted
systemd credentials; local development values belong only in ignored local
configuration.
