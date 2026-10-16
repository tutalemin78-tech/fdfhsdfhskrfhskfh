import asyncio,json,time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from datetime import datetime
import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient,TestServer
from pyrogram.enums import ChatType,ParseMode
import database as db, config, product, userbot_worker as worker, account_runtime as runtime, webapp_server as web
from test_security_product import signed


class FakeClient:
    def __init__(self,session,chat_type=ChatType.SUPERGROUP):
        self.session=session;self.sent=[];self.chat_type=chat_type
    async def start(self):return self
    async def stop(self):pass
    async def send_message(self,cid,body,**kwargs):
        self.sent.append((cid,body));return NS(chat=NS(id=cid),id=len(self.sent))
    async def send_photo(self,cid,path,caption,**kwargs):return await self.send_message(cid,caption)
    async def send_video(self,cid,path,caption,**kwargs):return await self.send_message(cid,caption)
    async def get_chat(self,cid):return NS(type=self.chat_type,title='Test group',id=cid)
    async def get_dialogs(self):
        yield NS(chat=NS(type=self.chat_type,id=-100123))
    async def get_messages(self,*args):
        return NS(empty=False,has_protected_content=False,chat=NS(has_protected_content=False),media_group_id=None,text=NS(html='<b>Original</b>'),caption=None,photo=None,video=None,media=None,reply_markup=None)


@pytest.mark.asyncio
async def test_send_footer_and_outbound_tracking(session):
    app=FakeClient(session);job=NS(user_id=session['owner_id'])
    await worker._send_payload(app,-100123,{'type':'text','text':'Hello'},job)
    assert app.sent[0][1]=='Hello\n\n'+product.FOOTER
    assert db.find_outbound(session['id'],-100123,1)['body'].endswith(product.FOOTER)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind',['photo','video','forward'])
async def test_footer_for_media_and_copied_post(session,kind):
    app=FakeClient(session)
    payload={'type':kind,'path':'test.jpg','text':'Hello','from_chat':'source','msg_id':1}
    await worker._send_payload(app,-100123,payload)
    assert app.sent[0][1].endswith('\n\n'+product.FOOTER)


@pytest.mark.asyncio
async def test_multisession_dedupe_and_restore(session,users,monkeypatch):
    db.add_session(users[0],'+10000000002',1,'hash2','session2')
    sessions=db.get_sessions(users[0]);apps={s['id']:FakeClient(s) for s in sessions}
    async def open_(s):return apps[s['id']]
    monkeypatch.setattr(worker,'_open',open_)
    targets={str(s['id']):[-100123] for s in sessions}
    job=await worker.start_broadcast_task({'type':'text','text':'Hello'},30,'all',users[0],targets=targets)
    await job.task
    assert sum(len(a.sent) for a in apps.values())==1
    assert len(job.accounts)==2
    # Same persistent job ID and ledger: nothing is repeated after restart.
    cfg=db.get_resumable_jobs()
    worker.JOBS.clear()
    restarted=await worker.start_broadcast_task({'type':'text','text':'Hello'},30,'all',users[0],targets=targets,resume_db_id=job.db_id)
    await restarted.task
    assert sum(len(a.sent) for a in apps.values())==1


@pytest.mark.asyncio
async def test_private_peer_excluded(session,users,monkeypatch):
    app=FakeClient(session,ChatType.PRIVATE)
    async def open_(s):return app
    monkeypatch.setattr(worker,'_open',open_)
    with pytest.raises(ValueError):await worker.prepare_targets(users[0])


@pytest.mark.asyncio
async def test_owner_controls(session,users):
    j=worker.Job(users[0],1,'all',30,'Test',{},None,None);j.db_id=db.save_job(users[0],'Test',{});j.id=j.db_id
    worker.JOBS[j.id]=j
    assert worker.get_job(j.id,users[1]) is None
    assert not worker.pause_job(j.id,users[1])
    assert worker.pause_job(j.id,users[0])
    assert j.status=='paused'
    assert worker.resume_job(j.id,users[0])
    assert worker.stop_job(j.id,users[0])
    await asyncio.sleep(.05)


@pytest.mark.asyncio
async def test_parallel_same_account_rejected(session,users,monkeypatch):
    j=worker.Job(users[0],1,'all',30,'Busy',{},None,None);j.account_ids=[session['id']];worker.JOBS[j.id]=j
    with pytest.raises(ValueError,match='занят'):
        await worker.start_broadcast_task({'type':'text','text':'Hello'},30,'all',users[0],targets={str(session['id']):[-100123]})


def incoming(private=False):
    return NS(outgoing=False,service=None,from_user=NS(id=44444,is_bot=False,first_name='Alice',last_name=None,username='alice_test'),chat=NS(id=44444 if private else -100123,type=ChatType.PRIVATE if private else ChatType.SUPERGROUP,title='Test group',username=None),id=90,reply_to_message_id=None if private else 5,text='<Hello>',caption=None)


@pytest.mark.asyncio
async def test_reply_notification_quote_dedup(session,monkeypatch):
    bot=NS(send_message=AsyncMock());monkeypatch.setattr(runtime,'BOT',bot)
    db.remember_outbound(session['id'],-100123,5,session['owner_id'],'Original')
    message=incoming()
    await runtime.handle_message(session,FakeClient(session),message)
    await runtime.handle_message(session,FakeClient(session),message)
    assert bot.send_message.await_count==1
    args=bot.send_message.call_args
    assert '<blockquote>&lt;Hello&gt;</blockquote>' in args.args[1]
    assert args.kwargs['reply_markup'].inline_keyboard[0][0].url=='https://t.me/c/123/90'


@pytest.mark.asyncio
async def test_dm_autoreply_once_and_notification(session,monkeypatch):
    opts=db.get_account_settings(session['id'],session['owner_id']);opts.update(dm_notify=True,autorespond=True,reply_text='Hello!',outside_hours=False)
    db.set_account_settings(session['id'],session['owner_id'],opts)
    bot=NS(send_message=AsyncMock());monkeypatch.setattr(runtime,'BOT',bot)
    client=FakeClient(session);message=incoming(True)
    await runtime.handle_message(session,client,message)
    message.id+=1
    await runtime.handle_message(session,client,message)
    assert len(client.sent)==1
    assert client.sent[0][1]=='Hello!\n\n'+product.FOOTER
    assert bot.send_message.await_count==2


@pytest.mark.asyncio
async def test_expired_subscription_no_reply(session,monkeypatch):
    db.remove_subscription(session['owner_id'])
    bot=NS(send_message=AsyncMock());monkeypatch.setattr(runtime,'BOT',bot)
    await runtime.handle_message(session,FakeClient(session),incoming(True))
    assert bot.send_message.await_count==0


def test_outside_hours():
    opts={'outside_hours':True,'quiet_start':9,'quiet_end':18,'timezone':'Europe/Berlin'}
    assert not runtime.allowed_hours(opts,datetime(2026,10,15,10))
    assert runtime.allowed_hours(opts,datetime(2026,10,15,19))


@pytest.mark.asyncio
async def test_delete_revokes_before_removal(session,monkeypatch):
    app=NS(log_out=AsyncMock(),stop=AsyncMock())
    runtime.CLIENTS[session['id']]=app
    async def get_(s):return app
    monkeypatch.setattr(runtime,'get_client',get_)
    assert await runtime.disconnect_account(session['owner_id'],session['id'])
    app.log_out.assert_awaited_once()
    assert not db.get_sessions(session['owner_id'])


@pytest.mark.asyncio
async def test_failed_revoke_retains_stopped_record(session,monkeypatch):
    app=NS(log_out=AsyncMock(side_effect=RuntimeError()),stop=AsyncMock());runtime.CLIENTS[session['id']]=app
    async def get_(s):return app
    monkeypatch.setattr(runtime,'get_client',get_)
    assert not await runtime.disconnect_account(session['owner_id'],session['id'])
    assert db.get_sessions(session['owner_id'])[0]['status']=='revoke_failed'


@pytest_asyncio.fixture
async def client():
    web._LIMITS.clear();web._PREVIEWS.clear()
    async with TestClient(TestServer(web.build_app())) as c:yield c


@pytest.mark.asyncio
async def test_api_idor(client,session,users):
    j=worker.Job(users[0],1,'all',30,'Test',{},None,None);j.account_ids=[session['id']];j.db_id=db.save_job(users[0],'Test',{});j.id=j.db_id;worker.JOBS[j.id]=j
    resp=await client.post('/api/action',json={'initData':signed(users[1]),'action':'pause','job_id':j.id})
    assert resp.status==404 and j.status=='running'


@pytest.mark.asyncio
async def test_api_auth_expiry_and_token(client,users):
    resp=await client.post('/api/state',json={'initData':signed(users[0],age=1000)})
    assert resp.status==403
    data=await (await client.post('/api/auth/verify',json={'initData':signed(users[0])})).json()
    assert data['access_token']
    resp=await client.post('/api/state',json={},headers={'Authorization':'Bearer '+data['access_token']})
    assert resp.status==200


@pytest.mark.asyncio
async def test_api_origin_rejected(client,users):
    resp=await client.post('/api/state',json={'initData':signed(users[0])},headers={'Origin':'https://evil.example'})
    assert resp.status==403
    assert resp.headers.get('Access-Control-Allow-Origin')!='*'


@pytest.mark.asyncio
async def test_settings_idor(client,session,users):
    resp=await client.post('/api/settings',json={'initData':signed(users[1]),'account_id':session['id']})
    assert resp.status==404


@pytest.mark.asyncio
async def test_launch_requires_server_preview(client,session,users):
    resp=await client.post('/api/action',json={'initData':signed(users[0]),'action':'start','text':'Hello','targets':{str(session['id']):[44444]}})
    assert resp.status==400


@pytest.mark.asyncio
async def test_server_preview_and_single_use(client,session,users,monkeypatch):
    app=FakeClient(session)
    async def open_(s):return app
    monkeypatch.setattr(worker,'_open',open_)
    body={'initData':signed(users[0]),'action':'preview','text':'Hello','delay':30,'cycles':1,'account_ids':[session['id']]}
    resp=await client.post('/api/action',json=body);d=await resp.json()
    assert d['ok'],d
    assert d['recipients'][0]['chat_id']==-100123
    resp=await client.post('/api/action',json={'initData':signed(users[0]),'action':'start','preview_id':d['preview_id']})
    assert resp.status==200,await resp.json()
    await asyncio.gather(*(j.task for j in worker.JOBS.values()))
    resp=await client.post('/api/action',json={'initData':signed(users[0]),'action':'start','preview_id':d['preview_id']})
    assert resp.status==400


@pytest.mark.asyncio
async def test_trial_full_pro_via_api(client):
    uid=99999
    resp=await client.post('/api/trial',json={'initData':signed(uid)})
    assert resp.status==200,await resp.json()
    assert db.is_paid_plan(uid)
    resp=await client.post('/api/trial',json={'initData':signed(uid)})
    assert resp.status==400


@pytest.mark.asyncio
async def test_preview_bearer_cannot_renew_without_fresh_initdata(client,users):
    data=await (await client.post('/api/auth/verify',json={'initData':signed(users[0])})).json()
    response=await client.post('/api/auth/verify',json={},headers={'Authorization':'Bearer '+data['access_token']})
    assert response.status==403


@pytest.mark.asyncio
async def test_paused_restore_keeps_pause(session,users,monkeypatch):
    app=FakeClient(session)
    async def open_(s):return app
    monkeypatch.setattr(worker,'_open',open_)
    jid=db.save_job(users[0],'Paused',{},status='paused')
    j=await worker.start_broadcast_task({'type':'text','text':'Hello'},30,'all',users[0],targets={str(session['id']):[-100123]},resume_db_id=jid,resume_status='paused')
    await asyncio.sleep(.05)
    assert j.status=='paused' and not app.sent
    worker.resume_job(j.id,users[0]);await j.task
    assert len(app.sent)==1


@pytest.mark.asyncio
async def test_uncertain_delivery_not_repeated(session,users,monkeypatch):
    app=FakeClient(session)
    async def open_(s):return app
    monkeypatch.setattr(worker,'_open',open_)
    jid=db.save_job(users[0],'Pending',{})
    db.claim_delivery(jid,session['id'],1,-100123)
    j=await worker.start_broadcast_task({'type':'text','text':'Hello'},30,'all',users[0],targets={str(session['id']):[-100123]},resume_db_id=jid)
    await j.task
    assert not app.sent
    assert db.delivery_rows(jid)[0]['status']=='uncertain'


@pytest.mark.asyncio
async def test_folder_ids_are_scoped_to_each_account(session,users,monkeypatch):
    db.add_session(users[0],'+10000000002',1,'hash','session2')
    sessions=db.get_sessions(users[0]);seen=[]
    async def open_(s):return FakeClient(s)
    async def folders(app,ids,names=None):seen.append((app.session['id'],ids));return [-100123] if ids else []
    monkeypatch.setattr(worker,'_open',open_);monkeypatch.setattr(worker,'_folder_chats',folders)
    targets,_=await worker.prepare_targets(users[0],'folders',[sessions[0]['id']*10000+2])
    assert seen==[(sessions[0]['id'],[2])]
    assert targets[str(sessions[1]['id'])]==[]


@pytest.mark.asyncio
async def test_expired_user_cannot_launch(session,users):
    db.remove_subscription(users[0])
    with pytest.raises(ValueError):await worker.start_broadcast_task({'type':'text','text':'Hello'},30,'all',users[0],targets={str(session['id']):[-100123]})


@pytest.mark.asyncio
async def test_already_revoked_session_can_be_removed(session,monkeypatch):
    from pyrogram.errors import SessionRevoked
    async def get_(s):raise SessionRevoked()
    monkeypatch.setattr(runtime,'get_client',get_)
    assert await runtime.disconnect_account(session['owner_id'],session['id'])
    assert not db.get_sessions(session['owner_id'])


@pytest.mark.asyncio
async def test_runtime_never_prompts_for_relogin(session,monkeypatch):
    app=NS(add_handler=lambda *a,**k:None,connect=AsyncMock(return_value=False),disconnect=AsyncMock(),start=AsyncMock())
    monkeypatch.setattr(runtime,'Client',lambda *a,**k:app)
    from pyrogram.errors import AuthKeyUnregistered
    with pytest.raises(AuthKeyUnregistered):await runtime.get_client(session)
    app.start.assert_not_awaited()
    app.disconnect.assert_awaited_once()
