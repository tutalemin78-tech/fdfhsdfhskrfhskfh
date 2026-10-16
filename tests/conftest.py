import os
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
# An explicit disposable DB is required; tests never default to the production DSN.
url=os.environ.get('TEST_DATABASE_URL','')
if not url or 'sendflow_test' not in url or not any(x in url for x in ('127.0.0.1','localhost')):
    raise RuntimeError('Set TEST_DATABASE_URL to a disposable local sendflow_test database')
os.environ['DATABASE_URL']=url
os.environ['BOT_TOKEN']='123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
os.environ['DATA_DIR']=str(Path(__file__).parent/'.test-data')
os.environ['REQUIRED_CHANNEL']=''
os.environ['SESSION_ENCRYPTION_KEYS']='YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE='
import pytest
import database as db

@pytest.fixture(autouse=True)
def clean_db():
    db.init_db()
    with db.transaction() as cur:
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        tables=[r['tablename'] for r in cur.fetchall() if r['tablename']!='schema_updates']
        from psycopg2 import sql
        cur.execute(sql.SQL('TRUNCATE {} RESTART IDENTITY CASCADE').format(sql.SQL(',').join(map(sql.Identifier,tables))))
    import userbot_worker as worker
    import account_runtime as runtime
    worker.JOBS.clear(); worker.SHUTTING_DOWN=False
    runtime.BLOCKED.clear();runtime.CLIENTS.clear();runtime.START_LOCKS.clear();runtime.JOB_LOCKS.clear();runtime.SEND_LOCKS.clear()
    worker._START_LOCK=__import__('asyncio').Lock()
    yield

@pytest.fixture
def users():
    for uid in (11111,22222,33333):
        db.track_user(uid,'test'+str(uid),'Test')
        db.set_subscription(uid,4,'trial')
    return (11111,22222,33333)

@pytest.fixture
def session(users):
    db.add_session(users[0],'+10000000001',123,'hash','fake-session')
    return db.get_sessions(users[0])[0]
