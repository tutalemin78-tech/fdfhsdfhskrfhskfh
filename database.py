# -*- coding: utf-8 -*-
# SendFlow database layer.
# Хранилище: PostgreSQL (Neon). Драйвер синхронный (psycopg2) — сигнатуры всех
# функций сохранены 1-в-1, поэтому остальной код (main / userbot_worker /
# webapp_server) менять не нужно.
#
# Подключение берётся из переменной окружения DATABASE_URL, например:
#   postgresql://user:pass@ep-xxx-pooler.eu-central-1.aws.neon.tech/neondb?sslmode=require
# На Neon бери строку "Pooled connection" (host с -pooler) — она держит много
# коротких подключений.
import os
import threading
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


def _execute(sql, params=(), fetch=None):
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


# ===== USERS =====
def track_user(tg_id, username=None, first_name=None):
    now = datetime.now().isoformat(timespec="seconds")
    row = _fetchone("SELECT telegram_id FROM users WHERE telegram_id=%s", (tg_id,))
    if row:
        _exec("UPDATE users SET username=%s, first_name=%s, last_seen=%s WHERE telegram_id=%s",
              (username, first_name, now, tg_id))
    else:
        _exec("INSERT INTO users(telegram_id,username,first_name,first_seen,last_seen,trial_used) "
              "VALUES(%s,%s,%s,%s,%s,0)", (tg_id, username, first_name, now, now))


def get_all_users():
    rows = _fetchall("SELECT * FROM users ORDER BY first_seen DESC")
    return [dict(r) for r in rows]


def count_users():
    row = _fetchone("SELECT COUNT(*) AS n FROM users")
    return row["n"] if row else 0


def trial_used(user_id):
    row = _fetchone("SELECT trial_used FROM users WHERE telegram_id=%s", (user_id,))
    return bool(row and row["trial_used"])


# ===== SESSIONS =====
def add_session(owner_id, phone, api_id, api_hash, ss, proxy=None):
    now = datetime.now().isoformat(timespec="seconds")
    _exec("INSERT INTO sessions(owner_id,phone,api_id,api_hash,session_string,status,created,proxy) "
          "VALUES(%s,%s,%s,%s,%s,%s,%s,%s)",
          (owner_id, phone, api_id, api_hash, ss, "active", now, proxy))


def get_sessions(owner_id):
    rows = _fetchall("SELECT * FROM sessions WHERE owner_id=%s ORDER BY id", (owner_id,))
    return [dict(r) for r in rows]


def get_all_sessions():
    rows = _fetchall("SELECT * FROM sessions ORDER BY id")
    return [dict(r) for r in rows]


def delete_session(session_id, owner_id=None):
    if owner_id is None:
        _exec("DELETE FROM sessions WHERE id=%s", (session_id,))
    else:
        _exec("DELETE FROM sessions WHERE id=%s AND owner_id=%s", (session_id, owner_id))


def delete_all_sessions(owner_id):
    _exec("DELETE FROM sessions WHERE owner_id=%s", (owner_id,))


# ===== SUBSCRIPTIONS =====
def subscription_expiry(user_id):
    row = _fetchone("SELECT expires_at FROM subscriptions WHERE user_id=%s", (user_id,))
    if not row or not row["expires_at"]:
        return None
    try:
        return datetime.fromisoformat(row["expires_at"])
    except Exception:
        return None


def set_subscription(user_id, days, source="paid"):
    base = datetime.now()
    cur_exp = subscription_expiry(user_id)
    if cur_exp and cur_exp > base:
        base = cur_exp
    exp = base + timedelta(days=days)
    _exec("""INSERT INTO subscriptions(user_id,expires_at,source,notified_expired)
             VALUES(%s,%s,%s,0)
             ON CONFLICT(user_id) DO UPDATE SET
               expires_at=EXCLUDED.expires_at, source=EXCLUDED.source, notified_expired=0""",
          (user_id, exp.isoformat(timespec="seconds"), source))
    return exp


def grant_trial(user_id, days):
    """Выдаёт бесплатный период ОДИН раз. Возвращает дату окончания или None."""
    row = _fetchone("SELECT trial_used FROM users WHERE telegram_id=%s", (user_id,))
    if row and row["trial_used"]:
        return None
    _exec("UPDATE users SET trial_used=1 WHERE telegram_id=%s", (user_id,))
    return set_subscription(user_id, days, source="trial")


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


# ===== PAID CHATS =====
def is_paid_chat(user_id, chat_id):
    row = _fetchone("SELECT 1 AS x FROM paid_chats WHERE user_id=%s AND chat_id=%s",
                    (user_id, str(chat_id)))
    return bool(row)


def mark_paid_chat(user_id, chat_id):
    _exec("INSERT INTO paid_chats(user_id,chat_id) VALUES(%s,%s) "
          "ON CONFLICT (user_id, chat_id) DO NOTHING",
          (user_id, str(chat_id)))


# ===== LOGS =====
def add_log(user_id, text):
    ts = datetime.now().strftime("%H:%M:%S")
    _exec("INSERT INTO logs(user_id,ts,text) VALUES(%s,%s,%s)", (user_id, ts, text))


def get_logs(user_id, limit=25):
    rows = _fetchall("SELECT ts,text FROM logs WHERE user_id=%s ORDER BY id DESC LIMIT %s",
                     (user_id, limit))
    return [{"ts": r["ts"], "text": r["text"]} for r in rows]


# ===== РЕФЕРАЛЫ / ШАБЛОНЫ / РАСШИРЕННЫЕ ПОДПИСКИ =====
def subscription_source(user_id):
    row = _fetchone("SELECT source FROM subscriptions WHERE user_id=%s", (user_id,))
    return (row["source"] if row else None)


def is_paid_plan(user_id):
    """True, если у пользователя активна именно ПЛАТНАЯ подписка (не тр��ал)."""
    if not is_subscribed(user_id):
        return False
    src = subscription_source(user_id)
    return (src in PAID_SOURCES) if src else False


def add_subscription_hours(user_id, hours, source="referral"):
    """Продлевает подписку на N часов (для рефералов и бонусов)."""
    row = _fetchone("SELECT expires_at FROM subscriptions WHERE user_id=%s", (user_id,))
    now = datetime.now()
    base = now
    if row and row["expires_at"]:
        try:
            cur_exp = datetime.fromisoformat(row["expires_at"])
            if cur_exp > now:
                base = cur_exp
        except Exception:
            base = now
    new_exp = (base + timedelta(hours=hours)).isoformat(timespec="seconds")
    if row:
        _exec("UPDATE subscriptions SET expires_at=%s, notified_expired=0 WHERE user_id=%s",
              (new_exp, user_id))
    else:
        _exec("INSERT INTO subscriptions(user_id,expires_at,source,notified_expired) VALUES(%s,%s,%s,0)",
              (user_id, new_exp, source))
    return new_exp


def remove_subscription(user_id):
    _exec("DELETE FROM subscriptions WHERE user_id=%s", (user_id,))


def get_user_by_username(username):
    if not username:
        return None
    u = username.lstrip("@").strip().lower()
    row = _fetchone("SELECT * FROM users WHERE lower(username)=%s", (u,))
    return dict(row) if row else None


def add_referral(referrer_id, referred_id):
    """Фиксирует приглашение. True — если это новый реферал."""
    if not referrer_id or not referred_id or int(referrer_id) == int(referred_id):
        return False
    exists = _fetchone("SELECT 1 AS x FROM referrals WHERE referred_id=%s", (referred_id,))
    if exists:
        return False
    _exec("INSERT INTO referrals(referrer_id,referred_id,ts) VALUES(%s,%s,%s)",
          (referrer_id, referred_id, datetime.now().isoformat(timespec="seconds")))
    return True


def count_referrals(user_id):
    row = _fetchone("SELECT COUNT(*) AS n FROM referrals WHERE referrer_id=%s", (user_id,))
    return row["n"] if row else 0


def add_template(user_id, name, payload):
    return _insert_id("INSERT INTO templates(user_id,name,payload,created) VALUES(%s,%s,%s,%s) RETURNING id",
                      (user_id, name, payload, datetime.now().isoformat(timespec="seconds")))


def get_templates(user_id):
    rows = _fetchall("SELECT * FROM templates WHERE user_id=%s ORDER BY id DESC", (user_id,))
    return [dict(r) for r in rows]


def get_template(template_id, user_id):
    row = _fetchone("SELECT * FROM templates WHERE id=%s AND user_id=%s", (template_id, user_id))
    return dict(row) if row else None


def delete_template(template_id, user_id):
    _exec("DELETE FROM templates WHERE id=%s AND user_id=%s", (template_id, user_id))


# ===== АДМИН: БАН / ЛОГ ДЕЙСТВИЙ / ОПЛАТЫ =====
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


# ===================== ДНЕВНОЙ ЛИМИТ ЦИКЛОВ =====================
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


# ===================== ГЛОБАЛЬНАЯ СТАТИСТИКА (Social Proof / /stats) =====================
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


# ===================== ПЕРСИСТ РАССЫЛОК (возобновление) =====================
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


# ===================== ВОРОНКА НАПОМИНАНИЙ =====================
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


# ===================== ЛИДЕРБОРД РЕФЕРАЛОВ =====================
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
