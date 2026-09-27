#!/usr/bin/env python3
"""Promote the two reviewed monitor HTML files without restarting active runs."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

REPO = Path(__file__).resolve().parent.parent
LIVE = Path('/home/ubuntu/agent_monitor_math')
ALLOWED = {'agent_monitor/web/console.html', 'monitor_core/harness_dashboard/web/index.html'}


def digest(content):
    return hashlib.sha256(content).hexdigest()


def replace_file(path, content, mode):
    descriptor, temporary = tempfile.mkstemp(prefix='.monitor-theme-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    if set(manifest['files']) != ALLOWED:
        raise SystemExit('Promotion must contain exactly the two monitor HTML files.')
    reviewed = []
    for relative, entry in manifest['files'].items():
        payload, target = Path(entry['payload']), LIVE / relative
        if payload.is_symlink() or target.is_symlink() or target.resolve() != target:
            raise SystemExit('Monitor promotion does not accept symlinks.')
        content, previous = payload.read_bytes(), target.read_bytes()
        if digest(content) != entry['sha256'] or digest(previous) != entry['previous_sha256']:
            raise SystemExit(f'{relative} changed since review.')
        if b'</html>' not in content or b'<script' not in content:
            raise SystemExit(f'{relative} is not a complete HTML document.')
        reviewed.append((relative, target, content, previous, target.stat().st_mode & 0o777))
    if not args.apply:
        print(json.dumps({'ok': True, 'files': sorted(ALLOWED), 'service_restart': False, 'applied': False}))
        return
    backup = REPO / 'data/deployment_backups' / time.strftime('monitor-theme-%Y%m%dT%H%M%SZ', time.gmtime())
    backup.mkdir(parents=True, mode=0o700)
    for relative, _, _, previous, _ in reviewed:
        path = backup / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(previous)
    changed = []
    try:
        for relative, target, content, previous, mode in reviewed:
            if target.read_bytes() != previous:
                raise RuntimeError(f'{relative} changed during promotion.')
            replace_file(target, content, mode)
            changed.append((target, content, previous, mode))
        for _, target, content, _, _ in reviewed:
            if target.read_bytes() != content:
                raise RuntimeError(f'{target} differs after promotion.')
    except Exception:
        for target, content, previous, mode in reversed(changed):
            if target.read_bytes() == content:
                replace_file(target, previous, mode)
        raise
    print(json.dumps({'ok': True, 'files': sorted(ALLOWED), 'backup': str(backup), 'service_restart': False, 'applied': True}))


if __name__ == '__main__':
    main()
