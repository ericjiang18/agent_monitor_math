"""Explicit, revocable read-only snapshots; never expose a workspace tree."""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit

from agent_monitor import DATA_DIR, RUNS_DIR, CACHE_DIR

DB_PATH = DATA_DIR / 'run_shares.sqlite3'
EXAMPLES = Path(__file__).parent / 'examples'
WEB = Path(__file__).parent / 'web'
RUN_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}')
TOKEN = re.compile(r'[A-Za-z0-9_-]{43}')
HEADERS = {'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
           'X-Robots-Tag': 'noindex, nofollow, noarchive'}


def _connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(DB_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('CREATE TABLE IF NOT EXISTS shares (run_id TEXT PRIMARY KEY, token TEXT UNIQUE NOT NULL, owner_id INTEGER NOT NULL, snapshot TEXT NOT NULL, created REAL NOT NULL)')
    return db


def _text(value, limit=100_000):
    return value[:limit] if isinstance(value, str) else ''


def _file(root: Path, name: str, limit=300_000):
    p = root / name
    try:
        if not p.resolve().is_relative_to(root.resolve()) or p.is_symlink():
            return ''
        with p.open('rb') as stream:
            return stream.read(limit).decode('utf-8', errors='replace')
    except OSError:
        return ''


def record(run_id):
    if not RUN_ID.fullmatch(run_id) or '..' in run_id:
        return None
    cache = Path(os.environ.get('LLM_DASHBOARD_CACHE', str(CACHE_DIR))) / 'harness'
    for folder in (cache, RUNS_DIR):
        try:
            value = json.loads(_file(folder, run_id + '.json', 10_000_000))
            if isinstance(value, dict):
                return value
        except (ValueError, TypeError):
            pass
    return None


def graph_snapshot(value):
    if not isinstance(value, dict):
        return {'nodes': [], 'edges': []}
    raw = value.get('graph', value)
    if not isinstance(raw, dict):
        return {'nodes': [], 'edges': []}
    nodes = [{k: _text(n.get(k), 20_000 if k == 'statement' else 240)
              for k in ('id', 'kind', 'lean_kind', 'label', 'statement', 'status')}
             for n in (raw.get('nodes') or [])[:100] if isinstance(n, dict)]
    ids = {n['id'] for n in nodes}
    edges = [{k: _text(e.get(k), 240) for k in ('from', 'to', 'label')}
             for e in (raw.get('edges') or [])[:300]
             if isinstance(e, dict) and e.get('from') in ids and e.get('to') in ids]
    return {'title': _text(raw.get('title'), 240), 'nodes': nodes, 'edges': edges}


def snapshot(run_id, data):
    ws = RUNS_DIR / 'workspaces' / run_id
    # The workspace itself must not redirect into another user's files.
    safe_ws = not ws.is_symlink() and ws.resolve().is_relative_to((RUNS_DIR / 'workspaces').resolve())
    def read(name):
        return _file(ws, name) if safe_ws else ''
    def graph(name):
        try:
            return graph_snapshot(json.loads(read(name)))
        except (ValueError, TypeError):
            return graph_snapshot({})
    agents = []
    for item in (data.get('agents') or [])[:100]:
        if not isinstance(item, dict):
            continue
        agents.append({
            'id': _text(item.get('agent_id') or item.get('id') or item.get('trace_id'), 200),
            'label': _text(item.get('agent_name') or item.get('name') or item.get('stage_name') or item.get('stage') or item.get('agent'), 200),
            'status': _text(item.get('status'), 80),
            'model': _text(item.get('model'), 160),
            'summary': _text(item.get('summary') or item.get('output_text') or item.get('response_text') or item.get('final_text') or item.get('output'), 20_000),
        })
    result = {
        'version': 1, 'type': 'shared', 'title': _text(data.get('title') or data.get('problem_text_preview') or run_id, 240),
        'status': _text(data.get('status'), 80), 'engine': _text(data.get('engine'), 100),
        'problem': _text(data.get('problem_text') or data.get('problem_text_preview')),
        'proof': read('proof.md') or read('proof.tex') or _text(data.get('proof_text')),
        'agents': agents, 'informal_graph': graph('proof_graph.json'),
        'formal_graph': graph('lean_proof_graph.json'), 'lean_source': read('lean/Proof.lean'),
        'notice': 'Read-only snapshot selected by the run owner. It does not update as the run continues. Graphs describe dependencies; they are not, by themselves, proof certificates.',
    }
    if not result['formal_graph']['nodes'] and result['lean_source']:
        from agent_monitor.proof_graph import graph_from_lean
        result['formal_graph'] = graph_snapshot(graph_from_lean(result['lean_source']))
    if len(json.dumps(result).encode()) > 4_000_000:
        raise ValueError('This snapshot is too large to share (4 MB maximum).')
    return result


def create(run_id, owner_id, data):
    now = time.time()
    data = {**data, 'shared_at': now}
    with _connect() as db:
        old = db.execute('SELECT * FROM shares WHERE run_id=?', (run_id,)).fetchone()
        token = old['token'] if old and old['owner_id'] == owner_id else secrets.token_urlsafe(32)
        db.execute('INSERT OR REPLACE INTO shares VALUES (?, ?, ?, ?, ?)',
                   (run_id, token, owner_id, json.dumps(data, ensure_ascii=False), now))
    return {'token': token, 'shared_at': now}


def owner_share(run_id, owner_id):
    with _connect() as db:
        row = db.execute('SELECT token, created FROM shares WHERE run_id=? AND owner_id=?', (run_id, owner_id)).fetchone()
    return {'token': row['token'], 'shared_at': row['created']} if row else None


def revoke(run_id, owner_id):
    with _connect() as db:
        db.execute('DELETE FROM shares WHERE run_id=? AND owner_id=?', (run_id, owner_id))


def lookup(token):
    if not TOKEN.fullmatch(token):
        return None
    with _connect() as db:
        row = db.execute('SELECT run_id, snapshot FROM shares WHERE token=?', (token,)).fetchone()
    # Deleting the source run immediately disables the snapshot too.
    return json.loads(row['snapshot']) if row and record(row['run_id']) else None


class RunSharingMixin:
    def _share_send(self, code, value):
        self._send(code, json.dumps(value, ensure_ascii=False), headers=HEADERS)

    def _share_owner(self, run_id):
        user = self._current_user()
        if not user:
            self._share_send(401, {'error': 'Sign in to manage shared links.'})
            return None
        if user.get('guest'):
            self._share_send(403, {'error': 'Sign in to an account to share runs.'})
            return None
        data = record(run_id)
        from agent_monitor import jobs
        if not data or jobs.run_owner(run_id) != user.get("id"):
            self._share_send(404, {'error': 'Run not found.'})
            return None
        return user, data

    def _sharing_get(self, path):
        example = path in ('/examples/erdos-straus', '/api/examples/erdos-straus')
        match = re.fullmatch(r'/(api/)?shared/([A-Za-z0-9_-]{43})', path)
        if example or match:
            data = json.loads((EXAMPLES / 'erdos-straus.json').read_text()) if example else lookup(match[2])
            if data is None:
                self._share_send(404, {'error': 'This shared link is unavailable or has been revoked.'})
                return True
            if path.startswith('/api/'):
                self._share_send(200, data)
            else:
                base = self._request_base_path()
                url = base + ('/api/examples/erdos-straus' if example else '/api/shared/' + match[2])
                html = (WEB / 'run-viewer.html').read_text().replace('__DATA_URL__', url).replace('__ASSET_BASE__', base + '/static').replace('__CONSOLE_URL__', base + '/')
                self._send(200, html, 'text/html; charset=utf-8', headers={**HEADERS,
                    'Content-Security-Policy': "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'self'"})
            return True
        match = re.fullmatch(r'/api/runs/([^/]+)/share', path)
        if not match:
            return False
        owned = self._share_owner(match[1])
        if owned:
            share = owner_share(match[1], owned[0]['id'])
            self._share_send(200, self._share_info(share))
        return True

    def _share_info(self, share):
        return {'shared': bool(share), **({'path': self._request_base_path() + '/shared/' + share['token'], 'shared_at': share['shared_at']} if share else {})}

    def _sharing_write(self, path, method, body):
        match = re.fullmatch(r'/api/runs/([^/]+)/share', path)
        if not match:
            return False
        # JSON plus same-origin checks prevent cross-site snapshot publication.
        origin = self.headers.get('Origin')
        if self.headers.get('Sec-Fetch-Site') == 'cross-site' or (origin and urlsplit(origin).netloc != self.headers.get('Host')):
            self._share_send(403, {'error': 'Use the console to manage sharing.'})
            return True
        if method == 'POST' and self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            self._share_send(415, {'error': 'Expected application/json.'})
            return True
        owned = self._share_owner(match[1])
        if not owned:
            return True
        user, data = owned
        if method == 'DELETE':
            revoke(match[1], user['id'])
            self._share_send(200, {'shared': False})
        elif body.get('confirm_share') is not True:
            self._share_send(400, {'error': 'Confirm that this snapshot may be viewed by anyone with the link.'})
        else:
            try:
                share = create(match[1], user['id'], snapshot(match[1], data))
            except ValueError as exc:
                self._share_send(400, {'error': str(exc)})
            else:
                self._share_send(200, self._share_info(share))
        return True
