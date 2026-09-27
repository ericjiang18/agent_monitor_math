#!/usr/bin/env python3
"""Publish the reviewed static problem data to the hosted checkout, atomically."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time

REPO = Path(__file__).resolve().parent.parent
LIVE = Path('/home/ubuntu/agent_monitor_math/agent_monitor/data/open_problems.json')
PAYLOAD = REPO / 'agent_monitor/data/open_problems.json'


def digest(content):
    return hashlib.sha256(content).hexdigest()


def main():
    manifest = json.loads(Path(sys.argv[1]).read_text())
    if Path(manifest['payload']) != PAYLOAD or PAYLOAD.is_symlink() or LIVE.is_symlink():
        raise SystemExit('Unexpected catalogue payload or symlink.')
    content, previous = PAYLOAD.read_bytes(), LIVE.read_bytes()
    if digest(content) != manifest['sha256'] or digest(previous) != manifest['previous_sha256']:
        raise SystemExit('Catalogue changed since review.')
    if content != (REPO / 'docs/public/research/open-problems.json').read_bytes():
        raise SystemExit('Public and hosted catalogue payloads differ.')
    data = json.loads(content)
    compact = data['compact_families']
    if (data['schema_version'] != 2 or data['total'] != manifest['total']
            or data['total'] != len(data['problems']) + sum(len(f['cases']) for f in compact)
            or any(f['encoding'] != 'difference-sets-v1' for f in compact)):
        raise SystemExit('Unexpected catalogue structure or count.')
    if '--apply' not in sys.argv[2:]:
        print(json.dumps({'ok': True, 'total': data['total'], 'service_restart': False}))
        return
    backup = REPO / 'data/deployment_backups' / time.strftime('problems-%Y%m%dT%H%M%SZ', time.gmtime())
    backup.mkdir(parents=True, mode=0o700)
    with (backup / 'open_problems.json').open('xb') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(previous)
    descriptor, temporary = tempfile.mkstemp(prefix='.problems-update-', dir=LIVE.parent)
    try:
        os.fchmod(descriptor, LIVE.stat().st_mode & 0o777)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if LIVE.read_bytes() != previous:
            raise RuntimeError('Catalogue changed during promotion.')
        os.replace(temporary, LIVE)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)
    print(json.dumps({'ok': True, 'total': data['total'], 'backup': str(backup), 'service_restart': False}))


if __name__ == '__main__':
    main()
