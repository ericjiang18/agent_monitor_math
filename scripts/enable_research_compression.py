#!/usr/bin/env python3
"""Apply the reviewed compression-only proxy change for the public atlas."""
import difflib
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import time


LIVE = Path('/etc/caddy/Caddyfile')
BASELINE = '76c5594350cfd63cba8fd0ad96480e55fd252a350e55c071499d242f81ddd5b4'
ANCHOR = '54-82-94-57.sslip.io, 54-235-45-210.sslip.io {\n'
ADDITION = '\t# Compress the public research catalogues for mobile downloads.\n\t@research_catalogues path /research/*.json\n\tencode @research_catalogues zstd gzip\n'


def run(*args):
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f'{args[0]} failed: {result.stderr[-1500:]}')
    return result


def main():
    original = LIVE.read_bytes()
    if hashlib.sha256(original).hexdigest() != BASELINE:
        raise SystemExit('Proxy configuration changed since review; re-review before applying.')
    text = original.decode()
    if text.count(ANCHOR) != 1:
        raise SystemExit('Expected public site not found exactly once.')
    proposed = text.replace(ANCHOR, ANCHOR + ADDITION)
    artifact = Path(__file__).resolve().parent.parent / 'artifacts/dag10k-update-2026-09-10/research-compression.patch'
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(''.join(difflib.unified_diff(text.splitlines(True), proposed.splitlines(True), fromfile='a/Caddyfile', tofile='b/Caddyfile')))
    with tempfile.TemporaryDirectory(prefix='ansatz-research-proxy-') as directory:
        candidate = Path(directory) / 'Caddyfile'
        candidate.write_text(proposed)
        run('caddy', 'adapt', '--config', str(candidate), '--adapter', 'caddyfile')
        if '--apply' not in sys.argv[1:]:
            print('Reviewed compression-only configuration parses successfully.')
            return
        run('sudo', '-n', 'caddy', 'validate', '--config', str(candidate), '--adapter', 'caddyfile')
        if LIVE.read_bytes() != original:
            raise SystemExit('Proxy configuration changed during validation.')
        backup = LIVE.with_name('Caddyfile.before-research-compression-' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()))
        run('sudo', '-n', 'cp', '--no-clobber', str(LIVE), str(backup))
        try:
            run('sudo', '-n', 'install', '-m', oct(LIVE.stat().st_mode & 0o777)[2:], str(candidate), str(LIVE))
            run('sudo', '-n', 'systemctl', 'reload', 'caddy')
        except BaseException:
            run('sudo', '-n', 'cp', str(backup), str(LIVE))
            run('sudo', '-n', 'systemctl', 'reload', 'caddy')
            raise
        print(f'Public research JSON compression enabled; backup: {backup}')


if __name__ == '__main__':
    main()
