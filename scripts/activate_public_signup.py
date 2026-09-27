#!/usr/bin/env python3
"""Activate the reviewed signup page and explicit public-registration setting."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request

REPO = Path(__file__).resolve().parent.parent
PAGE = Path('/home/ubuntu/agent_monitor_math/agent_monitor/web/login.html')
DROP_IN = Path('/etc/systemd/system/agent-monitor.service.d/95-public-signup.conf')
SETTING = b'[Service]\nEnvironment=AGENT_MONITOR_REGISTRATION_OPEN=1\n'
SERVICE = 'agent-monitor.service'
BASE = 'http://127.0.0.1:4600/proof-cb44c0b18a86684e40171e2c2ce96ddfda24'


def sha(content):
    return hashlib.sha256(content).hexdigest()


def service_idle():
    pid = int(subprocess.check_output(['systemctl', 'show', SERVICE, '-p', 'MainPID', '--value'], text=True))
    if not pid:
        raise RuntimeError('Expected console service is not running.')
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():
            continue
        try:
            parent = int((process / 'stat').read_text().rsplit(')', 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        if parent == pid:
            raise RuntimeError('A proof process is active; leave it running and retry later.')
    return pid


def atomic_page(content, mode):
    descriptor, temporary = tempfile.mkstemp(prefix='.signup-', dir=PAGE.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, PAGE)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    payload = Path(manifest['payload'])
    if payload.is_symlink() or PAGE.is_symlink() or PAGE.resolve() != PAGE:
        raise SystemExit('Symlinks are not accepted in page promotion.')
    content, previous = payload.read_bytes(), PAGE.read_bytes()
    if sha(content) != manifest['sha256'] or sha(previous) != manifest['previous_sha256']:
        raise SystemExit('Authentication page changed since review.')
    if DROP_IN.exists() or DROP_IN.is_symlink() or DROP_IN.parent.resolve() != DROP_IN.parent:
        raise SystemExit('Registration drop-in path is not the reviewed empty path.')
    old_pid = service_idle()
    if not args.apply:
        print(json.dumps({'ok': True, 'applied': False, 'active_children': 0, 'registration_setting': True, 'service_restart_required': True}))
        return
    backup = REPO / 'data/deployment_backups' / time.strftime('public-signup-%Y%m%dT%H%M%SZ', time.gmtime())
    backup.mkdir(parents=True, mode=0o700)
    for name, value in [('login.html', previous), ('registration.conf', SETTING)]:
        with (backup / name).open('xb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(value)
    mode = PAGE.stat().st_mode & 0o777
    installed = False
    restart_attempted = False
    try:
        if PAGE.read_bytes() != previous:
            raise RuntimeError('Authentication page changed during promotion.')
        atomic_page(content, mode)
        subprocess.run(['sudo', '-n', 'install', '-m', '0644', str(backup / 'registration.conf'), str(DROP_IN)], check=True)
        installed = True
        subprocess.run(['sudo', '-n', 'systemctl', 'daemon-reload'], check=True)
        service_idle()
        restart_attempted = True
        subprocess.run(['sudo', '-n', 'systemctl', 'restart', SERVICE], check=True)
        for _ in range(20):
            try:
                with urllib.request.urlopen(BASE + '/api/auth/me', timeout=2) as response:
                    if json.load(response).get('registration_open') is True:
                        break
            except (OSError, ValueError):
                pass
            time.sleep(.5)
        else:
            raise RuntimeError('Restarted service did not advertise open registration.')
        if PAGE.read_bytes() != content or DROP_IN.read_bytes() != SETTING:
            raise RuntimeError('Published files differ from reviewed content.')
    except BaseException:
        if PAGE.read_bytes() == content:
            atomic_page(previous, mode)
        if installed and DROP_IN.read_bytes() == SETTING:
            subprocess.run(['sudo', '-n', 'rm', '--', str(DROP_IN)], check=True)
            subprocess.run(['sudo', '-n', 'systemctl', 'daemon-reload'], check=True)
        if restart_attempted:
            subprocess.run(['sudo', '-n', 'systemctl', 'restart', SERVICE], check=True)
        raise
    print(json.dumps({'ok': True, 'applied': True, 'registration_open': True, 'page_sha256': sha(content), 'backup': str(backup), 'previous_pid': old_pid, 'service_restart': True, 'active_children_before_restart': 0}))


if __name__ == '__main__':
    main()
