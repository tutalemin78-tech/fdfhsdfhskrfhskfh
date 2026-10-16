# -*- coding: utf-8 -*-
# SendFlow database layer.
# Хранилище: PostgreSQL (Neon). Драйвер синхронный (psycopg2) — сигнатуры всех
# функций сохранены 1-в-1, поэтому остальной код (main / userbot_worker /
# webapp_server) менять не нужно.
#
# Подключение берётся из переменной окружения DATABASE_URL, например:
#   postgresql://user:pass@ep-xxx-pooler.eu-central-1.aws.neon.tech/neondb?sslmode=require
# Для этой сборки нужен Direct URL без -pooler: блокировка процесса
# требует постоянной PostgreSQL-сессии.
import os
import threading
from contextlib import contextmanager
import security
import json as _json
from datetime import datetime, timedelta

import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool


DATABASE_URL = (
    os.getenv("DATABASE_URL")
    or os.getenv("POSTGRES_URL")
    or os.getenv("NEON_DATABASE_URL")
    or ""
).strip()

_POOL = None
_POOL_LOCK = threading.Lock()


def _dsn():
    dsn = DATABASE_URL
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL не задан. Укажи строку подключения Neon Postgres "
            "в переменных окружения (Render → Environment)."
        )
    # Neon требует SSL. Если в строке его нет — добавим.
    if "sslmode=" not in dsn:
        dsn += ("&" if "?" in dsn else "?") + "sslmode=require"
    return dsn


def _pool():
    global _POOL
    if _POOL is None:
        with _POOL_LOCK:
            if _POOL is None:
                _POOL = ThreadedConnectionPool(1, 10, dsn=_dsn())
    return _POOL


_DB_SLOTS=threading.BoundedSemaphore(10)

def _execute(sql, params=(), fetch=None):
    with _DB_SLOTS:
        return _execute_impl(sql,params,fetch)


def _execute_impl(sql, params=(), fetch=None):
    """Единая точка выполнения запросов с пулом и одним ретраем на разрыв связи.
    fetch: None | 'one' | 'all' | 'id'.
    """
    pool = _pool()
    last_err = None
    for _attempt in range(2):
        conn = pool.getconn()
        try:
            conn.autocommit = True
            factory = RealDictCursor if fetch in ("one", "all") else None
            with conn.cursor(cursor_factory=factory) as cur:
                cur.execute(sql, params)
                if fetch == "one":
                    result = cur.fetchone()
                elif fetch == "all":
                    result = cur.fetchall()
                elif fetch == "id":
                    row = cur.fetchone()
                    result = row[0] if row else None
                else:
                    result = None
            pool.putconn(conn)
            return result
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            last_err = e
            try:
                pool.putconn(conn, close=True)
            except Exception:
                pass
            if not sql.lstrip().upper().startswith("SELECT"):
                raise
            continue
        except Exception:
            try:
                pool.putconn(conn)
            except Exception:
                pass
            raise
    raise last_err


def _fetchone(sql, params=()):
    return _execute(sql, params, "one")


def _fetchall(sql, params=()):
    return _execute(sql, params, "all")


def _exec(sql, params=()):
    return _execute(sql, params, None)


def _insert_id(sql, params=()):
    return _execute(sql, params, "id")


PAID_SOURCES = ("paid", "stars", "manual", "admin", "card", "ton", "ua", "crypto")


def init_db():
    security.cipher()
    """Создаёт все таблицы и индексы. Идемпотентно. Вызывается на старте."""
    _exec("""CREATE TABLE IF NOT EXISTS users(
        telegram_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT,
        first_seen TEXT, last_seen TEXT, trial_used INTEGER DEFAULT 0,
        banned INTEGER DEFAULT 0)""")
    _exec("""CREATE TABLE IF NOT EXISTS sessions(
        id BIGSERIAL PRIMARY KEY, owner_id BIGINT, phone TEXT,
        api_id BIGINT, api_hash TEXT, session_string TEXT,
        status TEXT DEFAULT 'active', created TEXT, proxy TEXT)""")
    _exec("""CREATE TABLE IF NOT EXISTS subscriptions(
        user_id BIGINT PRIMARY KEY, expires_at TEXT, source TEXT,
        notified_expired INTEGER DEFAULT 0)""")
    _exec("""CREATE TABLE IF NOT EXISTS paid_chats(
        user_id BIGINT, chat_id TEXT, PRIMARY KEY(user_id, chat_id))""")
    _exec("""CREATE TABLE IF NOT EXISTS logs(
        id BIGSERIAL PRIMARY KEY, user_id BIGINT, ts TEXT, text TEXT)""")
    _exec("""CREATE TABLE IF NOT EXISTS referrals(
        referrer_id BIGINT, referred_id BIGINT PRIMARY KEY, ts TEXT)""")
    _exec("""CREATE TABLE IF NOT EXISTS templates(
        id BIGSERIAL PRIMARY KEY, user_id BIGINT, name TEXT,
        payload TEXT, created TEXT)""")
    _exec("""CREATE TABLE IF NOT EXISTS admin_actions(
        id BIGSERIAL PRIMARY KEY, admin_id BIGINT, action TEXT,
        target_id BIGINT, detail TEXT, ts TEXT)""")
    _exec("""CREATE TABLE IF NOT EXISTS payments(
        id BIGSERIAL PRIMARY KEY, user_id BIGINT, amount DOUBLE PRECISION,
        currency TEXT, source TEXT, ts TEXT)""")
    _exec("""CREATE TABLE IF NOT EXISTS daily_usage(
        user_id BIGINT, day TEXT, cycles INTEGER DEFAULT 0,
        PRIMARY KEY(user_id, day))""")
    _exec("""CREATE TABLE IF NOT EXISTS stats(
        day TEXT PRIMARY KEY, sent BIGINT DEFAULT 0)""")
    _exec("""CREATE TABLE IF NOT EXISTS jobs(
        id BIGSERIAL PRIMARY KEY, user_id BIGINT, name TEXT,
        config TEXT, status TEXT, progress TEXT, created TEXT, updated TEXT)""")
    _exec("""CREATE TABLE IF NOT EXISTS reminders(
        user_id BIGINT, kind TEXT, ts TEXT, PRIMARY KEY(user_id, kind))""")
    # Миграции для старых баз (на свежей Neon просто пройдут вхолостую).
    for stmt in (
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS proxy TEXT",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS banned INTEGER DEFAULT 0",
    ):
        try:
            _exec(stmt)
        except Exception:
            pass
    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_logs_user ON logs(user_id, id DESC)",
        "CREATE INDEX IF NOT EXISTS idx_sessions_owner ON sessions(owner_id)",
        "CREATE INDEX IF NOT EXISTS idx_subs_exp ON subscriptions(expires_at)",
        "CREATE INDEX IF NOT EXISTS idx_daily_user ON daily_usage(user_id, day)",
        "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)",
        "CREATE INDEX IF NOT EXISTS idx_ref_referrer ON referrals(referrer_id)",
    ):
        try:
            _exec(stmt)
        except Exception:
            pass


    init_update_schema()


def track_user(tg_id, username=None, first_name=None):
    now = datetime.now().isoformat(timespec="seconds")
    row = _fetchone("INSERT INTO users(telegram_id,username,first_name,first_seen,last_seen,trial_used) VALUES(%s,%s,%s,%s,%s,0) ON CONFLICT(telegram_id) DO NOTHING RETURNING telegram_id", (tg_id,username,first_name,now,now))
    if not row:
        _exec("UPDATE users SET username=COALESCE(%s,username),first_name=COALESCE(%s,first_name),last_seen=%s WHERE telegram_id=%s", (username,first_name,now,tg_id))
    return bool(row)


def get_all_users():
    rows = _fetchall("SELECT * FROM users ORDER BY first_seen DESC")
    return [dict(r) for r in rows]


def count_users():
    row = _fetchone("SELECT COUNT(*) AS n FROM users")
    return row["n"] if row else 0


def trial_used(user_id):
    row = _fetchone("SELECT trial_used FROM users WHERE telegram_id=%s", (user_id,))
    return bool(row and row["trial_used"])


def add_session(owner_id, phone, api_id, api_hash, ss, proxy=None, telegram_user_id=None):
    import config
    encrypted=(security.encrypt(api_hash),security.encrypt(ss),security.encrypt(proxy))
    with transaction() as cur:
        _lock_user(cur,owner_id)
        cur.execute('SELECT count(*) AS n FROM sessions WHERE owner_id=%s',(owner_id,))
        if cur.fetchone()['n']>=config.PAID_MAX_ACCOUNTS: raise ValueError('Достигнут лимит аккаунтов PRO')
        cur.execute("INSERT INTO sessions(owner_id,phone,api_id,api_hash,session_string,status,created,proxy,telegram_user_id) VALUES(%s,%s,%s,%s,%s,'active',%s,%s,%s)",(owner_id,phone,api_id,encrypted[0],encrypted[1],datetime.now().isoformat(timespec='seconds'),encrypted[2],telegram_user_id))


def get_sessions(owner_id):
    rows = _fetchall("SELECT * FROM sessions WHERE owner_id=%s ORDER BY id", (owner_id,))
    return [_decode_session(dict(r)) for r in rows]


def get_all_sessions():
    # Admin screens never need authentication keys.
    rows = _fetchall("SELECT id,owner_id,phone,status,created FROM sessions ORDER BY id")
    return [dict(r) for r in rows]


def delete_session(session_id, owner_id=None):
    if owner_id is None:
        _exec("DELETE FROM sessions WHERE id=%s", (session_id,))
    else:
        _exec("DELETE FROM sessions WHERE id=%s AND owner_id=%s", (session_id, owner_id))


def delete_all_sessions(owner_id):
    _exec("DELETE FROM sessions WHERE owner_id=%s", (owner_id,))


def subscription_expiry(user_id):
    row = _fetchone("SELECT expires_at FROM subscriptions WHERE user_id=%s", (user_id,))
    if not row or not row["expires_at"]:
        return None
    try:
        return datetime.fromisoformat(row["expires_at"])
    except Exception:
        return None


def set_subscription(user_id, days, source="paid"):
    with transaction() as cur:
        _lock_user(cur, user_id)
        return _extend(cur, user_id, days * 24, source)


def grant_trial(user_id, days):
    with transaction() as cur:
        _lock_user(cur, user_id)
        cur.execute("UPDATE users SET trial_used=1 WHERE telegram_id=%s AND trial_used=0 RETURNING telegram_id", (user_id,))
        if not cur.fetchone(): return None
        return _extend(cur, user_id, days * 24, "trial")


def is_subscribed(user_id):
    exp = subscription_expiry(user_id)
    return bool(exp and exp > datetime.now())


def get_expired_to_notify():
    now = datetime.now().isoformat(timespec="seconds")
    rows = _fetchall("SELECT user_id FROM subscriptions WHERE expires_at < %s AND notified_expired=0",
                     (now,))
    return [r["user_id"] for r in rows]


def mark_notified(user_id):
    _exec("UPDATE subscriptions SET notified_expired=1 WHERE user_id=%s", (user_id,))


def count_active_subs():
    now = datetime.now().isoformat(timespec="seconds")
    row = _fetchone("SELECT COUNT(*) AS n FROM subscriptions WHERE expires_at > %s", (now,))
    return row["n"] if row else 0


def is_paid_chat(user_id, chat_id):
    row = _fetchone("SELECT 1 AS x FROM paid_chats WHERE user_id=%s AND chat_id=%s",
                    (user_id, str(chat_id)))
    return bool(row)


def mark_paid_chat(user_id, chat_id):
    _exec("INSERT INTO paid_chats(user_id,chat_id) VALUES(%s,%s) "
          "ON CONFLICT (user_id, chat_id) DO NOTHING",
          (user_id, str(chat_id)))


def add_log(user_id, text):
    ts = datetime.now().strftime("%H:%M:%S")
    _exec("INSERT INTO logs(user_id,ts,text) VALUES(%s,%s,%s)", (user_id, ts, text))


def get_logs(user_id, limit=25):
    rows = _fetchall("SELECT ts,text FROM logs WHERE user_id=%s ORDER BY id DESC LIMIT %s",
                     (user_id, limit))
    return [{"ts": r["ts"], "text": r["text"]} for r in rows]


def subscription_source(user_id):
    row = _fetchone("SELECT source FROM subscriptions WHERE user_id=%s", (user_id,))
    return (row["source"] if row else None)


def is_paid_plan(user_id):
    """All active subscriptions, including trial and referral bonuses, get PRO."""
    return is_subscribed(user_id)


def add_subscription_hours(user_id, hours, source="referral"):
    with transaction() as cur:
        _lock_user(cur, user_id)
        return _extend(cur, user_id, hours, source)


def remove_subscription(user_id):
    _exec("DELETE FROM subscriptions WHERE user_id=%s", (user_id,))


def get_user_by_username(username):
    if not username:
        return None
    u = username.lstrip("@").strip().lower()
    row = _fetchone("SELECT * FROM users WHERE lower(username)=%s", (u,))
    return dict(row) if row else None


def award_referral(referrer_id, referred_id, hours, eligible=True):
    if not eligible or not referrer_id or int(referrer_id) == int(referred_id): return None
    with transaction() as cur:
        # Consistent advisory lock order prevents reciprocal invitation deadlocks.
        for uid in sorted({int(referrer_id), int(referred_id)}): _lock_user(cur, uid)
        cur.execute("SELECT 1 FROM users WHERE telegram_id=%s AND banned=0", (referrer_id,))
        if not cur.fetchone(): return None
        cur.execute("INSERT INTO referrals(referrer_id,referred_id,ts) VALUES(%s,%s,%s) ON CONFLICT(referred_id) DO NOTHING RETURNING referred_id", (referrer_id,referred_id,datetime.now().isoformat(timespec="seconds")))
        if not cur.fetchone(): return None
        return _extend(cur, referrer_id, hours, "referral")


def count_referrals(user_id):
    row = _fetchone("SELECT COUNT(*) AS n FROM referrals WHERE referrer_id=%s", (user_id,))
    return row["n"] if row else 0


def add_template(user_id,name,payload):
    with transaction() as cur:
        _lock_user(cur,user_id)
        cur.execute('SELECT count(*) AS n FROM templates WHERE user_id=%s',(user_id,))
        if cur.fetchone()['n']>=100: raise ValueError('Достигнут лимит: 100 шаблонов. Удалите ненужный шаблон')
        cur.execute('INSERT INTO templates(user_id,name,payload,created) VALUES(%s,%s,%s,%s) RETURNING id',(user_id,name,payload,datetime.now().isoformat(timespec='seconds')))
        return cur.fetchone()['id']


def get_templates(user_id):
    rows = _fetchall("SELECT * FROM templates WHERE user_id=%s ORDER BY id DESC", (user_id,))
    return [dict(r) for r in rows]


def get_template(template_id, user_id):
    row = _fetchone("SELECT * FROM templates WHERE id=%s AND user_id=%s", (template_id, user_id))
    return dict(row) if row else None


def delete_template(template_id, user_id):
    _exec("DELETE FROM templates WHERE id=%s AND user_id=%s", (template_id, user_id))


def is_banned(user_id):
    try:
        row = _fetchone("SELECT banned FROM users WHERE telegram_id=%s", (user_id,))
    except Exception:
        row = None
    return bool(row and row["banned"])


def ban_user(user_id):
    _exec("UPDATE users SET banned=1 WHERE telegram_id=%s", (user_id,))


def unban_user(user_id):
    _exec("UPDATE users SET banned=0 WHERE telegram_id=%s", (user_id,))


def add_admin_action(admin_id, action, target_id=None, detail=""):
    _exec("INSERT INTO admin_actions(admin_id,action,target_id,detail,ts) VALUES(%s,%s,%s,%s,%s)",
          (admin_id, action, target_id, detail, datetime.now().isoformat(timespec="seconds")))


def get_admin_actions(limit=30):
    rows = _fetchall("SELECT * FROM admin_actions ORDER BY id DESC LIMIT %s", (limit,))
    return [dict(r) for r in rows]


def log_payment(user_id, amount, currency="XTR", source="stars"):
    _exec("INSERT INTO payments(user_id,amount,currency,source,ts) VALUES(%s,%s,%s,%s,%s)",
          (user_id, amount, currency, source, datetime.now().isoformat(timespec="seconds")))


def revenue_stars(days=None):
    """Сумма звёзд за период (или за всё время, если days=None)."""
    if days:
        since = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
        row = _fetchone("SELECT COALESCE(SUM(amount),0) AS s FROM payments WHERE currency='XTR' AND ts>=%s",
                        (since,))
    else:
        row = _fetchone("SELECT COALESCE(SUM(amount),0) AS s FROM payments WHERE currency='XTR'")
    return int(row["s"] or 0) if row else 0


def count_paid_subs():
    """Активные ПЛАТНЫЕ подписки (без триала)."""
    now = datetime.now().isoformat(timespec="seconds")
    marks = ",".join("%s" for _ in PAID_SOURCES)
    row = _fetchone("SELECT COUNT(*) AS n FROM subscriptions WHERE expires_at>%s AND source IN (%s)" % ("%s", marks),
                    (now, *PAID_SOURCES))
    return row["n"] if row else 0


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def get_daily_cycles(user_id):
    """Сколько циклов уже истрачено сегодня."""
    row = _fetchone("SELECT cycles FROM daily_usage WHERE user_id=%s AND day=%s",
                    (user_id, _today()))
    return int(row["cycles"]) if row else 0


def add_daily_cycles(user_id, n):
    """Добавляет n циклов к сегодняшнему расходу. Возвращает новое значение."""
    row = _fetchone("""INSERT INTO daily_usage(user_id, day, cycles) VALUES(%s,%s,%s)
                       ON CONFLICT(user_id, day) DO UPDATE SET
                         cycles=daily_usage.cycles+EXCLUDED.cycles
                       RETURNING cycles""",
                    (user_id, _today(), int(n)))
    return int(row["cycles"]) if row else int(n)


def daily_cycles_left(user_id, limit):
    return max(int(limit) - get_daily_cycles(user_id), 0)


def bump_sent(n=1):
    """Увеличивает счётчик успешно отправленных сообщений за сегодня."""
    try:
        _exec("""INSERT INTO stats(day, sent) VALUES(%s,%s)
                 ON CONFLICT(day) DO UPDATE SET sent=stats.sent+EXCLUDED.sent""",
              (_today(), int(n)))
    except Exception:
        pass


def sent_today():
    row = _fetchone("SELECT sent FROM stats WHERE day=%s", (_today(),))
    return int(row["sent"]) if (row and row["sent"]) else 0


def sent_total():
    row = _fetchone("SELECT COALESCE(SUM(sent),0) AS s FROM stats")
    return int(row["s"] or 0) if row else 0


def save_job(user_id, name, config_dict, status="running", progress=None, job_db_id=None):
    """Сохраняет/обновляет состояние рассылки. Возвращает id записи в БД."""
    now = datetime.now().isoformat(timespec="seconds")
    cfg = _json.dumps(config_dict, ensure_ascii=False)
    prg = _json.dumps(progress or {}, ensure_ascii=False)
    if job_db_id:
        _exec("UPDATE jobs SET status=%s, progress=%s, updated=%s WHERE id=%s",
              (status, prg, now, job_db_id))
        return job_db_id
    return _insert_id("INSERT INTO jobs(user_id,name,config,status,progress,created,updated) "
                      "VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                      (user_id, name, cfg, status, prg, now, now))


def update_job_status(job_db_id, status, progress=None):
    if not job_db_id:
        return
    now = datetime.now().isoformat(timespec="seconds")
    if progress is not None:
        _exec("UPDATE jobs SET status=%s, progress=%s, updated=%s WHERE id=%s",
              (status, _json.dumps(progress, ensure_ascii=False), now, job_db_id))
    else:
        _exec("UPDATE jobs SET status=%s, updated=%s WHERE id=%s", (status, now, job_db_id))


def get_resumable_jobs():
    """Рассылки, которые были активны в момент остановки бота."""
    rows = _fetchall("SELECT * FROM jobs WHERE status IN ('running','paused') ORDER BY id")
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["config"] = _json.loads(d.get("config") or "{}")
        except Exception:
            d["config"] = {}
        try:
            d["progress"] = _json.loads(d.get("progress") or "{}")
        except Exception:
            d["progress"] = {}
        out.append(d)
    return out


def reminder_sent(user_id, kind):
    row = _fetchone("SELECT 1 AS x FROM reminders WHERE user_id=%s AND kind=%s", (user_id, kind))
    return bool(row)


def mark_reminder(user_id, kind):
    _exec("INSERT INTO reminders(user_id,kind,ts) VALUES(%s,%s,%s) "
          "ON CONFLICT (user_id, kind) DO UPDATE SET ts=EXCLUDED.ts",
          (user_id, kind, datetime.now().isoformat(timespec="seconds")))


def clear_reminders(user_id):
    """После продления подписки сбрасываем отметки, чтобы воронка работала снова."""
    _exec("DELETE FROM reminders WHERE user_id=%s", (user_id,))


def get_active_trials():
    """Юзеры с активным триалом (для напоминаний «осталось N дней»)."""
    now = datetime.now().isoformat(timespec="seconds")
    rows = _fetchall("SELECT user_id, expires_at FROM subscriptions WHERE source='trial' AND expires_at>%s",
                     (now,))
    return [(r["user_id"], r["expires_at"]) for r in rows]


def referral_leaderboard(limit=10):
    rows = _fetchall("""SELECT r.referrer_id AS uid, COUNT(*) AS n,
                               u.username AS username, u.first_name AS first_name
                        FROM referrals r LEFT JOIN users u ON u.telegram_id=r.referrer_id
                        GROUP BY r.referrer_id, u.username, u.first_name
                        ORDER BY n DESC LIMIT %s""", (limit,))
    return [dict(r) for r in rows]


def referral_rank(user_id):
    """Место юзера в рейтинге рефералов (1 = лучший) или None."""
    rows = _fetchall("""SELECT referrer_id, COUNT(*) AS n FROM referrals
                        GROUP BY referrer_id ORDER BY n DESC""")
    for i, r in enumerate(rows, 1):
        if r["referrer_id"] == user_id:
            return i, r["n"]
    return None, 0


def count_templates(user_id):
    row = _fetchone("SELECT COUNT(*) AS n FROM templates WHERE user_id=%s", (user_id,))
    return int(row["n"]) if row else 0


@contextmanager
def transaction():
    _DB_SLOTS.acquire()
    try:
        pool = _pool()
        conn = pool.getconn()
    except BaseException:
        _DB_SLOTS.release()
        raise
    try:
        conn.autocommit = False
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.autocommit = True
        pool.putconn(conn)
        _DB_SLOTS.release()


def _lock_user(cur, uid):
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (int(uid),))


def _extend(cur, uid, hours, source):
    cur.execute("SELECT expires_at,source FROM subscriptions WHERE user_id=%s FOR UPDATE", (uid,))
    row = cur.fetchone()
    now = datetime.now()
    base = max(now, datetime.fromisoformat(row['expires_at'])) if row and row['expires_at'] else now
    exp = base + timedelta(hours=hours)
    # Referral bonuses must not turn an active paid/trial subscription into another plan.
    if source in ('ref', 'referral') and row and base > now:
        source = row['source']
    cur.execute("INSERT INTO subscriptions(user_id,expires_at,source,notified_expired) VALUES(%s,%s,%s,0) ON CONFLICT(user_id) DO UPDATE SET expires_at=EXCLUDED.expires_at,source=EXCLUDED.source,notified_expired=0", (uid,exp.isoformat(timespec='seconds'),source))
    return exp


def _decode_session(row):
    for key in ('api_hash','session_string','proxy'):
        row[key] = security.decrypt(row.get(key))
    return row


def init_update_schema():
    for sql in (
        "ALTER TABLE sessions ADD COLUMN IF NOT EXISTS telegram_user_id BIGINT",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_tg_user ON sessions(telegram_user_id) WHERE telegram_user_id IS NOT NULL",
        "CREATE TABLE IF NOT EXISTS schema_updates(name TEXT PRIMARY KEY, ts TIMESTAMPTZ DEFAULT now())",
        "CREATE TABLE IF NOT EXISTS payment_receipts(charge_id TEXT PRIMARY KEY,user_id BIGINT,amount INTEGER,currency TEXT,ts TIMESTAMPTZ DEFAULT now())",
        "CREATE TABLE IF NOT EXISTS account_settings(session_id BIGINT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,owner_id BIGINT,reply_notify BOOLEAN DEFAULT TRUE,dm_notify BOOLEAN DEFAULT FALSE,autorespond BOOLEAN DEFAULT FALSE,reply_text TEXT DEFAULT '',timezone TEXT DEFAULT 'Europe/Berlin',quiet_start INTEGER DEFAULT 9,quiet_end INTEGER DEFAULT 18,outside_hours BOOLEAN DEFAULT FALSE)",
        "CREATE TABLE IF NOT EXISTS outbound_messages(session_id BIGINT,chat_id BIGINT,message_id BIGINT,user_id BIGINT,body TEXT,ts TIMESTAMPTZ DEFAULT now(),PRIMARY KEY(session_id,chat_id,message_id))",
        "CREATE TABLE IF NOT EXISTS inbound_seen(session_id BIGINT,chat_id BIGINT,message_id BIGINT,PRIMARY KEY(session_id,chat_id,message_id))",
        "CREATE TABLE IF NOT EXISTS autoreply_times(session_id BIGINT,peer_id BIGINT,last_at TIMESTAMPTZ,PRIMARY KEY(session_id,peer_id))",
        "CREATE TABLE IF NOT EXISTS broadcast_deliveries(job_id BIGINT,session_id BIGINT,cycle INTEGER,chat_id BIGINT,status TEXT,updated TIMESTAMPTZ DEFAULT now(),PRIMARY KEY(job_id,session_id,cycle,chat_id))",
        "CREATE TABLE IF NOT EXISTS scheduled_jobs(id BIGSERIAL PRIMARY KEY,user_id BIGINT,config TEXT,next_at TIMESTAMPTZ,timezone TEXT,repeat_kind TEXT,weekdays TEXT DEFAULT '[]',status TEXT DEFAULT 'pending',last_error TEXT,created TIMESTAMPTZ DEFAULT now())",
        "CREATE INDEX IF NOT EXISTS idx_sched_pending ON scheduled_jobs(next_at) WHERE status='pending'",
        "CREATE INDEX IF NOT EXISTS idx_outbound_ts ON outbound_messages(ts)",
    ): _exec(sql)
    # All steps in one transaction: rollback also rolls back the migration marker.
    with transaction() as cur:
        cur.execute("INSERT INTO schema_updates(name) VALUES('trial_4_days_v1') ON CONFLICT DO NOTHING RETURNING name")
        if cur.fetchone():
            cur.execute("UPDATE subscriptions SET expires_at=((expires_at::timestamp)+interval '1 day')::text WHERE source='trial' AND expires_at::timestamp > now()::timestamp")
        cur.execute("SELECT id,api_hash,session_string,proxy FROM sessions FOR UPDATE")
        for row in cur.fetchall():
            values=[]
            for key in ('api_hash','session_string','proxy'):
                val=row[key]
                values.append(security.encrypt(security.decrypt(val) if val and val.startswith(security.PREFIX) else val))
            cur.execute("UPDATE sessions SET api_hash=%s,session_string=%s,proxy=%s WHERE id=%s", (*values,row['id']))


def apply_stars_payment(uid, charge_id, amount, currency, days):
    if not charge_id: raise ValueError('Missing charge ID')
    with transaction() as cur:
        _lock_user(cur,uid)
        cur.execute("INSERT INTO payment_receipts(charge_id,user_id,amount,currency) VALUES(%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING charge_id",(charge_id,uid,amount,currency))
        if not cur.fetchone(): return None
        exp=_extend(cur,uid,days*24,'stars')
        cur.execute("INSERT INTO payments(user_id,amount,currency,source,ts) VALUES(%s,%s,%s,'stars',%s)",(uid,amount,currency,datetime.now().isoformat(timespec='seconds')))
        return exp


def get_account_settings(sid, uid):
    row=_fetchone("SELECT * FROM account_settings WHERE session_id=%s AND owner_id=%s",(sid,uid))
    return dict(row) if row else {'session_id':sid,'owner_id':uid,'reply_notify':True,'dm_notify':False,'autorespond':False,'reply_text':'','timezone':'Europe/Berlin','quiet_start':9,'quiet_end':18,'outside_hours':False}


def set_account_settings(sid,uid,opts):
    _exec("INSERT INTO account_settings(session_id,owner_id,reply_notify,dm_notify,autorespond,reply_text,timezone,quiet_start,quiet_end,outside_hours) SELECT id,owner_id,%s,%s,%s,%s,%s,%s,%s,%s FROM sessions WHERE id=%s AND owner_id=%s ON CONFLICT(session_id) DO UPDATE SET reply_notify=EXCLUDED.reply_notify,dm_notify=EXCLUDED.dm_notify,autorespond=EXCLUDED.autorespond,reply_text=EXCLUDED.reply_text,timezone=EXCLUDED.timezone,quiet_start=EXCLUDED.quiet_start,quiet_end=EXCLUDED.quiet_end,outside_hours=EXCLUDED.outside_hours",(opts['reply_notify'],opts['dm_notify'],opts['autorespond'],opts['reply_text'],opts['timezone'],opts['quiet_start'],opts['quiet_end'],opts['outside_hours'],sid,uid))


def remember_outbound(sid,cid,mid,uid,body):
    _exec("INSERT INTO outbound_messages(session_id,chat_id,message_id,user_id,body) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",(sid,cid,mid,uid,body[:1500]))


def find_outbound(sid,cid,mid):
    row=_fetchone("SELECT * FROM outbound_messages WHERE session_id=%s AND chat_id=%s AND message_id=%s",(sid,cid,mid))
    return dict(row) if row else None


def claim_inbound(sid,cid,mid):
    return bool(_fetchone("INSERT INTO inbound_seen VALUES(%s,%s,%s) ON CONFLICT DO NOTHING RETURNING message_id",(sid,cid,mid)))


def claim_autoreply(sid,peer):
    return bool(_fetchone("INSERT INTO autoreply_times(session_id,peer_id,last_at) VALUES(%s,%s,now()) ON CONFLICT(session_id,peer_id) DO UPDATE SET last_at=now() WHERE autoreply_times.last_at < now()-interval '24 hours' RETURNING peer_id",(sid,peer)))


def claim_delivery(jid,sid,cycle,cid):
    return bool(_fetchone("INSERT INTO broadcast_deliveries(job_id,session_id,cycle,chat_id,status) VALUES(%s,%s,%s,%s,'pending') ON CONFLICT DO NOTHING RETURNING chat_id",(jid,sid,cycle,cid)))


def finish_delivery(jid,sid,cycle,cid,status):
    _exec("UPDATE broadcast_deliveries SET status=%s,updated=now() WHERE job_id=%s AND session_id=%s AND cycle=%s AND chat_id=%s",(status,jid,sid,cycle,cid))


def delivery_rows(jid):
    return [dict(r) for r in _fetchall("SELECT session_id,cycle,chat_id,status FROM broadcast_deliveries WHERE job_id=%s",(jid,))]


def list_listener_sessions():
    rows=_fetchall("SELECT s.* FROM sessions s LEFT JOIN account_settings a ON a.session_id=s.id WHERE COALESCE(a.reply_notify,TRUE) OR COALESCE(a.dm_notify,FALSE) OR COALESCE(a.autorespond,FALSE)")
    return [_decode_session(dict(r)) for r in rows]

class InstanceAlreadyRunning(RuntimeError):
    """Another process owns the database session lock; safe to wait."""


_INSTANCE_CONN=None

def validate_lock_connection():
    dsn=_dsn()
    options=psycopg2.extensions.parse_dsn(dsn)
    if '-pooler.' in options.get('host','') or options.get('host','').endswith('-pooler'):
        raise RuntimeError('Для этой сборки нужен Neon Direct connection: выключите Connection pooling и обновите DATABASE_URL. Базу не удаляйте')
    return dsn


def acquire_instance_lock():
    global _INSTANCE_CONN
    if _INSTANCE_CONN is not None and not _INSTANCE_CONN.closed:
        return
    conn=psycopg2.connect(validate_lock_connection(),connect_timeout=10,application_name='SendFlow-instance-lock')
    try:
        conn.autocommit=True
        with conn.cursor() as cur:
            cur.execute('SELECT pg_try_advisory_lock(739024001)')
            if not cur.fetchone()[0]:
                raise InstanceAlreadyRunning('SendFlow уже запущен для этой БД. Ожидается освобождение блокировки')
    except BaseException:
        conn.close()
        raise
    _INSTANCE_CONN=conn


def release_instance_lock():
    global _INSTANCE_CONN
    if _INSTANCE_CONN: _INSTANCE_CONN.close()
    _INSTANCE_CONN=None
