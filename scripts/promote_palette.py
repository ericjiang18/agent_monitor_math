#!/usr/bin/env python3
"""Atomically publish the four reviewed palette assets without a restart."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

ROOT=Path(__file__).resolve().parent.parent
LIVE=Path('/home/ubuntu/agent_monitor_math')
ALLOWED={'agent_monitor/web/console.html','agent_monitor/web/login.html',
         'monitor_core/harness_dashboard/web/index.html','agent_monitor/web/run-viewer.css'}

def digest(content):return hashlib.sha256(content).hexdigest()

def replace(path,content,mode):
    fd,name=tempfile.mkstemp(prefix='.palette-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            os.fchmod(stream.fileno(),mode)
            stream.write(content);stream.flush();os.fsync(stream.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest',type=Path);parser.add_argument('--apply',action='store_true')
    args=parser.parse_args();manifest=json.loads(args.manifest.read_text())
    if set(manifest['files'])!=ALLOWED:raise SystemExit('Unexpected palette scope.')
    reviewed=[]
    for name,entry in manifest['files'].items():
        source=Path(entry['payload']);target=LIVE/name
        if source.is_symlink() or target.is_symlink() or target.resolve()!=target:raise SystemExit('Symlink in palette scope.')
        content=source.read_bytes();previous=target.read_bytes()
        if digest(content)!=entry['sha256'] or digest(previous)!=entry['previous_sha256']:raise SystemExit(name+' changed since review.')
        if name.endswith('.html') and (b'</html>' not in content or b'id="clear-palette-style"' not in content):raise SystemExit('Incomplete palette HTML.')
        if name.endswith('.css') and b'--bg:#ffffff' not in content:raise SystemExit('Missing reviewed CSS palette.')
        reviewed.append((name,target,content,previous,target.stat().st_mode&0o777))
    if not args.apply:
        print(json.dumps({'ok':True,'files':sorted(ALLOWED),'applied':False}));return
    backup=ROOT/'data/deployment_backups'/time.strftime('palette-%Y%m%dT%H%M%SZ',time.gmtime())
    backup.mkdir(parents=True,mode=0o700)
    for name,_,_,previous,_ in reviewed:
        dest=backup/name;dest.parent.mkdir(parents=True,exist_ok=True)
        with dest.open('xb') as stream:os.fchmod(stream.fileno(),0o600);stream.write(previous)
    changed=[]
    try:
        for _,target,content,previous,mode in reviewed:
            if target.read_bytes()!=previous:raise RuntimeError('Live file changed during publication.')
            replace(target,content,mode);changed.append((target,content,previous,mode))
        for _,target,content,_,_ in reviewed:
            if target.read_bytes()!=content:raise RuntimeError('Published palette differs from review.')
    except BaseException:
        for target,content,previous,mode in reversed(changed):
            if target.read_bytes()==content:replace(target,previous,mode)
        raise
    print(json.dumps({'ok':True,'files':sorted(ALLOWED),'applied':True,'backup':str(backup),'service_restart':False}))

if __name__=='__main__':main()
