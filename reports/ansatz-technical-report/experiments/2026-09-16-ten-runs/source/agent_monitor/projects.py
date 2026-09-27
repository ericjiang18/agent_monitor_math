"""Shared research projects with character operations and member-owned runs."""
from __future__ import annotations
import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlsplit
from agent_monitor import DATA_DIR

DB_PATH = DATA_DIR / 'projects.sqlite3'
MAX_TEXT = 20_000
MAX_NODES = 100_000
ID = re.compile(r'[A-Za-z0-9:_-]{1,100}')


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(DB_PATH, os.O_CREAT | os.O_RDWR, 0o600); os.close(fd)
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.executescript('''CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, title TEXT NOT NULL, owner INTEGER NOT NULL, doc TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 0, invite TEXT UNIQUE, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS members (project TEXT NOT NULL, user INTEGER NOT NULL, name TEXT NOT NULL, seen REAL NOT NULL, PRIMARY KEY(project,user));
CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, project TEXT NOT NULL, user INTEGER NOT NULL, task TEXT NOT NULL, created REAL NOT NULL);''')
    return db


def _member(db, pid, uid):
    if not db.execute('SELECT 1 FROM members WHERE project=? AND user=?',(pid,uid)).fetchone():
        raise PermissionError('Project not found or invitation required.')
    row = db.execute('SELECT * FROM projects WHERE id=?',(pid,)).fetchone()
    if not row:
        raise PermissionError('Project not found.')
    return row


def _render(nodes):
    children = {}
    for n in nodes:
        children.setdefault(n['after'], []).append(n)
    for row in children.values():
        row.sort(key=lambda n:n['order'], reverse=True)
    stack = list(reversed(children.get('', []))); text=[]; ids=[]
    while stack:
        n=stack.pop()
        if not n.get('deleted'):
            text.append(n['value']); ids.append(n['id'])
        stack.extend(reversed(children.get(n['id'],[])))
    return ''.join(text), ids


def _payload(db, row, uid):
    text, ids = _render(json.loads(row['doc']))
    members = [dict(r) for r in db.execute('SELECT user, name, seen FROM members WHERE project=? ORDER BY seen DESC',(row['id'],))]
    runs = [dict(r) for r in db.execute('SELECT run_id,user,task,created FROM runs WHERE project=? ORDER BY created DESC LIMIT 200',(row['id'],))]
    return {'id':row['id'],'title':row['title'],'owner_id':row['owner'],'is_owner':row['owner']==uid,'problem':text,'character_ids':ids,'revision':row['revision'],'members':members,'runs':runs}


def create(user, title, text):
    if not isinstance(text,str) or len(text)>MAX_TEXT:
        raise ValueError('Problem text is limited to 20,000 characters.')
    title=str(title or 'Untitled research project').strip()[:160]
    nodes=[{'id':f'initial:{i}','after':f'initial:{i-1}' if i else '', 'value':c,'order':i} for i,c in enumerate(text)]
    pid=secrets.token_urlsafe(16)
    with connect() as db:
        db.execute('INSERT INTO projects VALUES (?,?,?,?,0,NULL,?)',(pid,title,user['id'],json.dumps(nodes),time.time()))
        db.execute('INSERT INTO members VALUES (?,?,?,?)',(pid,user['id'],str(user.get('name') or 'Project owner')[:100],time.time()))
        return _payload(db,_member(db,pid,user['id']),user['id'])


def get(pid, uid):
    with connect() as db:
        row=_member(db,pid,uid)
        db.execute('UPDATE members SET seen=? WHERE project=? AND user=?',(time.time(),pid,uid))
        return _payload(db,row,uid)


def list_projects(uid):
    with connect() as db:
        return [dict(r) for r in db.execute('SELECT p.id,p.title,p.owner FROM projects p JOIN members m ON m.project=p.id WHERE m.user=? ORDER BY p.created DESC',(uid,))]


def edit(pid, uid, operations):
    if not isinstance(operations,list) or len(operations)>40_000:
        raise ValueError('Invalid edit batch.')
    with connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=_member(db,pid,uid); nodes=json.loads(row['doc']); index={n['id']:n for n in nodes}
        for op in operations:
            if not isinstance(op,dict): raise ValueError('Invalid edit operation.')
            key=op.get('id','')
            if not isinstance(key,str) or not ID.fullmatch(key): raise ValueError('Invalid character ID.')
            if op.get('type')=='delete':
                if key not in index: raise ValueError('Unknown character. Reload the project.')
                index[key]['deleted']=True
            elif op.get('type')=='insert':
                after=op.get('after',''); value=op.get('value')
                if not isinstance(after,str) or (after and after not in index) or not isinstance(value,str) or len(value)!=1:
                    raise ValueError('Invalid insertion.')
                if key in index:
                    if index[key]['after']!=after or index[key]['value']!=value: raise ValueError('Character ID collision.')
                    continue
                n={'id':key,'after':after,'value':value,'order':len(nodes)};nodes.append(n);index[key]=n
            else: raise ValueError('Unknown edit operation.')
        text,_=_render(nodes)
        if len(text)>MAX_TEXT or len(nodes)>MAX_NODES: raise ValueError('Project document capacity reached. Copy the text into a new project.')
        db.execute('UPDATE projects SET doc=?,revision=revision+1 WHERE id=?',(json.dumps(nodes),pid))
        return _payload(db,_member(db,pid,uid),uid)


def invite(pid, uid, revoke=False):
    with connect() as db:
        row=_member(db,pid,uid)
        if row['owner']!=uid: raise PermissionError('Only the project owner can manage invitations.')
        token=None if revoke else (row['invite'] or secrets.token_urlsafe(32))
        db.execute('UPDATE projects SET invite=? WHERE id=?',(token,pid))
        return token


def join(token,user):
    if not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',token): raise PermissionError('Invitation unavailable.')
    with connect() as db:
        row=db.execute('SELECT * FROM projects WHERE invite=?',(token,)).fetchone()
        if not row: raise PermissionError('Invitation unavailable or revoked.')
        db.execute('INSERT OR IGNORE INTO members VALUES (?,?,?,?)',(row['id'],user['id'],str(user.get('name') or 'Collaborator')[:100],time.time()))
        return {'id':row['id']}


def remove_member(pid, uid, member):
    with connect() as db:
        row=_member(db,pid,uid)
        if row['owner']!=uid or member==uid: raise PermissionError('Only the owner can remove other members.')
        db.execute('DELETE FROM members WHERE project=? AND user=?',(pid,member))
        # Old invite links must not let removed members immediately rejoin.
        db.execute('UPDATE projects SET invite=NULL WHERE id=?',(pid,))


def attach(pid, uid, rid, task):
    with connect() as db:
        _member(db,pid,uid)
        db.execute('INSERT INTO runs VALUES (?,?,?,?,?)',(rid,pid,uid,str(task or '')[:2000],time.time()))


def can_read_run(rid, uid):
    if not DB_PATH.exists():
        return False
    with connect() as db:
        return bool(db.execute('SELECT 1 FROM runs r JOIN members m ON m.project=r.project WHERE r.run_id=? AND m.user=?',(rid,uid)).fetchone())


class ProjectsMixin:
    def _project_send(self, status, value):
        self._send(status,json.dumps(value,ensure_ascii=False),headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Robots-Tag':'noindex,nofollow'})

    def _projects_route(self,path,method,body=None):
        if method=='GET' and (path=='/projects' or re.fullmatch(r'/projects/[A-Za-z0-9_-]+',path) or re.fullmatch(r'/invite/[A-Za-z0-9_-]{43}',path)):
            html=(Path(__file__).parent/'web/projects.html').read_text().replace('__BASE__',self._request_base_path())
            self._send_html(html)
            return True
        if not (path=='/api/projects' or path.startswith('/api/projects/')):
            return False
        user=self._current_user()
        if not user or user.get('guest'):
            self._project_send(401,{'error':'Sign in to an account to collaborate.'});return True
        if method!='GET':
            origin=self.headers.get('Origin')
            if self.headers.get('Sec-Fetch-Site')=='cross-site' or (origin and urlsplit(origin).netloc!=self.headers.get('Host')):
                self._project_send(403,{'error':'Use the console to edit projects.'});return True
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':
                self._project_send(415,{'error':'Expected application/json.'});return True
        body=body or {};parts=path.split('/');pid=parts[3] if len(parts)>3 else '';action=parts[4] if len(parts)>4 else ''
        try:
            if path=='/api/projects' and method=='GET': result={'projects':list_projects(user['id'])}
            elif path=='/api/projects' and method=='POST':result=create(user,body.get('title'),body.get('problem',''))
            elif path=='/api/projects/join' and method=='POST':result=join(body.get('token'),user)
            elif len(parts)==4 and method=='GET':
                result=get(pid,user['id'])
                from agent_monitor.run_sharing import record
                for run in result['runs']:
                    data=record(run['run_id']);run['status']=data.get('status','pending') if data else 'deleted'
                    run['engine']=data.get('engine','') if data else ''
            elif len(parts)==5 and method=='POST' and action=='edit':result=edit(pid,user['id'],body.get('operations'))
            elif len(parts)==5 and method=='POST' and action=='invite':
                token=invite(pid,user['id'],body.get('revoke') is True);result={'path':self._request_base_path()+'/invite/'+token if token else None}
            elif len(parts)==5 and method=='POST' and action=='remove-member':
                remove_member(pid,user['id'],int(body.get('user_id')));result={'ok':True}
            else:
                self._project_send(404,{'error':'Not found.'});return True
        except PermissionError as exc:self._project_send(404,{'error':str(exc)})
        except (ValueError,TypeError) as exc:self._project_send(400,{'error':str(exc)})
        else:self._project_send(200,result)
        return True

def prepare_run(body, user):
    pid=body.get('project_id')
    if not pid:
        return
    if user.get('guest') or not isinstance(pid,str):
        raise PermissionError('An account and project membership are required.')
    project=get(pid,user['id'])
    task=str(body.get('project_task') or '').strip()[:2000]
    body['problem_id']=None
    body['problem_text']=project['problem']+ ('\n\nYOUR RESEARCH TASK\n'+task if task else '')


def start_run(pid, uid, task, launch):
    if not pid:
        return launch()
    # Serialize membership removal and run registration with launch admission.
    with connect() as db:
        db.execute('BEGIN IMMEDIATE');_member(db,pid,uid)
        job=launch()
        db.execute('INSERT INTO runs VALUES (?,?,?,?,?)',(job['run_id'],pid,uid,str(task or '')[:2000],time.time()))
        return job
