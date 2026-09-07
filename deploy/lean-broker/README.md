# Fixed-interface Lean checker broker

This deployment replaces per-request `sudo systemd-run` with two root-owned,
socket-activated services.  `agent-monitor.service` only opens a Unix socket and
sends one bounded UTF-8 Lean source frame.  It never chooses an executable,
argument, filesystem path, environment, server timeout, or resource limit.

## Profiles

- `/run/agent-monitor-lean/core.sock` always runs the exact provisioned Lean
  4.14.0 executable directly.
- `/run/agent-monitor-lean/mathlib.sock` always runs the exact provisioned Lake
  executable as `lake --dir <immutable-mirror> env <exact-lean> <private-source>`.

The worker accepts one request and emits one length-prefixed JSON response.
Lean stdout and stderr first go to bounded regular files in the worker's private
`/tmp`; they never share the protocol stream.  There is no unsandboxed fallback.

Every accepted connection starts a fresh `DynamicUser` service with:

- `NoNewPrivileges=yes`, empty capabilities, and no SUID/SGID execution;
- `PrivateNetwork=yes`, `RestrictAddressFamilies=none`, and
  `SystemCallFilter=~@network-io` (plain `read`/`write` on the inherited socket
  remain available, while new socket operations do not);
- `PrivatePIDs=yes`, invisible restricted `/proc`, `ProtectHome=yes`, and
  `InaccessiblePaths=/run` (the already inherited socket remains usable);
- a strict read-only host filesystem, a private temporary directory, no devices,
  namespace restrictions, and fixed cgroup/runtime/file/task limits.

The socket is `root:agent-monitor-lean` mode `0660`.  A systemd drop-in grants
only `agent-monitor.service` that supplementary group and sets
`NoNewPrivileges=yes`.  The installer does not place that drop-in until both
broker profiles pass their isolated smoke checks.

## Immutable release preparation

The approved identities are deliberately fixed in code:

- Lean `4.14.0`, commit `410fab728470`, Release;
- `leanprover/lean4:v4.14.0`;
- Mathlib input revision `v4.14.0`, commit
  `4bbdccd9c5f862bf90ff12f0a9e2c8be032b9a84`.

`prepare_release.py` never executes a source-tree binary as root.  It rejects
special files, mount crossings, escaping/directory symlinks, and inode changes.
Internal symlinks to same-device regular files are copied as regular files.
All content is hashed during the copy.  The result is atomically published at
`/opt/agent-monitor-lean/releases/<content-id>` with root ownership, `0555`
directories/executables, and `0444` other regular files.  Root-owned
environment files point each service at that exact content-addressed release.
The first sandboxed request runs `lean --version` and requires the full approved
version/commit string; Mathlib metadata is rechecked for every Mathlib worker.

Publication also requires two lowercase SHA-256 tree pins supplied by the
administrator. Obtain those pins from a previously reviewed/offline provenance
record. `prepare_release.py --measure-only` can produce a candidate measurement
without executing either tree, but measuring and trusting the same mutable tree
in one step is continuity checking, not independent provenance. Mathlib's Git
commit and the Lean release artifact should be checked through a separate
trusted channel before accepting new pins.

Stop all processes capable of modifying the source trees before preparing the
release.  The copy checks for replacements but a quiescent source is still an
important supply-chain prerequisite.

## Install without activation

Review the scripts and units, then run from the repository:

```bash
sudo deploy/lean-broker/install.sh \
  --toolchain /home/ubuntu/.elan/toolchains/leanprover--lean4---v4.14.0 \
  --mathlib /home/ubuntu/.cache/agent-monitor/mathlib-v4.14.0 \
  --expected-toolchain-tree-sha256 TOOLCHAIN_PIN \
  --expected-mathlib-tree-sha256 MATHLIB_PIN
```

The default invocation copies and installs files only.  It does not reload,
enable, start, or restart any unit.

After integrating `agent_monitor.lean_broker.check_source` and reviewing the
content-addressed release, activate explicitly:

```bash
sudo deploy/lean-broker/install.sh \
  --toolchain /home/ubuntu/.elan/toolchains/leanprover--lean4---v4.14.0 \
  --mathlib /home/ubuntu/.cache/agent-monitor/mathlib-v4.14.0 \
  --expected-toolchain-tree-sha256 TOOLCHAIN_PIN \
  --expected-mathlib-tree-sha256 MATHLIB_PIN \
  --activate
```

Activation starts only the two sockets, performs core and narrow-Mathlib smoke
checks, then stages the application `NoNewPrivileges` drop-in.  It deliberately
does not restart `agent-monitor.service`; that remains a separate reviewed
deployment step.

## Application contract

Use only:

```python
from agent_monitor.lean_broker import check_source

result = check_source(source, uses_mathlib=False)
```

`uses_mathlib=True` selects the other fixed socket.  Client timeouts only limit
how long the application waits; they are not transmitted and cannot expand the
server's fixed 60/90-second limits.  Treat any connection, framing, attestation,
or validation error as unavailable.  Never fall back to local Lean,
`sudo`, or `systemd-run` for an authoritative check.

Successful responses also contain `release_id`,
`toolchain_tree_sha256`, and `mathlib_tree_sha256`. The client reconstructs the
canonical identity document and rejects the response unless its SHA-256 equals
`release_id`; persist these fields in checker/KG cache provenance.
