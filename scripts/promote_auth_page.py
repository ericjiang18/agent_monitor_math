#!/usr/bin/env python3
"""Publish one reviewed authentication page without changing enrollment policy."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

REPO = Path(__file__).resolve().parent.parent
TARGET = Path('/home/ubuntu/agent_monitor_math/agent_monitor/web/login.html')


def digest(content):
    return hashlib.sha256(content).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    payload = Path(manifest['payload'])
    if payload.is_symlink() or TARGET.is_symlink() or TARGET.resolve() != TARGET:
        raise SystemExit('Authentication-page promotion does not accept symlinks.')
    content, previous = payload.read_bytes(), TARGET.read_bytes()
    if digest(content) != manifest['sha256'] or digest(previous) != manifest['previous_sha256']:
        raise SystemExit('Authentication page changed since review.')
    if b'id="form"' not in content or b'</html>' not in content:
        raise SystemExit('Payload is not a complete authentication page.')
    result = {'ok': True, 'applied': False, 'service_restart': False, 'registration_policy_changed': False}
    if args.apply:
        backup = REPO / 'data/deployment_backups' / time.strftime('auth-page-%Y%m%dT%H%M%SZ', time.gmtime())
        backup.mkdir(parents=True, mode=0o700)
        with (backup / 'login.html').open('xb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(previous)
        descriptor, temporary = tempfile.mkstemp(prefix='.auth-page-', dir=TARGET.parent)
        try:
            with os.fdopen(descriptor, 'wb') as stream:
                os.fchmod(stream.fileno(), TARGET.stat().st_mode & 0o777)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            if TARGET.read_bytes() != previous:
                raise RuntimeError('Authentication page changed during promotion.')
            os.replace(temporary, TARGET)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        result.update(applied=True, backup=str(backup))
    print(json.dumps(result))


if __name__ == '__main__':
    main()
