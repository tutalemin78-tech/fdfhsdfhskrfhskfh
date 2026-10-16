from datetime import datetime,timedelta,timezone
from concurrent.futures import ThreadPoolExecutor
import pytest
import database as db
import config,security,scheduling


def test_session_encrypted_at_rest(session):
    row=db._fetchone('SELECT * FROM sessions WHERE id=%s',(session['id'],))
    assert row['session_string'].startswith(security.PREFIX)
    assert db.get_sessions(session['owner_id'])[0]['session_string']=='fake-session'
    assert 'session_string' not in db.get_all_sessions()[0]


def test_duplicate_account_rejected(users):
    db.add_session(users[0],'+10000000001',1,'hash','session',telegram_user_id=999)
    with pytest.raises(Exception): db.add_session(users[1],'+10000000002',1,'hash','session2',telegram_user_id=999)


def test_trial_once_concurrently():
    uid=12345;db.track_user(uid)
    with ThreadPoolExecutor(max_workers=4) as pool:
        values=list(pool.map(lambda _:db.grant_trial(uid,4),range(4)))
    assert sum(v is not None for v in values)==1
    assert 3.99<(db.subscription_expiry(uid)-datetime.now()).total_seconds()/86400<=4
    assert db.is_paid_plan(uid)


def test_referral_atomic_once(users):
    before=db.subscription_expiry(users[0])
    with ThreadPoolExecutor(max_workers=4) as pool:
        values=list(pool.map(lambda _:db.award_referral(users[0],users[1],6),range(4)))
    assert sum(v is not None for v in values)==1
    assert db.count_referrals(users[0])==1
    assert db.subscription_expiry(users[0])-before==timedelta(hours=6)
    assert db.subscription_source(users[0])=='trial'
    assert db.award_referral(users[1],users[1],6) is None
    assert db.award_referral(users[0],users[2],6,eligible=False) is None


def test_paid_extension_concurrent(users):
    before=db.subscription_expiry(users[0])
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _:db.set_subscription(users[0],1),range(4)))
    assert db.subscription_expiry(users[0])-before==timedelta(days=4)


def test_payment_unique(users):
    before=db.subscription_expiry(users[0])
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:db.apply_stars_payment(users[0],'charge-test',50,'XTR',30),range(4)))
    assert sum(v is not None for v in results)==1
    assert db.subscription_expiry(users[0])-before==timedelta(days=30)
    assert db.revenue_stars()==50


def test_trial_migration_once(users):
    before=db.subscription_expiry(users[0])
    db._exec("DELETE FROM schema_updates WHERE name='trial_4_days_v1'")
    db.init_update_schema();db.init_update_schema()
    assert db.subscription_expiry(users[0])-before==timedelta(days=1)


def test_delivery_claim_once(session):
    sid=session['id'];jid=db.save_job(session['owner_id'],'Test',{})
    assert db.claim_delivery(jid,sid,1,-100123)
    assert not db.claim_delivery(jid,sid,1,-100123)
    db.finish_delivery(jid,sid,1,-100123,'sent')
    assert db.delivery_rows(jid)[0]['status']=='sent'


def test_account_settings_scoped(session,users):
    opts=dict(reply_notify=True,dm_notify=True,autorespond=True,reply_text='Hello',timezone='Europe/Berlin',quiet_start=9,quiet_end=18,outside_hours=False)
    db.set_account_settings(session['id'],users[0],opts)
    assert db.get_account_settings(session['id'],users[0])['reply_text']=='Hello'
    db.set_account_settings(session['id'],users[1],dict(opts,reply_text='Wrong owner'))
    assert db.get_account_settings(session['id'],users[0])['reply_text']=='Hello'


def test_autoreply_reservation(session):
    assert db.claim_autoreply(session['id'],123)
    assert not db.claim_autoreply(session['id'],123)
    assert db.claim_autoreply(session['id'],456)


def test_scheduled_storage_and_owner(session,users):
    future=(datetime.now()+timedelta(days=3)).replace(microsecond=0).isoformat()
    launch={'payload':{'type':'text','text':'Hello'},'cycles':1,'cooldown':30,'target_type':'all','account_ids':[session['id']],'targets':{str(session['id']):[-100123]}}
    sid=scheduling.create(users[0],launch,future,'Europe/Berlin')
    assert len(scheduling.list_for(users[0]))==1
    assert not scheduling.cancel(users[1],sid)
    assert scheduling.reschedule(users[0],sid,(datetime.now()+timedelta(days=4)).isoformat(),'Europe/Berlin')
    assert scheduling.cancel(users[0],sid)
    assert not scheduling.list_for(users[0])


def test_account_limit_is_atomic(users,monkeypatch):
    monkeypatch.setattr(config,'PAID_MAX_ACCOUNTS',2)
    def add(i):
        try: db.add_session(users[0],f'+1000000000{i}',1,'hash','session');return True
        except ValueError:return False
    with ThreadPoolExecutor(max_workers=4) as pool:results=list(pool.map(add,range(4)))
    assert sum(results)==2
    assert len(db.get_sessions(users[0]))==2


def test_template_limit_enforced_for_all_callers(users):
    for i in range(100):db.add_template(users[0],str(i),'{}')
    with pytest.raises(ValueError):db.add_template(users[0],'over limit','{}')
