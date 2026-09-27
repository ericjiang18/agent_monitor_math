#!/usr/bin/env python3
"""Promote only a reviewed, hash-bound set of console source files.

The manifest is created from the reconciled live candidate. This intentionally
does not copy a checkout, modify credentials, or alter proxy/service settings.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request

REPO = Path(__file__).resolve().parent.parent
LIVE = Path('/home/ubuntu/agent_monitor_math')
SERVICE = 'agent-monitor.service'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def systemctl(action):
    subprocess.run(['sudo', '-n', 'systemctl', action, SERVICE], check=True)


def install(path, content, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.website-update-', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main():
    manifest_path = Path(sys.argv[1]).resolve()
    manifest = json.loads(manifest_path.read_text())
    candidate = Path(manifest['candidate']).resolve()
    entries = manifest['files']
    if not entries or len(entries) > 30:
        raise SystemExit('Unexpected promotion scope.')
    for item in entries:
        relative = Path(item['path'])
        if relative.is_absolute() or '..' in relative.parts or relative.parts[0] != 'agent_monitor':
            raise SystemExit('Invalid promotion path.')
        source, destination = candidate / relative, LIVE / relative
        if source.is_symlink() or destination.is_symlink():
            raise SystemExit(f'Symlink in promotion scope: {relative}')
        if digest(source) != item['sha256'] or digest(destination) != item['previous_sha256']:
            raise SystemExit(f'Source changed since review: {relative}')
    pid = int(subprocess.check_output(['systemctl', 'show', SERVICE, '--property=MainPID', '--value'], text=True).strip())
    if not pid:
        raise SystemExit('The expected console is not running; inspect before promotion.')
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():
            continue
        try:
            parent = int((process / 'stat').read_text().rsplit(')', 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        if parent == pid:
            raise SystemExit('The console has an active child process. Retry after its run finishes.')
    if '--check-only' in sys.argv[2:]:
        print(json.dumps({'ok': True, 'reviewed_files': len(entries), 'active_child_processes': 0}))
        return
    backup = REPO / 'data' / 'deployment_backups' / time.strftime('website-%Y%m%dT%H%M%SZ', time.gmtime())
    backup.mkdir(parents=True, mode=0o700)
    records = []
    for item in entries:
        destination = LIVE / item['path']
        record = {'path': item['path'], 'existed': destination.exists(), 'mode': destination.stat().st_mode & 0o777 if destination.exists() else 0o644}
        records.append(record)
        if record['existed']:
            install(backup / item['path'], destination.read_bytes(), 0o600)
    (backup / 'manifest.json').write_text(json.dumps({'files': records, 'review': manifest}, indent=2))
    changed = []
    systemctl('stop')
    try:
        for record in records:
            relative = record['path']
            install(LIVE / relative, (candidate / relative).read_bytes(), record['mode'])
            changed.append(record)
        systemctl('start')
        health = manifest['health_url']
        for _ in range(30):
            try:
                with urllib.request.urlopen(health, timeout=2) as response:
                    if json.load(response).get('ok'):
                        break
            except (OSError, ValueError):
                time.sleep(1)
        else:
            raise RuntimeError('The updated console did not pass its health check.')
    except BaseException:
        systemctl('stop')
        for record in reversed(changed):
            destination = LIVE / record['path']
            if record['existed']:
                install(destination, (backup / record['path']).read_bytes(), record['mode'])
            else:
                destination.unlink(missing_ok=True)
        systemctl('start')
        raise
    print(json.dumps({'ok': True, 'files_promoted': len(changed), 'backup': str(backup)}))


if __name__ == '__main__':
    main()
