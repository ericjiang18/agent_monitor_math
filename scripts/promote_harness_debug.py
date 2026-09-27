#!/usr/bin/env python3
"""Publish the reviewed harness fixes, restarting only an idle console."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
LIVE = Path('/home/ubuntu/agent_monitor_math')
REVIEW = ROOT / 'artifacts/harness-debug-2026-09-10'
SERVICE = 'agent-monitor.service'
ALLOWED = {
    'agent_monitor/process_control.py', 'agent_monitor/jobs.py',
    'agent_monitor/cli_events.py', 'agent_monitor/lean_verify.py',
    'agent_monitor/runners/_stream.py', 'agent_monitor/runners/codex_backend.py',
    'agent_monitor/runners/claude_backend.py', 'agent_monitor/runners/hermes.py',
    'engines/hermes_core/agent/codex_runtime.py',
    'engines/hermes_core/agent/transports/codex_app_server_session.py',
}

def sha(payload):
    return hashlib.sha256(payload).hexdigest() if payload is not None else None

def content(path):
    return path.read_bytes() if path.exists() else None

def replace(path, payload, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.harness-update-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def systemctl(action):
    subprocess.run(['sudo', '-n', 'systemctl', action, SERVICE], check=True, timeout=45)

def service_pid():
    return int(subprocess.check_output(['systemctl', 'show', SERVICE, '-p', 'MainPID', '--value'], text=True, timeout=10))

def require_idle():
    pid = service_pid()
    if not pid:
        raise RuntimeError('The console service is not running.')
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            fields = (proc / 'stat').read_text().rpartition(') ')[2].split()
            parent = int(fields[1])
        except (OSError, ValueError, IndexError):
            continue
        if parent == pid and fields[0] != 'Z':
            raise RuntimeError('The console has an active child; leave its run uninterrupted.')
    for path in (ROOT / 'data/cache/harness').glob('*.json'):
        if time.time() - path.stat().st_mtime > 180:
            continue
        record = json.loads(path.read_text())
        if record.get('status') in {'running', 'queued', 'starting'}:
            raise RuntimeError('A run is active; leave it uninterrupted and retry later.')
    return pid

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.manifest.resolve() != REVIEW / 'manifest.json':
        raise RuntimeError('Unexpected review manifest.')
    manifest = json.loads(args.manifest.read_text())
    entries = manifest['files']
    if len(entries) != len(ALLOWED) or {x['path'] for x in entries} != ALLOWED:
        raise RuntimeError('Unexpected publication scope.')
    suites = list(ET.parse(REVIEW / 'final-regression.xml').iter('testsuite'))
    if not suites or any(int(s.get(k, 0)) for s in suites for k in ('errors','failures','skipped')):
        raise RuntimeError('Regression checks are not all passing.')
    if sum(int(s.get('tests', 0)) for s in suites) < 500:
        raise RuntimeError('Incomplete regression suite.')
    if not json.loads((REVIEW / 'native-protocol-report.json').read_text()).get('ok'):
        raise RuntimeError('Native protocol checks are not passing.')
    reviewed = []
    for item in entries:
        name = item['path']
        source = REVIEW / 'candidate' / name
        target = LIVE / name
        if source.is_symlink() or target.is_symlink() or target.resolve() != target:
            raise RuntimeError('Symlink in publication scope.')
        payload, previous = source.read_bytes(), content(target)
        if sha(payload) != item['sha256'] or sha(previous) != item['previous_sha256']:
            raise RuntimeError(name + ' changed since review.')
        compile(payload, name, 'exec')
        reviewed.append((name, target, payload, previous, target.stat().st_mode & 0o777 if target.exists() else 0o644))
    old_pid = require_idle()
    if not args.apply:
        print(json.dumps({'ok':True,'applied':False,'files':len(reviewed),'active_runs':0,'restart_required':True}))
        return
    backup = ROOT / 'data/deployment_backups' / time.strftime('harness-debug-%Y%m%dT%H%M%SZ', time.gmtime())
    backup.mkdir(parents=True, mode=0o700)
    for name, _, _, previous, _ in reviewed:
        if previous is not None:
            replace(backup / name, previous, 0o600)
    (backup / 'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    health_url = json.loads((ROOT / 'artifacts/dag10k-update-2026-09-10/manifest.json').read_text())['health_url']
    require_idle()
    changed = []
    systemctl('stop')
    try:
        for name, target, payload, previous, mode in reviewed:
            if content(target) != previous:
                raise RuntimeError(name + ' changed during publication.')
            replace(target, payload, mode)
            changed.append((target, previous, mode))
        systemctl('start')
        for _ in range(25):
            try:
                with urllib.request.urlopen(health_url, timeout=2) as response:
                    if json.load(response).get('ok'):
                        break
            except (OSError, ValueError):
                time.sleep(1)
        else:
            raise RuntimeError('Updated service did not pass its health check.')
        for _, target, payload, _, _ in reviewed:
            if content(target) != payload:
                raise RuntimeError('Deployed source differs from reviewed source.')
    except BaseException:
        systemctl('stop')
        for target, previous, mode in reversed(changed):
            if previous is None:
                target.unlink(missing_ok=True)
            else:
                replace(target, previous, mode)
        systemctl('start')
        raise
    report = {'ok':True,'applied':True,'files':len(changed),'backup':str(backup),
              'previous_pid':old_pid,'current_pid':service_pid(),'active_runs_before_restart':0,
              'health_check':True,'source_hashes_verified':True}
    (REVIEW / 'deployment.json').write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report))

if __name__ == '__main__':
    main()
