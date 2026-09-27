import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest
from agent_monitor import projects, run_sharing as shares, console_server as console, jobs


@pytest.fixture(autouse=True)
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(projects,'DB_PATH',tmp_path/'projects.sqlite3')
    monkeypatch.setattr(shares,'DB_PATH',tmp_path/'shares.sqlite3')
    monkeypatch.setattr(shares,'RUNS_DIR',tmp_path/'runs')
    monkeypatch.setattr(shares,'CACHE_DIR',tmp_path/'cache')
    monkeypatch.delenv('LLM_DASHBOARD_CACHE',raising=False)
    (tmp_path/'runs').mkdir()


def test_simultaneous_character_edits_merge_and_retry_is_idempotent():
    p=projects.create({'id':1,'name':'Alice'},'Test','ab')
    token=projects.invite(p['id'],1);projects.join(token,{'id':2,'name':'Bob'})
    first={'type':'insert','id':'alice:1','after':'initial:0','value':'X'}
    second={'type':'insert','id':'bob:1','after':'initial:0','value':'Y'}
    projects.edit(p['id'],1,[first]);result=projects.edit(p['id'],2,[second])
    assert result['problem']=='aYXb'
    assert projects.edit(p['id'],1,[first])['problem']=='aYXb'
    # A stale delete removes only the character that author actually saw.
    result=projects.edit(p['id'],1,[{'type':'delete','id':'initial:0'}])
    assert result['problem']=='YXb'
    result=projects.edit(p['id'],2,[{'type':'insert','id':'bob:2','after':'initial:0','value':'∑'}])
    assert result['problem']=='∑YXb'


def test_project_membership_revocation_and_atomic_invalid_edits():
    p=projects.create({'id':1},'Test','x');pid=p['id'];token=projects.invite(pid,1)
    with pytest.raises(PermissionError):projects.get(pid,2)
    projects.join(token,{'id':2})
    with pytest.raises(PermissionError):projects.invite(pid,2)
    with pytest.raises(ValueError):projects.edit(pid,2,[{'type':'insert','id':'b:1','after':'','value':'Y'},{'type':'insert','id':'b:2','after':'missing','value':'Z'}])
    assert projects.get(pid,2)['problem']=='x'
    projects.remove_member(pid,1,2)
    with pytest.raises(PermissionError):projects.get(pid,2)
    with pytest.raises(PermissionError):projects.join(token,{'id':2})


def test_snapshot_allowlist_and_symlinks(tmp_path):
    ws=shares.RUNS_DIR/'workspaces'/'run-1';ws.mkdir(parents=True)
    (ws/'proof.md').write_text('Public proof')
    (ws/'problem.txt').write_text('Secret system prompt')
    (ws/'chat.json').write_text('Private chat')
    (ws/'lean').mkdir();private=tmp_path/'secret';private.write_text('PRIVATE KEY')
    (ws/'lean'/'Proof.lean').symlink_to(private)
    raw={'problem_text':'Selected problem','owner_email':'secret@example.com','api_key':'PRIVATE KEY','agents':[{'name':'solver','summary':'Public summary','system_prompt':'PRIVATE KEY'}]}
    result=shares.snapshot('run-1',raw);encoded=json.dumps(result)
    assert result['proof']=='Public proof' and result['lean_source']==''
    assert 'PRIVATE KEY' not in encoded and 'Private chat' not in encoded and 'Secret system prompt' not in encoded
    assert 'owner_email' not in result
    assert shares.record('../secret') is None


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(console,'BASE_PATH','/test')
    monkeypatch.setattr(console.Handler,'_current_user',lambda self: {'id':int(self.headers['X-User']),'name':'Researcher '+self.headers['X-User'],'guest':self.headers.get('X-Guest')=='yes'} if self.headers.get('X-User') else None)
    owners={}
    monkeypatch.setattr(jobs,'run_owner',lambda rid:owners.get(rid))
    def start(**kwargs):
        rid='run-'+str(len(owners)+1);owners[rid]=kwargs['user']['id']
        (shares.RUNS_DIR/(rid+'.json')).write_text(json.dumps({'run_id':rid,'owner_id':owners[rid],'problem_text':kwargs['problem_text'],'engine':kwargs['engine'],'status':'finished'}))
        return {'run_id':rid,'job_id':rid}
    monkeypatch.setattr(jobs,'start_job',start)
    server=ThreadingHTTPServer(('127.0.0.1',0),console.Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def request(method,path,body=None,user=1,headers=None):
        conn=http.client.HTTPConnection(*server.server_address,timeout=5)
        h={'Content-Type':'application/json',**(headers or {})}
        if user is not None:h['X-User']=str(user)
        conn.request(method,'/test'+path,body=json.dumps(body) if body is not None else None,headers=h)
        res=conn.getresponse();raw=res.read();conn.close()
        return res.status,dict(res.getheaders()),json.loads(raw) if 'application/json' in res.getheader('Content-Type','') else raw.decode()
    yield request
    server.shutdown();server.server_close();thread.join(timeout=5)


def test_share_create_copy_revoke_and_deleted_source(api):
    _,_,run=api('POST','/api/runs',{'engine':'plain','problem_text':'hello'})
    rid=run['run_id'];path='/api/runs/'+rid+'/share'
    assert api('POST',path,{'confirm_share':True},user=None)[0]==401
    assert api('POST',path,{'confirm_share':True},user=2)[0]==404
    assert api('POST',path,{'confirm_share':True},headers={'X-Guest':'yes'})[0]==403
    assert api('POST',path,{})[0]==400
    assert api('POST',path,{'confirm_share':True},headers={'Origin':'https://evil.test'})[0]==403
    status,_,result=api('POST',path,{'confirm_share':True});assert status==200
    token=result['path'].split('/')[-1];assert result['path'].startswith('/test/shared/')
    status,headers,data=api('GET','/api/shared/'+token,user=None)
    assert status==200 and data['problem']=='hello'
    assert headers['Referrer-Policy']=='no-referrer' and headers['Cache-Control']=='no-store'
    assert api('GET','/shared/'+token,user=None)[0]==200
    assert api('GET','/api/run/'+rid,user=None)[0]==401
    assert api('GET',path)[2]['path']==result['path']
    assert api('DELETE',path,user=2)[0]==404
    assert api('DELETE',path)[0]==200
    assert api('GET','/api/shared/'+token,user=None)[0]==404
    result=api('POST',path,{'confirm_share':True})[2]
    assert result['path'].split('/')[-1]!=token
    (shares.RUNS_DIR/(rid+'.json')).unlink()
    assert api('GET',result['path'].replace('/test/shared/','/api/shared/'),user=None)[0]==404


def test_project_runs_require_membership_use_shared_problem_and_own_account(api):
    assert api('POST','/api/projects',{'title':'X'},user=None)[0]==401
    p=api('POST','/api/projects',{'title':'Joint work','problem':'Shared problem'})[2];pid=p['id']
    assert api('GET','/api/projects/'+pid,user=2)[0]==404
    token=api('POST','/api/projects/'+pid+'/invite',{})[2]['path'].split('/')[-1]
    assert api('POST','/api/projects/join',{'token':token},user=2)[0]==200
    status,_,run=api('POST','/api/runs',{'project_id':pid,'project_task':'Find a lemma','engine':'plain','problem_text':'should not override project'},user=2)
    assert status==200
    assert shares.record(run['run_id'])['problem_text']=='Shared problem\n\nYOUR RESEARCH TASK\nFind a lemma'
    assert projects.can_read_run(run['run_id'],1)
    assert not projects.can_read_run(run['run_id'],3)
    result=api('GET','/api/projects/'+pid)[2];assert result['runs'][0]['user']==2
    # Project editing rights must not grant public publication rights to another author's run.
    assert api('POST','/api/runs/'+run['run_id']+'/share',{'confirm_share':True})[0]==404
    assert api('POST','/api/runs',{'project_id':pid,'engine':'plain'},user=3)[0]==403
    assert api('POST','/api/projects/'+pid+'/edit',{'operations':[]},headers={'Origin':'https://evil.test'})[0]==403
    assert api('POST','/api/projects/'+pid+'/remove-member',{'user_id':2})[0]==200
    assert api('GET','/api/projects/'+pid,user=2)[0]==404


def test_public_example_never_requires_account_or_creates_run(api):
    status,_,data=api('GET','/api/examples/erdos-straus',user=None)
    assert status==200 and data['type']=='example'
    assert 'not a captured multi-agent execution' in data['notice']
    assert len(data['witnesses'])==499
    assert not list(shares.RUNS_DIR.glob('*.json'))
