"""One long-lived Pyrogram connection per account; notifications survive broadcasts."""
import asyncio
import html
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from pyrogram import Client, filters, errors
from pyrogram.raw.functions.updates import GetState
from pyrogram.handlers import MessageHandler
from pyrogram.enums import ChatType, ParseMode
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
import config
import database as db
from product import branded

log=logging.getLogger('sendflow.runtime')
BOT=None
CLIENTS={}
START_LOCKS={}
JOB_LOCKS={}
SEND_LOCKS={}
BLOCKED=set()
WATCHER=None


def job_lock(sid): return JOB_LOCKS.setdefault(sid, asyncio.Lock())
def send_lock(sid): return SEND_LOCKS.setdefault(sid, asyncio.Lock())


def allowed_hours(opts, now=None):
    if not opts.get('outside_hours'): return True
    hour=(now or datetime.now(ZoneInfo(opts['timezone']))).hour
    begin,end=opts['quiet_start'],opts['quiet_end']
    working=(begin <= hour < end) if begin < end else (hour >= begin or hour < end)
    return not working


def message_url(message):
    if message.chat.type in (ChatType.GROUP,ChatType.SUPERGROUP,ChatType.CHANNEL):
        if getattr(message.chat,'username',None):
            return f'https://t.me/{message.chat.username}/{message.id}', 'Ответить в Telegram'
        if str(message.chat.id).startswith('-100'):
            return f'https://t.me/c/{str(message.chat.id)[4:]}/{message.id}', 'Ответить в Telegram'
    user=message.from_user
    if user and user.username: return f'https://t.me/{user.username}', 'Открыть диалог'
    if user: return f'tg://user?id={user.id}', 'Открыть профиль'
    return None,None


async def handle_message(session,client,message):
    uid,sid=session['owner_id'],session['id']
    if sid in BLOCKED or not BOT or message.outgoing or not message.from_user or message.from_user.is_bot or getattr(message.from_user,'is_self',False) or message.service:
        return
    try:
        if await asyncio.to_thread(db.is_banned,uid): return
        if uid not in config.ADMIN_IDS and not await asyncio.to_thread(db.is_subscribed,uid): return
        opts=await asyncio.to_thread(db.get_account_settings,sid,uid)
        reply_id=getattr(message,'reply_to_message_id',None)
        original=await asyncio.to_thread(db.find_outbound,sid,message.chat.id,reply_id) if reply_id else None
        private=message.chat.type==ChatType.PRIVATE
        if not ((opts['reply_notify'] and original) or (private and (opts['dm_notify'] or opts['autorespond']))): return
        if not await asyncio.to_thread(db.claim_inbound,sid,message.chat.id,message.id): return
        body=message.text or message.caption or '[Медиа или другое сообщение]'
        user=message.from_user
        name=' '.join(x for x in [user.first_name,user.last_name] if x) or str(user.id)
        if (original and opts['reply_notify']) or (private and opts['dm_notify']):
            title='💬 Ответ на вашу рассылку' if original else '✉️ Новое личное сообщение'
            phone=session.get('phone','')
            text=f'<b>{title}</b>\nАккаунт: ••••{html.escape(phone[-4:])}\nОт: {html.escape(name)}'
            if not private: text+='\nЧат: '+html.escape(message.chat.title or str(message.chat.id))
            text+='\n\n<blockquote>'+html.escape(body[:1000])+'</blockquote>'
            if original: text+='\n\nВаше сообщение:\n<blockquote>'+html.escape(original['body'][:500])+'</blockquote>'
            url,label=message_url(message)
            markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=label,url=url)]]) if url else None
            if private: text+='\n\nЛичный диалог откроется только в аккаунте, которому написали.'
            await BOT.send_message(uid,text,reply_markup=markup)
        if private and opts['autorespond'] and opts['reply_text'] and allowed_hours(opts):
            if await asyncio.to_thread(db.claim_autoreply,sid,message.chat.id):
                async with send_lock(sid):
                    if sid not in BLOCKED:
                        await client.send_message(message.chat.id,branded(html.escape(opts['reply_text'])),parse_mode=ParseMode.HTML)
    except Exception as exc:
        # Never put message contents, session keys or raw API responses into logs.
        log.warning('Incoming handler failed for account %s (%s)',sid,type(exc).__name__)


async def get_client(session):
    sid=session['id']
    if sid in BLOCKED: raise ValueError('Аккаунт отключается')
    async with START_LOCKS.setdefault(sid,asyncio.Lock()):
        if sid in CLIENTS: return CLIENTS[sid]
        from userbot_worker import _parse_proxy
        app=Client(f"sendflow_{sid}",api_id=session['api_id'],api_hash=session['api_hash'],session_string=session['session_string'],in_memory=True,no_updates=False,proxy=_parse_proxy(session.get('proxy')),sleep_threshold=0)
        async def callback(client,message): await handle_message(session,client,message)
        app.add_handler(MessageHandler(callback,filters.incoming),group=0)
        try:
            # Never let a server worker fall back to an interactive phone/code prompt.
            authorized=await app.connect()
            if not authorized: raise errors.AuthKeyUnregistered()
            await app.invoke(GetState())
            app.me=await app.get_me()
            await asyncio.to_thread(db._exec,'UPDATE sessions SET telegram_user_id=%s WHERE id=%s',(app.me.id,sid))
            await app.initialize()
        except BaseException:
            try: await app.disconnect()
            except Exception: pass
            raise
        if sid in BLOCKED:
            await app.stop()
            raise ValueError('Аккаунт отключён')
        CLIENTS[sid]=app
        return app


class Lease:
    """Compatibility for the old worker: .stop() releases its use, not the listener."""
    def __init__(self,session): self.session=session; self.client=None
    async def start(self): self.client=await get_client(self.session); return self
    async def stop(self): pass
    def __getattr__(self,name):
        if self.client is None: raise RuntimeError('Client not started')
        return getattr(self.client,name)


async def close_account(sid,revoke=False):
    BLOCKED.add(sid)
    async with START_LOCKS.setdefault(sid,asyncio.Lock()):
        client=CLIENTS.pop(sid,None)
        if not client: return not revoke
        try:
            if revoke: await client.log_out()
            else: await client.stop()
            return True
        except Exception:
            try: await client.stop()
            except Exception: pass
            return False


async def disconnect_account(uid,sid):
    """Keep the encrypted credential on revoke failure so the user can retry."""
    sessions=await asyncio.to_thread(db.get_sessions,uid)
    session=next((s for s in sessions if s['id']==sid),None)
    if not session: raise ValueError('Аккаунт не найден')
    import userbot_worker as worker
    tasks=[]
    for job in worker.get_user_jobs(uid,active_only=True):
        if sid in getattr(job,'account_ids',[]):
            worker.stop_job(job.id,uid)
            if job.task: tasks.append(job.task)
    if tasks: await asyncio.gather(*tasks,return_exceptions=True)
    try:
        # Open first so log_out also works when this account has no active listener.
        BLOCKED.discard(sid)
        await get_client(session)
        revoked=await close_account(sid,revoke=True)
    except (errors.AuthKeyUnregistered,errors.SessionRevoked,errors.AuthKeyInvalid,errors.SessionExpired,errors.UserDeactivated):
        # Telegram confirms that this key can no longer authorize a client.
        BLOCKED.add(sid)
        revoked=True
    except Exception:
        BLOCKED.add(sid)
        revoked=False
    if revoked:
        await asyncio.to_thread(db.delete_session,sid,uid)
        await asyncio.to_thread(db._exec,'DELETE FROM outbound_messages WHERE session_id=%s',(sid,))
        await asyncio.to_thread(db._exec,'DELETE FROM inbound_seen WHERE session_id=%s',(sid,))
        await asyncio.to_thread(db._exec,'DELETE FROM autoreply_times WHERE session_id=%s',(sid,))
    else:
        # Paused, retained for retry; watcher must not silently reconnect this session.
        await asyncio.to_thread(db._exec,"UPDATE sessions SET status='revoke_failed' WHERE id=%s AND owner_id=%s",(sid,uid))
    return revoked


async def listener_loop(bot):
    global BOT
    BOT=bot
    while True:
        try:
            sessions=await asyncio.to_thread(db.list_listener_sessions)
            wanted=set()
            for session in sessions:
                uid,sid=session['owner_id'],session['id']
                if session.get('status')!='active' or sid in BLOCKED: continue
                if await asyncio.to_thread(db.is_banned,uid): continue
                if uid not in config.ADMIN_IDS and not await asyncio.to_thread(db.is_subscribed,uid): continue
                wanted.add(sid)
                try: await asyncio.wait_for(get_client(session),timeout=30)
                except Exception as exc: log.warning('Listener unavailable %s (%s)',sid,type(exc).__name__)
            import userbot_worker as worker
            busy={sid for j in worker.JOBS.values() if j.status in ('running','paused') for sid in getattr(j,'account_ids',[])}
            for sid in set(CLIENTS)-wanted-busy:
                await close_account(sid)
                BLOCKED.discard(sid)
        except Exception as exc:
            log.warning('Listener maintenance failed (%s)',type(exc).__name__)
        await asyncio.sleep(20)


async def shutdown():
    for sid in list(CLIENTS): await close_account(sid)
