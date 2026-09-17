import json
import sqlite3
import pytest
from test_console_guest_access import guest_console, running_server, request, _cookie_from, APP_MOUNT
from agent_monitor import auth

def account_sessions():
    user=auth.register('switch@example.test','long-enough-password')
    return user,auth.create_session(user['id']),auth.create_session(user['id'])

def switch(port, token, **kwargs):
    return request(port,'POST',APP_MOUNT+'/api/auth/guest',body={'switch_account':True},headers={'Cookie':auth.SESSION_COOKIE+'='+token,'X-Guest-Intent':'homepage'},**kwargs)

def test_switch_preserves_account_other_device_and_settings(guest_console):
    user,token,other=account_sessions()
    auth.set_user_env(user['id'],{'OPENAI_API_KEY':'fixture-key'})
    with sqlite3.connect(guest_console['db']) as conn:
        saved=conn.execute('SELECT * FROM user_env WHERE user_id=?',(user['id'],)).fetchall()
    with running_server() as port:
        status,headers,raw=switch(port,token)
    assert status==200,raw
    _,guest_token=_cookie_from(headers)
    assert auth.user_for_token(guest_token)['guest']
    assert auth.user_for_token(token) is None
    assert auth.user_for_token(other)['id']==user['id']
    with sqlite3.connect(guest_console['db']) as conn:
        assert conn.execute('SELECT count(*) FROM users WHERE id=?',(user['id'],)).fetchone()[0]==1
        assert conn.execute('SELECT * FROM user_env WHERE user_id=?',(user['id'],)).fetchall()==saved

def test_failed_switch_rolls_back_identity_and_keeps_account(guest_console,monkeypatch):
    user,token,other=account_sessions()
    original=auth._insert_session
    def fail(conn,*args,**kwargs):
        original(conn,*args,**kwargs)
        raise RuntimeError('fixture failure after insertion')
    monkeypatch.setattr(auth,'_insert_session',fail)
    with running_server() as port:
        status,headers,_=switch(port,token)
    assert status==503
    assert 'set-cookie' not in headers
    assert auth.user_for_token(token)['id']==user['id']
    assert auth.user_for_token(other)['id']==user['id']
    with sqlite3.connect(guest_console['db']) as conn:
        assert conn.execute('SELECT count(*) FROM users WHERE is_guest=1').fetchone()[0]==0

@pytest.mark.parametrize('body,extra', [({},{}),({'switch_account':True},{}),({'switch_account':'true'},{'X-Guest-Intent':'homepage'})])
def test_ordinary_guest_requests_cannot_switch(guest_console,body,extra):
    user,token,_=account_sessions()
    with running_server() as port:
        status,headers,_=request(port,'POST',APP_MOUNT+'/api/auth/guest',body=body,headers={'Cookie':auth.SESSION_COOKIE+'='+token,**extra})
    assert status==409
    assert 'set-cookie' not in headers
    assert auth.user_for_token(token)['id']==user['id']

def test_account_logout_keeps_other_device(guest_console):
    user,token,other=account_sessions()
    with running_server() as port:
        status,headers,_=request(port,'POST',APP_MOUNT+'/api/auth/logout',body={},headers={'Cookie':auth.SESSION_COOKIE+'='+token})
        login_status,_,html=request(port,'GET',APP_MOUNT+'/login')
    assert status==200 and login_status==200
    assert 'Max-Age=0' in headers['set-cookie']
    assert auth.user_for_token(token) is None
    assert auth.user_for_token(other)['id']==user['id']
