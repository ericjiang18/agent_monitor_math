#!/usr/bin/env python3
"""Atomically update only the reviewed, static informal-atlas index."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parent.parent
LIVE = Path('/home/ubuntu/agent_monitor_math/agent_monitor/data/informal-atlas.json')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def main():
    manifest = json.loads(Path(sys.argv[1]).read_text())
    payload = Path(manifest['payload'])
    if payload.is_symlink() or LIVE.is_symlink():
        raise SystemExit('Atlas index promotion does not accept symlinks.')
    content, previous = payload.read_bytes(), LIVE.read_bytes()
    if digest(content) != manifest['sha256'] or digest(previous) != manifest['previous_sha256']:
        raise SystemExit('Atlas index changed since review.')
    nodes = json.loads(content)['nodes']
    if len(nodes) != manifest['nodes'] or len({n['id'] for n in nodes}) != len(nodes):
        raise SystemExit('Atlas index count or uniqueness failed.')
    if any(n['layer'] != 'informal' or n['memory_eligible'] is not False for n in nodes):
        raise SystemExit('Source catalogue attribution changed.')
    if '--apply' not in sys.argv[2:]:
        print(json.dumps({'ok': True, 'nodes': len(nodes), 'service_restart': False}))
        return
    backup = REPO / 'data/deployment_backups' / time.strftime('atlas-%Y%m%dT%H%M%SZ', time.gmtime())
    backup.mkdir(parents=True, mode=0o700)
    with (backup / 'informal-atlas.json').open('xb') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(previous)
    descriptor, temporary = tempfile.mkstemp(prefix='.atlas-update-', dir=LIVE.parent)
    try:
        os.fchmod(descriptor, LIVE.stat().st_mode & 0o777)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if LIVE.read_bytes() != previous:
            raise RuntimeError('Atlas index changed during promotion.')
        os.replace(temporary, LIVE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(json.dumps({'ok': True, 'nodes': len(nodes), 'backup': str(backup), 'service_restart': False}))


if __name__ == '__main__':
    main()
