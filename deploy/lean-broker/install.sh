#!/bin/bash
set -euo pipefail

umask 022

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
toolchain_source=
mathlib_source=
activate=0
expected_toolchain_tree_sha256=
expected_mathlib_tree_sha256=

usage() {
  echo "Usage: sudo $0 --toolchain PATH --mathlib PATH \\"
  echo "  --expected-toolchain-tree-sha256 HEX --expected-mathlib-tree-sha256 HEX [--activate]"
  echo
  echo "Without --activate, install immutable files only; do not reload or start units."
  echo "With --activate, start both sockets, smoke them, then stage NoNewPrivileges"
  echo "for agent-monitor.service. The application service is never restarted here."
}

while (($#)); do
  case "$1" in
    --toolchain)
      (($# >= 2)) || { usage >&2; exit 2; }
      toolchain_source=$2
      shift 2
      ;;
    --mathlib)
      (($# >= 2)) || { usage >&2; exit 2; }
      mathlib_source=$2
      shift 2
      ;;
    --activate)
      activate=1
      shift
      ;;
    --expected-toolchain-tree-sha256)
      (($# >= 2)) || { usage >&2; exit 2; }
      expected_toolchain_tree_sha256=$2
      shift 2
      ;;
    --expected-mathlib-tree-sha256)
      (($# >= 2)) || { usage >&2; exit 2; }
      expected_mathlib_tree_sha256=$2
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ ${EUID} -ne 0 ]]; then
  echo "This installer must run as root." >&2
  exit 1
fi
if [[ -z ${toolchain_source} || -z ${mathlib_source} || \
      -z ${expected_toolchain_tree_sha256} || -z ${expected_mathlib_tree_sha256} ]]; then
  usage >&2
  exit 2
fi

for command in install groupadd getent; do
  command -v "${command}" >/dev/null || {
    echo "Required command is unavailable: ${command}" >&2
    exit 1
  }
done

if ! getent group agent-monitor-lean >/dev/null; then
  groupadd --system agent-monitor-lean
fi

install -D -o root -g root -m 0555 \
  "${script_dir}/agent_monitor_lean_broker_worker.py" \
  /usr/libexec/agent-monitor-lean-broker
install -D -o root -g root -m 0555 \
  "${script_dir}/prepare_release.py" \
  /usr/libexec/agent-monitor-lean-prepare-release
install -D -o root -g root -m 0555 \
  "${script_dir}/smoke_client.py" \
  /usr/libexec/agent-monitor-lean-broker-smoke
install -D -o root -g root -m 0444 \
  "${script_dir}/README.md" \
  /usr/share/doc/agent-monitor-lean-broker/README.md

release_id=$(
  /usr/libexec/agent-monitor-lean-prepare-release \
    --toolchain "${toolchain_source}" \
    --mathlib "${mathlib_source}" \
    --expected-toolchain-tree-sha256 "${expected_toolchain_tree_sha256}" \
    --expected-mathlib-tree-sha256 "${expected_mathlib_tree_sha256}"
)
if [[ ! ${release_id} =~ ^[0-9a-f]{64}$ ]]; then
  echo "Release preparation returned an invalid identity." >&2
  exit 1
fi

for unit in \
  agent-monitor-lean-core.socket \
  agent-monitor-lean-core@.service \
  agent-monitor-lean-mathlib.socket \
  agent-monitor-lean-mathlib@.service; do
  install -D -o root -g root -m 0444 \
    "${script_dir}/${unit}" "/etc/systemd/system/${unit}"
done

echo "Prepared immutable Lean broker release ${release_id}."
if [[ ${activate} -eq 0 ]]; then
  echo "No units were reloaded, enabled, started, or restarted."
  echo "Re-run with --activate after reviewing the installed release and units."
  exit 0
fi

command -v systemctl >/dev/null || {
  echo "systemctl is required for --activate." >&2
  exit 1
}
systemctl daemon-reload
systemctl enable --now \
  agent-monitor-lean-core.socket \
  agent-monitor-lean-mathlib.socket

# This must succeed before the application loses all privilege-escalation paths.
/usr/libexec/agent-monitor-lean-broker-smoke

install -d -o root -g root -m 0755 \
  /etc/systemd/system/agent-monitor.service.d
install -o root -g root -m 0444 \
  "${script_dir}/agent-monitor-lean-client.conf" \
  /etc/systemd/system/agent-monitor.service.d/lean-broker.conf
systemctl daemon-reload

echo "Both brokers passed. NoNewPrivileges is staged for agent-monitor.service."
echo "The application was not restarted; restart it only after client integration."
