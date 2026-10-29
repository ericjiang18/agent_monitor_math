# Hosted deployment map

This file documents how the hosted Agent Monitor deployment at
<https://ansatze.ai> is wired. The installable source of truth is
[`deploy/ansatze/`](deploy/ansatze/README.md): its `install.sh` renders the
Caddyfile and systemd units, and the files it installed under `/etc` are what
actually runs. Hostnames, TLS and the console mount are never taken from other
files in this repository.

## Public entry points

- Homepage and documentation: <https://ansatze.ai/>
- Stable console entry: <https://ansatze.ai/sign-in>
- `https://www.ansatze.ai/*` answers with a permanent (308) redirect to the apex
  when its DNS record points at this host. The site must stay on one origin:
  the docs hand problems to the console through same-origin storage, and the
  research client requires a same-origin `/sign-in` redirect.

The site runs on one EC2 instance (public IP `44.196.29.12`) behind Caddy with
Let's Encrypt certificates. The DNS records must be DNS-only (not proxied by
Cloudflare): synchronous DAG and Lean requests can outlast a proxy's 100-second
origin timeout. The security group must allow 80 and 443 only; the console
(4600) and the sponsored Kimi broker (4610) listen on loopback. Earlier
`sslip.io` and `moonshot.hailab.io` addresses are obsolete. A successful
loopback request using `curl --resolve` checks Caddy's routing but does not
establish public reachability; `deploy/ansatze/smoke.sh --public` also checks
through ordinary DNS.

The documentation site is mounted at `/`, so public pages use clean URLs such
as `/news` rather than `/docs/news`. `/docs/*` is not aliased on ansatze.ai;
only the historical `/docs/sign-in` variants remain direct, non-cacheable
sign-in redirects.

The `/sign-in` route is deliberately stable. Caddy redirects it to the current
mounted console, which means the randomized mount can be rotated without
rebuilding the documentation site. The redirect destination is observable to
any visitor; authentication, not knowledge of that path, is the security
boundary.

The redirect must land on `<mount>/login` (not `<mount>/`): the docs research
client follows `/sign-in` and derives the API base from that final path. Caddy
matches `/sign-in` before the static site, whose `sign-in.html` bridge would
otherwise loop.

The page can also deliberately create a restricted guest session. Guest access
is enabled by default in application code and can be disabled with
`AGENT_MONITOR_GUEST_ACCESS=0`. It is not equivalent to
`AGENT_MONITOR_PUBLIC=1`, which automatically creates guest sessions instead of
showing the sign-in/guest choice. Before enabling either mode on a hosted
instance, verify the guest engine allowlist, concurrency/session-creation
and rolling run/verification limits, output-token cap, and provider-key policy
from `.env.example`. Guests and new accounts default to **Ansatz CGM · Kimi K3**
(engine id `kimi`): the tool-free CGM pipeline of the Math Agent Harness
(strategist, up to two independent workers, one cold model-verifier call per
lemma, bounded repair, curator) pinned to Kimi K3. A guest run is limited to
12 model calls and 2400 s by the guest budget profile (`AGENT_MONITOR_GUEST=1`
is set for every guest run), with an 8192-token output ceiling per call; a
typical run takes 5–25 minutes on Kimi. Its verified facts, exploring graph
and deterministic `proof.md` are model-verifier results, never a Lean
certificate. Hosted runs use the sponsored Kimi broker (`ansatze-kimi.service`,
loopback only), which is the only process that holds the Kimi key; the console
environment holds no Kimi key. A visitor's own Kimi key goes to the operator's
Kimi endpoint (`AGENT_MONITOR_KIMI_BASE_URL`, default the built-in Kimi K3
base); account-level base URLs are ignored for this engine. The one-call
`plain` baseline is also eligible when enabled. Guests cannot set custom provider origins or inherit
unrelated server credentials. The allowance is **10 cumulative run starts** per
guest session, after which sign-in is required; deleting runs does not restore
starts. A sign-up is a member account: one with full access (the default;
the administrator can turn it off for an account) can link its own Claude
Code and Codex, one without it cannot, and the administrator's own accounts
are never shared. A `PLAIN_CMD` or
`KIMI_PROOF_CMD` override disables the corresponding engine for guests because
arbitrary local commands are outside the built-in contract.
Guest Formal/Lean and informal/formal DAG views are enabled by the reviewed
update in `artifacts/guest-formal-dag-2026-09-15/`. The derived pipeline starts
as soon as a guest run finishes and uses only short-lived Kimi proxy
credentials; Lean checking always uses the isolated broker. A run whose CGM
fact graph proves its target gets its informal DAG deterministically from the
facts (`cgm-fact-graph`, no model call; labelled model-verifier accepted only
with a valid harness receipt, otherwise "unattested CGM facts") before Lean
translation, which then follows the fact blueprint. Completed guest runs whose pipeline could not start (for
example, guest capacity was full) start it once when opened. Direct Verify,
Compile, Check, Audit, and feedback actions and DAG refreshes are available to
guests, bounded separately from proof-run starts
(`AGENT_MONITOR_GUEST_DERIVED_ACTIONS_PER_HOUR`, a global concurrency cap, at
most four Lean repair rounds for Verify/generation and one for a feedback
revision; see "Guest Formal/Lean and DAG actions" below). Coding-agent Formal
harnesses still require sign-in. Lean generation defaults to four repair rounds
(`lean_verify.AUTO_MAX_REPAIRS`) when a request omits `max_repairs`, as the
automatic pipeline does; signed-in requests may ask for at most six. The
console's Verify button requests two rounds for every user. Feedback revisions
allow at most three.
Self-registration is closed unless `AGENT_MONITOR_REGISTRATION_OPEN=1` is
explicitly set, including during first-account bootstrap; the owner's
administrator account is created on the host with `agent-monitor create-admin`.
ansatze.ai opens registration with a reviewed drop-in (see "Registration and
what members can do" in `deploy/ansatze/README.md`). Self-registered members
are untrusted: `agent_monitor/account_policy.py` keeps them on the tool-free
engines (`kimi`, `plain`, and `math_harness` on their own API keys only)
without subscriptions, subagents or custom provider endpoints, because ordinary
local harnesses do not isolate hostile prompts from the host. An administrator
can lift that for chosen accounts with **full access** (see "Accounts an
administrator manages"). Enrollment
changes require a daemon reload and service restart after
confirming no proof processes are active. `AGENT_MONITOR_REGISTRATION_EMAIL_SUFFIXES`
(for example `.edu`) limits which addresses may sign up, and
`AGENT_MONITOR_REQUIRE_EMAIL_VERIFICATION=1` requires an emailed confirmation
link before a new account can sign in; it needs the SMTP settings, which also
send password-reset links. `agent-monitor set-password` sets a password from the
host when email is not configured.

## Accounts an administrator manages

Admin view → **Accounts** (`GET /api/admin/accounts`, administrators only;
`agent_monitor/accounts_admin.py`):

- **Add accounts.** Paste one person per line: `Name - email`, `Name <email>`,
  `Name, email`, `Name<TAB>email`, `email - Name` or just `email` (numbering
  and bullets are ignored). Each line must hold exactly one address, which is
  taken whole: `mary-jane@uni.edu` is never cut at its hyphen. The console
  first shows a **check of the list**, with each line's name, address and
  what will happen (`preview: true`: nothing is created or sent). Then each
  new account gets a random password (12 letters and digits by default,
  8–32, no look-alike characters), shown once and never stored in clear.
  Accounts made this way can sign in at once (no email verification) and are
  never administrators. An address that already has an account is left as it
  is, except that it joins the group, if one is given (see **Groups**).
  - **Taken exactly as written:** an account's address always appears on
    its line as typed, apart from capitals and invisible characters.
  - **What is taken off:** a separator before it (`Name:addr`, `Name, addr`,
    `Name <addr>`), a mark wrapping it on both sides (`*addr*`, `` `addr` ``),
    and a sentence's punctuation after it.
  - **Refused, never guessed at:**
    - a line where the address's start is unclear (`Name|addr`,
      `Name -addr`, `_addr`);
    - an address that is not plain ASCII, or that mail would not reach
      exactly as written;
    - a line over 1000 characters.
  - `POST /api/admin/accounts {text, preview?, group?, password_length?,
    send_email?, subject?, message?, sender_name?}` → `{results: [{email,
    name, line, status, password?, email_sent?, group?, group_joined?,
    error?}]}`.
  - If the server hits an error it cannot get past (the database locked, say),
    the run stops at that line. The passwords already made are still in the
    answer; that line and the later ones come back `not_processed`, with a
    top-level `stopped`.
- **Email the sign-in details.** With `send_email`, each new person gets the
  message with `{name}`, `{email}`, `{password}`, `{url}` and `{sender}`
  filled in. A blank message means the default one. A message without
  `{password}` is refused, and no invitation is ever sent without its
  password.
  - **Sending:** it goes out with the site's mail settings (`SMTP_SERVER`,
    `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL`), from the site's
    address under "<sender name> via Ansätze", and replies go to the
    administrator's own address.
  - **`{url}`:** the site's sign-in address, `AGENT_MONITOR_APP_URL` (+ the
    mount) + `/login`. It is never taken from a request; the console shows it
    in the form.
  - **When email is refused (400):** without mail settings, or without
    `AGENT_MONITOR_APP_URL`. The console then offers the details to copy and
    send by hand.
  - **Failed sends:** an email that fails never loses the password; it is
    reported as not sent.
- **Access on/off.** `POST /api/admin/accounts/access {emails, access: "on" |
  "off"}`.
  - **Off** signs the person out at once (every session ends) and refuses
    their sign-in, password or Google, with "Your access to this site is
    paused for now…" (`AGENT_MONITOR_ACCESS_PAUSED_MESSAGE` replaces the
    text).
  - **On** lets them sign in again with the same password.
  - **`others: "off"`** (with `access: "on"` and a `group`) turns everyone
    else *in that group* off, in the same transaction, including accounts
    the page never showed. Accounts outside the group are never touched; the
    console's "Set access from a list" uses it.
  - **Administrators:** an administrator's access is always on.
  - **Other ways in:** a pause closes one account, not the site.
    - While sign-ups are open (`AGENT_MONITOR_REGISTRATION_OPEN`), a paused
      person can create a second account with another address or another
      Google account. That account is in no group, so a list never turns it
      off.
    - While guest access is on (`AGENT_MONITOR_GUEST_ACCESS`), they can use
      the site as an anonymous guest.
    - To keep a study's control arm fully out, close both for the study. The
      Accounts view says when either is open (`open_doors` in its data).
- **Groups.** An account can be in one group, such as a field test's
  participants. It joins one through "Add accounts" with a group, or through
  `POST /api/admin/accounts/group {emails, group}` (`""` takes it out).
  - **Spelling:** a group typed differently only in case, accents or spacing
    is the existing one ("Field Test" joins "Field test"). The answers carry
    the stored `group` and `group_is_new`.
  - **Scope:** a group only scopes "everyone else" for access, and changes
    nothing else. "Everyone else off" for a group nobody is in is refused.
- **Full access.** `POST /api/admin/accounts/full-access {emails,
  full_access: "on" | "off"}`, or `full_access: true | false` when adding
  accounts.
  - **On by default.** Every new member account starts with full access:
    sign-ups (password or Google) and accounts added here, unless the Add
    form's box is cleared (`full_access: false`, `--no-full-access`). Guests
    never have it. The first start of this release gives every existing
    member account full access, once (`auth_migrations`), so an account
    switched off afterwards stays off. `AGENT_MONITOR_FULL_ACCESS_DEFAULT=0`
    turns the default off for new accounts; it takes nothing back. While
    sign-ups are open this means anyone who signs up can run the local-tool
    harnesses on this host (see "What it lifts").
  - Adding gives it to new accounts only: an address that already had an
  account comes back `full_access_skipped`, because whoever registered it
  first may not be the person on the list. Give it from the table once
  checked.
  - **What it lifts:** an account with full access is no longer restricted
    (`account_policy.is_restricted`). It may link its own Claude Code and
    Codex in Settings, run Max on them, run Max (API) on the Anthropic and
    OpenAI keys saved on it, and run every engine except Hermes.
    That includes the coding-agent harnesses, which run commands on this
    host with no OS sandbox: such a run, or a prompt injection in what it
    reads, can read the server's files and other accounts' data. Give it
    only to people you trust. It may also use and verify custom provider
    endpoints.
  - **What it keeps:** it always runs on its own keys and linked accounts,
    never the administrator's. It keeps the member rate limits and feedback
    caps. It reaches the sponsored Kimi K3 only through the engines every
    member may (Kimi K3, Plain, Analyze, Readability), so never through the
    CGM or coding harnesses. Hermes stays the administrator's: it runs
    inside the console process, where its tools see the server's own
    environment. It
    never sees the server's keys, opens the admin pages, writes the
    operator's library or research atlas, or counts as an operator in
    projects (which stay tool-free once a non-administrator writes in them).
  - **Taking it back:** applies from the account's next request; runs it
    already started finish.
- **New password.** `POST /api/admin/accounts/password {email,
  send_email?}`: a new random password, shown once; the person is signed out.
  The email uses its own new-password message. Never for an administrator's
  account: they use "Forgot password" or `agent-monitor set-password` on the
  server.
- **From the server.** `python -m agent_monitor.accounts_admin list | create
  <file> [--group … --full-access --send-email --url … --sender … --reply-to …]
  | on <emails> | off <emails> | full-access on|off <emails> | group <group>
  <emails> | password <email>`. On
  ansatze.ai:

  ```bash
  cd /home/repo/agent_monitor_math
  sudo runuser -u ubuntu -- env AGENT_MONITOR_DATA_DIR=/var/lib/ansatze/console \
    AGENT_MONITOR_ENV_PATH=/etc/ansatze/console.env PYTHONPATH="$PWD" \
    ./.venv/bin/python -m agent_monitor.accounts_admin list
  ```

  - **`--send-email`:** the site's address and mount (`AGENT_MONITOR_APP_URL`,
    `AGENT_MONITOR_BASE_PATH`) are set in the service unit, not in
    `console.env`. Emailing from the command line therefore needs `--url
    https://ansatze.ai/sign-in`, as well as `--sender` and `--reply-to`.
  - **Environment:** it reads the env file as the console does, and it
    refuses to run when there is no account database there.
  - **`create`:** prints every password before it tries any email.
    `--send-email` refuses to start without mail settings or a sign-in
    address.
  - **Exit status:** non-zero whenever part of a request was not done.
  - **Pauses need the new release:** a pause is enforced only by a console
    running this release. Until the deploy has finished, a pause written
    from the command line is not enforced.

## The Sandbox conversation

The console's Sandbox view is one conversation, read top to bottom in a
single scrolling column (`#sandbox-thread`). The Lean Verification, Monitor
and DAG views open from the proof card's toolbar (beside Make readable, Edit
and Download: `#proof-views`, which follows the hidden tab bar's names,
availability and status dots); the top of the page has no tab bar, only
"← Sandbox" while another view is open, beside Share and Stop/Continue.

- **Order.** The run's problem first, as the reader's own message: the
  run's first message when the chat has one (the line, with its files, that
  the server writes before `▶ run started`; an older chat's opening line; or
  the session's recovered copy of it), otherwise the record's problems
  (`problemItems`; for an analysis or a readable version its `problem_text`,
  focus included, and its source link). A later follow-up in the same words
  is shown as a follow-up; the problem stays first. Then the run's opening
  notices (the chat lines before the first message sent after the problem,
  `#chat-msgs`). The latest proof, report or readable version follows as a
  card with its full toolbar, then any questions and answers about a
  manuscript. The rest of the chat comes last, in order, under the card and
  just above the message box (`#chat-later`): each message sent (with its
  files), the engine's replies and the notices after it. A message sent from
  a thread at or near its bottom brings the thread there, and the thread
  then follows what arrives after it (the queued notice, the reply), as a
  chat does; a send error is shown there too. The card no longer repeats the problem; with several
  problems, each proof keeps a small "Problem N" heading (without the
  problem's length, which would read as the proof's).
- **One proof per turn.** A proof run reads like a chat: you → proof → you →
  proof. Each turn (the first run, then each follow-up) keeps a copy of its
  proof as it was when the turn ended, in the hidden `.proof_turns/` folder of
  the run's workspace (`agent_monitor/proof_turns.py`: `index.json` lists
  each turn's number, start, end, status and kept file; `<n>.md`, or
  `<n>.tex` for a LaTeX-only proof). A dot-folder, it is never in the run's
  file list or export. The run record (`/api/runs/<id>`) carries the list as
  `proof_turns`.
  - The latest turn's proof is the live card (Edit, Download, Lean). It
    follows the message that started that turn. A message sent after that
    turn started, and what follows it, goes under the card (`#chat-later`)
    until its own turn starts.
  - An answer-only turn (easy_route) keeps its answer, not the proof: it is an
    "Answer" card after its message, and the live card stays with the turn
    that last worked on the proof.
  - Each earlier turn with a proof kept is a read-only card ("Proof · turn
    N", with its status if it did not finish, and its end time). It sits
    before the first message sent after that turn started, or else before
    the first line after it ended. It shows its start until Show more, typeset
    like the live card, with its own Download (`proof-turn-N.md`). Its text is
    read once through the workspace file endpoint. A turn that kept no proof
    (it failed before writing one) has no card.
  - While a later turn works and has not rewritten `proof.md` yet (some
    engines write it only at the end), the live card shows no proof: the
    turn before's is already in its own card above. It shows "Turn N is
    working on a new proof…" (no Edit, Download or readable tabs) until the
    proof is rewritten, the turn ends or the run stops working, or the
    editor is open. An answer-only turn never hides the proof.
  - A turn ends (its proof kept) before any feedback queued for the next
    turn starts. A turn left running by a restart is marked `interrupted`
    when the next one starts. A deleted run records nothing. A symlinked
    `.proof_turns` is never written through.
  - Runs from before this change have no turns: their chat splits at the
    first message sent after the problem, as above.
- **The card's header.** On a screen at least 761px wide and 560px tall
  the card's toolbar and, for a readable version, its tabs (Readable
  version, Original, Notation, Map) stay at the top of the conversation as
  one piece while the card is read, so the tabs are always in reach; when
  the card's end goes by they leave together, and nothing of the header
  slides over the rest. The conversation's scroll padding is the header's
  height (measured as drawn: a toolbar of two rows, the tabs), so that a
  link or field reached with Tab is never left under it, and focusing a
  control in the header (Tab, the arrow keys) scrolls nothing. On phones
  and in short windows the header scrolls with the card.
- **What is left out.** Chat lines that the status line already says: a turn
  starting, continuing or ending (`▶ run started`, `✓ run finished`,
  `✗ run failed · …`, `✗ run stopped · …`, `■ run stopped by user`, `Run
  stopped by user.`, `Continuing run…`, `Continuing proof with your
  feedback…`), a re-check's `started` line, a follow-up's reply that is only
  `Run finished.` / `Run failed.`, and the operator's notes on what a harness
  was given (`library injected: …`, `profile skills materialized: …`,
  `native SKILL.md profile injected into this harness`). A first run's
  `✗ run failed: <reason>` is left out only while it is the run's own state
  (the status line shows the reason); after a later turn it stays in the
  thread. Every other system line, including one the console does not know,
  is shown as a small notice: the `📎 Read for the model` pages, queued or
  removed feedback, an interrupted run, a re-check's result, a summary, a
  setup that failed. An analysis's runner log is never shown, and a readable
  version shows only its request; the raw process log is no longer on the
  page.
- **Replies.** A reply in which any line starts with an activity-log tag
  (`thinking · `, `assistant · `, `stage · `, `edit · `, `exec · `,
  `tool · `, `error · `, `mcp_tool_call · `, `web_search · `,
  `todo_list · `, `· usage in `, `✓ result · `, `✗ result · `,
  `✓ usage · `, `◦ `, `$ `: every line `cli_events.py` writes) is what the
  CLI and CGM engines store: a chunk of the run's log (the first 3,000
  characters of its last 12,000, 2,000 for a follow-up), not its answer,
  which is the card. It is shown collapsed as "Engine log" with Show log /
  Hide log; opened, the stored text as written, in monospace, in a box that
  scrolls sideways. No line of it is taken for a summary. A prose reply
  (e.g. `I could not apply that feedback: …`) shows its first line, and Show
  more opens the rest. Long messages show their start the same way.
- **Errors.** A message that could not be sent is said under the
  conversation, one notice at a time, in the server's words when it gives
  them (a limit, a refusal), else by its status (`The message could not be
  sent (HTTP 502). Try again.`); the message stays in the box, and the notice
  goes once a message is sent.
- **Under the conversation.** The status line (stage and detail, live chips,
  the manuscript stages), the note on what a message does (a proof run:
  feedback continues the run, which may start paid calls; an analysis or a
  readable version: answers are a model's, not a certificate), then the
  message box or the question box, which grows with its text. What the
  engine reads of an attached file has a row of its own in the box, in full
  (a tool-free engine's says formulas may be garbled); on a phone and in a
  short window its short form (`Text is extracted (formulas may be
  garbled)`) stands for it until a file is staged. The files staged keep to
  about two rows of cards, then their list scrolls, so that eight files
  never push the box's text out of the window. The status line's stage
  (`Re-checking…`, `Drafting…`) stays on one line; its detail is cut short
  instead. On a phone the stages of an analysis or a readable version have
  a row of their own, and the note shows up to three lines; a failed run's
  reason wraps in full there and on wider screens. In a window 520px tall
  or less (a phone held sideways) the dock keeps to one status row (its
  stages and chips scroll sideways; a failed run's reason keeps to two
  lines, with `more` / `less` (`aria-expanded`) for the rest when the two
  lines cut it short, and all of it in its tooltip), a one-line note
  (all of it in its tooltip) and a tighter box that grows to three lines
  (two in a window 380px tall or less) and then scrolls; the staged files
  sit on one row beside Attach files that scrolls sideways (a long name is
  cut short, all of it in the card's tooltip), the help in full under them,
  and the passage a question is about shows two lines; so the conversation
  keeps most of the height. Wherever the stages scroll sideways, the strip
  shows the running stage by its own scroll (never the conversation's),
  again whenever the strip changes size, until the reader scrolls it.
  Read-only project members and guests see no message box (and no feedback
  note) on a proof run, as before.
- **Place.** A run opens on its latest exchange. A document newly shown in
  the card (Written proof / Verified facts, by a click or the arrow keys, a
  readable version's tabs, an analysis's stored copy, a report or readable
  version just published, a PDF proof) opens at the card's top: brought to
  the top of the view when the reader was below it (reading the card, or
  the questions after it), never scrolled down to. A placeholder drawn
  first (`Loading the stored copy…`, `Loading the original text…`, a PDF
  being drawn) loses no place: the thread is too short for a moment (the
  browser clamps it), the place is held, and the document that follows
  takes it: the card's top for a reader who was below it, and for one at
  or above it (the card's top in view, where its toolbar and tabs are
  clicked) the very place they were, even when other output (an answer)
  arrives meanwhile. A document that stays shorter than the window (the
  Map's note, Notation with no table) cannot take that place: the thread
  stays at its end, where the browser left it, the place still held; as the
  thread grows again (an answer, a taller window) the reader is moved toward
  it, never past it, and given it back once the thread is long enough.
  Live output, and `proof.md` as a running run writes it, is followed only
  from within 80px of the bottom; only the reader's own scroll keeps them
  at the bottom. A
  reply or notice drawn above the part being read never moves it, nor does
  the card's toolbar growing a row above it; a question asked elsewhere
  (the owner in another tab) never moves a reader who is reading above the
  bottom (one at the bottom follows it). The place survives the other
  views, a click on the open run (a running one too; an analysis keeps its
  stored copy shown, a PDF proof is drawn again where it was) and Done
  after editing. While `proof.md` is in the editor, neither a message sent,
  nor one that could not be sent (its notice is an alert), nor the
  engine's newer `proof.md` (which replaces the text when nothing was
  typed) scrolls the editor away, and the caret stays where it was; only
  typing at the end keeps the last line in view. An editor given newer
  text while another view is shown is sized to it when the Sandbox is
  shown again, before the place is given back.
- **PDF proofs.** A proof that is only `proof.tex` or `proof.pdf` stays on
  screen as a PDF, under its file's name, while the run is not running,
  whatever text its record carries (the agents' writes: a record with any
  agent output has some), and while a follow-up runs until the record's
  text is no longer the text it had when the PDF was drawn (a follow-up's
  record keeps the previous turn's writes until it writes its own); its
  live text is shown then, and the PDF again once it ends. A run opened
  again draws its PDF without showing that text first. The PDF on screen
  is drawn anew (its pages under `Rendering…`, a `/pdf` request) only as
  a turn ends or the run is opened, or for a new file: the derived views moving after
  a turn (Lean, the DAG) and their end refresh it as its files say.
- **Closing lines.** A run's record says it ended a moment before its
  closing chat lines are written: the `✓ run finished` summary, the
  engine's reply, an analysis's or a readable version's summary, a
  re-check's result. The console reads the chat again one and four seconds
  after it sees the run (or its re-check or question) end, while that run
  is still shown, so these appear without a reload, said once. Feedback
  queued while the run ran starts only after that (its job is listed
  running before its record says so), and a Continue from another tab
  starts a turn this page did not ask for: the run list, read every five
  seconds, follows the run shown by its latest job, starting from the list
  read when the run was opened. A job it did not list before, or one it
  listed running that has ended since, means a turn ran meanwhile (a short
  one, a fast failure, can start and end between two reads, or before the
  first periodic read after the run is opened): the record is read once
  more, so its end, its reply and its proof are drawn. A job still listed
  running has the record read again once per read, for up to a minute per
  job while its record catches up, so the queued turn is followed to its
  reply. Only that periodic read takes the
  polling up again, at most once per read and never in the same moment
  (the read a poll makes as its run ends never restarts it, which would
  poll back to back); a poll taken up again that reads the record
  unchanged draws nothing again (a PDF proof is not drawn anew) and queues
  no closing reads again. The closing reads and the proof's last refresh
  are queued once per end.
- **A turn this page started.** After ▶ Continue, a message that continues
  the run, or a ▶ Continue the server answers with "already running" (a
  turn starting: a queued message's, another tab's), the server has
  registered the turn's job, but its worker writes the record "running"
  only once the turn is prepared (the record, the proof and, for most
  engines, the files' text read first), so the polls just after may still
  read the record as it ended. Until the record's turn changes (its
  status, time, error or job; an edit of proof.md, a Lean check or the
  derived views moving meanwhile do not count) the turn is awaited: the
  run shows as continuing (`Continuing…`, the `continuing…`
  pill and the session bar's line, ■ Stop, no ▶ Continue to press a second
  time), and the record is read again two seconds later, then 3.2 seconds
  later, then every five seconds, the run list's pace (about 13 reads a
  minute; nothing drawn or queued again, no periodic refresh of the proof
  or the chat, and none after a read that fails), so the turn is followed
  within five seconds (two at first) of its record saying running, or of
  its end if it ends first. ▶ in the run list for a run not shown reads
  the run's record just before the Continue is sent, and that record is
  the one before the turn: a turn that fails as the server takes it (a
  failed run failing again included) is drawn at its first read, and a
  turn of another tab that ended since the list's last read is never taken
  for this one. It is awaited for up to a minute, until a Stop sent from
  the page, or until the run list shows the turn's job ended without the
  record changing; the record is then read at once and drawn as it reads.
- **Stale answers.** A reply about a run no longer shown, or an older read
  that answers after a newer one, is never drawn: another run's `proof.md`
  never lands in the card, and the newest chat lines are never taken away.

## Lean and DAG views after a follow-up

A run's automatic Lean and DAG views (`auto_pipeline.json` in its workspace)
are paid model calls on the run's own route. When a follow-up of any engine
ends, they are derived again only when something is missing:

- **Unchanged proof, every view produced:** if the follow-up left `proof.md`
  (or `proof.tex`) as it was (one that failed, say) and every view now
  recorded for that proof was produced, no new pass starts; it would only
  repeat the paid calls. The views count whether the run's last full pass
  produced them or a Lean Check, Compile or Lean-harness run did since.
  A Lean result of incomplete, unfaithful or compiled counts as produced, and
  so does a DAG built from the text when its model call failed; refresh those
  from the Lean and DAG tabs.
- **Changed proof, or a view that failed or is blocked:** derived again, as
  before.
- **Run's own pass still running when the follow-up ends:** an unchanged proof
  is never derived again (unchanged behaviour).
- **Run deleted while its views are derived:** the pass stops. A model call
  already in flight finishes; a Lean session then stops before its next call,
  Lean probe or compile, no later stage runs, nothing more is written, and
  anything written after the deletion is removed when the pass ends. A Lean
  action started from the console (Verify, Revise, Audit) stops the same way
  and answers 410.

## Stop, Continue and Delete

- **Stop** records the run stopped at once, while the session's worker may
  still wind down (a CGM runner for up to 30 s). Until that worker is done,
  `GET /api/run/<id>` has `"stopping": true` (and `/api/jobs` marks its job
  so), `POST /api/runs/<id>/continue` answers 409 "This run is still
  stopping; try again in a few seconds", and the console keeps ■ in place,
  disabled and "Stopping…", for at least 3 s: ▶ Continue takes that spot
  only once the stop has settled. A follow-up's worker counts from the
  moment its job is registered, before its thread has started, so a Stop
  and a Continue in that moment never run a second session.
- Feedback queued for the stopped session is dropped, and the message leaves
  the session: it is no longer a session problem (`problem.txt` and
  `problems.json` follow), so no later turn, a bare Continue included,
  answers it, and it no longer counts toward a member's feedback cap. It
  stays out when the stopped turn still ends finished (a CGM turn that had
  published, or a Stop just after a turn's runner ended), and a member gets
  back the run start its queued message booked, while that start is still
  within the hour. The chat says it was removed and must be sent again to
  be answered; files it brought stay with the run (the chat says so), and
  a CGM turn no longer shows them first as new. A queued copy of a
  message the session already had takes nothing away. A message sent
  after the Stop waits for the stopped worker, then continues the run.
- A follow-up's task tags "← new this turn" only the session problems its
  own message added; a bare ▶ Continue (also right after a Stop) tags none.
- A Plain run on the included Kimi K3 (`credential_source`
  `sponsored_kimi`) takes no follow-up: the sponsored service ran its
  problem as one isolated run of its own, and the Plain runner a follow-up
  would start has no sponsored credential. ▶ Continue and a chat message are
  refused before anything is recorded or a member's run start kept ("A
  Plain run on the included Kimi K3 can't be continued; start a new Plain
  run with your feedback").
- A Stop or Delete while a run's attachments are being read (a CGM turn's
  or Plain's PDFs extracted, or waiting for an extraction slot) ends it at
  once: no runner starts, and a deleted run stays deleted. Plain reads its
  files only once the run's record says running, as a CGM turn does.
- **Delete** stops the run's session for good. A worker still running writes
  no record, manifest entry or chat line afterwards, so the run stays deleted
  (the usage it still reports is booked under the run's owner). A call already
  in flight is paid and booked as usual, and its result dropped.

## Ansatz CGM runs

The `kimi` and `math_harness` engines run the CGM pipeline in
`agent_monitor/runners/math_harness_runner.py`. Its budgets come from the
console environment (`/etc/ansatze/console.env`; restart `agent-monitor`
after a change) and are clamped to hard ceilings. Guest, public and sponsored
runs (`AGENT_MONITOR_GUEST`, `AGENT_MONITOR_PUBLIC_KIMI` or
`AGENT_MONITOR_SPONSORED_KIMI` is `1`, which includes a signed-in account's
sponsored Kimi runs) always use the guest profile, whose defaults are also its
ceilings, so an environment value can only lower them.

| Variable | Signed-in default (ceiling) | Guest, public, sponsored |
|---|---|---|
| `AGENT_MONITOR_CGM_MAX_CALLS` (recorded provider calls, low-effort and format retries included) | 16 (64) | 12 |
| `AGENT_MONITOR_CGM_MAX_WORKERS` (also bounded by `AGENT_MONITOR_MAX_ITERATIONS`) | 3 (3) | 2 |
| `AGENT_MONITOR_CGM_MAX_REPAIRS` | 2 (4) | 1 |
| `AGENT_MONITOR_CGM_MAX_VERIFY_CANDIDATES` | 2 (3) | 2 |
| `AGENT_MONITOR_CGM_MAX_LEMMAS` | 8 (16) | 6 |
| `AGENT_MONITOR_CGM_MAX_SECONDS` (wall clock) | 5400 (14400) | 2400 |
| `AGENT_MONITOR_CGM_CONCURRENCY` (provider calls in flight) | 3 on API routes, 1 on Codex/Claude subscription routes unless set (6) | 2 |

- `AGENT_MONITOR_CGM_API_TIMEOUT` (default 300 s, or 630 s on a sponsored
  Kimi run; 60–900; on an OpenAI model at extra-high or maximum effort the
  default is 1800 s and the ceiling 3600; a Max (API) run sets its own
  1800 s, below) is each API call's total timeout, and every call still ends
  by the run's deadline;
  `AGENT_MONITOR_CGM_REASONING_EFFORT` (`low`, `high` or
  `max`) overrides the Kimi effort of every stage but the curator. Guest and
  sponsored CGM calls always have an 8192-token output ceiling.
- On one linked Codex or Claude Code account each call may take 600 s
  (`AGENT_MONITOR_CODEX_TIMEOUT`; `AGENT_MONITOR_MATH_HARNESS_CLAUDE_TIMEOUT`,
  where Claude Code workers and repairs get 1200 s). At Extra high or Ultra
  effort, CGM and Analyze calls, follow-ups included, get 1800 s unless one
  of those or `AGENT_MONITOR_CLAUDE_TIMEOUT` is set. Every call still ends
  with the run's wall clock.
- A problem statement may be at most 44,000 bytes of UTF-8 text; larger ones
  are refused before a run slot or a sponsored credential is used.
- **Kimi models.** Kimi K3 (`kimi-k3`) is the only Kimi model of the
  engines that serve every Kimi model name as Kimi K3, or as no Kimi model
  at all, so that a run named for another one (`kimi-k2.5` typed as a
  custom model, on an account's own Kimi key) would run under a name its
  record, receipt and usage do not match: Plain, the two Ansatz CGM
  engines, Meta-Harness, Analyze and Readability call Kimi through
  `api_backend.api_chat`, which always asks for Kimi K3
  (`kimi_k3.chat_payload`), and DeepSeek Harness and IMProof run Kimi K3 for
  `kimi-k3` alone (another Kimi name runs DeepSeek, or IMProof's default
  workflow). These engines (`engines_registry.KIMI_K3_ONLY_ENGINE_IDS`, each
  listed to the console as `kimi_k3_only`) refuse another Kimi model when
  the run starts, on every route, with or without a Kimi key of the
  account's own, before any key is projected or sponsored credential
  issued, whatever the sponsored service's health: "Plain (no harness) calls
  Kimi K3 for every Kimi model name; choose kimi-k3" ("DeepSeek Harness runs
  Kimi K3 as its only Kimi model; …", as IMProof; Analyze and Readability
  word it for their mode, below; Ansatz CGM · Kimi K3 says "Kimi Proof uses
  kimi-k3", and to a member "Ansatz CGM · Kimi K3 runs only kimi-k3"). The
  coding-agent harnesses (Codex CLI, DeepAgents, OpenHands,
  OpenClaw, OpenClaude, Hermes) send the model they are given to an
  account's own Kimi key, so there they run another Kimi model as named;
  only the sponsored service asks for Kimi K3 whatever an engine names, so
  on that route another Kimi model is refused for them too ("The sponsored
  Kimi service calls Kimi K3 for every Kimi model name; choose kimi-k3, or
  add your own Kimi API key in Settings"). Follow-ups keep the rule: one of
  a run recorded on another Kimi model where it would run as Kimi K3 is
  refused ("…; this run ran on kimi-k2.5, so it cannot continue: start a
  new run on kimi-k3"), and a follow-up that names one for a Kimi K3 run
  there stays on Kimi K3; `api_backend.api_chat` never sends a call named
  for one. A run that names no model, whose saved default is another Kimi
  model, runs Kimi K3 on those engines where the account runs it (its own
  Kimi key, or the sponsored service), else the first model of its keys
  that runs, as the console's menu would select it (never one it cannot
  run); a coding-agent harness takes that default as named on an own Kimi
  key, and as Kimi K3 on the sponsored route. The launcher offers no other
  Kimi model for those engines. Analyze and Readability take Kimi K3 in any
  case or with its provider prefix (`Kimi-K3`, `kimi:kimi-k3`) and record it
  as `kimi-k3`, a member's start in public mode included; the other engines
  are offered the `kimi-k3` of the menus. Ansatz CGM · Kimi K3 refuses any
  other spelling. Plain also runs `Kimi-K3` on an own Kimi key, recorded as
  typed, and on the sponsored route, recorded as `kimi-k3`; on a linked
  account it is refused like any API model. Plain runs `kimi:kimi-k3`,
  recorded as typed, only when a Kimi key is the account's only key: with
  another key beside it, or no key, the start is refused ("No compatible API
  key is configured; …"), and with another provider's key alone it starts
  on that key and the run fails for want of a Kimi key. An administrator's
  or a full-access account's own-key Kimi K3 runs of Plain, Ansatz CGM,
  Meta-Harness, DeepSeek Harness and IMProof, and their follow-ups, go to
  the operator's Kimi endpoint (`AGENT_MONITOR_KIMI_BASE_URL`) unless the
  account set a `KIMI_API_BASE` of its own, which stands; Ansatz CGM · Kimi
  K3 runs and a restricted member's always go to the operator's. The
  derived Lean and DAG calls of such a run do not follow this: they still
  use the account's own `KIMI_API_BASE` or, without one, the built-in Kimi
  base, whatever `AGENT_MONITOR_KIMI_BASE_URL` says.
- Calls still in flight 15 s after `AGENT_MONITOR_CGM_MAX_SECONDS` are
  abandoned. Stop sends SIGTERM: the runner abandons its in-flight calls
  (the console then ends their Claude Code or Codex processes), publishes
  nothing and exits 143 (`kimi` and `math_harness` alike); any other
  failure exits 1.
- A run writes `proof.md` only when it publishes, so the derived-views
  pipeline (Lean and the DAGs) waits at least the run's outer watchdog, which
  follows `AGENT_MONITOR_CGM_MAX_SECONDS`, plus 300 s
  (`AGENT_MONITOR_AUTO_PIPELINE_TIMEOUT`, default 7200 s, only raises it).
  That is a signed-in run's watchdog whatever the console's own
  `AGENT_MONITOR_SPONSORED_KIMI` service switch; guest and sponsored runs
  have shorter ones.
- Every published run carries `math_harness/project/engine_receipt.json`.
  The current writer records receipt schema 3 (schema 4 for Max, below) with verification policy
  `solved-first-topological-waves-stop-on-target@1` (the first block, in the
  worker's written order, that the verifier does not accept decides the
  outcome; at concurrency 1 this is exactly the schema-2 order). Receipts of
  schema 3 with the earlier `solved-first-level-waves-stop-on-target@2` label
  and schema-2 receipts (`solved-first-topological-stop-on-target@1`) still
  attest; anything else, including a Codex-route receipt that claims response-only
  isolation version 4 (see "Codex vetting"), is shown as unattested.
  Validation is fail-closed, and an attested result is labelled model-verifier
  accepted, never a Lean certificate.
- The run's owner may edit `proof.md` in the Sandbox, also while a follow-up
  runs. The edit unbinds the receipt and deletes nothing: Facts shows the
  facts unattested (the Outline keeps their lemma names), the write-up is
  stale, and the DAGs, Lean and coverage are marked stale. Explore, whose
  receipt no longer binds `proof.md`, says the exploration graph is not
  published yet until the next pass publishes one. The next follow-up
  or Continue still runs: its prompt quotes the edited `proof.md` as the
  previous turn's write-up (both CGM engines hand that prompt to the
  pipeline; see "Attached files" for how a long one is quoted), and its
  publication retires the edited generation, receipt first, like any
  earlier one, with its own `proof.md` bound to a new receipt. An edit
  saved while a pass runs is replaced by that pass's `proof.md`. A receipt,
  `TARGET.md`, `PROBLEM.md` or fact file that no longer matches still fails
  the pass (before any call when it is found at the start).
- **Max** (model `max`, route `dual_subscription`) runs `math_harness` on
  both of the run owner's linked subscriptions at once. Claude Code, on the
  first of `fable`, `opus`, `sonnet` the account offers, answers the
  strategist, verifier and curator calls; Codex, on the first of
  `gpt-6-astra`, `gpt-6.1-sol`, `gpt-6-sol`, `gpt-5.6-sol` it offers, answers
  the worker and repair calls; both at maximum effort, with the run mode fixed.
  The console offers it only when both accounts are linked, which on this site
  means the administrator, or an account given full access on its own linked
  accounts; other members and guests are refused, and Max is never
  billed to an API key. Each call sees only its own account: Codex calls get
  `CODEX_HOME` and the service `HOME`, never `CLAUDE_CONFIG_DIR`, and Claude
  calls never get `CODEX_HOME`. Each call may take 1800 s and the run 10800 s
  (`agent_monitor/cgm_max.py`); concurrency stays 1. The outer watchdog is at
  least 11100 s: `AGENT_MONITOR_CLI_TIMEOUT` and
  `AGENT_MONITOR_MATH_HARNESS_TIMEOUT` can raise it but never lower it, since
  a stopped run publishes nothing. The receipt is schema 4:
  transport `hybrid`, with `workflow.routes` naming the advisor (Claude Code)
  and worker (Codex) models, and every call checked against its side. When
  an account must sign in again, reaches its usage limit or cannot use its
  model during a run (any Claude Code or Codex route), the run says why and
  sends that account no further verifier, repair or curator call; a verifier
  stopped so leaves the candidate unattested, with the reason in `proof.md`. A
  follow-up continues as Max, or stops if either account was disconnected.
  Lean and graph work after a Max run uses the Codex account with the worker
  model at high effort. Derived calls have a fixed per-call limit, so the
  same cap applies after any Codex-subscription or API-key run made at
  extra-high or maximum effort (an API-key run's DAG call gets 180 s, its
  Lean calls 240 s); the console's run-profile note says so.
- **Max (API)** (model `max-api`, on the ordinary `api_key` route) is the same
  split on API keys: a Claude model through the Anthropic API answers the
  strategist, verifier and curator calls, and an OpenAI model through the
  OpenAI API answers the worker and repair calls, both at effort `max`
  (`output_config.effort` on the Claude side, `reasoning.effort` on the OpenAI
  side), with the run mode fixed. Neither Max falls back to the other: Max is
  never billed to an API key, and Max (API) never uses a linked account.
  - **Who may run it:** the administrator, on the server's Anthropic and
    OpenAI keys unless it saved its own (as its other API runs do), and
    accounts given full access, on the Anthropic and OpenAI keys saved on that
    account, never the server's, even in public mode. It needs both keys.
    Guests, other members and projects that stay tool-free (a
    non-administrator wrote in them) are refused, and so is a follow-up by an
    account whose full access has ended. The console offers "Max (API)" next
    to Max in the Ansatz CGM model picker when `GET /api/settings` reports
    `cgm_max_api.available` (otherwise the plan's `reason` says what is
    missing, and a start is refused with it), whatever the account's runtime
    for new runs and never as the automatic choice; the picker, the launcher
    and the run's chips say it is billed to API keys.
  - **Models:** `claude-fable-5-1` (advisor) and `gpt-6-astra` (worker) by
    default. `AGENT_MONITOR_CGM_MAX_API_ADVISOR_MODEL` and
    `AGENT_MONITOR_CGM_MAX_API_WORKER_MODEL` in the console environment pin
    another model of each allowlist (advisor `claude-fable-5-1`,
    `claude-opus-5-5`, `claude-sonnet-5-5`; worker `gpt-6-astra`,
    `gpt-6.1-sol`, `gpt-6-sol`, `gpt-5.6-sol`); any other value is ignored. A
    run and its follow-ups keep the models in its `route_plan`, and a
    follow-up stops if either key is gone. On the Claude side, a request that
    Anthropic's safety classifiers refuse is answered in the same call by the
    fallback model Anthropic recommends for that refusal (server-side
    `fallbacks: "default"`, on `api.anthropic.com` only), at that model's
    rates. A refusal that remains is unusable output, like an empty reply:
    its partial text is never used, a strategist so refused leaves the run
    on one direct plan, and a verifier so refused gives that candidate an
    "error" verdict while the others are still checked.
  - **Limits:** each call may take 1800 s and write up to 64,000 output
    tokens, reasoning included, and the run 10,800 s, as for Max
    (`agent_monitor/cgm_max.py`). Unlike Max, up to 3 calls are in flight
    (`AGENT_MONITOR_CGM_CONCURRENCY`, 1-6). The usual call budgets above
    apply (`AGENT_MONITOR_CGM_MAX_CALLS`, 16 recorded calls by default), plus
    the written-proof round's own calls. The derived views wait for the
    run's own outer watchdog plus 300 s.
  - **Data retention:** `claude-fable-5-1` needs 30-day data retention: a key
    whose Anthropic organisation uses zero data retention gets HTTP 400 on
    every advisor call, and the run stops at its first call with Anthropic's
    message. `AGENT_MONITOR_CGM_MAX_API_ADVISOR_MODEL` can pin another
    advisor (for every account).
  - **Cost: there is no spending cap.** At list prices ($10 per million input
    and $50 per million output tokens on both default models), 16 calls of
    about 40,000 input and 20,000 output tokens each cost about $22 ($6.40 of
    input and $16 of output); longer outputs cost more, up to $3.20 of output
    for a call that uses its whole 64,000-token ceiling: at the default
    budgets the worst case is about $70 of output per run (22 calls including
    the written-proof round, each at 64,000 tokens), plus up to about $20 for
    the automatic Lean and graph pass on the worker's key (about a dozen
    calls of up to 32,768 output tokens), more with higher budgets, and as
    much again for each follow-up that changes the proof. The spend limits
    set with Anthropic and OpenAI on the accounts that own the keys are the
    cap.
  - **Records and billing:** the run records model `max-api` on route
    `api_key`, with `route_plan` `{"mode": "api", "advisor": {…,
    "provider": "anthropic", …}, "worker": {…, "provider": "openai", …}}`.
    Its receipt is schema 4 (transport `hybrid`) with the Anthropic and
    OpenAI `api_chat` route table; a revision older than this one shows such
    runs as unattested and refuses their follow-ups until this one is back.
    Its billing class is `api`, and each call is priced at the rates of the
    model that billed it: the Claude model that answered, or each model's
    own share of a call a refusal fallback passed on (the reply's node names
    the model that answered and carries its share; each model that declined
    first gets a node of its own, "declined first by <model>"); and the
    requested OpenAI model,
    since OpenAI replies name a dated snapshot that may have no price of its
    own. Calls cut at their time limit, abandoned at the run's deadline or
    in flight at a Stop are not in the Monitor's cost, though the provider
    may bill them. Lean and graph work after a Max (API) run uses the worker
    model on its API key at high effort.
- **Claude API calls** that runs make through `api_backend.api_chat` (Max
  (API), Plain, Meta-Harness, single-route Ansatz CGM, Analyze, and the
  agent harnesses' own audit calls) go through the official SDK
  (`anthropic`, pinned in `pyproject.toml`). The DeepAgents and OpenClaw
  agent loops and the console's own Lean and DAG model calls use their own
  clients, without the rules below. DeepAgents' audit calls run in its own
  venv: `setup.sh` installs the same pinned SDK there, and a venv made
  before this SDK must be refreshed (re-run `setup.sh`), or its Claude
  audit calls fail with a message that says so.
  - **Endpoints.** Calls to `api.anthropic.com` stream the reply; with a
    custom `ANTHROPIC_BASE_URL` (a gateway) each attempt is one plain
    request, as before.
  - **Refusal fallbacks.** On `api.anthropic.com`, `claude-fable-5-1`,
    `claude-opus-5-5`, `claude-opus-5` and `claude-sonnet-5-5` calls opt
    into Anthropic's server-side fallbacks in every run: a request their
    safety classifiers refuse is answered in the same call by the model
    Anthropic recommends for the refusal's category (some categories have
    none, and the refusal stands). The declined attempt is billed when it
    declined mid-reply or in a billed category; the Monitor books every
    attempt Anthropic reports in `usage.iterations`, so its cost can exceed
    the bill. A CGM, Analyze or Readability call, in Max (API) or not,
    prices each model's share at its own rates (a re-check's or question's
    call too), also when the refused call was still billed: in the run's
    trace the reply's node names the model that answered, and each model
    that declined first gets a node of its own ("declined first by
    <model>"). Plain, Meta-Harness and the agent harnesses' audit calls
    book the call as one. A refusal that remains is never used as an
    answer: it fails a Plain or agent run, and a CGM or Analyze stage
    treats it like an empty reply (Analyze says the model declined, and
    why).
  - **Time limit.** An attempt must connect within 10 s and receive its
    response headers within what is left of the call; from then on the
    reply is ended at the call's timeout however it is still arriving, and
    a reply whose final event arrived is kept even if the connection then
    failed.
  - **Retries.** Only a request Anthropic refused before processing it
    (HTTP 429 rate limited or 529 overloaded) is sent again, at most twice,
    after the wait the refusal names (otherwise 2 s, then 4 s) and only
    while at least 5 s would remain; anything else (a timeout, a 5xx, a
    dropped stream) may have been processed and billed, so it fails the call.
  - Redirects are never followed, and runs never inherit
    `ANTHROPIC_CUSTOM_HEADERS` (the SDK would add those headers to every
    request).
- **OpenAI output limit:** a call through `api_chat` gets
  `AGENT_MONITOR_API_MAX_TOKENS` output tokens (default 16,384). At
  extra-high or maximum effort an OpenAI call gets 64,000 by default
  instead, and 1800 s when its caller sets no timeout (CGM, Analyze and
  Readability calls: unless `AGENT_MONITOR_CGM_API_TIMEOUT`, or for Analyze
  and Readability their own `*_API_TIMEOUT`, is set; up to 3600 s):
  reasoning counts against the limit, and at 16,384 it could use all of it
  and return no answer. A Plain or Meta-Harness run at those efforts gets a
  3-hour watchdog instead of 1 hour (`AGENT_MONITOR_CLI_TIMEOUT` and the
  engine's own timeout still win). An empty reply cut at its limit says so
  in the run's error; a reply cut after some text is used as it is.
  Readability's rewrites and repairs on an own key get their own long
  limits, 64,000 tokens and 1800 s, at any effort (Readability runs, below):
  `AGENT_MONITOR_API_MAX_TOKENS` and `AGENT_MONITOR_CGM_API_TIMEOUT` may
  only raise those, never lower them, so either at its documented default
  (16,384 tokens, 300 s) leaves them as they are; only Readability's own
  `AGENT_MONITOR_READABILITY_REWRITE_MAX_TOKENS` and
  `AGENT_MONITOR_READABILITY_API_TIMEOUT` set them exactly. A rewrite or
  repair that the provider says stopped at its output limit (OpenAI
  `incomplete_details.reason` `max_output_tokens`, Anthropic `stop_reason`
  `max_tokens`, a Chat Completions `finish_reason` `length`, Gemini's
  `finishReason` `MAX_TOKENS`) is reported as "the reply reached the model's
  output limit (64,000 tokens) before it finished", with the call's limit.
- **Estimated cost:** in a CLI engine's run (Plain, the CGM engines,
  Analyze, Readability, the agent harnesses) a trace node is priced at the
  model the node (or its stream) names when the price table has that
  model; else, outside the two Max modes, at the run's requested model
  when the table has an exact entry for it (a route alias such as `opus`
  or `max` never has one); else at `gpt-4o` rates. So Plain on
  `gpt-6-astra`, whose reply names a dated snapshot the table lacks
  (`gpt-6-astra-2026-09-03`), is priced at `gpt-6-astra`. A CGM call on a
  provider-prefixed route (`openai:gpt-5.2`) is priced at the id it
  requested (`gpt-5.2`), a failed call that was still billed too. A name
  the table lacks is looked up without its Bedrock region and provider
  prefix (`us.anthropic.…`, a plain word before the dot and a name that
  starts with a letter after it), then by its family, which can land on
  another listing of that family (`llama3.3-70b` on a distilled variant's
  entry) but never on a fragment of the name: a dotted name keeps its dots
  (`kimi-k2.5` is not `5`, nor `qwen3.5-flash` `5-flash`), a family under
  four characters or without a letter is never matched, and an
  OpenRouter-style `vendor/model` id no entry contains whole is looked up by
  its model (`meta-llama/llama-3.1-70b-instruct` by `llama-3.1-70b-instruct`).
- Sponsored calls go through the Kimi broker's credential gateway, which runs
  at most `PUBLIC_KIMI_GATEWAY_CONCURRENCY` (default 8) upstream calls at once.
  Each guest run keeps up to 2 in flight, so a few concurrent runs can fill
  it. A call that finds every slot busy waits for one, first come first
  served, for up to `PUBLIC_KIMI_GATEWAY_QUEUE_SECONDS` (default 60, at most
  120; 0 turns the wait off) while fewer than `PUBLIC_KIMI_GATEWAY_QUEUE_DEPTH`
  (default 32) calls wait. The wait comes out of that call's own total time
  (`PUBLIC_KIMI_UPSTREAM_TIMEOUT_SECONDS`, default 600 s), and the gateway
  answers every call within that time (HTTP 504 if Kimi is slower). Clients
  must wait longer than that: the sponsored ones wait 630 s and send their
  remaining time as `X-Ansatze-Timeout`, so ship the broker with them (until
  then, `PUBLIC_KIMI_UPSTREAM_TIMEOUT_SECONDS=225` keeps the old timing). A
  call whose caller hangs up first is stopped within a second or two, so it
  keeps no slot. Only a full queue, or a wait that
  runs out, returns HTTP 429, and such a call never reached the provider. The
  sponsored clients may send such a call again (next item).
- Sponsored Kimi clients (the CGM runner, derived Formal/DAG and Lean calls,
  the news summarizer) send their whole remaining timeout as
  `X-Ansatze-Timeout`, so the broker and gateway can only answer sooner;
  CGM, Formal/DAG and Lean calls wait 630 s (a CGM call less near the run's
  deadline), the news summarizer 90 s. A sponsored call is retried only when
  it certainly never reached processing (the broker could not be reached; a
  typed `forwarded: false` busy, unavailable or soon-freed quota refusal; or
  an upstream 429/503 within 2 s): at most three attempts, after the reply's
  `retry_after` (2–30 s) or 5 s then 15 s, never with less than 30 s of the
  call left, and at most six retries per CGM run. A timeout, any other 5xx,
  an empty completion or an untyped error is never replayed. On a sponsored
  CGM run a strategist call that stays refused is sent again after 30, 60
  and 120 s; when it still fails, or fails ambiguously, the run continues
  with the direct plan instead of ending (a spent call budget or an expired
  token still ends it), and when every call of every worker was refused one
  rescue worker turn runs after 60 s (no pause leaves the run less than
  300 s). The run's final error says what happened, for example "Kimi's
  provider is temporarily overloaded and refused the request (HTTP 503).
  Please try again in a few minutes."; it adds "No tokens were used." only
  when no call of the run was processed.
- Those resends are not counted in `AGENT_MONITOR_CGM_MAX_CALLS`: a run sends
  at most six transport retries and three strategist resends, all of calls
  refused before processing, so the strategist's one call record can stand
  for up to ten requests and a guest run makes at most 21 (the rescue worker
  is a recorded call). Derived Formal/DAG and Lean calls retry at most twice
  each. The gateway counts a call against `PUBLIC_KIMI_GATEWAY_CALLS_PER_HOUR`
  before forwarding it, so upstream 429/503 refusals use that quota too.
- Every 2xx reply a direct API call received is booked, whatever the
  provider and whether sponsored or not: an empty completion that ended a
  CGM call keeps its tokens in the receipt's call record and the usage
  events, a user-key Kimi call that replaced an empty completion books both
  replies, and Formal/DAG and Lean calls book each reply in the usage ledger.
- After deploying a change to `agent_monitor/kimi_k3.py`, restart
  `agent-monitor`: the console loads that module at start, and Formal/DAG and
  Lean calls keep their previous sponsored handling until the restart.
- An accepted run's `proof.md` names each lemma by the label its proofs cite
  it by (`## Lemma L1 · fact …`, `**Uses:** Lemma L1 (fact …)`; ties in the
  order are broken by label, L2 before L10). The receipt records this as
  `proof_rendering: verified-closure@2`; runs published earlier keep their
  numbered `verified-closure@1` proof and attest as before.

### Attached files

The CGM engines (`kimi`, `math_harness`) and Plain make tool-free calls: they
open no file and see no image. A run's attachments reach them as text
(`agent_monitor/cgm_sources.py`):

- Text files as they are, of which a turn reads only what it can use (its
  budget plus 4 KB; the rest is counted, never held in memory); PDFs through
  Analyze's sandboxed pypdf extractor (a resource-limited child, at most two
  at a time in the console process, which never parses a PDF itself). The
  text is cached per attachment in `attachments/.text/` (0600), so a share
  never includes it, deleting the run removes it, and neither the run's file
  list nor its export shows it. Images, PDFs without text (scanned), and
  encrypted or damaged PDFs are listed as NOT AVAILABLE with the reason;
  none fails a run. A PDF whose extraction stopped early (a CPU, memory or
  file-size limit, a page it could not walk past, a timeout) keeps the pages
  it read: the 📎 line says "at least N pp" and that the rest could not be
  read, the SOURCE MATERIAL's file list (and Plain's) "at least N pages" and
  the page it stopped after.
- The files' text is data: a tag-like `<NAME` in it (also with whitespace,
  zero-width, blank-rendering (a braille blank, a Hangul filler) or control
  characters in it, or fullwidth brackets) gets `‹` for its `<`, and a line
  that imitates a file, page or excerpt marker starts with `‹` (such
  characters in it read as nothing, as spaces, or as the line renders them;
  a line ends at any line break: a lone carriage return, a form feed or a
  Unicode line separator too), so a file can neither close a prompt's
  delimiter nor forge a file or page; file names are fenced alike, also in
  the list of files the server appends to a follow-up's message (which these
  engines' tasks and `problem.txt` quote; the chat shows the names as they
  are). Mathematics keeps its `<` and `>`.
- Uploads refuse images for `kimi`, `math_harness` and `plain` (for a
  follow-up, the run's own engine) before anything is saved, saying the
  engine cannot see images, to paste the formula or statement as text, and
  that a run on Claude Code or Codex, where the account has them, can read
  images; those engines and the others with tools keep images and file
  paths.
- Every CGM turn (a first run, a follow-up, queued feedback) renders SOURCE
  MATERIAL from all of the run's files, those attached since the last turn
  first, into `.agent-monitor-sources.md` (0600) in the workspace, which the
  runner reads once and deletes (`AGENT_MONITOR_CGM_SOURCES` names it). Only
  the strategist and worker prompts carry it, at their start and within
  120,000 bytes; repair, verifier, curator and write-up prompts never do, so
  workers restate what they use from it (a worker's format retry keeps a
  clipped copy of its reply, clipping the sources further when it must). It
  never enters `problem.txt`, the run record or the receipt. The chat says
  what the model reads, each file numbered as the model cites it
  (`[File n, p. k]`), and how to bring in pages that did not fit, for example
  `📎 Read for the model: File 1 "notes.pdf" (12 pp, all) · File 2 "paper.pdf" (40 pp: pages 1–17; the rest did not fit). To read other pages, name them in a message, e.g. "pp. 18–23 of paper.pdf".`
- Its size in UTF-8 bytes is at most `AGENT_MONITOR_CGM_SOURCE_BYTES`
  (default 96,000) on API-key and subscription routes and
  `AGENT_MONITOR_CGM_SPONSORED_SOURCE_BYTES` (default 48,000) on guest,
  public and sponsored runs (both 4,000–112,000), less when a long problem or
  task leaves less room. Small files are whole and large ones share the rest.
- What a turn's message refers to comes first, else what the session's
  latest earlier message that refers to anything does. The server's words
  never count: the list of a message's attached files, the text standing for
  a message that brought only files (a first run's too, and each one in a
  batch of queued messages), and a bare Continue's feedback (whose
  "problem" and "proof" would otherwise name problem.pdf or proof.tex). A
  file referred to by name or stem, as "File n" or as "the second file" (of
  files sharing a name, the latest upload) is served first: whole if it
  fits, within 70% of the room or what the other files do not need, each
  other file keeping its 8,000-byte reserve; the chat line says it was given
  priority. Pages named ("p. 12", "pp. 3–5", "pages 3, 7 and 9", LaTeX's
  "pp.~3--5") in this turn's message, then in the session's earlier ones (at
  most 24 a file), come first in their file, which is the file named right
  after them ("pp. 3–5 of paper.pdf"), else a file the message refers to
  that has them, else every PDF that has them.
- A CGM task has no persona or user library (the published DAG memory still
  applies), no server path and no file-tool advice; `math_harness` receives
  it through `--prompt-file`, as `kimi` does. A follow-up's task puts the
  feedback first, then the session problems and the previous turn's
  write-up: whole up to 8,000 characters; a longer one as its first 2,000 or
  so, a marked cut, and its end from a heading when its last `## Main
  theorem` section fits (the lemmas before it when they fit too), else its
  last characters, the header saying it is an excerpt.
- Plain gets excerpts in its single prompt: at most 12,000 bytes per file and
  40,000 in all; the chat says what it read, as for a CGM turn (Plain reads
  no page a message names).

### Written proof (write-up)

After an accepted run is published, a written-proof round turns the verified
fact closure into one proof written for a reader (`agent_monitor/cgm/writeup.py`,
prompt version `cgm-writeup@1`). A writer receives only the problem and the
verified blocks (each statement and proof exactly as the verifier accepted
it, in the worker's order, without fact ids) and writes a **Theorem**, any
**Conventions** once, and the **Proof** in Markdown with LaTeX. An independent
checker, the CGM verifier with no predecessors, then reads the problem and
that text alone. A rejected text may be revised and checked again.

- The write-up is `writeup.md` (a fixed header by status, then the model's
  text) with its sidecar `math_harness/project/writeup.json`: status, the
  generation it was written from (receipt binding, `proof.md` hash, target,
  rendering), its own hash, the prompt hash, routes, call records, the check
  and `certificate: false`. Neither is part of the engine receipt: `proof.md`,
  the facts and attestation are unchanged, and the verified facts remain the
  proof. Model-verifier acceptance of the write-up is not a certificate.
- Status `accepted` (the last check passed), `rejected` (it found problems,
  listed at the end of `writeup.md`) or `unchecked` (the check could not be
  made). When the writer gives no usable text there is no write-up. Every
  publication of a generation first removes the previous write-up, and the
  console marks a write-up stale once `proof.md` or the receipt changed.
- The round never fails or changes a published run: provider, budget and
  format failures only degrade it (`writeup.json` and the run result list why).
- Stop during the round ends only the round. Stop sends the runner SIGTERM
  (see above): before its publication the run is stopped and nothing is
  published; after it, the runner records the write-up as far as it got,
  reports and exits 0, and the console records the run as finished (a
  receipt written during the run is the evidence; the Stop itself is
  recorded before the runner is signalled, so the run's outcome is the last
  word) and drops the feedback queued for it (see Stop, Continue and
  Delete). Stop pressed again meanwhile
  sends nothing more; a runner that
  has not exited 30 s after the first is killed. The watchdog firing during
  the round likewise leaves the run finished. A server restart during the
  round marks the run interrupted, as for any running run: its `proof.md`
  and receipt stay published and attest, and the write-up may be missing or
  not checked.
- The console decides per run: `AGENT_MONITOR_CGM_WRITEUP` (default on; `0`
  turns the round off for every CGM run) and
  `AGENT_MONITOR_CGM_WRITEUP_GUESTS` (default on; `0` turns it off for guest
  and sponsored runs). The runner reads only the `AGENT_MONITOR_CGM_WRITEUP`
  value jobs.py passes, and an unset value means off.

| Variable | Signed-in default (range) | Guest, public, sponsored |
|---|---|---|
| `AGENT_MONITOR_CGM_WRITEUP_MAX_CALLS` (the round's own calls, retries included) | 6 (2–12) | 4 |
| `AGENT_MONITOR_CGM_WRITEUP_MAX_SECONDS` (its own wall clock) | 1800, Max 3600 (120–3600) | 1200 |
| `AGENT_MONITOR_CGM_WRITEUP_MAX_FIXES` (revisions of a rejected text) | 1 (0–2) | 0 |

- The round runs on the run's route; in Max, Codex writes and revises and
  Claude Code checks. Kimi writes at low effort under the 8192-token ceiling.
  Claude Code calls use the candidate (writer), revision (fix) and critic
  (check) timeouts.
- The outer watchdog, a sponsored token's lifetime and the derived-views wait
  add the round's `MAX_SECONDS` plus 60 s when it is on (Max: at least
  14760 s), also on top of an operator's `AGENT_MONITOR_CLI_TIMEOUT` or
  `AGENT_MONITOR_{KIMI,MATH_HARNESS}_TIMEOUT`. Those bound the run until it
  publishes: a run that has not published by then is timed out as before,
  and only a published run gets the round's allowance (not for Max, which
  operator timeouts never cut short). A
  sponsored run stays within the 6600 s cap: 2400 + 900 + 300 + 1200 + 60 s
  at most.
- Lean and the DAGs of a run that gets the round start once the run has
  ended, as for a guest's run, not as soon as its `proof.md` is stable: the
  round may still be calling the same Claude Code or Codex account, or the
  sponsored pool. Until then those stages read "Waiting for the run's
  written-proof round to end". Should the derived-views wait run out first,
  they start from the published `proof.md` all the same.
- Policy note for the owner: with the round on, a guest or sponsored run
  whose proof is accepted makes up to 4 more Kimi calls (16 recorded calls
  instead of 12, plus the run's shared six transport retries), counted
  against the broker's per-token quota and `PUBLIC_KIMI_GATEWAY_CALLS_PER_HOUR`.
  `AGENT_MONITOR_CGM_WRITEUP_GUESTS=0` keeps guests and sponsored runs at 12.

### Guest Formal/Lean and DAG actions

Manual guest Verify, Compile, Check, Audit, feedback revisions and
model-backed DAG refreshes (Outline, Formal · Lean) each use one guest
Formal/DAG action. Starting the automatic pipeline when a guest proof finishes
also counts as one; at most two guest pipelines run at once, and one that
cannot start then starts when the run is next opened.

| Variable | Default | Range |
|---|---|---|
| `AGENT_MONITOR_GUEST_DERIVED_ACTIONS_PER_HOUR` (per guest session) | 24 | 1–60 |
| `AGENT_MONITOR_GUEST_DERIVED_GLOBAL_ACTIONS_PER_HOUR` (all guests) | 120 | 1–480 |
| `AGENT_MONITOR_GUEST_DERIVED_GLOBAL_CONCURRENCY` (guest actions running at once) | 2 | 1–8 |
| `AGENT_MONITOR_GUEST_DERIVED_ACTIONS_PER_CLIENT_PER_HOUR` (per client address) | 48 | 1–480 |

- A guest runs one action at a time. While the automatic pipeline is still
  running the answer is HTTP 409; a limit gives HTTP 429. Both carry a
  `Retry-After` header and a plain `{"error": …}` sentence, which the console
  turns into "try again in about N minutes".
- Compile, Check, Audit and feedback revisions refresh the formal DAG on a
  background thread afterwards, which can make a sponsored Kimi call. That
  refresh keeps the action's slot until it finishes, so the one-at-a-time and
  site-wide concurrency limits cover it; the request itself still returns at
  once.
- Lean repair rounds are capped at 4 for Verify/generation
  (`lean_verify.GUEST_MAX_REPAIRS`) and 1 for a feedback revision
  (`lean_verify.GUEST_REVISE_MAX_REPAIRS`). A guest's automatic formalization
  uses all 4; the console's Verify button requests 2. Guest Lean source is limited to
  100,000 characters, and every model call uses Kimi K3 through the sponsored
  broker.
- Reading any graph or Lean result, refreshing the model-free Informal · CGM
  view (`kind=engine`, by GET or POST), and reading the Lean toolchain status
  (`GET /api/lean/status`: broker availability and the pinned toolchain only,
  never host paths) are free.

### Guest fairness, request bodies and retention

The guest limits above are per guest identity and site-wide. Because anyone
can create a guest identity, each client address also has its own budget:

| Variable | Default | Range |
|---|---|---|
| `AGENT_MONITOR_GUEST_SESSIONS_PER_CLIENT_PER_HOUR` (new guest sessions) | 6 | 1–120 |
| `AGENT_MONITOR_GUEST_RUNS_PER_CLIENT_PER_HOUR` (guest run starts) | 12 | 1–240 |
| `AGENT_MONITOR_GUEST_CLIENT_CONCURRENCY` (active guest runs) | 1 | 1–16 |
| `AGENT_MONITOR_GUEST_RUN_RETENTION_DAYS` (0 keeps finished guest runs) | 0 (30 on ansatze.ai) | 0–3650 |

- The client is the last `X-Forwarded-For` hop when the console's peer is the
  loopback proxy (Caddy replaces any value a client sends), or the remote peer
  itself; IPv6 addresses are grouped by /64. Requests from this host are
  exempt. Clients behind one NAT share a budget, and someone with many
  addresses can still reach the site-wide caps: those remain the cost bound.
- The sponsored broker still keys its own per-client limits by a hash of the
  account id (`X-Public-Client-IP`); the console's per-client limits above are
  the per-address control.
- Guests can attach at most two text files (Markdown, LaTeX, Lean, plain text,
  CSV, TSV, JSON), 64 KB in total; `guest_policy.attachments` advertises it.
  A guest `/api/runs` body is limited to 1 MB and a guest `/chat` request is
  refused before its body is read; members keep the 29 MiB attachment
  ceiling. Every connection has a 300 s socket timeout, and the unit sets
  `MemoryHigh=10G`, `MemoryMax=12G` and `TasksMax=4096`.
- With a retention period set, the console deletes finished guest runs (run
  record and workspace; usage history is kept) that ended more than that many
  days ago, checking every six hours. `agent-monitor purge-guest-runs` remains
  for manual clean-ups.
- Every POST and DELETE must come from the console's own origin: a request
  whose `Origin` differs from `Host`, or whose `Sec-Fetch-Site` is
  `cross-site`/`same-site`, gets HTTP 403, and the HTML-form content types
  (`text/plain`, `application/x-www-form-urlencoded`, `multipart/form-data`)
  get HTTP 415, before the body is read. Tools that send neither header (curl,
  `smoke.sh`) are unaffected.

## Easy requests on Plain

A run started on Ansatz CGM (`math_harness`) or on Ansatz CGM · Kimi K3
(`kimi`) by a signed-in account or a guest has each turn rated first by a
small judge model (`agent_monitor/easy_route.py`). The first run is
rated by its problem, and each follow-up by its message, with the session's
first problem as context. A turn rated **easy** (a routine exercise, a short
computation, a definition, a small edit) runs on **Plain**, one call of the
same model on the same account route. Any other turn runs on Ansatz CGM. The
thread says when a turn moves to Plain, and when a later turn goes back to
Ansatz CGM. A message that asks for Ansatz CGM by name ("use Ansatz CGM")
always gets it, without the judge.

- **Setup.** `AGENT_MONITOR_EASY_ROUTE_OPENROUTER_KEY` in
  `/etc/ansatze/console.env` (the operator's OpenRouter key; empty = off).
  `AGENT_MONITOR_EASY_ROUTE_MODEL` is the judge, by default
  `typesafe/jev-1.13`. `AGENT_MONITOR_EASY_ROUTE=0` switches it off.
  `AGENT_MONITOR_EASY_ROUTE_URL` points it at another endpoint of the same
  shape (https, or this machine for tests).
- **The judge is a decision model.** Jev is served on OpenRouter's Decisions
  API (`POST https://openrouter.ai/api/alpha/decisions`), not on
  chat/completions (which refuses it with a 400). The request is one `choice`
  question, `difficulty`, with the options `easy` and `hard` described, and
  `state` holding the request (and, for a follow-up, the session's first
  problem). A turn is easy when the answer's `choice` is `easy` with
  `probabilities.easy` of at least 0.6. A rating takes about 0.1 to 0.3 s and
  costs about $0.00002.
- **The key is the server's alone.** Only `easy_route.judge` reads it. It is
  not `OPENROUTER_API_KEY`, which other features would pick up as a provider
  key. `subprocess_env` strips it, and the other judge settings, from every
  engine's environment. It is never in Settings, the run record or the chat.
  LaTeX previews compile without the server's secrets too. The judge gets the
  request text and nothing else.
- **What is never moved to Plain.** Analyze and Readability, runs
  started on another engine, Max and Max (API), a start or follow-up for
  which Plain is not allowed (member policy) or would answer with another
  model or on another route (a start that Plain would put on the sponsored
  Kimi service is refused and starts Ansatz CGM; Ansatz CGM · Kimi K3 runs
  on that service itself, through Plain, so its easy turns may), a bare
  Continue, and a
  message with files (each keeps the run's current engine).
- **Failures.** No key, the judge's error or timeout (6 s in all), or an answer
  that is not clearly "easy": the turn runs on Ansatz CGM. Routing never stops
  a run or a follow-up.
- **A proof Ansatz CGM wrote stays.** Once a turn of the run has run on
  Ansatz CGM, a later turn rated easy (say "explain step 3") is
  **answer-only**. Plain answers the message with the proof as reference,
  writes its answer to the hidden `.turn_reply.md` (never `proof.md`; that
  name is also never promoted to the proof), and the Lean/DAG views are not
  derived again. The turn keeps the answer as its own (`proof_turns`, with
  `answer_only`). The Sandbox shows it as an **Answer** card after the
  message, and the live card keeps the proof Ansatz CGM wrote. A run that has
  only ever been on Plain lets Plain write the proof.
- **Said where a run is chosen.** With the judge on, the launcher's "Runs on
  …" line for Ansatz CGM (not Max) and Ansatz CGM · Kimi K3 adds: "Easy requests may be rated by an
  external model (OpenRouter · <judge>) and answered with Plain on the same
  model." (`/api/settings` → `easy_route`: on or off and the judge's name,
  never its key.)
- **Record.** The run's hidden `.easy_route.json` keeps the engine it started
  on (`home`) and each turn's engine, verdict, reason and judge. The run
  record's `engine` is the current turn's.

## Auto mode and mathematics only

With the judge on (`AGENT_MONITOR_EASY_ROUTE_OPENROUTER_KEY`, see above), a
signed-in account's start page offers **Auto** first among the modes, chosen
by default (`proving-console-auto-mode-v1` in the browser; choosing a mode by
hand turns it off, and choosing Auto again turns it back on). On Start the page
asks `POST /api/route-request` (`easy_route.rate_request`: one Decisions API
call with the questions `topic`, `formalize` and, with a manuscript link or
files, `task`), then starts the run as the chosen mode would, with
`auto_mode` in the request; the thread says "Auto mode: … (chosen by …)".

- **Modes.** Without a manuscript: Informal + formalization unless the judge
  rates the request clearly informal (P ≥ 0.6), then Informal only. With one:
  Analyze or Readability when the judge picks it (P ≥ 0.5), Analyze by
  default, or a proof when the request asks to prove something. A mode this
  site does not offer becomes Informal + formalization. With the judge off or
  failing, Auto picks the default (Analyze with a manuscript, else Informal +
  formalization).
- **Mathematics only.** A request the judge rates `not_math` with probability
  at least 0.8 is refused with "Ansätze answers mathematics only …": a new
  proof start (`/api/runs`, 400 with `not_math`, before any run start is
  reserved) and a follow-up message (`/api/runs/<id>/chat`, rated with the
  run's first problem as context; nothing is recorded and the message stays
  in the box). Any doubt, error or timeout lets the request through. Analyze
  and Readability manuscripts are not rated.
- **Guests too.** A guest's start is rated like an account's: Auto, the
  mathematics-only refusal (a refused start costs no guest run) and an easy
  request on Ansatz CGM · Kimi K3 moved to Plain (only while Plain is one of
  the operator's guest engines; the guest's limits go with it). Guests send no
  follow-ups. The guest note under the start box says requests are rated by
  the external model. Every judge call a guest makes counts against 30 an
  hour (`GUEST_RATINGS_PER_HOUR`): its Auto ratings, the mathematics-only
  check of its starts and the easy-request rating of its starts. Past them
  Auto takes its default and a start goes on unrated (and costs a guest run
  as usual). A guest's easy-request rating is taken before the guest start
  lock, so a judge call never holds up other guests' starts.

## Analyze runs

Analyze checks an existing manuscript (a link, pasted text or uploads) into a
verdict-annotated statement graph. The runner is
`agent_monitor/runners/analyze_runner.py`, started in the run workspace like
the CGM runners; it reads `analysis/request.json` (written by `jobs.py` with
`agent_monitor.analyze.request`) and makes tool-free model calls through the
same response-only turn machinery and routes as `math_harness` (sponsored
Kimi through the loopback broker with the `math_harness` credential scope, an
own key, or the Codex or Claude Code account an administrator, or an account
given full access, linked itself; Max and Max (API) are refused). The
request says whether the account may use its linked accounts
(`own_subscriptions`, written from the stored account as for every route);
the runner refuses a linked account's route without it, and the run (and
its re-checks and questions) gets that account's own `CODEX_HOME` or
`CLAUDE_CONFIG_DIR`, never the service's. A model variable in the service's
environment (`MATH_HARNESS_MODEL`, `AGENT_MONITOR_ANALYZE_MODEL`,
`AGENT_MONITOR_READABILITY_MODEL`) never replaces the run's model.

The console offers it as the third mode next to "Informal only" and
"Informal + formalization": `POST /api/runs` with `run_kind: "analyze"`
(engine `analyze`). Old records without `run_kind` are proofs; the engine
listing advertises `modes: ["analyze"]` for this engine (`["prove"]` for every
other one) and a per-viewer `analyze` policy, so a browser shows the mode only
when the server offers it.

- **In the console.** The composer's mode switch shows Analyze only while the
  listing offers it (a page served before the server knew Analyze, or with
  `AGENT_MONITOR_ANALYZE_ENABLED=0`, keeps the two proof modes; a runner that
  still needs setup is shown to administrators only). One pasted link is
  previewed (`/api/analyze/resolve`; the openai/math root lists its
  manuscripts to pick one); otherwise the pasted text or the attached files
  are the manuscript. A Problems-page hand-off (`mode: "analyze"`,
  `analyze_url`) fills the composer and previews the link; nothing starts
  until the visitor presses Analyze. An analysis shows its report (raw HTML in
  it is shown as text), the runner stages, and the Manuscript DAG: blue
  checked by a model verifier, amber gap, red error, grey unclear, hollow not
  checked (green stays reserved for Lean-kernel results), with verdict
  filters, reference / inferred / model-proposed edges, and per-result
  Re-check, Formalize statement and Open in source. A result whose verifier
  reply could not be read is "not checked (no usable verifier output)", as in
  the report, never "unclear". Continue and the message box are hidden for
  analyses (questions go in the question box); the Sandbox's conversation
  shows an analysis's request, its notices (a re-check's result among them)
  and its report, never its runner's log, and the hidden process log drops
  raw model JSON and token-usage lines; the Lean tab is named "Lean
  statements" and never speaks of verifying a proof; projects offer a
  Prove / Analyze kind.

- **Who may analyze what.**

  | Role | Route | Sources | Results / calls / wall clock |
  |---|---|---|---|
  | guest | sponsored Kimi K3 only (a guest's own key is not used) | arXiv, GitHub and openai/math links; paste ≤ 20,000 characters; the text-only guest uploads (2 files, 64 KB) | 12 / 16 / 2,400 s |
  | member | sponsored Kimi K3, or its own API key; with full access (the default) also its own linked Codex or Claude Code account (never Max) | + any public HTTPS PDF/TeX/zip/tar.gz link; paste ≤ 400,000 characters; TeX/Markdown/text/PDF and zip/tar(.gz) uploads | sponsored 30 / 36 / 3,600 s; own key 120 / 140 / 3,600 s |
  | admin | any route, including a Codex or Claude account (response-only, tool-free) | all | 256 / 300 / 14,400 s |

  A guest's generic HTTPS link is refused with "Guests can analyze arXiv and
  GitHub links; this file needs a member account." A guest starts at most one
  analysis per hour (three per client network), and each one also counts
  against the guest run limits (session, hourly and site-wide). A guest
  re-checks at most 2 results of an analysis (counted on the run record),
  2 per guest session and 4 per client network each hour, on top of the guest
  Formal/DAG limits. A member's analysis counts against the member run limits
  (2 at once, 12 per hour). In public mode (`AGENT_MONITOR_PUBLIC=1`, where
  every account inherits the server's provider keys) a member analyzes on
  sponsored Kimi K3 only, full access or not, like the own-key CGM harness,
  which public mode removes: its `analyze` policy (as a guest's) lists
  `api_models: ["kimi-k3"]`, so the console offers Kimi K3 alone whatever
  the account runtime, and its start names no model and the API route. A
  start that names a linked account's route (an older console sent the
  runtime's) runs on sponsored Kimi K3 on the API route; another model is
  refused ("Members analyze on the included Kimi K3 on this site"). Kimi K3
  is Analyze's only Kimi model, as it is that of every engine whose Kimi
  calls ask for Kimi K3 whatever is named ("Kimi models" under Ansatz CGM
  runs), on every route, with or without a Kimi key of the account's own:
  another Kimi model (`kimi-k2.5` typed as a custom model) is refused before
  anything is reserved, whatever the sponsored service's health ("Analyze
  runs Kimi K3 as its only Kimi model (kimi-k3)"; the runner refuses it
  too), and so are a re-check of, and a question about, an analysis
  recorded on one; Kimi K3 typed in another case (`Kimi-K3`) or with its
  provider prefix (`kimi:kimi-k3`) is Kimi K3. A start that names no model
  (an API caller's, or the Projects page's "Default account model") runs on
  the account's saved default where its route runs it, never Max, and a
  saved Kimi model other than Kimi K3 counts as Kimi K3. On a linked Codex
  or Claude Code account (the route asked for, or the account runtime's) it
  is else the first model that account offers, as the console's menu
  selects it (refused when it offers none). On the API route, for every
  account but a guest or a member in public mode (Kimi K3), the saved
  default runs where the account's keys run it, or, for Kimi K3, where its
  own Kimi key or the sponsored service does; else the start takes the
  first model of the account's API keys that runs, in the order of the
  console's menu (Kimi K3 first while the sponsored service runs it), so
  with the sponsored service down and an OpenAI key, a saved `kimi-k2.5`
  (or none) runs on that key's first model; when nothing runs, the saved
  default (Kimi K3 when there is none) is refused with what is missing
  ("Kimi K3 requires your own API key, or the sponsored Kimi service must
  be available", "No compatible API key is configured; …"). Every refusal
  is a plain message: 400 for the request, 403 for a switch, 429 with
  `Retry-After` for capacity.
- **Sponsored capacity.** At most two sponsored analyses (and re-checks) run
  at once (`AGENT_MONITOR_ANALYZE_SPONSORED_CONCURRENCY`, 1-8, default 2): an
  in-memory semaphore keyed by run, released when the worker exits however it
  ends (finished, failed, stopped, crashed); a restart starts with none held.
  A site-wide daily cap (`AGENT_MONITOR_ANALYZE_SPONSORED_RUNS_PER_DAY`,
  default 40, UTC days; `0` turns sponsored Analyze off) counts every
  sponsored analysis and every sponsored re-check in
  `$AGENT_MONITOR_DATA_DIR/analyze_sponsored_usage.json` (file-locked; a start
  that never launched is refunded). Refusals: "Sponsored Analyze is busy …"
  (429, retry in 60 s) and "Sponsored Analyze is at today's limit — try
  tomorrow or use your own API key" (429, retry at UTC midnight). Guests get a
  share of that pool, never all of it: at most
  `AGENT_MONITOR_ANALYZE_GUEST_SPONSORED_CONCURRENCY` slots (default half of
  the slots, at least one) and `AGENT_MONITOR_ANALYZE_GUEST_SPONSORED_RUNS_PER_DAY`
  of the daily runs (default half of the daily cap, rounded down; at most the
  cap; `0` keeps guests off sponsored Analyze), counted as `guest_runs` in the
  same file ("Sponsored Analyze for guests is busy …" / "… at today's limit").
  The share follows who asks: an administrator's re-check of a guest's
  analysis is not charged to it. A sponsored analysis is also held to 150
  calls and 5,400 s whatever the role, inside the broker token's 200 calls
  and 7,200 s.
- **Kill switches.** `AGENT_MONITOR_ANALYZE_ENABLED=0` hides the engine from
  the listing and refuses Analyze for everyone (403); existing analyses stay
  readable. The switches also cover model work on existing analyses:
  re-checks and statement formalization (∎ Formalize statements, and the
  automatic Lean stage) answer the same 403 when Analyze is off, when guests
  are off (for a guest), and, for an analysis that ran on the sponsored
  route, when sponsored Analyze is off (a daily cap of 0, or a guests' share
  of 0 for a guest). The deterministic Manuscript DAG and compiling or
  checking Lean that already exists are not model work and stay available. `AGENT_MONITOR_ANALYZE_GUESTS=0` refuses guests only ("Analyze
  needs a signed-in account on this site."), and so does
  `AGENT_MONITOR_GUEST_HOSTED_KIMI=0` (guests analyze on sponsored Kimi K3
  only). An `ANALYZE_CMD` override makes the runner operator-chosen local
  code, so it removes Analyze from guests and members. `AGENT_MONITOR_GUEST_ENGINES` and `AGENT_MONITOR_MEMBER_ENGINES`
  name proof engines; Analyze follows its own switches.
- **Run record.** `run_kind: "analyze"`, `formalize` (false unless asked) and
  `run["analyze"]` (`agent_monitor.analyze.record.run_block`: source card,
  caps, focus, summary; `recheck` for the latest re-check, `rechecks` for the
  running tally: re-checks started, their calls, tokens and estimated cost).
  A re-check runs as a job of the console process: one that a restart (or a
  crash) left "running" becomes `interrupted` ("the previous verdict is
  kept") when the run is read, at startup (the manifest flags it with
  `recheck_running`) or on Stop. Each re-check call is booked in the usage
  ledger as stage `recheck`, billed like the analysis, at the model the
  call names (on a Claude account the model Claude Code reports, never the
  run's `opus`; a call that names none, at the run's model only when the
  price table has that model exactly); `totals` stay the analysis's own
  engine usage, as for Lean and DAG calls. `run["analysis"]`
  stays the Monitor block. The run list shows "Analyze · <title>". The library
  and published DAG memory are never injected into an analysis; the
  operator's memory gets only its label and verdict counts, and an analysis
  never becomes shared DAG memory.
- **Success.** Exit 0 with `analysis/report.md` and `analysis/analysis_graph.json`
  published; nothing is promoted to `proof.md`. The outer watchdog is the
  run's deadline plus one API call plus 300 s (`AGENT_MONITOR_CLI_TIMEOUT`
  does not apply; the caps above are the knob). The derived-views pipeline
  waits at least that long for an analysis to end
  (`AGENT_MONITOR_AUTO_PIPELINE_TIMEOUT` only raises it). Stop sends SIGTERM
  first, so the runner publishes what had settled.
- **Environment.** The runner gets the run's route exactly as `math_harness`
  does (the sponsored broker token is issued under the `math_harness` scope),
  the console data directory (`AGENT_MONITOR_DATA_DIR`, for the shared arXiv
  slot) and, for Analyze only, `AGENT_MONITOR_ANALYZE_GITHUB_TOKEN`, which is
  scrubbed from every other child process. An own-key route never carries a
  broker token. Guests always run with `AGENT_MONITOR_GUEST=1` and 8,192
  output tokens per call.
- **HTTP API.** `POST /api/analyze/resolve {url}` previews a link (metadata
  only, at most 10 s; guests 10 per hour and 20 per network, members 30,
  administrators 120). `GET /api/run/<id>/proof_graph?kind=analysis` returns
  the manuscript graph; `kind=informal` its deterministic projection (an
  analysis's DAG is never regenerated from its report).
  `POST /api/run/<id>/analysis/recheck {node_id}` re-checks one result in the
  background (owner or administrator; guests spend a re-check of their
  allowance and a derived action, members a run start); the outcome lands in
  `run["analyze"]["recheck"]`.
  `POST /api/run/<id>/lean_verify {action: "verify", targets: [...], prove:
  false}` formalizes the statements of the selected results (default: the
  main results) on the server's Lean 4.14 / Mathlib checker: "Statements
  compiled · proofs not attempted" (`scope: "statements"`; Compile and Check
  keep that scope); upstream Lean is linked and not re-checked by this server. Continue and chat answer 400
  "Analyze runs can't be continued. Start a new analysis, or re-check a node
  in the DAG." Compilation (`/pdf`, `final.pdf`, `final_latex`) is refused:
  the TeX is a third-party manuscript.
- **Questions (Ask panel).** `POST /api/run/<id>/analysis/ask {question
  (≤ 2,000 characters), selection? (≤ 4,000), view: "report" | "source",
  node_id?}` answers a question about a published analysis in the background
  (202 `{ask_id}`) with one call (stage `ask`; four with its retries, 900 s)
  on the analysis's own route: `analyze_runner.py --ask <id>` reads
  `analysis/asks/<id>.json` and writes `<id>.reply.json`
  (`agent_monitor.analyze.ask`). The context is what the analysis published,
  never anything fetched again: title, abstract and summary, each result
  with its verdict and the verifier's main finding (as many as fit: 16 KB, 6 KB
  on sponsored and guest routes, the main results and those with findings
  first, the rest counted as omitted), and in full (statement,
  the proof the manuscript gives, verdict, findings, the results it uses and
  is used by; a document-order guess is marked as one) at most three
  results: the one asked about from the DAG; those a selected passage
  belongs to (a passage of the stored source by the lines of the file it
  starts in, one of the report by its start in what the report prints about a
  result, its label and findings, then in the verifier's other notes; a
  passage that stands in several results belongs to none) or names ("Lemma
  3.2", `\ref{lem:x}`);
  and those the question names, or the main results for "the main theorem".
  The last three questions answered about the run and their answers come
  along (`<EARLIER>`), so a follow-up reads; a follow-up that selects and
  names nothing is also given the results the last answer was about.
  Answers link results as `[[result:<id>]]` and are refused when they carry
  raw HTML. Limits as for Readability's questions, counted together with
  them: one question at a time per run and none during a re-check; guests 3
  per analysis and 6 per hour (12 per client network), members 30 per hour,
  administrators 120. A sponsored question holds a slot of the pool while it
  is answered and counts against the day's sponsored questions, never the
  analyses; while sponsored runs are off (a daily cap of 0, or a guests'
  share of 0 for a guest) questions about sponsored analyses and readable
  versions are refused (403), as re-checks are. `run["analyze"]` keeps `ask` (in flight), `asks` (the last 50),
  `asks_started` and `ask_usage`; re-checks and refreshes of the block keep
  them. `AGENT_MONITOR_ANALYZE_ASKS=0` turns questions about analyses off
  (403 "Questions about analyses are turned off on this site."); the Analyze
  switches above cover them too. Project members read the answers but may
  not ask; calls are booked as stage `ask`, priced as re-checks are; a
  question a restart left running becomes `interrupted`, and an in-flight
  question defers an automatic deploy.
- **What leaves the server.** The Sandbox shows `analysis/report.md`; the owner
  can open the stored source file, never anyone else. A shared snapshot holds
  the report, the Manuscript DAG (node statements, labels, verdicts:
  "checked by a model verifier", never "verified") and the source link, never
  `analysis/source/**`, proof excerpts or agent transcripts.

- **Stages.** 1 Ingest (`analysis/source/`, `manuscript.json`), 2 Segment
  (`segments.json`: LaTeX via pylatexenc, Markdown/text by headers, PDF text
  via pypdf), 3 Check (one paper-map call, then one verifier call per result,
  main results and their dependencies first; proofs over the prompt budget
  are checked in parts), 4 Roll-up, 5 Report (`analysis_graph.json`,
  `report.md`, `receipt.json` written last, and the `proof_graph.json`
  projection). `nodes/<id>.json` keeps each result's verifier reply.
- **Verdicts.** `checked`, `gap`, `error`, `unclear`, `not_checked`, with a
  closure verdict over each result's dependencies (definitions, remarks and
  other statements without a verdict pass it on; no closure is given when
  the model, not the parser, located the statements). A reply that cannot be
  interpreted stays `unclear` with `interpreted: false` and never counts as
  checked (`coverage.uninterpretable`). They are model judgements: receipts
  say `certificate: false`, and the report names the model and route.
- **Structure.** Proofs attach by `\ref`, by number ("Proof of Theorem 1.1")
  or to the preceding statement; prose proofs are recognized by their start
  and end sentences ("In this part we deduce Theorem~\ref{..}", "This proves
  Theorem~\ref{..}"); a result whose proof the parser cannot locate is
  reported as "No proof was located by the parser", never as unproved.
- **Nothing runs.** Fetched TeX, `.py`, `.cpp` and notebooks are never
  compiled or executed (archives keep manuscript files only and list the
  rest). The LaTeX parser, the PDF text extractor and the Markdown/text
  segmenter run in isolated subprocesses (`python -I`, an empty working
  directory, no credentials in their environment) with a 1 GiB address
  space and 60 s (PDF 90 s) of CPU; Stop kills them at once.
- **Fetches.** `agent_monitor/safe_fetch.py`: HTTPS on 443 to public
  addresses only (the connection is pinned to the checked address), at most
  three revalidated redirects, 25 MiB per file, 30 MiB and 120 s per source,
  and at most 40 s per request including TLS and headers (a watchdog closes
  the socket; Stop does too). arXiv requests start at least 3 s apart, across
  every runner and the console when they share
  `$AGENT_MONITOR_DATA_DIR/cache/analyze-arxiv-slot` (the runner sets this;
  the console sets `analyze.sources.ARXIV_SLOT_PATH = "auto"`). GitHub refs
  resolve through the git ref advertisement, so only a folder listing (or a
  repository whose ref list exceeds 4 MiB) uses the REST API. Set
  `AGENT_MONITOR_ANALYZE_GITHUB_TOKEN` to lift the 60/hour limit; it is sent
  only with those connector calls (never with a pasted link, and links to
  `api.github.com` or `codeload.github.com` are refused), and a folder is
  listed only for a repository whose ref advertisement answers anonymously
  (a public one). Use a
  **fine-grained token limited to public repositories with no permissions**:
  a classic token, or one that can read private repositories, would let the
  connector read whatever the token can. Guests may use arXiv and GitHub
  links only.
- **Parser packages.** A run whose venv lacks `pylatexenc` or `pypdf` stops
  with a configuration error (exit 2) naming the package, before any model
  call.
- **Exit status.** 0 published, 2 configuration, 143 stopped (settled
  results are still published), 1 failed (also when no attempted check
  produced a verdict: provider errors, empty answers, uninterpretable output
  or the time limit; the structure is still published).
- **Run record.** `agent_monitor.analyze.record.run_block(workspace)` builds
  the `run["analyze"]` block (source, caps, focus, summary) from the
  workspace after the runner exits or after a re-check.
- **Re-check one result.** `analyze_runner.py --recheck <node_id>` in the run
  workspace, with the run's route environment: at most 8 calls and 15
  minutes; the receipt lists every re-check. It prints
  `{"type": "analyze.recheck", "result": {...}}` and exits 0; a re-check that
  yields no verdict keeps the previous result and exits 1, and a stopped one
  changes nothing (143).
- **Evaluation.** `scripts/eval_analyze.py` runs the withdrawn openai/math
  Weil-classes paper (archived revision `adc7f12`) and control papers;
  `--dry-run --local NAME=DIR` plans without model calls or network.
- **Dependencies.** `pylatexenc==2.11` and `pypdf==6.20.0` (pinned in
  `pyproject.toml`) must be installed in the shared venv.

| Variable | Default |
|---|---|
| `AGENT_MONITOR_ANALYZE_ENABLED` / `AGENT_MONITOR_ANALYZE_GUESTS` | on / on |
| `AGENT_MONITOR_ANALYZE_SPONSORED_RUNS_PER_DAY` | 40 (0 turns sponsored Analyze off) |
| `AGENT_MONITOR_ANALYZE_SPONSORED_CONCURRENCY` | 2 (1-8) |
| `ANALYZE_CMD` | unset (an override removes Analyze from guests and members) |
| `AGENT_MONITOR_ANALYZE_<PROFILE>_MAX_NODES`, `_MAX_CALLS`, `_MAX_SECONDS` (`GUEST`, `MEMBER_SPONSORED`, `MEMBER_OWN_KEY`, `ADMIN`) | 12/16/2400, 30/36/3600, 120/140/3600, 256/300/14400 (ceilings 256/300/14400) |
| `AGENT_MONITOR_ANALYZE_CONCURRENCY` | 3 (sponsored and guest at most 2, subscriptions 1) |
| `AGENT_MONITOR_ANALYZE_API_TIMEOUT` | 300 s (630 s on sponsored Kimi; 1800 s, up to 3600, on an OpenAI model at Extra high or Ultra) |
| `AGENT_MONITOR_ANALYZE_GITHUB_TOKEN` | unset |

## Readability runs

Readability rewrites an existing manuscript (a link, pasted text or uploads)
so that a chosen audience can follow it, keeping every statement and
argument, and checks each rewritten part against the original with an
independent model call. The runner is
`agent_monitor/runners/readability_runner.py`; it reads
`readability/request.json` (written by `jobs.py` with
`agent_monitor.readability.request`) and reuses Analyze's ingest, its
sandboxed parsers and the same response-only turn machinery and routes. The
console offers it as the fourth mode: `POST /api/runs` with
`run_kind: "readability"` (engine `readability`, `modes: ["readability"]` in
the listing, with a per-viewer `readability` policy).

- **Request.** `{run_kind: "readability", engine: "readability", model,
  auth_route, reasoning_effort, speed_mode, source: {url} | {text, format?} |
  attachments: [{name, data}], readability: {audience: "research" |
  "graduate" | "undergraduate", focus: "≤ 2,000 characters"}, project_id?,
  project_task?}`. The sources follow Analyze's rules by role (guests:
  arXiv, GitHub and openai/math links, a paste of at most 20,000 characters,
  the text-only guest uploads; accounts also HTTPS PDF/TeX/zip links, pastes
  of 400,000 characters and PDF or archive uploads). The run list shows
  "Readability · <title>"; a project's task becomes the focus.
- **Routes and models.** As for Analyze:

  | Role | Route | Parts / calls / wall clock |
  |---|---|---|
  | guest | sponsored Kimi K3 only (a guest's own key is not used) | 6 / 16 / 2,400 s |
  | member | sponsored Kimi K3, or any model of its own API keys; with full access (the default) also its own linked Codex or Claude Code account (never Max) | sponsored 12 / 30 / 3,600 s; own key 40 / 90 / 3,600 s |
  | admin | any route, including a Codex or Claude Code account (response-only, tool-free, one call in flight) | 120 / 300 / 14,400 s |

  On an API key a readability run takes any model the account's keys run,
  as Settings lists them (OpenAI, Anthropic, an own Kimi key on the
  operator's endpoint, the other providers), or a custom `provider:model`;
  its only Kimi model is Kimi K3, as for Analyze (below).
  Each model is called exactly as in Analyze: an OpenAI model at the run's
  reasoning effort (the run profile; at Extra high or Ultra a call gets
  64,000 output tokens and 30 minutes), an Anthropic model at its own
  default effort, and every other call with a 16,384-token output limit
  unless `AGENT_MONITOR_API_MAX_TOKENS` sets one; only a Kimi route gets a
  per-stage effort and the low-effort retry of an empty reply. One
  exception, Readability's own: a part's rewrite and its repair (and their
  format reminders) are long replies, so on an own key an Anthropic model,
  or an OpenAI model of GPT-5 or later that is not a chat model, gets
  64,000 output tokens (reasoning or thinking included) and 30 minutes for
  them, held to the run's deadline (`readability_runner.LONG_OUTPUT_TOKENS`);
  the Claude API streams them. Only Readability's own settings set these
  two limits, exactly: `AGENT_MONITOR_READABILITY_REWRITE_MAX_TOKENS`
  (1,024–128,000) and `AGENT_MONITOR_READABILITY_API_TIMEOUT` (60–3,600 s
  for them; it sets every other Readability call's timeout too). The
  settings of every API call, `AGENT_MONITOR_API_MAX_TOKENS` and
  `AGENT_MONITOR_CGM_API_TIMEOUT`, may only raise them (to 128,000 tokens
  and 3,600 s), never lower them: at their documented defaults (16,384,
  300 s) they leave the long limits as they are. A rewrite or repair that
  stopped at its output limit says so, with the limit ("the reply reached
  the model's output limit (64,000 tokens) before it finished"), in the
  part's reason and the run's progress; a repair that fails keeps the first
  rewrite. Guest and sponsored runs keep their ceiling, and Kimi and the
  other providers their limits. The bounded
  sizes (12,000-character parts, a short guide at low effort, the
  8,192-token output ceiling) belong to guest and sponsored runs; an
  own-key run uses the full sizes whatever its model. A part whose rewrite
  the model declines (an Anthropic refusal) says so in its reason. In
  public mode (`AGENT_MONITOR_PUBLIC=1`) a member runs on sponsored Kimi K3
  only, on the API route whatever its account runtime, as for Analyze
  ("Members use Readability on the included Kimi K3 on this site" for
  another model). Max and Max (API) are refused ("Max is available only for
  Ansatz CGM", and by the runner). Kimi K3 is Readability's only Kimi model,
  as it is Analyze's ("Kimi models" under Ansatz CGM runs), on every route
  and with or without a Kimi key of the account's own (its Kimi calls ask
  for Kimi K3 whatever is named): another one (`kimi-k2.5` typed as a
  custom model) is refused before a sponsored start is taken or anything
  launched, whatever the sponsored service's health ("Readability runs Kimi
  K3 as its only Kimi model (kimi-k3)"; the runner refuses it too), and so
  is a question about a run recorded on one; `kimi:kimi-k3` is Kimi K3; the
  launcher offers no other Kimi model in either mode. A start that names no
  model runs on the model Analyze's would (above). A viewer on sponsored
  Kimi K3 only (guests, members in public mode) is offered Kimi K3 alone and
  no custom model in either mode, whatever its account runtime (each mode's
  policy lists `api_models: ["kimi-k3"]`).
  A sponsored run is also held to 150 calls and 5,400 s. Guests start one
  readability run per hour (three per client network) on top of the guest
  run limits; members count against the member run limits (2 at once, 12 per
  hour).
  Policy note for the owner: a member's sponsored run may spend up to four
  of its 30 calls on repairs (two parts rewritten again and re-checked);
  guests' runs are never repaired.
- **Sponsored capacity.** Readability shares Analyze's sponsored pool
  (`AGENT_MONITOR_ANALYZE_SPONSORED_*`, the same daily count file): a run
  holds the key `<run id>` and counts as one sponsored start. A question
  holds `<run id>#ask` while it is answered but never uses up the
  manuscript starts: it counts against the day's sponsored questions
  (`asks` in the same file, guests at most half of them). Refusals name
  Readability ("Sponsored Readability is busy …", "… at today's limit …",
  "Sponsored questions are at today's limit …"). Credentials are issued
  under the broker's `math_harness` scope.
- **Stages.** 1 Ingest (`readability/source/`, `manuscript.json`,
  `segments.json` as Analyze segments, and `original.txt`: the manuscript in
  reading order; for TeX the root file with its `\input`/`\include`/`\subfile`
  closure inlined, comments and `\iffalse` blocks removed, the body only; an
  included file that is a document of its own, such as a subfile or a
  standalone figure, gives its body only, and text after an
  `\end{document}` is left out with a warning in `manuscript.json`, as is
  any result the parser found outside the text that was read; Markdown and
  text are segmented as Analyze does, in the same sandboxed child, and also
  read a CGM run's verified fact closure (`proof.md` in any of its
  renderings, also as copied from the console: one result per lemma and the
  main theorem, dependencies from its **Uses:** lines, parser
  `text-1+cgm-closure`) and Markdown headings that name a result
  ("## Theorem 2.1", "### Lemma L2"); a text that is not clearly a closure,
  such as one with a heading or a result of its own after it, is never read
  as one), 2 Guide (one call: what the manuscript shows, its
  key idea, roadmap, notation and terms, the role and importance of each
  result, with its mathematics in `$...$`; on guest and sponsored runs it
  runs at low effort and is asked to stay short; a guide that fails becomes
  a deterministic outline), 3 Rewrite (one call per part, in waves; parts
  follow top-level sections, and the pages of a PDF fill each part in turn,
  at most 30,000 characters of source, 12,000 on guest and sponsored runs;
  the bibliography is never rewritten; each rewrite gets the printed name
  of every label its part cites, `<REFERENCES>`), 4 Check
  (one independent call per rewritten part; a part with major problems is
  rewritten once more and checked again, within the run's call cap: at most
  two parts on a member's sponsored run, none on a guest's), 5 Publish (`readable.md`, `paper_map.json`, `notation.json`,
  `guide.json`, `check.json`, and `receipt.json` written last). In
  `readable.md` (and in answers) leftover LaTeX is written out: `\ref`,
  `\eqref`, `\cref`, `\Cref`, `\autoref` and `\pageref` become the printed
  name ("Section 3"; "Lemmas 3.4 and 3.5" in a list), or the label in
  brackets when the parser does not know it. Equation labels are known only
  where the same part tags the display (`\tag{2.1}\label{x}`: `\eqref{x}`
  reads "(2.1)"); the parser numbers no equations. `\cite[x]{a,b}` becomes
  "[a, b, x]" and `\label` is dropped; text-mode LaTeX a rewrite kept is
  written as Markdown (a theorem-like environment opens with its printed
  name in bold, a proof ends with ∎, lists keep the manuscript's labels,
  `\emph`, `\section`, footnotes, TeX quotes, accents and dashes), and
  each part and answer is then held to the parser's contract again (no line
  opens a link definition, one of the runner's marks or a fence, and no raw
  HTML outside math); each part's headings are shifted so
  that its top one is `##`. A manuscript with no title of its own (pasted
  without one, or headed only "Proof" or "Written proof", as Make readable
  pastes a written proof) is named by the guide's title in `readable.md`,
  the paper map and the run list. The receipt's `notes` say why the first reply
  of a stage could not be read (the parser's message, or that the reply was
  refused unread for its size or tool-call markup), which is what its format
  reminder answered.
- **Budget.** The plan keeps a check for every part it rewrites: parts past
  the part cap or the call budget are not rewritten, and `readable.md` shows
  them in their original form, marked; nothing is dropped silently. A
  rewrite must carry one `[[result:<id>]]` anchor per result its part holds;
  one that only misses an anchor is accepted after its format reminder and
  marked as a coverage issue.
- **Statuses.** Each part is `faithful`, `issues` (the check found problems
  that change the meaning; `readable.md` opens the part with a
  "⚠ Check" blockquote), `unchecked` or `not-rewritten`. The counts
  (receipt `parts`, the run record's summary, the provenance note) cover the
  parts meant to be rewritten; the bibliography, kept as in the original, is
  counted apart as `kept`. A stop publishes the parts whose rewrite had
  already come back, unchecked. The checks are a model's judgement: the
  receipt says `certificate: false`, and `readable.md` opens with a
  provenance note (which also says when the text was extracted from a PDF,
  so formulas may be garbled).
- **Run record.** `run_kind: "readability"` and `run["readability"]`
  (`agent_monitor.readability.record.run_block`: source card, caps,
  audience, focus, summary), plus `ask` (a question in flight), `asks` (the
  last 50 with their answers), `asks_started` and `ask_usage`. Success is exit
  0 with `readability/readable.md` and `readability/receipt.json`; nothing is
  promoted to `proof.md`, no derived-views pipeline starts, the library and
  DAG memory are never injected, and a readability run never becomes shared
  DAG memory. Continue, chat, proof editing, Lean actions, model-built DAGs
  and compilation (`/pdf`, `final.pdf`, `final_latex`) are refused.
- **HTTP API.** `GET /api/run/<id>` carries `sandbox_preview` with
  `kind: "readability"` (the readable version, or the progress notes while
  it runs, the parts and their checks — `check[].issues[]` is `{severity,
  location, problem, fix}`: the major issues are also marked in the text,
  the minor ones are the part's check notes — notation, macros, `original_path`;
  at most 400,000 characters of the readable version, cut before a part
  with a note saying so, as in a shared link).
  `GET /api/run/<id>/proof_graph?kind=paper-map` returns the paper map
  (results with label, statement, section, importance 1-5, role and part;
  dependencies with `kind` proof or statement and provenance ref, inferred or
  model). The owner reads `readability/original.txt` and the stored source
  through `/api/workspace/<id>/file`. A shared snapshot holds the readable
  version without the parts shown in their original form, the paper map, the
  notation and the check summary; never `readability/source/**` or the
  original text.
- **Questions.** `POST /api/run/<id>/readability/ask {question (≤ 2,000
  characters), selection? (≤ 4,000), view: "readable" | "original",
  node_id?}` answers in the background (202 `{ask_id}`) with one call
  (stage `ask`) on the run's own route: `readability_runner.py --ask <id>`
  reads `readability/asks/<id>.json` and writes `<id>.reply.json`. The
  context is the part holding the selected passage (looked up in the view
  it was selected in, then the other, ignoring Markdown marks, math
  delimiters and whitespace; else the selected result's part; else, for a
  question that selects nothing, the part the last answered question was
  read in; else the guide alone). The last three questions answered about
  the run and their answers come along (`<EARLIER>`), so a follow-up reads. One question at a
  time per run; guests 3 per run and 6 per hour (12 per client network),
  members 30 per hour, administrators 120, counted together with questions
  about analyses. Answers, like rewrites, are refused when they carry raw
  HTML outside math.
  Project members read the answers but may not ask. Each call is booked in
  the usage ledger as stage `ask`; a question a restart left running becomes
  `interrupted`, and an in-flight question defers an automatic deploy.
- **Exit status.** 0 published, 2 configuration, 143 stopped (what had
  settled is published), 1 failed (also when no part could be rewritten;
  what exists is still published).

| Variable | Default |
|---|---|
| `AGENT_MONITOR_READABILITY_ENABLED` | on |
| `AGENT_MONITOR_READABILITY_GUESTS` | as `AGENT_MONITOR_ANALYZE_GUESTS` |
| `READABILITY_CMD` | unset (an override removes Readability from guests and members) |
| `AGENT_MONITOR_READABILITY_<PROFILE>_MAX_PARTS`, `_MAX_CALLS`, `_MAX_SECONDS` (`GUEST`, `MEMBER_SPONSORED`, `MEMBER_OWN_KEY`, `ADMIN`) | 6/16/2400, 12/30/3600, 40/90/3600, 120/300/14400 (ceilings 120/300/14400) |
| `AGENT_MONITOR_READABILITY_CONCURRENCY` | 3 (sponsored and guest at most 2, subscriptions 1) |
| `AGENT_MONITOR_READABILITY_API_TIMEOUT` | 300 s (630 s on sponsored Kimi; 1800 s, up to 3600, on an OpenAI model at Extra high or Ultra); an own key's rewrites and repairs 1800 s. When set, every call's timeout (60–900 s, 3600 at Extra high or Ultra) and exactly the rewrites' and repairs' (60–3600 s); `AGENT_MONITOR_CGM_API_TIMEOUT` may only raise theirs |
| `AGENT_MONITOR_READABILITY_REWRITE_MAX_TOKENS` | 64,000: the output limit of an own key's rewrites and repairs (Anthropic, OpenAI GPT-5 or later but chat models), exactly (1,024–128,000); `AGENT_MONITOR_API_MAX_TOKENS` may only raise it |
| `AGENT_MONITOR_READABILITY_WATCHDOG_SECONDS` | set per run: its deadline + one call + 300 s |
| `AGENT_MONITOR_READABILITY_SPONSORED_ASKS_PER_DAY` | 200 sponsored questions per UTC day, about readable versions and analyses together (0 turns them off) |
| `AGENT_MONITOR_READABILITY_GUEST_SPONSORED_ASKS_PER_DAY` | half of the above |
| `AGENT_MONITOR_ANALYZE_ASKS` | on (questions about analyses) |

## Research atlas and shared memory

The Problems catalogue at `/problems` (`/research/open-problems.json`, schema 3)
lists every Erdős problem (all states), every problem of the Kourovka Notebook’s
main section (arXiv:1401.0300v48), the seven Millennium problems, Hilbert’s and
Smale’s lists, Ramanujan’s legacy and researched entries of Wikipedia’s list.
The old `/open-problems` URL is a redirect stub that keeps `?source=` and
`?problem=`. Each entry has one category — solved, AI proposed, partial or open
— with rule codes, badges and evidence; AI proposed never counts as solved.
Categories come from operator decisions, then source-of-record markers (teorth
state, Kourovka ∗/♯, the Clay listing), then research that cites accepted
evidence, then curation; see `docs/problem-sources.md`. Inputs:
`agent_monitor/data/problem_sources/*.json` (pinned source facts, refreshed by
`scripts/refresh_problem_sources.py`), `open_problem_curation.json`,
`problem_status_research.json` and `problem_status_decisions.json`.
`scripts/build_problem_catalogue.py` writes the three identical copies and the
bundled empty overlay deterministically and refuses to drop a published id.
Publishing a rebuilt catalogue is a static docs release (commit, `npm run
docs:build`); the console does not read the catalogue. “Click to solve”
preserves an existing account or opens a guest workspace, loads source context,
and waits for the visitor to choose Prove.

## News refresh

The News page at `/news` replaces Overview in public navigation. The combined
platform tour and getting-started guide are at `/quickstart`; `/overview` is
kept as a compatibility redirect.

`ansatze-news.timer` (optional; `deploy/ansatze/install.sh --with-news`) runs
`scripts/update_math_news.py` from the checkout on the hour and half-hour, so a
change to `agent_monitor/news*.py` or `agent_monitor/data/news_*.json` is live
at the next tick: apply such changes right after a tick, or stop the timer
while you do. The `ansatze-news.service` worker runs as a dedicated
`ansatze-news` system user that sees only the checkout (read-only) and its
interpreter. It stores its feed and source-cache state under
`/var/lib/ansatze-news/`. Caddy serves `/var/lib/ansatze-news/news.json` at
`/research/news.json` whenever that file exists, independently of the static
website release; until the first refresh publishes it, the snapshot bundled
with the docs build is served. Rebuilding VitePress cannot replace a live feed
with an older snapshot. The page polls this read-only endpoint every three
minutes.

Sources (`agent_monitor/data/news_sources.json`, schema 2; at most 40) are:

- RSS and Atom feeds of labs, journals, journalism and mathematicians' blogs.
  Each source has a role (primary source, journalism, commentary, preprint,
  repository), a minimum check interval, a per-source item cap, keyword lists
  for titles, for excerpts and for the feed's own categories, and
  deterministic category rules (an OpenAI post about a theorem is filed under
  Mathematics). An excerpt-only match from a source with `triage` set waits
  until Kimi K3 includes it. Blog posts (role commentary) appear only as
  related coverage inside a story; a post that belongs to no story stays in
  the cache, unpublished (a reviewed seed record from a blog is always
  shown). Sources with `excerpt_fallback: false` (the Kourovka
  Notebook editors' feed) show only a headline until Kimi K3 has summarized
  the post. E-mail addresses are removed from all source text.
- The openai/math repository history (`type: changelog`): one item per dated
  `## Month D, YYYY` section of `history.md`. A section is published only once
  it is pinned to the commit that added it: a commit of the `history.md`
  commit feed, dated from 14 hours before the stated date to two days after
  it, where `history.md` at that commit (read from `revision_url`) has the
  section and `history.md` at the previous commit of the feed does not. A pin
  is made once and kept in `state.json` (and recovered from `news.json` if the
  state is lost), so commits that later leave GitHub's short commit feed
  change nothing. A section that cannot be pinned is held and listed in the
  journal line (`held_changelog`: section, time first held, reason). A
  `waiting` section is retried every run; a `before-window` section is older
  than the commits in its window and stays held, so publish it as a reviewed
  seed record if it matters. A "Withdrawals" sub-heading makes a section a
  Correction, otherwise it is an Update. Section text is source text; only its
  summary may come from the model, and every number in that summary must
  appear in the section.
- arXiv: RSS listings keep earlier daily batches for 30 days (`retention:
  rolling`) and take new and cross-listed announcements only; one arXiv
  search-API query (at most every three hours) adds AI-assisted and formal
  mathematics preprints with their first-version dates. Versioned and
  unversioned links are one item.
- Evidence-only commit feeds (`publish: false`): the `history.md` commits
  (kept for 90 days; a changelog's evidence feed must not filter commits by
  keyword), and google-deepmind/formal-conjectures commits whose titles name a
  catalogue problem (recorded as `formalization` hints, below).

Story rules at the end of the same file group reviewed members (the
openai/math release, its updates and the reaction to it) without the model.

Kimi K3 works through the loopback broker as client `ansatze-news` (its own
hashed client address), with reasoning effort "low", no tools, and strict JSON
replies tied to supplied ids. Its tasks are summaries (at most six per call;
opinions attributed to their authors; every number must appear in the source
text), triage (include or exclude borderline items; importance 0 to 3; one
topic), changelog summaries, problem links (candidate problems come from the
names and numbers in `docs/public/research/open-problems.json`; the model
chooses a relation and must quote the source verbatim) and story grouping (a
story needs two published items, one of them a primary source, journalism or
repository record). The model never writes titles, URLs, dates, categories,
statuses or links; a malformed reply is discarded whole, and an overlong or
unsupported summary row only for that item. Model text is published only if it
is already plain text: links, markup, TeX and character references such as
`&lt;` are rejected. A strong item that triage excludes keeps its place without
a model rating, and the model rates a preprint or a blog post at most 1, so
their own text cannot lift them into the Highlights strip (importance 2 or
more). A run makes at most four calls and a UTC day at most
`ANSATZE_NEWS_MODEL_CALLS_PER_DAY` (default 60), counted in `state.json`,
because the broker's own counters reset when it restarts; 0 turns the model
off. A run records its possible calls in `state.json` before making any, so a
failed publication or a killed run still counts them, and it starts model
stages only within its first 10 minutes. `--dry-run` never calls the model.
Set the cap with `sudo systemctl edit ansatze-news.service` (`[Service]` and
`Environment=ANSATZE_NEWS_MODEL_CALLS_PER_DAY=…`). A failed task backs off for
30 minutes, doubling up to a day (with five minutes of slack for the timer's
start jitter). A task skipped without a call (cap or deadline reached, or the
broker refused an earlier task of the same run) is not counted as a failure.
Without the model, or while a task backs off, cards show headlines and quoted
source excerpts, and items wait for summaries. Problem links point to
`/problems?problem=<id>` and never change a problem's status.

The worker validates configured HTTPS sources, dates, redirects, article
links, duplicates, size limits, and relevance rules. It accepts gzip bodies up
to the size limit and retries a source once when a cache sends an unexpected
encoding. Source failures preserve last-good entries, and the page reports
source-check and publication dates separately; the journal line lists failing
source ids (`source_error_ids`) and model task errors. A summary belongs to
the content it was written from (title and excerpt, without arXiv's "Announce
Type" prefix), so a re-announced preprint keeps it and a corrected record gets
a new one, at most twice a week. Editorial seed records remain unchanged by the
model. Automatic arXiv entries show the source headline while a summary is
pending. Proof announcements do not become certified solutions.

Outputs under `/var/lib/ansatze-news/`:

- `news.json` (0644, schema 1): items, sources and top-level `stories`. Items
  may carry `importance`, `topic`, `problem_links`, `story_id`,
  `source_role`, `date_precision`, `updated_at` and `summary_source`
  (`editor` for reviewed seed records, `model` for Kimi K3, `source` for the
  source's own text); statuses include Update and Correction. The page labels
  every summary with its origin and shows source text as a quotation. A story
  is filed under its lead's category and moves up the list with its lead,
  primary sources, journalism and corrections, not with blog posts.
- `status_proposals.json` (0644): unreviewed hints for the problems updater.
  These are problem links of published items (`matched_by: kimi-k3`) and
  `formalization` hints from formal-conjectures commit titles
  (`deterministic`). They are kept for 90 days, at most 500, and are never a
  status change. Caddy does not serve this file.
- `state.json` (0600): source caches, summaries, triage, links, stories,
  changelog pins, task back-off and the daily model budget.

Reviewed seed records (`agent_monitor/data/news_seed.json`) include the
openai/math release of October 6, 2026 (722 manuscripts, 719 after the October
7 update) and that update as a Correction, linked to commits `adc7f12` and
`fd4aeeb`. Their facts are taken from the repository at those commits; the
reaction is the AHM statement, which the release card links.

Check a change before it reaches the timer by running the candidate code from
a copy against copies of the worker's files (as `ubuntu`; the state file is
private to the worker, so copy it first). The script imports the code next to
it, so the copy runs, not the live checkout that the timer uses:

```bash
mkdir -m 700 /tmp/news-check
cp -a <the patched tree> /tmp/news-check/code
sudo install -m 600 -o ubuntu /var/lib/ansatze-news/state.json /var/lib/ansatze-news/news.json /tmp/news-check/
cd /tmp/news-check/code
/home/repo/agent_monitor_math/.venv/bin/python scripts/update_math_news.py --output /tmp/news-check/news.json \
  --state /tmp/news-check/state.json --seed agent_monitor/data/news_seed.json --no-model
```

This writes only the copies (including `/tmp/news-check/status_proposals.json`)
and prints the journal line. Add `--dry-run` to write nothing. The one-time
backfill after this release fetches each source's `backfill_url` (the arXiv
search over about 30 days) and triages more than usual. Preview it with
`--backfill --dry-run` on the copies, then run it once as the worker (stop the
timer first; the runtime override is gone after a reboot):

```bash
sudo systemctl stop ansatze-news.timer
sudo systemctl edit --runtime ansatze-news.service
#   [Service]
#   ExecStart=
#   ExecStart=<the unit's ExecStart line> --backfill --max-model-calls 12
sudo systemctl start ansatze-news.service
journalctl -u ansatze-news.service -n 3 --no-pager
sudo rm -r /run/systemd/system/ansatze-news.service.d && sudo systemctl daemon-reload
sudo systemctl start ansatze-news.timer
```

The backfill run counts its 12 possible calls before making any, stops at
the daily model cap, and starts model stages only within its first 10 minutes,
so it ends inside the unit's 15-minute start timeout; items it could not
summarize or triage are handled by the next scheduled runs.

Maintained inputs: `agent_monitor/data/news_sources.json` and
`agent_monitor/data/news_seed.json`. Code: `agent_monitor/news.py`,
`agent_monitor/news_kimi.py`, `agent_monitor/news_links.py`,
`agent_monitor/kimi_tasks.py` and `scripts/update_math_news.py`.

Check scheduler health with `systemctl status ansatze-news.timer` and
`systemctl status ansatze-news.service`; inspect worker results with
`journalctl -u ansatze-news.service`. Run a manual check with
`systemctl start ansatze-news.service`. To pause automatic collection, stop
`ansatze-news.timer`; the last published feed remains available. Public refresh
metadata shows whether source checks are current. Never put provider keys in
feed records, static files, command arguments, or service-unit environment.

## Problem status refresh

`ansatze-problems.timer` (optional; `deploy/ansatze/install.sh
--with-problem-status`) runs `scripts/update_problem_status.py` every six hours
(00:15, 06:15, 12:15, 18:15 UTC plus up to five minutes) as the
`ansatze-problems` system user, with the news worker’s sandbox. It publishes
`/var/lib/ansatze-problems/problem-status.json` (0644, served by Caddy at
`/research/problem-status.json` with `Cache-Control: no-cache`; until then the
empty overlay bundled with the docs build is served) and keeps `state.json`
(0600: HTTP validators, upstream snapshots, the signal ledger, the model budget)
beside it. The output is written before the state.

The overlay is computed against the checkout’s catalogue (`catalogue_sha256`)
and never adds or removes entries. Category changes come only from deterministic
source signals: a teorth state change, Kourovka markers in a newer arXiv
version, an openai/math withdrawal of a manuscript an entry cites, or a Clay
listing change; a row whose category a reviewed decision set keeps it and gets
“Sources disagree” instead (unless the source now agrees with the decision).
arXiv (every named problem searched about once a week, each search looking back
to the previous one; model only), Palomar, formal-conjectures commits (and
research-category changes of the Erdős files they touch; a source note says when
more commits landed than the feed lists), linked Wikipedia articles and the
public `news.json` problem links become “New reports (unreviewed)”. Wikipedia
reports carry no article text (the quote stays in `state.json`), wait 12 hours
before triage and are withdrawn when the edit is reverted. Each source fails
closed and keeps its last good result; errors are listed in `source_errors`,
including `kimi-k3` when the broker is down or refusing. Kimi K3 (client id
`ansatze-problems`, through the loopback broker) only labels candidate matches
with a verbatim quote: at most 8 calls per run and
`ANSATZE_PROBLEMS_MODEL_CALLS_PER_DAY` (default 48; `0` disables) per UTC day,
counted in `state.json` because the broker’s own counters reset on restart. It
backs off for the rest of a run when the broker is busy or over quota; arXiv
entries it could not classify wait for the next run.

Review with `scripts/review_problem_status.py list --overlay
/var/lib/ansatze-problems/problem-status.json`, then `show`, `accept`, `set`,
`reject` or `revoke`; decisions go to
`agent_monitor/data/problem_status_decisions.json` and take effect after
`scripts/build_problem_catalogue.py`, a commit and a docs build; `show --state
/var/lib/ansatze-problems/state.json` prints a Wikipedia report’s private quote.
A dry run on copied state: `scripts/update_problem_status.py --output
/tmp/x/out.json --state /tmp/x/state.json --dry-run` (a dry run writes nothing
and never calls the model; to try the model, run without `--dry-run` against
copies). `--only <sources>` checks some sources; the others keep their last
published entries and status.

Two source notes need a person. A newer Kourovka edition whose parse loses
problems or ∗/♯ markers in bulk (or gains markers in bulk, or grows parse
anomalies) is refused with a
`kourovka` source error: check the PDF, then rebuild the fact file with
`scripts/refresh_problem_sources.py --kourovka-pdf … --force` and the catalogue.
An `openai-math` note about withdrawals that match no manuscript means
`history.md` lists a title missing from the pinned contents (added later, or
worded differently); the matched withdrawals still apply. Refresh
`openai_math.json` (with `--openai-previous-commit` for an intermediate
revision if needed) and rebuild.

Check with `systemctl status ansatze-problems.timer`,
`journalctl -u ansatze-problems.service` (one JSON line per run: entries,
signals, source errors, model calls) and `curl -sI
https://ansatze.ai/research/problem-status.json`. Stop the timer to pause; the
last overlay stays published. A newer Kourovka edition needs pypdf in the venv
(`pyproject.toml` pins it); without it that source reports an error and the
others continue.

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
`scripts/import_informal_atlas.py`. The ansatze.ai Caddyfile compresses
responses and serves the prebuilt `research/*.json.gz` files directly. The catalogue is source literature, separate from model
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

## Claude Code pinning

The console runs Claude Code with `DISABLE_AUTOUPDATER=1`, but the binary it
finds on the service `PATH` was `~/.local/bin/claude`, a symlink into the
standalone `~/.local/share/claude/versions/` tree that the owner's own
interactive Claude Code sessions update in place, so the console's Claude Code
version could change without a deploy. Pin it like Codex, user-level and first
on the service `PATH` (the unit template puts `~/.npm-global/bin` first):

```bash
PATH=/home/ubuntu/.local/node24/bin:$PATH \
  npm i -g --prefix ~/.npm-global @anthropic-ai/claude-code@2.1.283
~/.npm-global/bin/claude --version       # 2.1.283 (Claude Code)
```

2.1.283 is the build the live console used on 2026-09-27. After pinning, run
one Claude-subscription Plain run and one Math Harness run before relying on
it, and record any newer vetted build here before switching.

## Codex vetting

The hosted response-only Codex adapter is vetted against exactly
`codex-cli 0.153.4`, `codex-cli 0.157.1` and `codex-cli 0.160.0`
(`RESPONSE_ONLY_CODEX_CLI_VERSIONS` in `agent_monitor/runners/codex_backend.py`);
any other build makes Codex-subscription Plain, Math Harness and derived
Lean/DAG calls fail closed with an error naming the binary it found. Install the
pinned build user-level and keep it first on the service `PATH` (the unit
template puts `~/.npm-global/bin` first; engine discovery searches it first):

```bash
PATH=/home/ubuntu/.local/node24/bin:$PATH \
  npm i -g --prefix ~/.npm-global @openai/codex@0.160.0
~/.npm-global/bin/codex --version        # codex-cli 0.160.0
```

Do not rely on `~/.local/bin/codex`: it points at the standalone
`~/.codex/packages/standalone/current` tree, which can self-update past the pin.
To re-vet a build, put it first on `PATH` and run
`tests/test_codex_response_only_contract.py`; its live test drives the real CLI
against a loopback capture provider and fails if any tool reaches the model,
either in the top-level `tools` field or inside an `input` item
(`additional_tools`). Only then add the version to the exact pin set. The
adapter's contract is `RESPONSE_ONLY_TOOL_ISOLATION_VERSION = 5`; Math
Harness receipts record it, and Codex-route receipts that claim version 4 were
written before the catalog fix (when "responses lite" models could still
receive code-mode and multi-agent tools), so they are no longer attested.
After a
deploy, run one real Codex-subscription Plain run on `gpt-5.6-sol` and one on
`gpt-5.5` and confirm both complete (the capture provider cannot show whether
the real backend accepts the tool-free request shape). Tests and narrow
hosted-source patches for the earlier release are retained in
`artifacts/dag10k-update-2026-09-10/`. The older workspace backend is not a
drop-in replacement for the hosted backend.

## Find the live wiring

| Component | Where |
|---|---|
| Code and virtualenv | `/home/repo/agent_monitor_math` (this checkout, `.venv` on Python 3.13) |
| Console | `agent-monitor.service`, user `ubuntu`, `127.0.0.1:4600`, data in `/var/lib/ansatze/console` |
| Kimi key holder | `ansatze-kimi.service`, user `ansatze-kimi`, `127.0.0.1:4610`, data in `/var/lib/ansatze/kimi` |
| Credentials | `/etc/ansatze/credentials/` (root 0400; loaded with `LoadCredential=`) |
| Console settings | `/etc/ansatze/console.env` (root-managed, no provider keys) |
| Console mount | `/etc/ansatze/mount` |
| Edge | `/etc/caddy/Caddyfile`; backups in `/etc/ansatze/backup/` |
| Lean checks | `/run/agent-monitor-lean/{core,mathlib}.sock` (see `deploy/lean-broker/`) |
| Static site | `/home/repo/agent_monitor_math/docs/.vitepress/dist` |

On the host, inspect the effective configuration instead of guessing:

```bash
sudo systemctl cat agent-monitor.service ansatze-kimi.service
sudo systemctl show agent-monitor.service \
  -p FragmentPath -p DropInPaths -p WorkingDirectory -p ExecStart
sudo sed -n '1,240p' /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
deploy/ansatze/smoke.sh
```

A Python change in this checkout is live only after
`sudo systemctl restart ansatze-kimi agent-monitor`; imported modules stay in
memory until then. Edge or unit changes go through the templates in
`deploy/ansatze/` and `sudo deploy/ansatze/install.sh --steps render,services,caddy`.

## Publish documentation

Install dependencies once with `npm ci`, then publish with:

```bash
npm run docs:build
```

The build is rendered into a staging directory and atomically exchanged with
the directory Caddy serves, so readers do not see a half-built site. The swap
uses `mv --exchange` where available (GNU coreutils 9.5+), otherwise the Linux
`renameat2(RENAME_EXCHANGE)` call, and only as a last resort two renames.

After changing routing, validate before reloading Caddy and smoke-test the
public surface:

```bash
sudo deploy/ansatze/install.sh --steps render,caddy
curl -I https://ansatze.ai/
curl -I https://ansatze.ai/news
curl -I https://ansatze.ai/sign-in
deploy/ansatze/smoke.sh --www --public
```

Do not put API keys, password-reset tokens, the console mount, or DNS provider
credentials in this file. Production secrets are loaded through encrypted
systemd credentials; local development values belong only in ignored local
configuration.
