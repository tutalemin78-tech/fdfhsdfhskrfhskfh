# -*- coding: utf-8 -*-
import asyncio
import logging
import random
import re
import time
from pyrogram.enums import ChatType, ParseMode, ChatAction
from pyrogram.errors import FloodWait, PeerFlood
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import html
import product
import account_runtime as runtime
import config
import database as db
log = logging.getLogger('sendflow.worker')
SHUTTING_DOWN = False
JOBS = {}
_SEQ = {'n': 0}
_FOLDERS_CACHE = {}
_FOLDERS_TTL = 120
from emoji import em

def _next_id():
    _SEQ['n'] += 1
    return _SEQ['n']

class Job:

    def __init__(self, user_id, cycles, target_type, cooldown, name, payload, chat_list, folder_ids, autosub=True, autofolder=None, invisible_tags=False):
        self.id = _next_id()
        self.invisible_tags = invisible_tags
        self.flood_until = 0
        self.db_id = None
        self.member_cache = {}
        self.user_id = user_id
        self.cycles = max(int(cycles), 1)
        self.target_type = target_type
        self.cooldown = max(int(cooldown), 1)
        self.name = name
        self.payload = payload
        self.chat_list = chat_list
        self.folder_ids = folder_ids
        self.folder_names = []
        self.autosub = autosub
        self.autofolder = autofolder
        self.warmup = True
        self.human_mode = True
        self.status = 'running'
        self.pause_event = asyncio.Event()
        self.pause_event.set()
        self.task = None
        self.accounts = {}
        self.created = time.time()
        self.error = None
        self.titles = {}

def get_job(job_id, user_id=None):
    try:
        j = JOBS.get(int(job_id))
    except (TypeError, ValueError):
        return None
    return j if j and user_id is not None and (j.user_id == user_id) else None

def get_user_jobs(user_id, active_only=False):
    js = [j for j in JOBS.values() if j.user_id == user_id]
    if active_only:
        js = [j for j in js if j.status in ('running', 'paused')]
    return sorted(js, key=lambda j: j.id)

def pause_job(job_id, user_id):
    j = get_job(job_id, user_id)
    if j and j.status == 'running':
        j.status = 'paused'
        j.pause_event.clear()
        asyncio.create_task(asyncio.to_thread(db.update_job_status, j.db_id, 'paused'))
        return True
    return False

def resume_job(job_id, user_id):
    j = get_job(job_id, user_id)
    if j and j.status == 'paused':
        j.status = 'running'
        j.pause_event.set()
        asyncio.create_task(asyncio.to_thread(db.update_job_status, j.db_id, 'running'))
        return True
    return False

def stop_job(job_id, user_id):
    j = get_job(job_id, user_id)
    if j and j.status in ('running', 'paused'):
        j.status = 'stopped'
        j.pause_event.set()
        if j.task and (not j.task.done()):
            j.task.cancel()
        asyncio.create_task(asyncio.to_thread(db.update_job_status, j.db_id, 'stopped'))
        return True
    return False

def cleanup_finished(user_id, keep=4):
    """Убираем старые завершённые рассылки, оставляя последние."""
    done = [j for j in get_user_jobs(user_id) if j.status in ('done', 'stopped')]
    for j in done[:-keep] if len(done) > keep else []:
        JOBS.pop(j.id, None)

def eta_seconds(job):
    worst = 0
    for st in job.accounts.values():
        done = st['sent'] + st['failed'] + st['paid']
        rem = max(st['total'] - done, 0)
        worst = max(worst, rem * job.cooldown)
    return int(worst)

def fmt_eta(sec):
    sec = int(sec)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h:
        return '%d:%02d:%02d' % (h, m, s)
    return '%d:%02d' % (m, s)

def _bar(done, total, width=12):
    if total <= 0:
        total = 1
    done = min(done, total)
    filled = int(width * done / total)
    pct = int(100 * done / total)
    return ('█' * filled + '▁' * (width - filled), pct)
HEAD = {'running': em('play') + ' Идёт', 'paused': em('pause') + ' Пауза', 'stopped': em('stop') + ' Остановлена', 'done': em('ok') + ' Завершена'}

def render_job(job, short=False):
    if not job:
        return 'Рассылка не найдена.'
    head = HEAD.get(job.status, job.status)
    total = sum((st['total'] for st in job.accounts.values()))
    done = sum((st['sent'] + st['failed'] + st['paid'] for st in job.accounts.values()))
    bar, pct = _bar(done, total)
    eta = fmt_eta(eta_seconds(job)) if job.status in ('running', 'paused') else '—'
    title = '<b>%s</b> · %s' % (job.name, head)
    if short:
        return '%s\n<code>%s</code> %d%%  ·  ⏳ %s' % (title, bar, pct, eta)
    lines = [title, '<code>%s</code> %d%%' % (bar, pct), '%s До конца: <b>%s</b> · циклов: %d · задержка: %dс' % (em('wait'), eta, job.cycles, job.cooldown)]
    if job.target_type == 'folders' and getattr(job, 'folder_names', None):
        lines.append('%s Папки (%d): <b>%s</b>' % (em('folder'), len(job.folder_names), html.escape(', '.join(job.folder_names))))
    elif job.target_type == 'folders':
        lines.append('%s По выбранным папкам' % em('folder'))
    else:
        lines.append('%s По всем чатам' % em('rocket'))
    lines.append('')
    if job.error:
        lines.append('%s %s' % (em('warn'), html.escape(job.error)))
    if not job.accounts:
        lines.append('<i>Готовлю аккаунты...</i>')
    for phone, st in job.accounts.items():
        lines.append('<b>%s</b> · цикл %d/%d · %s%d %s%d %s%d (из %d)' % (phone, st['cycle'], job.cycles, em('ok'), st['sent'], em('cross'), st['failed'], em('card'), st['paid'], st['total']))
    return '\n'.join(lines).strip()

def render_all(user_id):
    jobs = get_user_jobs(user_id)
    active = [j for j in jobs if j.status in ('running', 'paused')]
    if not jobs:
        return 'Нет рассылок. Нажми «Рассылка» в меню.'
    parts = []
    if active:
        parts.append('<b>Активные (%d):</b>' % len(active))
    for j in jobs[-6:]:
        parts.append(render_job(j, short=True))
        parts.append('')
    return '\n'.join(parts).strip()

def parse_subscription_targets(text):
    targets = []
    for m in re.finditer('(?:https?://)?t\\.me/\\+([\\w_-]+)', text):
        targets.append('https://t.me/+' + m.group(1))
    for m in re.finditer('(?:https?://)?t\\.me/joinchat/([\\w_-]+)', text):
        targets.append('https://t.me/joinchat/' + m.group(1))
    for m in re.finditer('(?:https?://)?t\\.me/(?!\\+|joinchat/)([A-Za-z][\\w]{3,})', text):
        targets.append(m.group(1))
    for m in re.finditer('@([A-Za-z][\\w]{3,})', text):
        u = m.group(1)
        if u.lower() not in ('chat', 'channel', 'bot', 'joinchat'):
            targets.append(u)
    seen, out = (set(), [])
    for t in targets:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out

async def check_and_handle_op(client, chat_id, user_id, my_id, joined_collector=None, allowed=None):
    if not allowed:
        return False
    allowed = {str(t).lower().rstrip('/').replace('https://t.me/', '').lstrip('@') for t in allowed}
    'Обязательная подписка (ОП): бот-страж пишет «подпишись на каналы»\n    и даёт ссылки в тексте ИЛИ в кнопках под сообщением. Вступаем во все,\n    архивируем. Возвращаем True, если реально куда-то вступили.'
    try:
        async for message in client.get_chat_history(chat_id, limit=8):
            from_bot = message.from_user and message.from_user.is_bot or bool(message.sender_chat)
            text = message.text or message.caption or ''
            low = text.lower()
            btn_urls = []
            rm = getattr(message, 'reply_markup', None)
            if rm and getattr(rm, 'inline_keyboard', None):
                for row in rm.inline_keyboard:
                    for b in row:
                        if getattr(b, 'url', None):
                            btn_urls.append(b.url)
            reply_to_me = bool(message.reply_to_message and message.reply_to_message.from_user and (message.reply_to_message.from_user.id == my_id))
            looks_op = 'подпис' in low or 'subscrib' in low or 'join' in low or ('вступ' in low) or ('канал' in low) or ('доступ' in low) or bool(btn_urls)
            has_targets = bool(btn_urls) or 't.me/' in low or '@' in text
            if not (btn_urls or (from_bot and looks_op and has_targets) or (looks_op and reply_to_me and has_targets)):
                continue
            targets = parse_subscription_targets(text)
            for u in btn_urls:
                targets.extend(parse_subscription_targets(u))
            seen, uniq = (set(), [])
            for t in targets:
                if t and t not in seen:
                    seen.add(t)
                    uniq.append(t)
            joined_any = False
            for tgt in uniq:
                if str(tgt).lower().rstrip('/').replace('https://t.me/', '').lstrip('@') not in allowed:
                    continue
                try:
                    joined = await client.join_chat(tgt)
                    joined_any = True
                    if joined_collector is not None:
                        try:
                            joined_collector.append(joined.id)
                        except Exception:
                            pass
                    await asyncio.to_thread(db.add_log, user_id, '✅ Автоподписка: вступил в %s' % tgt)
                except Exception as e:
                    await asyncio.to_thread(db.add_log, user_id, '⚠️ Не смог подписаться на %s: %s' % (tgt, str(e)[:40]))
            if uniq:
                return joined_any
    except Exception as e:
        await asyncio.to_thread(db.add_log, user_id, 'Ошибка проверки ОП: %s' % str(e)[:40])
    return False

async def _fetch_folders(app):
    """Возвращает (folders, diag). Источник истины — raw GetDialogFilters
    (именно его использует сам Telegram-клиент). Высокоуровневый
    get_folders() — только запасной путь. diag — короткая строка для логов."""
    folders = []
    diag = []
    try:
        from pyrogram.raw.functions.messages import GetDialogFilters
        res = await app.invoke(GetDialogFilters())
        diag.append('res=%s' % type(res).__name__)
        raw = getattr(res, 'filters', res) or []
        try:
            raw = list(raw)
        except Exception:
            raw = []
        diag.append('raw=%d:%s' % (len(raw), ','.join((type(x).__name__ for x in raw[:8]))))
        for f in raw:
            tname = type(f).__name__
            if tname == 'DialogFilterDefault':
                continue
            fid = getattr(f, 'id', None)
            if fid in (None, 0):
                diag.append('skip:%s(id=%r)' % (tname, fid))
                continue
            title = getattr(f, 'title', None)
            title = getattr(title, 'text', title)
            folders.append({'id': fid, 'title': str(title or 'Папка %s' % fid)})
    except Exception as e:
        diag.append('raw_err=%s:%s' % (type(e).__name__, str(e)[:80]))
    if not folders:
        try:
            gf = app.get_folders()
            items = []
            if hasattr(gf, '__aiter__'):
                async for f in gf:
                    items.append(f)
            else:
                items = list(await gf) if gf is not None else []
            diag.append('hl=%d' % len(items))
            for f in items:
                fid = getattr(f, 'id', None)
                if fid in (None, 0):
                    continue
                title = getattr(f, 'title', None)
                title = getattr(title, 'text', title)
                folders.append({'id': fid, 'title': str(title or 'Папка %s' % fid)})
        except Exception as e:
            diag.append('hl_err=%s' % str(e)[:60])
    seen, out = (set(), [])
    for f in folders:
        if f['id'] in seen:
            continue
        seen.add(f['id'])
        out.append(f)
    return (out, '; '.join(diag))

def _parse_proxy(s):
    """Строка прокси -> dict для Pyrogram. Поддержка:
    scheme://user:pass@host:port | host:port:user:pass | host:port."""
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None
    try:
        scheme = 'socks5'
        if '://' in s:
            scheme, s = s.split('://', 1)
            scheme = scheme.lower().strip() or 'socks5'
        username = password = None
        if '@' in s:
            creds, s = s.rsplit('@', 1)
            if ':' in creds:
                username, password = creds.split(':', 1)
            else:
                username = creds
        parts = s.split(':')
        host = parts[0]
        port = int(parts[1]) if len(parts) > 1 else 1080
        if username is None and len(parts) >= 4:
            username, password = (parts[2], parts[3])
        if scheme in ('socks', 'socks5h'):
            scheme = 'socks5'
        if scheme == 'socks4a':
            scheme = 'socks4'
        d = {'scheme': scheme, 'hostname': host, 'port': port}
        if username:
            d['username'] = username
        if password:
            d['password'] = password
        return d
    except Exception:
        return None

async def _open(session):
    return runtime.Lease(session)

async def check_spam_status(session):
    """Спрашивает у @SpamBot статус аккаунта. Возвращает текст от��ета."""
    import asyncio as _a
    app = await _open(session)
    try:
        await app.start()
        try:
            await app.send_message('SpamBot', '/start')
        except Exception as e:
            return 'Не удалось написать @SpamBot: %s' % str(e)[:80]
        text = None
        for _ in range(12):
            await _a.sleep(1)
            try:
                async for m in app.get_chat_history('SpamBot', limit=3):
                    if not getattr(m, 'outgoing', False) and (m.text or m.caption):
                        text = m.text or m.caption
                        break
            except Exception:
                pass
            if text:
                break
        return text or 'Ответ от @SpamBot не получен. Попробуй позже.'
    except Exception as e:
        return 'Ошибка проверки: %s' % str(e)[:100]
    finally:
        try:
            await app.stop()
        except Exception:
            pass

async def get_user_folders(session, use_cache=True):
    """Список папок аккаунта. Надёжно: get_folders -> raw GetDialogFilters,
    плюс кэш на _FOLDERS_TTL сек. Если свежий запрос пуст из-за сбоя —
    возвращаем последний удачный список из кэша."""
    sid = session.get('id')
    now = time.time()
    if use_cache and sid is not None and (sid in _FOLDERS_CACHE):
        ts, cached = _FOLDERS_CACHE[sid]
        if now - ts < _FOLDERS_TTL and cached:
            return cached
    app = await _open(session)
    out, diag = ([], '')
    try:
        await app.start()
        out, diag = await _fetch_folders(app)
    except Exception as e:
        out, diag = ([], 'open_err=%s' % str(e)[:80])
    finally:
        try:
            await app.stop()
        except Exception:
            pass
    try:
        oid = session.get('owner_id')
        if oid:
            msg = '🗂 Папки аккаунта %s: найдено %d [%s]' % (session.get('phone') or sid, len(out), diag or '—')
            await asyncio.to_thread(db.add_log, oid, msg)
            log.info(msg)
    except Exception:
        pass
    if out:
        _FOLDERS_CACHE[sid] = (now, out)
    elif sid in _FOLDERS_CACHE:
        return _FOLDERS_CACHE[sid][1]
    return out

async def get_user_folders_any(sessions, use_cache=False):
    out = []
    for session in sessions or []:
        folders = await get_user_folders(session, use_cache=use_cache)
        for f in folders:
            out.append({'id': session['id'] * 10000 + int(f['id']), 'raw_id': int(f['id']), 'account_id': session['id'], 'title': f"{f['title']} · ••••{(session.get('phone') or '')[-4:]}"})
    return out

async def _all_writable_dialogs(app):
    """Все группы/супергруппы, куда можно писать."""
    chats = []
    async for d in app.get_dialogs():
        ct = d.chat.type
        if ct in (ChatType.GROUP, ChatType.SUPERGROUP):
            chats.append(d.chat.id)
    return chats

def _folder_title(f):
    """Строковое название raw DialogFilter (в Layer 158 title — TextWithEntities с .text)."""
    t = getattr(f, 'title', None)
    if t is None:
        return ''
    return getattr(t, 'text', t) or ''

async def _folder_chats(app, folder_ids, folder_names=None):
    """СТРОГО только те чаты, что ЯВНО добавлены в выбранные папки.

    КРИТИЧНО: у каждого аккаунта СВОИ внутренние ID папок. Если матчить по id,
    то на втором аккаунте тот же id = СОВСЕМ ДРУГАЯ папка -> рассылка летит
    в ЛЕВЫЕ чаты. Поэтому матчим СТРОГО ПО ИМЕНИ папки (регистр/пробелы
    игнорируем). id — только fallback, если имена не переданы.

    Берём исключительно include_peers + pinned_peers конкретной папки. Авто-флаги
    Telegram (все контакты / все группы и т.п.) НАМЕРЕННО игнорируем.
    """
    wanted_ids = set((int(x) for x in folder_ids or []))
    wanted_names = set(((n or '').strip().casefold() for n in folder_names or [] if (n or '').strip()))
    chats = set()
    try:
        from pyrogram.raw.functions.messages import GetDialogFilters
        res = await app.invoke(GetDialogFilters())
        raw_filters = getattr(res, 'filters', res) or []
        for f in raw_filters:
            title = _folder_title(f).strip().casefold()
            fid = getattr(f, 'id', None)
            if wanted_names:
                if title not in wanted_names:
                    continue
            elif fid is None or fid not in wanted_ids:
                continue
            for attr in ('pinned_peers', 'include_peers'):
                for peer in getattr(f, attr, None) or []:
                    cid = None
                    if getattr(peer, 'channel_id', None):
                        try:
                            cid = int('-100' + str(peer.channel_id))
                        except Exception:
                            cid = None
                    elif getattr(peer, 'chat_id', None):
                        try:
                            cid = -int(peer.chat_id)
                        except Exception:
                            cid = None
                    if cid is not None:
                        chats.add(cid)
    except Exception as e:
        log.info('🗂 _folder_chats ошибка: %s', e)
    return list(chats)

async def _resolve_targets(app, target_type, chat_list, folder_ids, folder_names=None):
    if target_type == 'all':
        chats = await _all_writable_dialogs(app)
    elif target_type == 'folders' and (folder_ids or folder_names):
        chats = await _folder_chats(app, folder_ids, folder_names)
    elif target_type == 'folders':
        chats = []
    elif chat_list:
        chats = list(chat_list)
    else:
        chats = await _all_writable_dialogs(app)
    seen, out = (set(), [])
    for c in chats:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out

def _to_pyro_html(html):
    """aiogram отдаёт премиум-эмодзи как <tg-emoji emoji-id="ID">…</tg-emoji>,
    а Pyrogram понимает только <emoji id="ID">…</emoji>. Конвертируем, чтобы
    жирный/курсив/ссылки И премиум-эмодзи переносились в рассылку 1-в-1."""
    if not html:
        return html
    html = re.sub('<tg-emoji emoji-id="(\\d+)">', '<emoji id="\\1">', html)
    html = html.replace('</tg-emoji>', '</emoji>')
    return html
_INVIS = '\u2060'

async def _invisible_mentions(app, chat, job, n=10):
    """Платная функция: скрытые упоминания до n случайных участников.
    Возвращает HTML-строку: невидимые символы, вшитые в ссылки-меншены.
    Для читателя это выглядит как обычное сообщение, но люди получают пинг."""
    if chat in job.member_cache:
        return job.member_cache[chat]
    import random
    ids = []
    try:
        async for m in app.get_chat_members(chat, limit=200):
            u = getattr(m, 'user', None)
            if u and (not u.is_bot) and (not getattr(u, 'is_deleted', False)):
                ids.append(u.id)
    except Exception:
        ids = []
    html = ''
    if ids:
        random.shuffle(ids)
        picks = ids[:n]
        parts = ['<a href="tg://user?id=%d">%s</a>' % (uid, _INVIS) for uid in picks]
        html = _INVIS + ''.join(parts)
    job.member_cache[chat] = html
    return html

def _build_markup(buttons):
    """Inline-клавиатура из [{text,url}]. Настоящие кнопки умеют слать только
    бот-аккаунты; с пользовательского аккаунта будет fallback на ссылки."""
    if not buttons:
        return None
    try:
        rows = [[InlineKeyboardButton(b['text'], url=b['url'])] for b in buttons if b.get('url')]
        return InlineKeyboardMarkup(rows) if rows else None
    except Exception:
        return None

async def _send_payload(app, chat, payload, job=None):
    """Every delivery is branded, including copied posts and media captions."""
    payload = await materialize_payload(app, payload)
    variants = payload.get('variants') or []
    selected = random.choice([payload.get('text') or ''] + list(variants)) if variants else payload.get('text') or ''
    text = _to_pyro_html(selected)
    text = product.with_links(text, payload.get('buttons'))
    text = product.branded(text)
    limit = 1024 if payload.get('type') in ('photo', 'video') else 4096
    if product.visible_units(text) > limit:
        raise ValueError('Сообщение с подписью превышает лимит Telegram')
    if payload.get('type') == 'photo':
        result = await app.send_photo(chat, payload.get('file_id') or payload['path'], caption=text, parse_mode=ParseMode.HTML)
    elif payload.get('type') == 'video':
        result = await app.send_video(chat, payload.get('file_id') or payload['path'], caption=text, parse_mode=ParseMode.HTML)
    else:
        result = await app.send_message(chat, text, parse_mode=ParseMode.HTML)
    if job is not None:
        await asyncio.to_thread(db.remember_outbound, app.session['id'], result.chat.id, result.id, job.user_id, re.sub('<[^>]*>', '', html.unescape(text)))
    return result

async def materialize_payload(app, payload):
    if payload.get('type') != 'forward':
        return payload
    message = await app.get_messages(payload['from_chat'], int(payload['msg_id']))
    if not message or message.empty:
        raise ValueError('Исходное сообщение недоступно')
    if message.has_protected_content or getattr(message.chat, 'has_protected_content', False):
        raise ValueError('Источник запрещает копирование')
    if message.media_group_id:
        raise ValueError('Альбомы по ссылке пока не поддерживаются. Отправьте одно фото или видео')
    text = message.text or message.caption
    text = text.html if text else ''
    result = {'type': 'text', 'text': text, 'buttons': []}
    if message.photo:
        result.update(type='photo', file_id=message.photo.file_id)
    elif message.video:
        result.update(type='video', file_id=message.video.file_id)
    elif message.media:
        raise ValueError('По ссылке поддерживаются текст, одиночное фото или видео')
    rm = getattr(message, 'reply_markup', None)
    if rm and getattr(rm, 'inline_keyboard', None):
        result['buttons'] = [{'url': b.url, 'text': b.text} for row in rm.inline_keyboard for b in row if b.url and b.url.startswith(('https://', 'http://', 'tg://'))]
    limit = 1024 if result['type'] != 'text' else 4096
    if product.visible_units(product.branded(product.with_links(result['text'], result['buttons']))) > limit:
        raise ValueError('Пост слишком длинный для копирования с подписью')
    return result

async def _chat_title(app, cid, cache):
    """Название чата по id (с кэшем). Никогда не падает."""
    if cid in cache:
        return cache[cid]
    title = str(cid)
    try:
        co = await app.get_chat(cid)
        title = getattr(co, 'title', None) or ' '.join((x for x in [getattr(co, 'first_name', None), getattr(co, 'last_name', None)] if x)) or getattr(co, 'username', None) or str(cid)
    except Exception:
        pass
    cache[cid] = title
    return title

def _human_err(msg):
    """Превращаем технenv ошибку Telegram в понятную новичку фразу."""
    m = (msg or '').lower()
    if 'peer id invalid' in m:
        return 'чат недоступен (аккаунт его не видит)'
    if 'forbidden' in m or 'banned' in m:
        return 'нет прав писать или аккаунт забанен в чате'
    if 'flood' in m:
        return 'Telegram просит паузу (слишком часто)'
    if 'private' in m or 'invite' in m:
        return 'это приватный чат без доступа'
    if 'not a member' in m or 'not_participant' in m or 'not in the chat' in m:
        return 'аккаунт не состоит в этом чате'
    if 'slowmode' in m or 'slow mode' in m:
        return 'в чате включён медленный режим'
    return (msg or 'ошибка')[:40]

async def _warmup_chat(app, chat):
    """«Живой режим» (Warm-up): читаем последние 5-10 сообщений чата
    и помечаем их прочитанными. Telegram ��идит «прочтение» и считает
    аккаунт живым, а не ботом — шанс бана ниже. Никогда не роняет рассылку."""
    try:
        n = random.randint(5, 10)
        seen = 0
        async for _ in app.get_chat_history(chat, limit=n):
            seen += 1
        try:
            await app.read_chat_history(chat)
        except Exception:
            pass
        await asyncio.sleep(random.uniform(1.0, 2.5))
        return seen
    except Exception:
        return 0

async def _human_typing(app, chat):
    """Human Mode: иногда «печатает» перед отправкой — поведение как у человека."""
    try:
        if random.random() < 0.6:
            await app.send_chat_action(chat, ChatAction.TYPING)
            await asyncio.sleep(random.uniform(1.5, 3.5))
    except Exception:
        pass

async def prepare_targets(uid, target_type='all', folder_ids=None, folder_names=None, account_ids=None, chat_list=None):
    """Freeze explicit recipients per account. Recheck chat types before each send."""
    await asyncio.to_thread(product.require_access, uid)
    sessions = await asyncio.to_thread(db.get_sessions, uid)
    sessions = [x for x in sessions if x.get('status') == 'active']
    if account_ids:
        sessions = [x for x in sessions if x['id'] in account_ids]
    snapshots = {}
    summary = []
    for session in sessions:
        app = await _open(session)
        try:
            await app.start()
            async for _ in app.get_dialogs():
                pass
            if target_type == 'folders':
                raw_ids = [int(fid) % 10000 for fid in folder_ids or [] if int(fid) // 10000 == session['id']]
                chats = await _folder_chats(app, raw_ids, None) if raw_ids else []
            elif chat_list:
                chats = list(chat_list)
            else:
                chats = await _all_writable_dialogs(app)
            approved = []
            for cid in dict.fromkeys(chats):
                chat = await app.get_chat(cid)
                if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
                    continue
                if chat.type == ChatType.CHANNEL:
                    from pyrogram.enums import ChatMemberStatus
                    member = await app.get_chat_member(cid, 'me')
                    if member.status not in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR):
                        continue
                    if member.status == ChatMemberStatus.ADMINISTRATOR and (not getattr(member.privileges, 'can_post_messages', False)):
                        continue
                approved.append(chat.id)
                summary.append({'account_id': session['id'], 'chat_id': chat.id, 'title': chat.title or str(chat.id)})
            snapshots[str(session['id'])] = approved
        finally:
            await app.stop()
    if not summary:
        raise ValueError('В выбранных аккаунтах и папках нет доступных групп или каналов')
    return (snapshots, summary)

async def run_single_account_broadcast(job, session):
    sid = session['id']
    phone = session.get('phone') or str(sid)
    chats = job.targets.get(str(sid), [])
    previous = [r for r in job.previous if r['session_id'] == sid]
    st = {'cycle': 0, 'sent': sum((r['status'] == 'sent' for r in previous)), 'failed': sum((r['status'] != 'sent' for r in previous)), 'paid': 0, 'total': len(chats) * job.cycles, 'cur': '—', 'next': '—'}
    job.accounts[phone] = st
    async with runtime.job_lock(sid):
        app = await _open(session)
        try:
            await app.start()
            for cycle in range(1, job.cycles + 1):
                st['cycle'] = cycle
                for idx, cid in enumerate(chats):
                    if job.status == 'stopped':
                        return
                    await job.pause_event.wait()
                    if not await asyncio.to_thread(_can_continue, job.user_id):
                        job.error = 'Подписка закончилась или доступ заблокирован'
                        return
                    if not await asyncio.to_thread(db.claim_delivery, job.db_id, sid, cycle, cid):
                        continue
                    key = (cycle, cid)
                    if job.dedupe and key in job.claimed_chats:
                        await asyncio.to_thread(db.finish_delivery, job.db_id, sid, cycle, cid, 'duplicate_skipped')
                        st['paid'] += 1
                        continue
                    job.claimed_chats.add(key)
                    st['cur'] = await _chat_title(app, cid, job.titles)
                    st['next'] = str(chats[idx + 1]) if idx + 1 < len(chats) else '—'
                    try:
                        chat = await app.get_chat(cid)
                        if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP, ChatType.CHANNEL):
                            raise ValueError('Личные сообщения исключены из рассылки')
                        async with runtime.send_lock(sid):
                            await job.pause_event.wait()
                            if job.status == 'stopped' or sid in runtime.BLOCKED:
                                raise asyncio.CancelledError()
                            await _send_payload(app, cid, job.payload, job)
                        st['sent'] += 1
                        await asyncio.to_thread(db.finish_delivery, job.db_id, sid, cycle, cid, 'sent')
                        await asyncio.to_thread(db.bump_sent, 1)
                        await asyncio.to_thread(db.add_log, job.user_id, '✅ Отправлено в «%s» (••••%s)' % (st['cur'], phone[-4:]))
                        if job.autosub and job.autosub_targets:
                            me = await app.get_me()
                            await check_and_handle_op(app, cid, job.user_id, me.id, allowed=job.autosub_targets)
                    except FloodWait as exc:
                        await asyncio.to_thread(db.finish_delivery, job.db_id, sid, cycle, cid, 'flood_skipped')
                        st['failed'] += 1
                        job.flood_until = time.time() + int(exc.value)
                        await asyncio.to_thread(db.add_log, job.user_id, f'⏳ Telegram требует ожидания {exc.value} с. Текущий получатель пропущен без повтора')
                        while time.time() < job.flood_until:
                            if job.status == 'stopped':
                                return
                            await asyncio.sleep(1)
                        job.flood_until = 0
                    except PeerFlood:
                        st['failed'] += 1
                        await asyncio.to_thread(db.finish_delivery, job.db_id, sid, cycle, cid, 'restricted')
                        job.error = 'Telegram ограничил отправку. Аккаунт остановлен; проверьте его в Telegram'
                        return
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        st['failed'] += 1
                        await asyncio.to_thread(db.finish_delivery, job.db_id, sid, cycle, cid, 'uncertain' if isinstance(exc, (TimeoutError, ConnectionError, OSError)) else 'failed')
                        await asyncio.to_thread(db.add_log, job.user_id, '⚠️ Отправка не подтверждена в «%s» (%s); автоматического повтора нет' % (st['cur'], type(exc).__name__))
                    await asyncio.to_thread(db.update_job_status, job.db_id, job.status)
                    if idx + 1 < len(chats) or cycle < job.cycles:
                        for _ in range(job.cooldown):
                            if job.status == 'stopped':
                                return
                            await job.pause_event.wait()
                            await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            job.error = 'Аккаунт недоступен (%s)' % type(exc).__name__
        finally:
            await app.stop()

def _can_continue(uid):
    return not db.is_banned(uid) and (uid in config.ADMIN_IDS or db.is_subscribed(uid))

async def _run_job(job):
    try:
        sessions = await asyncio.to_thread(db.get_sessions, job.user_id)
        sessions = [s for s in sessions if s['id'] in job.account_ids]
        await asyncio.gather(*(run_single_account_broadcast(job, s) for s in sessions))
        if job.status != 'stopped':
            job.status = 'done'
    except asyncio.CancelledError:
        if not SHUTTING_DOWN:
            job.status = 'stopped'
    except Exception as exc:
        job.status = 'done'
        job.error = 'Ошибка задачи (%s)' % type(exc).__name__
    finally:
        await asyncio.to_thread(db.update_job_status, job.db_id, job.status)
        await asyncio.to_thread(db.add_log, job.user_id, '🏁 %s: %s%s' % (job.name, job.status, ' — ' + job.error if job.error else ''))
_START_LOCK = asyncio.Lock()

async def start_broadcast_task(payload, cooldown, target_type, user_id, cycles=1, chat_list=None, folder_ids=None, folder_names=None, autosub=False, autofolder=None, invisible_tags=False, account_ids=None, targets=None, dedupe=True, autosub_targets=None, resume_db_id=None, resume_status=None):
    async with _START_LOCK:
        cleanup_finished(user_id)
        sessions = await asyncio.to_thread(product.validate_launch, user_id, payload, cycles, cooldown, folder_ids, account_ids)
        account_ids = [s['id'] for s in sessions]
        active = get_user_jobs(user_id, active_only=True)
        if len(active) >= config.MAX_CONCURRENT_JOBS:
            raise ValueError('Достигнут лимит параллельных рассылок')
        if any((set(j.account_ids) & set(account_ids) for j in active)):
            raise ValueError('Выбранный аккаунт уже занят рассылкой. Остановите её или выберите другой')
        if targets is None:
            targets, _ = await prepare_targets(user_id, target_type, folder_ids, folder_names, account_ids, chat_list)
        if set(targets) - {str(i) for i in account_ids}:
            raise ValueError('Получатели относятся к другому аккаунту')
        if not any(targets.values()):
            raise ValueError('Нет получателей')
        if sum((len(v) for v in targets.values())) > 10000:
            raise ValueError('Слишком много получателей в одной задаче')
        job = Job(user_id, cycles, target_type, cooldown, 'Рассылка', payload, chat_list, folder_ids, autosub=bool(autosub and autosub_targets), autofolder=None, invisible_tags=False)
        job.folder_names = folder_names or []
        job.account_ids = account_ids
        job.targets = targets
        job.dedupe = bool(dedupe)
        job.autosub_targets = autosub_targets or []
        cfg = {'payload': payload, 'cooldown': int(cooldown), 'target_type': target_type, 'cycles': int(cycles), 'folder_ids': folder_ids, 'folder_names': folder_names, 'autosub': job.autosub, 'autosub_targets': job.autosub_targets, 'account_ids': account_ids, 'targets': targets, 'dedupe': job.dedupe}
        job.db_id = resume_db_id or await asyncio.to_thread(db.save_job, user_id, 'Рассылка', cfg)
        job.id = job.db_id
        job.name = f'Рассылка #{job.id}'
        job.previous = await asyncio.to_thread(db.delivery_rows, job.db_id) if resume_db_id else []
        job.claimed_chats = {(r['cycle'], r['chat_id']) for r in job.previous if r['status'] != 'duplicate_skipped'}
        if resume_db_id:
            if any((r['status'] in ('pending', 'uncertain') for r in job.previous)):
                await asyncio.to_thread(db.add_log, user_id, '⚠️ После сбоя есть неподтверждённые отправки. Они не будут автоматически повторены')
            await asyncio.to_thread(db._exec, "UPDATE broadcast_deliveries SET status='uncertain' WHERE job_id=%s AND status='pending'", (job.db_id,))
        if resume_status == 'paused':
            job.status = 'paused'
            job.pause_event.clear()
        await asyncio.to_thread(db.update_job_status, job.db_id, job.status)
        JOBS[job.id] = job
        job.task = asyncio.create_task(_run_job(job))
        return job

async def _add_to_folder(app, folder_name, chat_ids):
    """Создаёт/дополняет папку folder_name каналами chat_ids (best-effort,
    через сырые вызовы Telegram). Никогда не валит рассылку."""
    if not folder_name or not chat_ids:
        return 0
    from pyrogram.raw import functions, types as raw_types
    try:
        res = await app.invoke(functions.messages.GetDialogFilters())
        filters = getattr(res, 'filters', res) or []
    except Exception:
        filters = []
    target = None
    used_ids = set()
    for f in filters:
        fid = getattr(f, 'id', None)
        if fid is not None:
            used_ids.add(fid)
        title = getattr(f, 'title', None)
        title_txt = getattr(title, 'text', title)
        if title_txt and str(title_txt) == folder_name:
            target = f
    peers = []
    for cid in chat_ids:
        try:
            peers.append(await app.resolve_peer(cid))
        except Exception:
            try:
                await app.get_chat(cid)
                peers.append(await app.resolve_peer(cid))
            except Exception:
                pass
    if not peers:
        return 0
    new_id = 2
    while new_id in used_ids:
        new_id += 1
    fid = getattr(target, 'id', new_id) if target else new_id
    include = list(getattr(target, 'include_peers', []) or []) if target else []
    include.extend(peers)
    try:
        title_obj = raw_types.TextWithEntities(text=folder_name, entities=[])
    except Exception:
        title_obj = folder_name
    try:
        flt = raw_types.DialogFilter(id=fid, title=title_obj, pinned_peers=list(getattr(target, 'pinned_peers', []) or []) if target else [], include_peers=include, exclude_peers=list(getattr(target, 'exclude_peers', []) or []) if target else [])
    except Exception:
        flt = raw_types.DialogFilter(id=fid, title=title_obj, pinned_peers=[], include_peers=peers, exclude_peers=[])
    await app.invoke(functions.messages.UpdateDialogFilter(id=fid, filter=flt))
    return len(peers)
