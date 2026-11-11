# ansatze.ai deployment bundle

This directory installs the public site **https://ansatze.ai** on this EC2 host
(Ubuntu 26.04, public IP `44.196.29.12`) from the checkout at
`/home/repo/agent_monitor_math`. The owner runs one script with `sudo`. Each
step announces itself and asks before it runs. Re-running the script is safe.

```text
Browser ── HTTPS ──► Caddy :443  (ansatze.ai; www.ansatze.ai → 308 to the apex)
                      ├── /                    docs/.vitepress/dist (static site, 404 page)
                      ├── /sign-in             302 → <mount>/login   (matched before static files)
                      ├── <mount>, <mount>/*   127.0.0.1:4600  agent-monitor.service (user ubuntu)
                      ├── /research/news.json  /var/lib/ansatze-news  (only if the news timer published one)
                      ├── /research/problem-status.json  /var/lib/ansatze-problems  (only if the problems timer published one)
                      └── /internal/*          404, always

agent-monitor.service ── loopback tokens ──► 127.0.0.1:4610  ansatze-kimi.service
                                              (the ONLY holder of the Kimi key; never routed by Caddy)
agent-monitor.service ── Unix sockets ─────► /run/agent-monitor-lean/{core,mathlib}.sock  (Lean broker)
```

`<mount>` is a random `/proof-<40 hex>` path. The installer generates it once
and keeps it in `/etc/ansatze/mount`. Anyone who follows `/sign-in` can see it,
so it is not a secret. Authentication is the security boundary.

| File | Installed as | Purpose |
|---|---|---|
| `Caddyfile` | `/etc/caddy/Caddyfile` | Edge template (mount substituted). Written for Ubuntu's caddy 2.6.2. |
| `agent-monitor.service` | `/etc/systemd/system/agent-monitor.service` | Console on 127.0.0.1:4600. Keep this exact name: the Lean broker drop-in targets it. |
| `ansatze-kimi.service` | `/etc/systemd/system/ansatze-kimi.service` | Sponsored Kimi broker on 127.0.0.1:4610. Runs as user `ansatze-kimi`. |
| `ansatze-news.service`, `.timer` | same names | Optional news refresh every 30 minutes (`--with-news`). |
| `ansatze-problems.service`, `.timer` | same names | Optional problem-status overlay refresh every six hours (`--with-problem-status`). Runs as user `ansatze-problems`. |
| `install.sh` | – | Installer, `--dry-run`, `--render-to`, and `--uninstall`. |
| `smoke.sh` | – | Checks a running deployment. Needs no root. |

## Prerequisites (owner)

1. **AWS security group.** Allow inbound TCP **80 and 443** from `0.0.0.0/0`
   (and `::/0` for IPv6). Keep 22 restricted to your own address ranges (or
   use SSM Session Manager and close it): on 2026-09-27 it was open to the
   internet and `journalctl -u ssh` showed 234 failed logins from 19 addresses
   in a day. Never open 4600 or 4610. Caddy needs port 80 for the ACME HTTP
   challenge and for redirects.
2. **DNS.**
   - Create an `A` record for `ansatze.ai` → `44.196.29.12`.
   - Create an `A` (or `CNAME`) record for `www.ansatze.ai` pointing at the same host.
   - Remove any `AAAA` record that does not point at this host.
   - On 2026-09-27 both names already resolved to `44.196.29.12` from this host.
   - If `www` is missing, the installer leaves out the www redirect (`--www auto`).
3. **No Cloudflare proxy.** If the zone is on Cloudflare, set both records to
   **DNS only** (grey cloud). The console's synchronous DAG and Lean requests
   can run longer than Cloudflare's 100-second origin timeout. If you must
   proxy, raise that timeout above 100 s first (Enterprise plans only).
4. **Staged Kimi key.** `/home/repo/agent_monitor_math/.secrets/kimi.env`
   (mode 0600) contains `KIMI_API_KEY=` and `KIMI_API_BASE=`. The installer
   copies both into root-only systemd credentials. The console never receives
   either value.
5. **Lean.**
   - This host already has the Lean v4.14.0 toolchain
     (`~ubuntu/.elan/toolchains/leanprover--lean4---v4.14.0`) and a built Mathlib
     v4.14.0 project (`~ubuntu/.cache/agent-monitor/mathlib-v4.14.0`).
   - Their reviewed tree pins are built into `install.sh`.
   - Stop anything that could modify those trees while the `lean` step runs.
   - **Soundness limit to know before enabling it.** "Verified" means the file
     compiled with no `sorry` warning, passed the text screens (a file that
     uses `#exit`, `#guard_msgs`, `#print "…"`, custom syntax/elaborators,
     metaprogramming or ambiguous literals is downgraded to `unfaithful`,
     never verified; the Lean tab's Compile/Check refuses a file that runs
     code while Lean elaborates it — `run_cmd`, `#eval`, `initialize`,
     elaborators, the metaprogramming API — before it reaches the sandbox,
     while model-generated drafts are compiled inside the sandbox and then
     downgraded), and passed a `#print axioms`
     probe whose report must appear between two markers carrying a fresh
     random nonce. That probe still runs in the same file as the checked
     source, so a file that hijacks the probe's syntax *and* evades the text
     screens could forge it. A trusted second pass inside the broker (compile
     to `.olean`, then collect axioms from a fixed program that imports it)
     would close that gap; it needs a broker protocol change that has not been
     made.
6. Recommended: require **IMDSv2 with hop limit 1** on the instance. Signed-in
   users can send provider-check requests to URLs of their choosing.

## Install

```bash
cd /home/repo/agent_monitor_math
deploy/ansatze/install.sh --dry-run                    # print every privileged command
deploy/ansatze/install.sh --render-to /tmp/ansatze-rendered   # inspect the exact files
sudo deploy/ansatze/install.sh --acme-email you@example.com --admin-email you@example.com
```

Useful flags:

- `--yes` skips the per-step questions.
- `--steps render,caddy` runs a subset of steps.
- `--skip lean` leaves out one step.
- `--with-news` adds the news timer.
- `--with-problem-status` adds the problem-status timer (creates the system
  user `ansatze-problems`; the Caddy route is in the Caddyfile either way and
  falls back to the overlay bundled with the docs).
- `--guest-run` makes the smoke step run one full guest proof.
- `--remove-staged-key` shreds `.secrets/kimi.env` once it is stored.

`install.sh --help` lists every option.

What each step does:

| Step | Actions |
|---|---|
| `packages` | `apt-get install caddy sqlite3`. Until the `caddy` step runs, the package serves its default page on :80. |
| `credentials` | Creates `/etc/ansatze` (0755), `/etc/ansatze/credentials` (0700 root), `/var/lib/ansatze/console` (0700 ubuntu) and the system user `ansatze-kimi`.<br>Generates `agent_monitor_data_key` (a Fernet key, 0400 root) once and never regenerates it.<br>Copies the staged Kimi key and base into `kimi_api_key`/`kimi_api_base` (0400 root).<br>Writes `/etc/ansatze/console.env` (0640 root:ubuntu, no provider keys; SMTP and Google placeholders) and `/etc/ansatze/kimi-limits.env`.<br>Refuses to continue if `console.env` contains a Kimi key. |
| `render` | Generates the console mount once (`/etc/ansatze/mount`).<br>Renders the Caddyfile and units into `/etc/ansatze/rendered/`.<br>Checks them with `systemd-analyze verify` and, once caddy is installed, `caddy adapt`. |
| `services` | Installs the units and runs `daemon-reload`.<br>Enables and (re)starts `ansatze-kimi`, then waits for `127.0.0.1:4610/healthz`.<br>Then enables and (re)starts `agent-monitor`, and waits for `127.0.0.1:4600/health`.<br>Refuses to start a unit if another process already holds its port.<br>Restarts only units whose files changed. A restart stops any proof in progress. |
| `lean` | Re-measures both trees as `ubuntu` with `deploy/lean-broker/prepare_release.py --measure-only`.<br>Refuses to continue if they differ from the pins (`--accept-measured` overrides this after you review them).<br>Runs `deploy/lean-broker/install.sh … --activate`.<br>Restarts `agent-monitor` so the broker drop-in applies, then checks that both sockets exist. |
| `docs` | Runs `npm ci && npm run docs:build` as `ubuntu`. The build is published atomically into `docs/.vitepress/dist`. |
| `caddy` | Runs `caddy validate` on the rendered file.<br>Backs up the old `/etc/caddy/Caddyfile` in `/etc/ansatze/backup/`, installs the new one, and reloads caddy.<br>If the reload fails, the previous file is restored. |
| `admin` | Runs `agent-monitor create-admin` as `ubuntu`. It prompts for the password and does not open registration.<br>Then restarts the console. The command has no data key, so it stores the account's public defaults (engine, model, runtime; no secrets) unencrypted; the console re-encrypts them on first use, so running `create-admin` by hand later needs no restart.<br>Skipped when an admin already exists (exit status 3). |
| `smoke` | Runs `smoke.sh` (see below). |

Caddy requests the certificate on the first HTTPS request. Follow it with
`journalctl -u caddy -f`. ACME errors almost always mean a DNS or security
group problem.

## Verify

```bash
deploy/ansatze/smoke.sh                # loopback + Caddy (via curl --resolve) + one tiny Kimi call
deploy/ansatze/smoke.sh --www --public # also the www redirect and real public DNS
deploy/ansatze/smoke.sh --guest-run    # one full guest proof (≤12 CGM calls plus derived Formal work, 5-25 min)
curl -s http://127.0.0.1:4600/health; curl -s http://127.0.0.1:4610/healthz
journalctl -u agent-monitor -u ansatze-kimi -u caddy --since today
```

The smoke script checks the following:

- **Health.** Both services answer their health checks.
- **Console settings.** Registration is closed, `AGENT_MONITOR_PUBLIC` is off,
  and guest access is on.
- **Broker link.** The console reaches the broker (`hosted_guest_available`).
- **Guest sessions.** A guest session can be created, and it is then ended.
- **Kimi.** The broker issues a loopback token, and one tiny completion
  succeeds through it.
- **Sign-in redirect.** `/sign-in` answers with a 302 to `<mount>/login`, keeps
  its query string, and following it ends on a 200 login page. That is the
  docs research client's contract.
- **Headers and cookies.** HSTS is present, the `Server` header is removed, and
  the session cookie is `Secure; HttpOnly` behind Caddy.
- **Routing.** `/internal/*` returns 404, unknown pages return 404 with the
  same edge headers (HSTS, `nosniff`, no `Server`), `/research/news.json`
  is served, and `/research/problem-status.json` is served `no-cache` as a
  schema-1 overlay (live or bundled).
- **www.** `www` answers with a 308 to the apex, with HSTS and no `Server`
  header (only with `--www`).

Tokens and cookies are never printed.

## Connect Codex and Claude Code (owner)

1. Open `https://ansatze.ai/sign-in` and sign in with the admin account.
2. Open **Settings**. Set **Account runtime** to **Codex**, press **Connect**,
   open the verification URL on any device and enter the code shown. The
   console runs `codex login --device-auth` with a private home under
   `/var/lib/ansatze/console/codex_home/users/user_<id>`. It never uses
   `~ubuntu/.codex`.
   - The CLI comes from `/home/ubuntu/.npm-global/bin/codex`, which is first on
     the unit's `PATH`. Install the pinned build once, as `ubuntu`:
     `PATH=/home/ubuntu/.local/node24/bin:$PATH npm i -g --prefix ~/.npm-global @openai/codex@0.160.0`.
     Do not rely on `~/.local/bin/codex`: that standalone install can
     self-update past the pin.
   - Response-only Plain and Math Harness routes through a Codex subscription
     require exactly `codex-cli 0.153.4`, `codex-cli 0.157.1` or `codex-cli 0.160.0`
     (the only one with GPT-6.1 Sol and its Fast tier); any other
     build fails closed. Check with `~/.npm-global/bin/codex --version`, and see
     "Codex vetting" in `OPERATIONS.md` before adding a new build.
3. For **Claude Code**, choose **Claude**, press **Connect**, open the OAuth
   URL, then paste the returned code into the Settings field. The account must
   be Claude Pro or Max.
4. Both connections belong to your account only. Guests cannot use them. Saving
   either setting needs the data key, which the installer provides.

## Rotate the Kimi key

The current key was pasted in chat, so treat it as exposed.

1. At the provider, create a new key and set a **spend cap / budget alert** for
   it. The broker's limits are in-memory and reset when it restarts, so the
   provider-side cap is the real cost backstop.
2. Write the key to a new root-only file:

   ```bash
   sudo install -m 0600 /dev/null /root/kimi.env
   sudoedit /root/kimi.env
   ```

   The file needs two lines, `KIMI_API_KEY=...` and
   `KIMI_API_BASE=https://…/v1`.
3. Install the new key and delete the file:

   ```bash
   sudo deploy/ansatze/install.sh --steps credentials --update-kimi-key \
     --kimi-env /root/kimi.env --remove-staged-key --yes
   ```

   This replaces the two credentials, restarts `ansatze-kimi`, and shreds the file.
4. Run `deploy/ansatze/smoke.sh --no-edge`, then **revoke the old key** at the
   provider.
5. Delete the staged copy in the checkout if it still exists:
   `shred -u /home/repo/agent_monitor_math/.secrets/kimi.env`.

The pasted key is also stored in plain text in the `ubuntu` account's Claude
Code files (`~ubuntu/.claude/history.jsonl` and session transcripts under
`~ubuntu/.claude/projects/`), which any process running as `ubuntu` can read.
Rotation (above) is the fix; afterwards those files only hold a revoked key.
`agent-monitor.service` hides `~ubuntu/.claude`, `~ubuntu/.claude.json` and
`~ubuntu/.codex` from the console and its harnesses (re-run the `render` and
`services` steps to install that unit). Never paste a key into a chat
session again: stage it in a root-only file as in step 2.

## Kimi broker limits

Set limits in `/etc/ansatze/kimi-limits.env`, then run
`sudo systemctl restart ansatze-kimi`. A value that is not an integer stops the
broker from starting. Out-of-range values are clamped to the ceiling.

| Variable | Default | Ceiling |
|---|---|---|
| `PUBLIC_KIMI_GATEWAY_CALLS_PER_HOUR` (site-wide model calls) | 900 | 2000 |
| `PUBLIC_KIMI_GATEWAY_CONCURRENCY` | 8 | 16 |
| `PUBLIC_KIMI_GATEWAY_QUEUE_SECONDS` (wait for a busy slot; 0 = refuse at once) | 60 | 120 |
| `PUBLIC_KIMI_GATEWAY_QUEUE_DEPTH` (calls allowed to wait) | 32 | 128 |
| `PUBLIC_KIMI_MAX_OUTPUT_TOKENS` (per call) | 8192 | 16384 |
| `PUBLIC_KIMI_SPONSOR_CALLS_PER_TOKEN` (per run and scope) | 200 | 1000 |
| `PUBLIC_KIMI_SPONSOR_STARTS_PER_HOUR` (per client IP, as forwarded by the console) | 60 | 600 |
| `PUBLIC_KIMI_SPONSOR_TOKENS_PER_CLIENT` (live tokens per client IP, as forwarded by the console) | 64 | 256 |
| `PUBLIC_KIMI_RUNS_PER_HOUR`, `PUBLIC_KIMI_GLOBAL_RUNS_PER_HOUR` | 5, 20 | 120, 1000 |
| `PUBLIC_KIMI_ACTIVE_RUNS`, `PUBLIC_KIMI_GLOBAL_ACTIVE_RUNS` | 1, 2 | 4, 16 |
| `PUBLIC_KIMI_UPSTREAM_TIMEOUT_SECONDS` (total time of one model call, queue wait included; at least 60; see below) | 600 | 900 |

- The console forwards a stable per-account address in `X-Public-Client-IP`
  (a hash of the account id in 198.18.0.0/15), so in practice these two limits
  apply per console account. Background workers send their own hashed address
  the same way (the news worker, client id `ansatze-news`, is 198.18.61.234),
  so each has its own token starts and live tokens; only requests without the
  header (`smoke.sh`) share `127.0.0.1`. The site-wide calls per hour and
  concurrent calls are shared by everyone, guest runs included, which is why
  the news worker also keeps its own persisted daily cap (see Operating).
  Because a visitor can create many guest accounts, these broker limits are
  not a per-visitor control; the console's per-client guest limits (below) are.
- Each guest CGM run keeps up to 2 Kimi calls in flight, so about four
  concurrent runs fill 8 gateway slots. A call that finds them all busy waits
  for one, first come first served, instead of failing at once. The wait is
  taken from the call's own total time (`PUBLIC_KIMI_UPSTREAM_TIMEOUT_SECONDS`).
  Only a full queue or a wait that runs out gives HTTP 429, and that call
  never reached the provider.
- `PUBLIC_KIMI_UPSTREAM_TIMEOUT_SECONDS` is a wall-clock limit from the moment
  a call reaches the gateway. A provider that is still sending bytes is cut
  off too, and the caller gets HTTP 504 (error type `upstream_timeout`); a
  streamed answer (`stream: true`) has already sent its headers, so it just
  ends early. The broker waits 15 seconds longer for the gateway, and a
  client must wait longer still: the sponsored clients wait 630 seconds and
  send `X-Ansatze-Timeout: <seconds>` (their own remaining time), which each
  hop takes 5 seconds off. That header can only shorten these limits, never
  lengthen them, so with those clients a call gets at most about 620 seconds
  at the gateway even if this setting is higher; a higher value only reaches
  callers that send no header (and wait longer themselves).
- Deploy this broker together with those client changes. Older clients send
  no header and give up after 240-300 seconds, before the gateway answers, so
  their slow calls end in their own timeout instead of the gateway's typed
  504. Until the clients are updated, `PUBLIC_KIMI_UPSTREAM_TIMEOUT_SECONDS=225`
  keeps the old timing.
- A caller that hangs up first (its own timeout, or a stopped run) gets its
  call cut off too: the broker and then the gateway notice within about a
  second each, so no call keeps a gateway slot, or the provider working,
  after its caller left.
- Errors from `/internal/v1/*` have the body `{"error": {"message", "type",
  "forwarded", "upstream_status", "upstream_elapsed_ms", "retry_after"}}`.
  `forwarded: false` means the call never reached Kimi. Kimi's own error text
  is never passed on.
- `journalctl -u ansatze-kimi` has one line per model call from the gateway
  (`public-kimi gateway call`: status, error type, Kimi's status and response
  time, queue wait, total time `ms`, the call's budget `budget_s`, size, and
  whether the caller was still connected) and one from the broker (with its
  wait for the gateway, `wait_s`). `status=- type=client_gone` is a call
  whose caller had already left. These lines never contain prompts, outputs
  or tokens. `curl -s 127.0.0.1:4610/healthz` also reports `upstream`: calls
  and failures in the last 10 minutes, seconds since the last success, and
  `degraded`. It never calls Kimi, and it leaves out calls whose caller left
  or cut them short with its own shorter budget. Only local callers see
  `upstream`, never a request through the public edge. While `degraded` is
  true, the console shows a notice under the composer when a Kimi model is
  chosen on an engine the broker serves (not to accounts with their own Kimi
  key), and an open console checks again every minute.
- A guest proof (Ansatz CGM · Kimi K3) makes at most 12 informal model calls
  (the guest CGM budget) plus typically 3-4, at most about 10, Formal/Lean
  calls, so 900 calls per hour is roughly 40 complete runs across the site in
  the worst case. While Kimi refuses calls at once (upstream 429/503), those
  refusals count too, and a run may send up to nine refused calls again
  (Formal/Lean calls up to two each; see `OPERATIONS.md`), so fewer runs fit.
- Guest run limits are separate: see the `AGENT_MONITOR_GUEST_*` settings in
  `.env.example`. You can put those settings in `/etc/ansatze/console.env`,
  then restart the console.
- The console also limits guests per client address (default: six new guest
  sessions, 12 guest runs and 48 Formal/DAG actions per hour, and one active
  guest run). It takes the address from the `X-Forwarded-For` value Caddy
  sets (Caddy 2.6 replaces whatever a client sends), groups IPv6 by /64, and
  exempts requests from this host. Visitors behind one NAT share a budget; a
  client with many addresses can still reach the site-wide caps, which remain
  the cost bound. The stock Ubuntu Caddy has no rate-limit module, so there is
  no edge rate limit.

## Registration and what members can do

Self-registration is controlled by `AGENT_MONITOR_REGISTRATION_OPEN`. The
template leaves it unset (closed). ansatze.ai opens it with a drop-in:

```bash
sudo install -d /etc/systemd/system/agent-monitor.service.d
printf '[Service]\nEnvironment=AGENT_MONITOR_REGISTRATION_OPEN=1\n' |
  sudo tee /etc/systemd/system/agent-monitor.service.d/90-registration.conf
sudo systemctl daemon-reload && sudo systemctl restart agent-monitor.service
```

To close it again, remove `90-registration.conf`, reload and restart; then
`deploy/ansatze/smoke.sh --no-kimi` confirms "self-registration is closed".
Restarting stops proofs in progress, so check for active runs first.

A self-registered account that is not the administrator (a "member") is
untrusted. `agent_monitor/account_policy.py` and the run path enforce the
rules below for every member except one the administrator gave **full
access** (Admin → Accounts, or `python -m agent_monitor.accounts_admin
full-access on <emails>`). Such an account links its own Claude Code and
Codex and runs every engine but Hermes, including the harnesses that run
commands on this host without a sandbox, so give it only to people you
trust. It keeps the member rate limits and never uses the administrator's
accounts or keys
(OPERATIONS.md, "Accounts an administrator manages"):

- **Tool-free engines only.** Members may run only the engines in
  `AGENT_MONITOR_MEMBER_ENGINES` (default `kimi,plain,math_harness`), and the
  code caps that list at those three tool-free engines, so a typo cannot widen
  it. A command override (`KIMI_PROOF_CMD`, `PLAIN_CMD`, `MATH_HARNESS_CMD`)
  makes that engine operator-chosen local code, so it also removes the engine
  from the list. Other harnesses are refused with "That engine runs local
  tools and is limited to the site administrator", and an engine the operator
  left out with "The site administrator has not enabled that engine for member
  accounts". Codex, OpenClaw, OpenHands and DeepAgents run as `ubuntu` with no
  OS sandbox and stay administrator-only.
- **Ansatz CGM on the member's own keys.** `math_harness` is the same
  response-only CGM pipeline as `kimi`. A member runs it only on the API route
  and only with a model from its own provider keys, for example a Claude model
  with its own Anthropic key. A `kimi-*` model is refused (Kimi K3 stays on the
  sponsored `kimi` engine), and so are Max and Max (API) (`max-api`, which is
  for the administrator and full-access accounts, the latter on their own
  Anthropic and OpenAI keys). The run and its automatic Formal,
  DAG and Lean stages call the provider with that key and the run's model;
  they never use the server's keys, the administrator's subscriptions or the
  sponsored Kimi broker. A run started without a model (the Projects page's
  "Default account model") uses the member's saved default model when that is
  one from its own keys. `AGENT_MONITOR_PUBLIC=1` would give members the
  server's keys, so `math_harness` is not offered to members in public mode.
  To narrow members back to `kimi` and `plain`, add a drop-in and restart
  (check for active runs first). Members then lose Ansatz CGM on their own
  keys: the composer offers only Kimi K3 there, and the Settings notes and the
  refused login requests point them at Plain instead.

  ```bash
  printf '[Service]\nEnvironment=AGENT_MONITOR_MEMBER_ENGINES=kimi,plain\n' |
    sudo tee /etc/systemd/system/agent-monitor.service.d/91-member-engines.conf
  sudo systemctl daemon-reload && sudo systemctl restart agent-monitor.service
  ```
- **No subscriptions or subagents.** Signing in with a Claude or ChatGPT
  (Codex) subscription is administrator-only. Anthropic does not let
  third-party sites sign in with or route requests through members' Claude
  plans, and OpenAI requires partner approval for hosted sites to use members'
  ChatGPT plans. A member's Settings shows that explanation on both cards, with
  no Connect button and a shortcut to the API-provider key that works instead,
  and the login request is refused (HTTP 403) with the same text. Settings
  never probes a member's Claude or Codex login, a member's runtime is always
  the API route, and member runs use it without subagents. A member cannot
  continue a run billed to a subscription or to Max (for example one it
  started before this policy), nor a Max (API) run once its full access has
  ended; the refusal says so. In a shared project,
  members can open each other's runs, but only the member who started a run
  can continue, stop or change it.
- **Projects with members run tool-free.** A run started from a project gets
  a copy of its shared document, which every member can edit. Once a member
  who is not the administrator has joined (or created, or edited) a project,
  its runs (the administrator's too), their follow-ups and their Formal
  harness use only `kimi`, `plain` or `math_harness` on an API key (never
  Max or Max (API)), without subagents; anything else is refused with HTTP
  403. Coding agents and the
  subscription CLIs run as `ubuntu`, with the administrator's sign-ins in
  their home, so text a member wrote must not steer them. The text stays in
  the document after the member is removed, so the project keeps the rule
  (the `untrusted` table in `projects.sqlite3`, never cleared); to use a
  coding agent again, copy text you have checked into a new project. When
  that table is first created in an existing database, it lists the projects
  whose members, or the accounts that started their runs, include a member;
  a member removed earlier who started no run there left no record, so check
  the documents of projects you removed members from. This is a security
  policy you can relax in code (`account_policy.project_run_error` says what
  such a project may run, `projects._untrusted` which projects it covers,
  decided at launch by `projects.start_run`).
- **Sign-ins stored before this policy.** A member who linked Claude or Codex
  before members were limited keeps that sign-in on the server until it is
  removed. It is never used for the member's runs. Settings shows a **Remove
  stored sign-in** button on that card, which calls the existing logout. To
  list leftovers on the host (`user_<id>` is the account id; the administrator
  is usually `user_1`):

  ```bash
  sudo find /var/lib/ansatze/console/codex_home/users -name auth.json
  sudo find /var/lib/ansatze/console/claude_home/users -name .credentials.json
  ```
- **No custom endpoints.** `settings.restrict_account_base_urls` drops any
  provider base URL a member saved, for proof runs as well as the console's DAG
  and Lean calls, and only the administrator can test keys against a custom URL.
- **Operator-only shared context.** Writes to the shared agent profile, skills,
  memory and library return HTTP 403 for other accounts.
- **Rate limits.** Two active runs and twelve run starts per hour per member
  (`AGENT_MONITOR_MEMBER_CONCURRENCY`, `AGENT_MONITOR_MEMBER_RUNS_PER_HOUR`);
  continuations count. A Continue on a run that is already running, a start
  or continuation that is refused, and feedback that joins feedback the same
  member already queued for a running run do not use a start. Feedback a
  member queues for a running run continues as that member, on its own keys,
  never as the run's owner. Every feedback message counts towards
  `AGENT_MONITOR_MEMBER_MESSAGES_PER_HOUR` (default 60). A message holds at
  most 20,000 characters, a run at most 100,000 characters of human feedback,
  and the queue for a running run at most 200,000 bytes. Sign-ups are limited
  per network and site-wide.

Do not use `AGENT_MONITOR_PUBLIC=1`. It gives every account the server's keys
and breaks the `/sign-in` contract.

## Email, password reset, Google sign-in and .edu-only sign-up

All four are optional and set in `/etc/ansatze/console.env` (root-managed;
restart `agent-monitor.service` after editing, once no proofs are running).

- **Outgoing email** (`SMTP_SERVER`, `SMTP_PORT`, `SMTP_USERNAME`,
  `SMTP_PASSWORD`, `SMTP_FROM_EMAIL`) sends password-reset and confirmation
  links over authenticated STARTTLS. Without it the sign-in page says that
  reset by email is unavailable, and the operator sets a password from the
  host instead (it prompts for the new password and signs the account out):

  ```bash
  cd /home/repo/agent_monitor_math
  sudo runuser -u ubuntu -- env AGENT_MONITOR_DATA_DIR=/var/lib/ansatze/console \
    AGENT_MONITOR_ENV_PATH=/etc/ansatze/console.env PYTHONPATH="$PWD" \
    ./.venv/bin/python -m agent_monitor.cli set-password --email someone@ucla.edu
  ```

  Publish the provider's SPF and DKIM records for the sending domain so the
  links are not filed as spam.
- **Sign in with Google** needs `GOOGLE_OAUTH_CLIENT_ID`: a "Web application"
  OAuth client in Google Cloud with `https://ansatze.ai` as an authorized
  JavaScript origin (no redirect URI). A Google account whose verified address
  matches an existing account signs in to that account.
- **`AGENT_MONITOR_REGISTRATION_EMAIL_SUFFIXES=.edu`** limits new accounts,
  by password or Google, to matching addresses. Accounts created earlier keep
  working.
- **`AGENT_MONITOR_REQUIRE_EMAIL_VERIFICATION=1`** makes a new account confirm
  its address with an emailed link (valid 24 hours) before it can sign in. It
  needs outgoing email: without SMTP, sign-up answers 503. Google sign-ups and
  completed password resets count as confirmed; administrators and accounts
  that predate the setting are exempt.

## Operating

- **Code update.**
  1. Review the change, update the checkout, and run the tests.
  2. Restart the services: `sudo systemctl restart ansatze-kimi agent-monitor`.
  3. Rebuild the docs with `npm run docs:build` as `ubuntu`. This publishes
     atomically and needs no restart.
  - `console.html` and the monitor page are read from the checkout on every
    page load, so a change to them reaches browsers on their next reload
    without a restart; Python changes need the restart.
- **News feed (`--with-news`).** The timer runs the checkout's
  `scripts/update_math_news.py` every half hour, so news code and its data
  files go live at the next tick without a restart: apply them right after a
  tick. The worker writes `news.json` (0644, served at `/research/news.json`),
  `status_proposals.json` (0644, unreviewed problem-link hints for the
  problems updater; not served) and `state.json` (0600) in
  `/var/lib/ansatze-news`. Its Kimi K3 calls (client id `ansatze-news`) are
  capped at 4 per run and `ANSATZE_NEWS_MODEL_CALLS_PER_DAY` (default 60) per
  UTC day, counted in `state.json`. Change the cap with
  `sudo systemctl edit ansatze-news.service` (`[Service]`,
  `Environment=ANSATZE_NEWS_MODEL_CALLS_PER_DAY=…`; 0 turns the model off and
  cards show headlines and quoted source excerpts only). A run records its
  possible calls in `state.json` before making any, so a failed publication
  still counts them; `--dry-run` never calls the model. The journal line names
  failing sources (`source_error_ids`) and openai/math history sections held
  back because no commit could be shown to add them (`held_changelog`).
  Copy-first checks and the one-time backfill are in `OPERATIONS.md` (News
  refresh).
- **Analyze dependencies and settings.** Analyze needs `pylatexenc==2.11`
  and `pypdf==6.21.4` in `/home/repo/agent_monitor_math/.venv` (one venv for
  `agent-monitor`, `ansatze-kimi` and `ansatze-news`): dry-run first, then
  `sudo -u ubuntu .venv/bin/python -m pip install --no-deps pylatexenc==2.11 pypdf==6.21.4`.
  Its caps, kill switches (`AGENT_MONITOR_ANALYZE_ENABLED`,
  `AGENT_MONITOR_ANALYZE_GUESTS`; both on by default), the sponsored daily cap
  and concurrency (`AGENT_MONITOR_ANALYZE_SPONSORED_RUNS_PER_DAY`, default 40,
  0 turns sponsored Analyze off; `AGENT_MONITOR_ANALYZE_SPONSORED_CONCURRENCY`,
  default 2), the guests' share of it
  (`AGENT_MONITOR_ANALYZE_GUEST_SPONSORED_RUNS_PER_DAY`, default half the daily
  cap, 0 keeps guests off sponsored Analyze;
  `AGENT_MONITOR_ANALYZE_GUEST_SPONSORED_CONCURRENCY`, default half the slots,
  at least one) and the optional `AGENT_MONITOR_ANALYZE_GITHUB_TOKEN` (a
  fine-grained token limited to public repositories with no permissions) go in
  `/etc/ansatze/console.env`; see OPERATIONS.md "Analyze runs". The daily
  count lives in `/var/lib/ansatze/console/analyze_sponsored_usage.json`
  (`runs`, and `guest_runs` for the guests' share).
  `AGENT_MONITOR_GUEST_ENGINES` and `AGENT_MONITOR_MEMBER_ENGINES` name proof
  engines only, so an existing narrowing drop-in does not hide Analyze; the
  Analyze switches do. Sponsored analyses use the broker's `math_harness`
  scope, so `ansatze-kimi` needs no change. After installing the packages, check them with
  `.venv/bin/python -c "import pylatexenc, pypdf"`: a run without them stops
  with a configuration error. The runner fetches user-supplied links itself
  and refuses non-public addresses in code; adding
  `IPAddressDeny=169.254.169.254` to `agent-monitor.service` is cheap defence
  in depth.
- **Caddy admin API.** The Caddyfile moves Caddy's admin API from the
  default unauthenticated `localhost:2019` (reachable by every local process,
  including console harnesses, which could have rewritten the public edge
  with one `POST /load`) to the unix socket `/var/lib/caddy/admin.sock` inside
  Caddy's 0750 home. `systemctl reload caddy` keeps working. The first install
  of this Caddyfile restarts Caddy once instead of reloading it (the `caddy`
  step does this). Check with `curl -sS -m 3 http://localhost:2019/config/`
  (connection refused is correct).
- **Logs.** Both units set `PYTHONUNBUFFERED=1` (and the console also
  line-buffers its own output), so access lines reach the journal as they
  happen instead of in 8 KiB bursts that were lost on every stop.
- **Rotate the console mount.**
  1. Run `sudo rm /etc/ansatze/mount`.
  2. Run `sudo deploy/ansatze/install.sh --steps render,services,caddy`.
  3. Existing sessions end, because the cookie path changes. The docs
     site finds the new mount through `/sign-in`, so it needs no rebuild.
- **Backups.** Back up `/var/lib/ansatze/console`, using
  `sqlite3 users.db ".backup …"` for the database, together with
  `/etc/ansatze/credentials/agent_monitor_data_key`. Saved keys and
  subscription settings cannot be decrypted without that key.
- **Old data is not migrated.** Accounts, runs and settings in the checkout's
  `data/` directory are not copied to the new data directory.
- **The broker runs code from the checkout.** Anyone who can write to
  `/home/repo/agent_monitor_math` controls the code the key holder runs.
  - The unit shows the broker only the checkout, read-only, and its
    interpreter. It hides the rest of `/home`, `.env`, `.secrets`, `data/` and
    the console's data.
  - For stronger isolation, move the broker to the immutable-release flow in
    `deploy/public-kimi/`.
- **Passwordless sudo.** The `ubuntu` account has passwordless `sudo`.
  `NoNewPrivileges=yes` stops `sudo` for processes *inside* the console unit,
  and the unit hides `/run/user`, so a harness cannot ask the `ubuntu` user
  manager (`systemd-run --user`) to start a process outside the unit. It does
  **not** stop persistence through files the harness can write as `ubuntu`
  (for example `~/.bashrc` or `~/.config/systemd/user`), which run with full
  `sudo` the next time the owner logs in. Remove the `NOPASSWD` rule before
  exposing tool-capable harnesses to anyone but the owner.
- **Guest artifact retention.** Guest identities expire after 24 hours, but
  their run records and workspaces stay in `/var/lib/ansatze/console/runs`
  until removed. The unit sets `AGENT_MONITOR_GUEST_RUN_RETENTION_DAYS=30`, so
  the console itself deletes finished guest runs 30 days after they end
  (checked every six hours; usage history is kept). Guests may attach at most
  two text files, 64 KB in total, per run. To list, then delete, finished
  guest runs by hand (for example with a shorter period):

  ```bash
  cd /home/repo/agent_monitor_math
  sudo -u ubuntu env AGENT_MONITOR_DATA_DIR=/var/lib/ansatze/console \
    AGENT_MONITOR_ENV_PATH=/etc/ansatze/console.env \
    .venv/bin/python -m agent_monitor.cli purge-guest-runs --older-than-days 30
  sudo -u ubuntu env AGENT_MONITOR_DATA_DIR=/var/lib/ansatze/console \
    AGENT_MONITOR_ENV_PATH=/etc/ansatze/console.env \
    .venv/bin/python -m agent_monitor.cli purge-guest-runs --older-than-days 30 --apply
  ```

  The first command only lists the runs. Run the second while the console is
  idle or stopped: a running console keeps its own index of runs in memory
  until the next restart (the automatic purge runs inside the console, so it
  does not have this problem).
- **Resource ceilings.** `agent-monitor.service` sets `MemoryHigh=10G`,
  `MemoryMax=12G` and `TasksMax=4096` for the console, its request threads
  and the local harnesses it starts. Guest request bodies are capped at 1 MB
  (29 MiB for members, for attachments), a guest `/chat` request is refused
  before its body is read, and every connection has a 300 s socket timeout.
- **Cross-origin writes.** The console refuses every POST and DELETE whose
  `Origin` does not match its `Host`, or whose `Sec-Fetch-Site` is
  `cross-site` or `same-site` (403), and the HTML-form content types (415),
  before reading the body. That covers the unauthenticated sign-in, guest and
  sign-out routes, where `SameSite=Lax` gives no protection. Opening `/guest`
  while signed in asks before switching the account to a guest session.
- **Third-party scripts.** The console and monitor load marked, KaTeX and
  pdf.js from jsDelivr/cdnjs with Subresource Integrity, and the pdf.js
  worker is fetched with an integrity check; a changed CDN file is refused.
  The console's CSP still has no `script-src` (the pages use inline
  scripts), and the Google Sign-In script (only when Google sign-in is
  configured) and Google Fonts CSS cannot be pinned.

### Problem-status overlay (optional)

`ansatze-problems.timer` runs `scripts/update_problem_status.py` at 00:15,
06:15, 12:15 and 18:15 UTC (plus up to five minutes; missed runs catch up). The
unit uses the news worker's sandbox, `MemoryMax=768M` (parsing a new Kourovka
PDF), and no key: Kimi K3 calls go through the loopback broker with client id
`ansatze-problems`, which the sponsored client hashes to its own address
(198.18.0.245), so the worker has its own token and start limits; the gateway's
900 calls per hour and 8 concurrent calls are shared with everything else.
`ANSATZE_PROBLEMS_MODEL_CALLS_PER_DAY` (default 48, `0` disables model calls;
set with `systemctl edit ansatze-problems.service`) is counted in the worker's
`state.json`, which survives broker restarts; a run makes at most 8 calls and
stops calling when the broker is busy or over quota.

- Output: `/var/lib/ansatze-problems/problem-status.json` (0644, served by
  Caddy with `Cache-Control: no-cache`); private state `state.json` (0600). The
  worker reads the public `/var/lib/ansatze-news/news.json` for problem links
  when the news timer is installed.
- Check: `systemctl list-timers ansatze-problems.timer`,
  `journalctl -u ansatze-problems.service` (one JSON line per run) and
  `curl -s https://ansatze.ai/research/problem-status.json | head`.
- Pause with `sudo systemctl stop ansatze-problems.timer`; the last overlay
  stays published. Remove the published file to fall back to the bundled one.
- Categories in the overlay change only on deterministic source signals; model
  matches are "New reports (unreviewed)". Review them with
  `scripts/review_problem_status.py` (see `OPERATIONS.md`).

## Troubleshooting

| Symptom | Check |
|---|---|
| `ansatze-kimi` fails with `status=226/NAMESPACE` | systemd could not build its sandbox. Check that `/home/ubuntu/.local/share/uv/python/cpython-3.13-linux-x86_64-gnu` exists (`uv python install 3.13`) and that `/home/repo/agent_monitor_math/.venv` still points at it. |
| `ansatze-kimi` exits with "A valid Kimi LoadCredential is required" | `/etc/ansatze/credentials/kimi_api_key` is missing or empty. Re-run the `credentials` step with `--update-kimi-key`. |
| `agent-monitor` exits with "refusing to start: account database/data key check failed" | The console opens `users.db` at startup. The data key credential is missing, not a valid Fernet key, or not the key that encrypted the saved settings. Restore `/etc/ansatze/credentials/agent_monitor_data_key` from backup; a newly generated key cannot read existing saved settings. |
| The smoke check says "hosted guest Kimi available" failed | The console cannot reach `127.0.0.1:4610`. Run `systemctl status ansatze-kimi` and `journalctl -u ansatze-kimi`. |
| Kimi runs fail with HTTP 503, 429 or 504 | Check `upstream` in `curl -s 127.0.0.1:4610/healthz` and the `public-kimi gateway call` lines in `journalctl -u ansatze-kimi`. `type=upstream_rejected upstream_status=503` with a small `upstream_ms` means Kimi refused at once (overloaded). `type=upstream_timeout` means Kimi took longer than the call's budget, `budget_s` on the same line: `PUBLIC_KIMI_UPSTREAM_TIMEOUT_SECONDS`, or the shorter `X-Ansatze-Timeout` its caller sent (raising the setting does not help those). `type=gateway_busy` or `gateway_quota` means this site's own limits. |
| Every Kimi run fails with `upstream_status=401`, `403`, `402` or `404` | The sponsor key is revoked or unfunded (401/402/403), or `kimi_api_base` points at the wrong place (404, or a 3xx redirect). `upstream` in `/healthz` counts these as failures. Check the account, then re-run the `credentials` step with `--update-kimi-key`. |
| The edge checks fail with "Caddy is not serving" | No certificate yet. Check DNS, the security group (80/443) and the ACME errors in `journalctl -u caddy`. |
| Formal checks say the Lean broker is unavailable | Run the `lean` step. Then check `systemctl status agent-monitor-lean-core.socket agent-monitor-lean-mathlib.socket`. |
| Settings shows "no user-environment encryption key" | The unit's `LoadCredential=agent_monitor_data_key` line is missing. Re-run the `render,services` steps. |

## Uninstall and rollback

```bash
sudo deploy/ansatze/install.sh --uninstall          # stop and remove units, restore the pre-install Caddyfile
sudo deploy/ansatze/install.sh --uninstall --purge  # ALSO delete /var/lib/ansatze*, /etc/ansatze, service users
```

- `--uninstall` keeps data, credentials and the Lean broker.
- `--purge` destroys every account, run and the data key. It asks you to type
  `PURGE`.

To roll back parts of the deployment:

- **Caddy only.** Every replaced Caddyfile is kept in `/etc/ansatze/backup/`:

  ```bash
  sudo install -m 0644 /etc/ansatze/backup/Caddyfile.<timestamp> /etc/caddy/Caddyfile
  sudo systemctl reload caddy
  ```

- **Lean broker.**

  ```bash
  sudo systemctl disable --now agent-monitor-lean-core.socket agent-monitor-lean-mathlib.socket
  sudo rm /etc/systemd/system/agent-monitor.service.d/lean-broker.conf
  sudo systemctl daemon-reload && sudo systemctl restart agent-monitor
  ```

  Formal checks then report the broker as unavailable. There is no local
  fallback.
- **Code.** Check out the previous reviewed revision, then run
  `sudo systemctl restart ansatze-kimi agent-monitor`.
