#!/bin/bash
set -euo pipefail

umask 077

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
release_source=
expected_tree_sha256=
credential_source=
base_source="${script_dir}/kimi_api_base.default"
replace_credential=0
activate=0

usage() {
  echo "Usage: sudo $0 --release-source PATH --expected-tree-sha256 HEX \\"
  echo "  --credential-file PATH [--base-url-file PATH] [--replace-credential] [--activate]"
  echo
  echo "The default stages an immutable release, credentials, and one new unit only."
  echo "It does not reload, enable, start, restart, or change Caddy or agent-monitor.service."
  echo "--activate reloads systemd and enables/restarts only proving-kimi-public.service."
}

while (($#)); do
  case "$1" in
    --release-source)
      (($# >= 2)) || { usage >&2; exit 2; }
      release_source=$2
      shift 2
      ;;
    --expected-tree-sha256)
      (($# >= 2)) || { usage >&2; exit 2; }
      expected_tree_sha256=$2
      shift 2
      ;;
    --credential-file)
      (($# >= 2)) || { usage >&2; exit 2; }
      credential_source=$2
      shift 2
      ;;
    --base-url-file)
      (($# >= 2)) || { usage >&2; exit 2; }
      base_source=$2
      shift 2
      ;;
    --replace-credential)
      replace_credential=1
      shift
      ;;
    --activate)
      activate=1
      shift
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
if [[ -z ${release_source} || -z ${expected_tree_sha256} || -z ${credential_source} ]]; then
  usage >&2
  exit 2
fi
if [[ ! ${expected_tree_sha256} =~ ^[0-9a-f]{64}$ ]]; then
  echo "--expected-tree-sha256 must be lowercase SHA-256." >&2
  exit 2
fi

for command in install mktemp sed stat cmp grep rm; do
  command -v "${command}" >/dev/null || {
    echo "Required command is unavailable: ${command}" >&2
    exit 1
  }
done

validate_bounded_regular() {
  local source=$1
  local label=$2
  local maximum=$3
  if [[ -L ${source} || ! -f ${source} ]]; then
    echo "${label} must be a regular, non-symlink file." >&2
    exit 1
  fi
  local size
  size=$(stat -c %s -- "${source}")
  if ((size < 1 || size > maximum)); then
    echo "${label} has an invalid size." >&2
    exit 1
  fi
}

validate_bounded_regular "${credential_source}" "Kimi credential" 4096
validate_bounded_regular "${base_source}" "Kimi base URL" 2048

install -D -o root -g root -m 0555 \
  "${script_dir}/prepare_release.py" \
  /usr/libexec/proving-kimi-public-prepare-release
install -D -o root -g root -m 0444 \
  "${script_dir}/README.md" \
  /usr/share/doc/proving-kimi-public/README.md
install -D -o root -g root -m 0444 \
  "${script_dir}/Caddyfile.example" \
  /usr/share/doc/proving-kimi-public/Caddyfile.example

release_id=$(
  /usr/libexec/proving-kimi-public-prepare-release \
    --source "${release_source}" \
    --expected-tree-sha256 "${expected_tree_sha256}"
)
if [[ ${release_id} != "${expected_tree_sha256}" ]]; then
  echo "Release preparer returned an unexpected identity." >&2
  exit 1
fi
release_root="/opt/proving-kimi-public/releases/${release_id}"

credentials_dir=/etc/proving-kimi-public/credentials
install -d -o root -g root -m 0700 "${credentials_dir}"
credential_target="${credentials_dir}/kimi_api_key"
if [[ -e ${credential_target} && ${replace_credential} -ne 1 ]]; then
  if ! cmp -s -- "${credential_source}" "${credential_target}"; then
    echo "A different public Kimi credential is already staged." >&2
    echo "Review it, then re-run with --replace-credential to rotate explicitly." >&2
    exit 1
  fi
else
  install -o root -g root -m 0400 \
    "${credential_source}" "${credential_target}"
fi
base_target="${credentials_dir}/kimi_api_base"
if [[ ! -e ${base_target} || ! ${base_source} -ef ${base_target} ]]; then
  install -o root -g root -m 0400 \
    "${base_source}" "${base_target}"
fi

unit_tmp=$(mktemp /tmp/proving-kimi-public.XXXXXX.service)
trap 'rm -f -- "${unit_tmp}"' EXIT
sed "s|@@RELEASE_ROOT@@|${release_root}|g" \
  "${script_dir}/proving-kimi-public.service.in" >"${unit_tmp}"
if grep -q '@@RELEASE_ROOT@@' "${unit_tmp}"; then
  echo "Unit template substitution was incomplete." >&2
  exit 1
fi
if command -v systemd-analyze >/dev/null; then
  systemd-analyze verify "${unit_tmp}"
fi
install -o root -g root -m 0444 \
  "${unit_tmp}" /etc/systemd/system/proving-kimi-public.service

echo "Staged public Kimi release ${release_id}."
echo "Private agent-monitor.service and Caddy were not modified or restarted."
if [[ ${activate} -eq 0 ]]; then
  echo "No unit was reloaded, enabled, started, or restarted."
  echo "Review /etc/systemd/system/proving-kimi-public.service and the Caddy template."
  exit 0
fi

for command in systemctl curl sleep; do
  command -v "${command}" >/dev/null || {
    echo "${command} is required for --activate." >&2
    exit 1
  }
done
systemctl daemon-reload
systemctl enable proving-kimi-public.service
systemctl restart proving-kimi-public.service
healthy=0
for ((attempt = 0; attempt < 20; attempt++)); do
  if curl --fail --silent --max-time 1 \
    http://127.0.0.1:4610/healthz >/dev/null 2>&1; then
    healthy=1
    break
  fi
  sleep 0.5
done
if [[ ${healthy} -ne 1 ]]; then
  echo "Public Kimi health check failed; Caddy was not changed." >&2
  exit 1
fi

echo "Public Kimi backend is healthy on 127.0.0.1:4610."
echo "Caddy remains unchanged; merge and validate the separate-host template manually."
