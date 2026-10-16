import asyncio,json
from types import SimpleNamespace as NS
from datetime import datetime,timezone,timedelta
from unittest.mock import AsyncMock
import pytest
import database as db, scheduling, userbot_worker as worker, config


@pytest.mark.asyncio
@pytest.mark.parametrize('missed',[False,True])
async def test_scheduler_dispatch_or_pause(session,users,monkeypatch,missed):
    launch={'payload':{'type':'text','text':'Hello'},'cycles':1,'cooldown':30,'target_type':'all','account_ids':[session['id']],'targets':{str(session['id']):[-100123]}}
    sid=scheduling.create(users[0],launch,(datetime.now()+timedelta(days=3)).isoformat(),'Europe/Berlin')
    db._exec('UPDATE scheduled_jobs SET next_at=%s WHERE id=%s',(datetime.now(timezone.utc)-timedelta(seconds=300 if missed else 1),sid))
    dispatched=AsyncMock();monkeypatch.setattr(worker,'start_broadcast_task',dispatched)
    finished=asyncio.Event()
    async def notify(*args,**kwargs):finished.set()
    task=asyncio.create_task(scheduling.loop(NS(send_message=notify)))
    try:await asyncio.wait_for(finished.wait(),2)
    finally:
        task.cancel();await asyncio.gather(task,return_exceptions=True)
    if missed:
        assert not dispatched.await_count
        assert scheduling.list_for(users[0])[0]['status']=='paused'
    else:
        assert dispatched.await_count==1
        assert not scheduling.list_for(users[0])


@pytest.mark.asyncio
async def test_payment_precheckout_validation(users):
    import main
    q=NS(invoice_payload=f'subscription_{config.SUB_DAYS}',currency='XTR',total_amount=config.SUB_PRICE_STARS,from_user=NS(id=users[0]),answer=AsyncMock())
    await main.pre_checkout(q)
    assert q.answer.call_args.kwargs['ok'] is True
    q.total_amount=1
    await main.pre_checkout(q)
    assert q.answer.call_args.kwargs['ok'] is False


@pytest.mark.asyncio
async def test_chat_timer_flow_media(session,users):
    import main
    # A real temporary file is used; no Telegram or external account is contacted.
    from pathlib import Path
    media=Path(config.DATA_DIR)/'sample.jpg';media.parent.mkdir(parents=True,exist_ok=True);media.write_bytes(b'test')
    data={'payload':{'type':'photo','path':str(media),'text':'Hello'},'targets':{str(session['id']):[-100123]},'account_ids':[session['id']],'schedule_local':(datetime.now()+timedelta(days=3)).isoformat(),'schedule_zone':'Europe/Berlin','cycles':1,'delay':30}
    state=NS(get_data=AsyncMock(return_value=data),clear=AsyncMock())
    message=NS(text='один раз',from_user=NS(id=users[0]),answer=AsyncMock())
    await main.st_schedule_repeat(message,state)
    assert len(scheduling.list_for(users[0]))==1
    state.clear.assert_awaited_once()
