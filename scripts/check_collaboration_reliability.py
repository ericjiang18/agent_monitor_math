#!/usr/bin/env python3
"""Exercise real project HTTP routes against disposable storage; no model calls.

Run from the repository root:
  .venv/bin/python scripts/check_collaboration_reliability.py --output /tmp/report.json
Temporary databases are retained under /tmp/ansatz-reliability-* for inspection.
Authentication is a synthetic test identity, NOT production session authentication.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import multiprocessing
import os
from pathlib import Path
import platform
import sqlite3
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def serve(db_path, pipe):
    from agent_monitor import projects
    projects.DB_PATH = Path(db_path)

    class Handler(projects.ProjectsMixin, BaseHTTPRequestHandler):
        def _current_user(self):
            uid = self.headers.get('X-Test-User')
            return {'id': int(uid), 'name': 'Synthetic editor ' + uid} if uid else None

        def _request_base_path(self):
            return ''

        def _send(self, status, body, headers=None):
            data = body.encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if not self._projects_route(self.path, 'GET'):
                self._send(404, '{}')

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
            if not self._projects_route(self.path, 'POST', body):
                self._send(404, '{}')

        def log_message(self, *args):
            pass

    class Server(ThreadingHTTPServer):
        request_queue_size = 128

    server = Server(('127.0.0.1', 0), Handler)
    pipe.send(server.server_port)
    pipe.close()
    server.serve_forever()


def start(db_path):
    ctx = multiprocessing.get_context('spawn')
    parent, child = ctx.Pipe(duplex=False)
    process = ctx.Process(target=serve, args=(str(db_path), child))
    process.start()
    child.close()
    if not parent.poll(20):
        process.kill()
        process.join()
        raise RuntimeError('Test server did not start')
    port = parent.recv()
    parent.close()
    return process, port


def trial(directory, clients, rounds):
    db_path = directory / f'projects-{clients}.sqlite3'
    process, port = start(db_path)
    timings = []
    lock = threading.Lock()

    def request(method, path, body=None, uid=1, expected=200, measured=False):
        headers = {'Content-Type': 'application/json'}
        if uid is not None:
            headers['X-Test-User'] = str(uid)
        begin = time.perf_counter()
        conn = http.client.HTTPConnection('127.0.0.1', port, timeout=30)
        try:
            conn.request(method, path, json.dumps(body) if body is not None else None, headers)
            response = conn.getresponse()
            data = json.loads(response.read())
            require(response.status == expected, f'{method} {path}: {response.status}, expected {expected}: {data}')
        finally:
            conn.close()
        if measured:
            with lock:
                timings.append((method, (time.perf_counter() - begin) * 1000))
        return data

    try:
        project = request('POST', '/api/projects', {'title': 'Synthetic concurrency check', 'problem': 'ab'})
        path = '/api/projects/' + project['id']
        token = request('POST', path + '/invite', {})['path'].split('/')[-1]
        for uid in range(2, clients + 1):
            request('POST', '/api/projects/join', {'token': token}, uid=uid)

        # All editors use the same original anchor, including after it is deleted.
        # Barriers force overlapping rounds; each operation is uniquely identifiable.
        barrier = threading.Barrier(clients, timeout=30)
        def editor(uid):
            for round_no in range(rounds):
                barrier.wait()
                after = 'initial:0'
                ops = []
                for index, value in enumerate('∑🙂x'):
                    key = f'u{uid}:r{round_no}:c{index}'
                    ops.append({'type': 'insert', 'id': key, 'after': after, 'value': value})
                    after = key
                request('POST', path + '/edit', {'operations': ops}, uid, measured=True)
                if round_no % 3 == 0:
                    request('POST', path + '/edit', {'operations': ops}, uid, measured=True)
                if round_no == 0:
                    request('POST', path + '/edit', {'operations': [{'type': 'delete', 'id': 'initial:0'}]}, uid, measured=True)
                request('GET', path, uid=uid, measured=True)

        begin = time.perf_counter()
        with ThreadPoolExecutor(max_workers=clients) as pool:
            list(pool.map(editor, range(1, clients + 1)))
        duration = time.perf_counter() - begin
        final = request('GET', path)
        expected_ids = {'initial:1'} | {
            f'u{u}:r{r}:c{c}' for u in range(1, clients + 1)
            for r in range(rounds) for c in range(3)
        }
        require(set(final['character_ids']) == expected_ids, 'Lost or unexpected character IDs')
        require(len(final['character_ids']) == len(expected_ids), 'Duplicate characters after retries')
        require(final['problem'] == '∑🙂x' * (clients * rounds) + 'b', 'Unicode, insertion chain, or tombstone mismatch')
        with ThreadPoolExecutor(max_workers=clients) as pool:
            snapshots = list(pool.map(lambda uid: request('GET', path, uid=uid), range(1, clients + 1)))
        require(all((s['problem'], s['character_ids'], s['revision']) ==
                    (final['problem'], final['character_ids'], final['revision']) for s in snapshots),
                'Clients did not converge after writes settled')

        # A valid prefix followed by an invalid operation must roll back in full.
        request('POST', path + '/edit', {'operations': [
            {'type': 'insert', 'id': 'rollback:1', 'after': '', 'value': '!'},
            {'type': 'insert', 'id': 'rollback:2', 'after': 'missing', 'value': '!'}]}, expected=400)
        request('POST', path + '/edit', {'operations': [
            {'type': 'insert', 'id': 'u1:r0:c0', 'after': '', 'value': '!'}]}, expected=400)
        request('GET', path, uid=None, expected=401)
        request('POST', path + '/edit', {'operations': []}, uid=999, expected=404)
        request('POST', path + '/remove-member', {'user_id': clients})
        request('POST', path + '/edit', {'operations': []}, uid=clients, expected=404)
        request('POST', '/api/projects/join', {'token': token}, uid=clients, expected=404)
        after_rejections = request('GET', path)
        require(all(after_rejections[k] == final[k] for k in ('problem', 'character_ids', 'revision')),
                'Rejected operation changed document or revision')

        # Kill only our isolated server after commits have been acknowledged.
        process.kill()
        process.join(10)
        require(not process.is_alive(), 'Old test server survived kill')
        process, port = start(db_path)
        restored = request('GET', path)
        require(all(restored[k] == after_rejections[k] for k in ('problem', 'character_ids', 'revision')),
                'Acknowledged state did not survive process restart')
        # GET refreshes presence timestamps, so compare membership identity only.
        require(sorted((m['user'], m['name']) for m in restored['members']) ==
                sorted((m['user'], m['name']) for m in after_rejections['members']),
                'Membership did not survive process restart')
        request('GET', path, uid=clients, expected=404)
        request('POST', '/api/projects/join', {'token': token}, uid=clients, expected=404)
        with sqlite3.connect(db_path) as db:
            require(db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'SQLite integrity check failed')
        def latency(method):
            values = sorted(t for m, t in timings if m == method)
            return {'count': len(values), 'p50_ms': round(values[math.ceil(len(values)*.50)-1], 2),
                    'p95_ms': round(values[math.ceil(len(values)*.95)-1], 2), 'max_ms': round(max(values), 2)}
        return {'clients': clients, 'rounds': rounds, 'inserted_characters': clients * rounds * 3,
                'replayed_batches': clients * len(range(0, rounds, 3)), 'requests_measured': len(timings),
                'duration_seconds': round(duration, 3), 'requests_per_second': round(len(timings)/duration, 2),
                'edit_latency': latency('POST'), 'read_latency': latency('GET'),
                'unexpected_errors': 0, 'lost_characters': 0, 'duplicate_characters': 0,
                'checks': {name: True for name in ['unicode_and_deleted_anchors', 'retry_idempotence',
                    'convergence', 'invalid_batch_rollback', 'id_collision_rejected', 'nonmember_rejected',
                    'removed_member_rejected', 'revoked_invite_rejected', 'restart_persistence', 'sqlite_integrity']}}
    finally:
        if process.is_alive():
            process.kill()
        process.join(10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    require(not args.output.exists(), 'Choose a new output path; existing reports are preserved.')
    directory = Path(tempfile.mkdtemp(prefix='ansatz-reliability-'))
    report = {'schema_version': 1, 'recorded_at': datetime.now(timezone.utc).isoformat(),
              'scope': 'Loopback HTTP, real ProjectsMixin and SQLite, synthetic identities; no model runs or production traffic.',
              'environment': {'python': platform.python_version(), 'sqlite': sqlite3.sqlite_version,
                              'os': platform.system(), 'architecture': platform.machine(), 'logical_cpus': os.cpu_count()},
              'source_sha256': {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
                                ['agent_monitor/projects.py', 'scripts/check_collaboration_reliability.py']},
              'trials': []}
    for clients in (8, 16):
        result = trial(directory, clients, 40)
        report['trials'].append(result)
        print(json.dumps(result), flush=True)
    report['passed'] = True
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(f'Report: {args.output}\nIsolated test databases retained: {directory}')


if __name__ == '__main__':
    main()
