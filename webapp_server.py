# -*- coding: utf-8 -*-
import asyncio
import time
import html
from collections import deque
from zoneinfo import ZoneInfo
import security
import product
import scheduling
import account_runtime as runtime
import json
import os
import re
from aiohttp import web
import config
import database as db
import userbot_worker as worker

def _verify_init_data(init_data, bot_token):
    return security.verify_init_data(init_data, bot_token, config.INITDATA_MAX_AGE)

def _cors(resp):
    return resp

async def _options(request):
    return web.Response(status=204)

async def _auth(request):
    try:
        body = await request.json()
        if not isinstance(body, dict):
            return (None, {})
    except Exception:
        return (None, {})
    header = request.headers.get('Authorization', '')
    user = security.read_token(header[7:], config.BOT_TOKEN) if header.startswith('Bearer ') else None
    if not user:
        user = _verify_init_data(body.get('initData', ''), config.BOT_TOKEN)
    if user:
        _rate('uid:' + str(user['id']), 120, 60)
    return (user, body)
_LIMITS = {}

def _rate(key, limit, seconds):
    now = time.monotonic()
    if len(_LIMITS) > 20000:
        for k in list(_LIMITS):
            if not _LIMITS[k] or _LIMITS[k][-1] < now - 600:
                _LIMITS.pop(k, None)
        if len(_LIMITS) > 20000:
            raise web.HTTPTooManyRequests()
    bucket = _LIMITS.setdefault(key, deque())
    while bucket and bucket[0] < now - seconds:
        bucket.popleft()
    if len(bucket) >= limit:
        raise web.HTTPTooManyRequests()
    bucket.append(now)

@web.middleware
async def safety_middleware(request, handler):
    origin = request.headers.get('Origin')
    allowed = set(config.WEBAPP_ALLOWED_ORIGINS or [])
    own = f'{request.scheme}://{request.host}'
    allowed.update({own, 'https://' + request.host})
    if origin and origin not in allowed:
        return web.json_response({'ok': False, 'message': 'Этот источник не разрешён'}, status=403)
    try:
        if request.path.startswith('/api/'):
            _rate('ip:' + (request.remote or 'unknown'), 600, 60)
        response = await handler(request)
    except web.HTTPTooManyRequests:
        response = web.json_response({'ok': False, 'message': 'Слишком много запросов. Подождите минуту'}, status=429)
    except (ValueError, TypeError, KeyError):
        response = web.json_response({'ok': False, 'message': 'Некорректные данные. Проверьте форму и повторите'}, status=400)
    except Exception:
        response = web.json_response({'ok': False, 'message': 'Не удалось выполнить запрос. Повторите позже'}, status=500)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    if origin:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Vary'] = 'Origin'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        response.headers['Access-Control-Allow-Methods'] = 'POST, GET, OPTIONS'
    return response

def _has_access(uid):
    if db.is_banned(uid):
        return False
    return uid in config.ADMIN_IDS or db.is_subscribed(uid)

def _tpl_brief(t):
    txt = ''
    try:
        snap = json.loads(t.get('payload') or '{}')
        txt = snap.get('raw_text') or (snap.get('payload') or {}).get('text') or ''
    except Exception:
        txt = ''
    return {'id': t['id'], 'name': t['name'], 'text': txt}

def _state_for(uid, user=None):
    exp = db.subscription_expiry(uid)
    jobs = worker.get_user_jobs(uid)
    job_list = []
    for j in jobs[-6:]:
        total = sum((s['total'] for s in j.accounts.values()))
        done = sum((s['sent'] + s['failed'] + s['paid'] for s in j.accounts.values()))
        accs = []
        for phone, s in j.accounts.items():
            accs.append({'phone': phone, 'cur': s.get('cur', '—'), 'next': s.get('next', '—'), 'sent': s.get('sent', 0), 'failed': s.get('failed', 0), 'paid': s.get('paid', 0), 'cycle': s.get('cycle', 0)})
        if j.target_type == 'folders' and getattr(j, 'folder_names', None):
            _target = 'Папки: ' + ', '.join(j.folder_names)
        elif j.target_type == 'folders':
            _target = 'Выбранные папки'
        else:
            _target = 'Все чаты'
        job_list.append({'id': j.id, 'name': j.name, 'status': j.status, 'total': total, 'done': done, 'pct': int(100 * done / total) if total else 0, 'eta': worker.fmt_eta(worker.eta_seconds(j)) if j.status in ('running', 'paused') else '—', 'error': j.error or '', 'cycles': j.cycles, 'target': _target, 'folders': list(getattr(j, 'folder_names', []) or []), 'accounts': accs})
    out = {'ok': True, 'is_admin': uid in config.ADMIN_IDS, 'has_access': _has_access(uid), 'subscribed': db.is_subscribed(uid), 'banned': db.is_banned(uid), 'expires_at': exp.isoformat() if exp else None, 'accounts': [{'id': s['id'], 'phone': s.get('phone') or str(s['id']), 'status': s.get('status', 'active')} for s in db.get_sessions(uid)], 'account_count': len(db.get_sessions(uid)), 'jobs': job_list, 'bot_name': config.BOT_NAME, 'price_stars': config.SUB_PRICE_STARS, 'min_cooldown': config.MIN_COOLDOWN, 'owner_contact': config.OWNER_CONTACT, 'owner_contact_url': config.OWNER_CONTACT_URL, 'free_days': config.FREE_TRIAL_DAYS, 'trial_max': config.PAID_MAX_ACCOUNTS, 'max_accounts': config.PAID_MAX_ACCOUNTS, 'sub_days': config.SUB_DAYS, 'trial_used': db.trial_used(uid), 'ai_available': __import__('ai').is_configured(), 'plan_source': db.subscription_source(uid), 'ref_hours': config.REF_HOURS, 'required_channel_url': config.REQUIRED_CHANNEL_URL, 'is_paid': db.is_paid_plan(uid), 'ref_count': db.count_referrals(uid), 'templates': [_tpl_brief(t) for t in db.get_templates(uid)]}
    if user:
        out['user'] = {'id': uid, 'name': user.get('first_name'), 'username': user.get('username')}
    return out
_BOT_USERNAME = None

async def _ref_link(request, uid):
    global _BOT_USERNAME
    bot = request.app.get('bot')
    if bot and (not _BOT_USERNAME):
        try:
            _BOT_USERNAME = (await bot.me()).username
        except Exception:
            _BOT_USERNAME = None
    if _BOT_USERNAME:
        return 'https://t.me/%s?start=%d' % (_BOT_USERNAME, uid)
    return ''

def _resolve_target(s):
    s = (s or '').strip()
    if not s:
        return None
    if s.startswith('@') or not s.lstrip('-').isdigit():
        row = db.get_user_by_username(s)
        return row['telegram_id'] if row else None
    try:
        return int(s)
    except Exception:
        return None

async def api_verify(request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    user = _verify_init_data(body.get('initData', ''), config.BOT_TOKEN) if isinstance(body, dict) else None
    if not user:
        return _cors(web.json_response({'ok': False, 'error': 'bad_init_data'}, status=403))
    uid = user.get('id')
    await asyncio.to_thread(db.track_user, uid, user.get('username'), user.get('first_name'))
    out = await asyncio.to_thread(_state_for, uid, user)
    out['ref_link'] = await _ref_link(request, uid)
    out['access_token'] = security.issue_token(user, config.BOT_TOKEN)
    return _cors(web.json_response(out))

async def api_state(request):
    user, body = await _auth(request)
    if not user:
        return _cors(web.json_response({'ok': False, 'error': 'bad_init_data'}, status=403))
    uid = user.get('id')
    out = await asyncio.to_thread(_state_for, uid, user)
    out['ref_link'] = await _ref_link(request, uid)
    return _cors(web.json_response(out))

async def api_folders(request):
    """Список папок по всем аккаунтам — для выбора в мини аппе."""
    user, body = await _auth(request)
    if not user:
        return _cors(web.json_response({'ok': False, 'error': 'bad_init_data'}, status=403))
    uid = user.get('id')
    sessions = await asyncio.to_thread(db.get_sessions, uid)
    folders = []
    if sessions:
        try:
            folders = await worker.get_user_folders_any(sessions, use_cache=False)
        except Exception:
            folders = []
    if sessions and (not folders):
        try:
            await asyncio.to_thread(db.add_log, uid, 'ℹ️ Папки Telegram не найдены — доступна рассылка по «Всем чатам».')
        except Exception:
            pass
    return _cors(web.json_response({'ok': True, 'folders': folders, 'has_accounts': bool(sessions)}))

def _md_to_html(s):
    """Мини-апп шлёт обычный текст. Разворачиваем markdown в HTML-теги
    (как у Telegram), чтобы рассылка из мини аппа тоже была с форматированием.
    Премиум-эмодзи в textarea ввести нельзя — для них пишите боту напрямую."""
    if not s:
        return s
    s = s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    s = re.sub('\\*\\*(.+?)\\*\\*', '<b>\\1</b>', s, flags=re.S)
    s = re.sub('__(.+?)__', '<u>\\1</u>', s, flags=re.S)
    s = re.sub('~~(.+?)~~', '<s>\\1</s>', s, flags=re.S)
    s = re.sub('`(.+?)`', '<code>\\1</code>', s, flags=re.S)
    return s
_PREVIEWS = {}

def _launch_input(body):
    text = (body.get('text') or '').strip()
    mode = body.get('mode') or 'text'
    if mode == 'forward':
        link = body.get('source_link') or ''
        m = re.fullmatch('https://t\\.me/c/(\\d+)/(?:\\d+/)?(\\d+)', link.strip())
        if m:
            source, mid = (int('-100' + m.group(1)), int(m.group(2)))
        else:
            m = re.fullmatch('https://t\\.me/([A-Za-z0-9_]+)/(?:\\d+/)?(\\d+)', link.strip())
            source, mid = (m.group(1), int(m.group(2))) if m else (None, None)
        payload = {'type': 'forward', 'from_chat': source, 'msg_id': mid}
    else:
        if not text:
            raise ValueError('Введите текст сообщения')
        payload = {'type': 'text', 'text': _md_to_html(text), 'variants': [_md_to_html(str(v)) for v in body.get('variants') or [] if str(v).strip()]}
    folders = [int(v) for v in body.get('folders') or []]
    return dict(payload=payload, cooldown=int(body.get('delay') or config.MIN_COOLDOWN), cycles=int(body.get('cycles') or 1), target_type='folders' if folders else 'all', folder_ids=folders or None, account_ids=[int(v) for v in body.get('account_ids') or []] or None, dedupe=body.get('dedupe', True) is True, autosub=False)

async def api_action(request):
    user, body = await _auth(request)
    if not user:
        return web.json_response({'ok': False, 'error': 'bad_init_data'}, status=403)
    uid = user['id']
    action = body.get('action')
    try:
        if action in ('pause', 'resume', 'stop'):
            job = worker.get_job(body.get('job_id'), uid)
            if not job:
                return web.json_response({'ok': False, 'message': 'Рассылка не найдена'}, status=404)
            {'pause': worker.pause_job, 'resume': worker.resume_job, 'stop': worker.stop_job}[action](job.id, uid)
        elif action == 'stop_all':
            for job in worker.get_user_jobs(uid, active_only=True):
                worker.stop_job(job.id, uid)
        elif action == 'ai':
            import ai
            await asyncio.to_thread(product.require_access, uid)
            _rate('ai:' + str(uid), 5, 60)
            text = (body.get('text') or '').strip()
            if not text or len(text) > 4096:
                raise ValueError('Введите текст до 4096 символов')
            if not ai.is_configured():
                raise ValueError('AI пока не настроен владельцем сервиса')
            result = await ai.personalize(text, style='sell')
            if not result:
                raise ValueError('AI временно недоступен')
            return web.json_response({'ok': True, 'text': html.unescape(re.sub('<[^>]+>', '', result))})
        elif action == 'preview':
            _rate('preview:' + str(uid), 5, 60)
            import secrets
            launch = _launch_input(body)
            sessions = await asyncio.to_thread(product.validate_launch, uid, launch['payload'], launch['cycles'], launch['cooldown'], launch.get('folder_ids'), launch.get('account_ids'))
            launch['account_ids'] = [s['id'] for s in sessions]
            for session in sessions:
                app = await worker._open(session)
                try:
                    await app.start()
                    await worker.materialize_payload(app, launch['payload'])
                finally:
                    await app.stop()
            targets, summary = await worker.prepare_targets(uid, launch['target_type'], launch.get('folder_ids'), account_ids=launch['account_ids'])
            launch['targets'] = targets
            for k, v in list(_PREVIEWS.items()):
                if v['expires'] < time.monotonic() or v['uid'] == uid:
                    _PREVIEWS.pop(k, None)
            if len(_PREVIEWS) > 5000:
                raise ValueError('Сервис занят. Повторите позже')
            ticket = secrets.token_urlsafe(24)
            _PREVIEWS[ticket] = {'uid': uid, 'launch': launch, 'expires': time.monotonic() + 600}
            return web.json_response({'ok': True, 'preview_id': ticket, 'recipients': summary, 'footer': product.FOOTER, 'dedupe': launch['dedupe'], 'cycles': launch['cycles']})
        elif action in ('start', 'schedule'):
            ticket = body.get('preview_id')
            preview = _PREVIEWS.get(ticket)
            if not preview or preview['uid'] != uid or preview['expires'] < time.monotonic():
                raise ValueError('Предпросмотр устарел. Сформируйте его заново')
            launch = preview['launch']
            if action == 'schedule':
                sid = await asyncio.to_thread(scheduling.create, uid, launch, body.get('local_at'), body.get('timezone') or 'Europe/Berlin', body.get('repeat_kind') or 'once', body.get('weekdays') or [])
                _PREVIEWS.pop(ticket, None)
                return web.json_response({'ok': True, 'schedule_id': sid, 'message': 'Расписание сохранено'})
            _PREVIEWS.pop(ticket, None)
            await worker.start_broadcast_task(user_id=uid, **launch)
        elif action == 'test':
            await asyncio.to_thread(product.require_access, uid)
            sessions = await asyncio.to_thread(db.get_sessions, uid)
            sid = int(body.get('account_id') or 0)
            session = next((s for s in sessions if s['id'] == sid), None)
            if not session:
                raise ValueError('Выберите аккаунт для теста')
            _rate('test:' + str(uid), 5, 60)
            launch = _launch_input(body)
            product.validate_payload(launch['payload'])
            app = await worker._open(session)
            try:
                await app.start()
                async with runtime.send_lock(sid):
                    await worker._send_payload(app, 'me', launch['payload'])
            finally:
                await app.stop()
            return web.json_response({'ok': True, 'message': 'Отправлено в Избранное выбранного аккаунта'})
        elif action == 'save_template':
            await asyncio.to_thread(product.require_access, uid)
            if await asyncio.to_thread(db.count_templates, uid) >= 100:
                raise ValueError('Не более 100 шаблонов')
            launch = _launch_input(body)
            product.validate_payload(launch['payload'])
            snap = {'payload': launch['payload'], 'delay': launch['cooldown'], 'cycles': launch['cycles'], 'raw_text': body.get('text') or ''}
            await asyncio.to_thread(db.add_template, uid, (body.get('name') or 'Шаблон')[:60], json.dumps(snap, ensure_ascii=False))
        elif action == 'del_template':
            await asyncio.to_thread(db.delete_template, int(body['id']), uid)
        else:
            raise ValueError('Неизвестное действие')
        out = await asyncio.to_thread(_state_for, uid, user)
        out['ref_link'] = await _ref_link(request, uid)
        return web.json_response(out)
    except ValueError as exc:
        return web.json_response({'ok': False, 'message': str(exc)}, status=400)

async def api_settings(request):
    user, body = await _auth(request)
    if not user:
        return web.json_response({'ok': False}, status=403)
    uid = user['id']
    sid = int(body.get('account_id') or 0)
    sessions = await asyncio.to_thread(db.get_sessions, uid)
    if sid not in {s['id'] for s in sessions}:
        return web.json_response({'ok': False, 'message': 'Аккаунт не найден'}, status=404)
    if body.get('save'):
        await asyncio.to_thread(product.require_access, uid)
        text = str(body.get('reply_text') or '')
        if product.visible_units(product.branded(html.escape(text))) > 4096:
            raise ValueError('Автоответ слишком длинный')
        zone = body.get('timezone') or 'Europe/Berlin'
        ZoneInfo(zone)
        begin, end = (int(body.get('quiet_start', 9)), int(body.get('quiet_end', 18)))
        if begin not in range(24) or end not in range(24) or begin == end:
            raise ValueError('Укажите разные часы рабочего дня от 0 до 23')
        opts = {k: body.get(k, False) is True for k in ('reply_notify', 'dm_notify', 'autorespond', 'outside_hours')}
        if opts['autorespond'] and (not text.strip()):
            raise ValueError('Введите текст автоответа')
        opts.update(reply_text=text, timezone=zone, quiet_start=begin, quiet_end=end)
        await asyncio.to_thread(db.set_account_settings, sid, uid, opts)
    return web.json_response({'ok': True, 'settings': await asyncio.to_thread(db.get_account_settings, sid, uid)})

async def api_schedules(request):
    user, body = await _auth(request)
    if not user:
        return web.json_response({'ok': False}, status=403)
    uid = user['id']
    if body.get('action') == 'cancel':
        await asyncio.to_thread(scheduling.cancel, uid, int(body['id']))
    elif body.get('action') == 'reschedule':
        await asyncio.to_thread(product.require_access, uid)
        changed = await asyncio.to_thread(scheduling.reschedule, uid, int(body['id']), body['local_at'], body.get('timezone') or 'Europe/Berlin')
        if not changed:
            raise ValueError('Расписание уже выполняется или отменено')
    return web.json_response({'ok': True, 'schedules': await asyncio.to_thread(scheduling.list_for, uid)})

async def api_trial(request):
    user, body = await _auth(request)
    if not user:
        return web.json_response({'ok': False}, status=403)
    uid = user['id']
    if await asyncio.to_thread(db.is_banned, uid):
        return web.json_response({'ok': False}, status=403)
    if await asyncio.to_thread(db.is_subscribed, uid):
        raise ValueError('PRO уже действует')
    bot = request.app.get('bot')
    if config.REQUIRED_CHANNEL:
        if not bot:
            raise ValueError('Проверка подписки временно недоступна')
        from aiogram.enums import ChatMemberStatus
        try:
            member = await bot.get_chat_member(config.REQUIRED_CHANNEL, uid)
        except Exception:
            raise ValueError('Не удалось проверить канал. Попробуйте позже') from None
        if member.status not in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR):
            raise ValueError('Сначала подпишитесь на канал по ссылке в разделе PRO')
    await asyncio.to_thread(db.track_user, uid, user.get('username'), user.get('first_name'))
    if not await asyncio.to_thread(db.grant_trial, uid, 4):
        raise ValueError('Пробный период уже использован')
    return web.json_response({'ok': True, 'message': 'Полный PRO активирован на 4 дня'})

async def api_logs(request):
    user, body = await _auth(request)
    if not user:
        return _cors(web.json_response({'ok': False, 'error': 'bad_init_data'}, status=403))
    uid = user.get('id')
    logs = await asyncio.to_thread(db.get_logs, uid, 40)
    active = []
    for j in worker.get_user_jobs(uid, active_only=True):
        for phone, s in j.accounts.items():
            active.append({'job': j.name, 'phone': phone, 'cur': s.get('cur', '—'), 'next': s.get('next', '—'), 'sent': s.get('sent', 0), 'failed': s.get('failed', 0), 'cycle': s.get('cycle', 0), 'cycles': j.cycles})
    return _cors(web.json_response({'ok': True, 'logs': logs, 'active': active}))

async def api_admin(request):
    user, body = await _auth(request)
    if not user:
        return _cors(web.json_response({'ok': False, 'error': 'bad_init_data'}, status=403))
    uid = user.get('id')
    if uid not in config.ADMIN_IDS:
        return _cors(web.json_response({'ok': False, 'error': 'not_admin'}, status=403))
    op = body.get('op')
    if op == 'give':
        target = _resolve_target(body.get('target'))
        try:
            days = int(body.get('days') or 0)
        except Exception:
            days = 0
        if target and days > 0:
            await asyncio.to_thread(db.set_subscription, target, days, source='manual')
            await asyncio.to_thread(db.add_log, target, 'Админ выдал подписку на %d дн.' % days)
            await asyncio.to_thread(db.add_admin_action, uid, 'give', target, '%d дн.' % days)
            bot = request.app.get('bot')
            if bot:
                try:
                    await bot.send_message(target, 'Тебе выдали подписку на %d дн.!' % days)
                except Exception:
                    pass
    elif op == 'take':
        target = _resolve_target(body.get('target'))
        if target:
            await asyncio.to_thread(db.remove_subscription, target)
            await asyncio.to_thread(db.add_log, target, 'Админ забрал подписку')
            await asyncio.to_thread(db.add_admin_action, uid, 'take', target, 'снята подписка')
    elif op == 'announce':
        bot = request.app.get('bot')
        atext = (body.get('text') or '').strip()
        if bot and atext:
            for au in await asyncio.to_thread(db.get_all_users):
                try:
                    await bot.send_message(au['telegram_id'], atext)
                except Exception:
                    pass
            await asyncio.to_thread(db.add_admin_action, uid, 'announce', None, '')
    elif op == 'massgive':
        ids = str(body.get('ids') or '').replace(',', ' ').replace(';', ' ').replace('\n', ' ')
        try:
            days = int(body.get('days') or 0)
        except Exception:
            days = 0
        bot = request.app.get('bot')
        if days > 0:
            for tok in ids.split():
                t = _resolve_target(tok)
                if not t:
                    continue
                await asyncio.to_thread(db.set_subscription, t, days, source='manual')
                await asyncio.to_thread(db.add_log, t, 'Массовая выдача подписки на %d дн.' % days)
                await asyncio.to_thread(db.add_admin_action, uid, 'massgive', t, '%d дн.' % days)
                if bot:
                    try:
                        await bot.send_message(t, 'Тебе выдали подписку на %d дн.!' % days)
                    except Exception:
                        pass
    elif op == 'ban':
        target = _resolve_target(body.get('target'))
        if target:
            await asyncio.to_thread(db.ban_user, target)
            await asyncio.to_thread(db.add_log, target, 'Забанен администратором')
            await asyncio.to_thread(db.add_admin_action, uid, 'ban', target, '')
    elif op == 'unban':
        target = _resolve_target(body.get('target'))
        if target:
            await asyncio.to_thread(db.unban_user, target)
            await asyncio.to_thread(db.add_log, target, 'Разбанен администратором')
            await asyncio.to_thread(db.add_admin_action, uid, 'unban', target, '')
    elif op == 'userinfo':
        target = _resolve_target(body.get('target'))
        if not target:
            return _cors(web.json_response({'ok': True, 'info': None}))
        exp = await asyncio.to_thread(db.subscription_expiry, target)
        info = {'id': target, 'subscribed': await asyncio.to_thread(db.is_subscribed, target), 'banned': await asyncio.to_thread(db.is_banned, target), 'expires': exp.isoformat()[:16].replace('T', ' ') if exp else None, 'accounts': len(await asyncio.to_thread(db.get_sessions, target)), 'logs': [(l.get('ts') or '')[:16].replace('T', ' ') + '  ' + (l.get('text') or '') for l in await asyncio.to_thread(db.get_logs, target, 10)]}
        return _cors(web.json_response({'ok': True, 'info': info}))
    users = []
    for u in (await asyncio.to_thread(db.get_all_users))[:60]:
        users.append({'id': u['telegram_id'], 'username': u.get('username'), 'name': u.get('first_name'), 'subscribed': await asyncio.to_thread(db.is_subscribed, u['telegram_id']), 'banned': await asyncio.to_thread(db.is_banned, u['telegram_id'])})
    accounts = [{'id': s['id'], 'phone': s.get('phone') or str(s['id']), 'owner': s['owner_id']} for s in (await asyncio.to_thread(db.get_all_sessions))[:80]]
    active_bc = sum((1 for j in worker.JOBS.values() if getattr(j, 'status', '') in ('running', 'paused')))
    actions = []
    for a in await asyncio.to_thread(db.get_admin_actions, 30):
        actions.append({'ts': (a.get('ts') or '')[:16].replace('T', ' '), 'action': a.get('action'), 'target': a.get('target_id'), 'detail': a.get('detail') or ''})
    return _cors(web.json_response({'ok': True, 'users_count': await asyncio.to_thread(db.count_users), 'active_subs': await asyncio.to_thread(db.count_active_subs), 'paid_subs': await asyncio.to_thread(db.count_paid_subs), 'accounts_count': len(await asyncio.to_thread(db.get_all_sessions)), 'active_broadcasts': active_bc, 'revenue_30d': await asyncio.to_thread(db.revenue_stars, 30), 'revenue_total': await asyncio.to_thread(db.revenue_stars, None), 'users': users, 'accounts': accounts, 'actions': actions}))

def _pay_info(method):
    if method == 'card':
        return {'title': 'Оплата по номеру карты (РФ)', 'card': config.CARD_NUMBER or '', 'holder': config.CARD_HOLDER or '', 'note': 'После оплаты напиши %s — подписку активируют вручную.' % config.OWNER_CONTACT if config.CARD_NUMBER else 'Реквизиты ещё не добавлены. Напиши %s.' % config.OWNER_CONTACT}
    if method == 'ua':
        return {'title': 'Оплата украинской картой', 'card': config.UA_CARD_NUMBER or '', 'holder': config.UA_CARD_HOLDER or '', 'note': 'После оплаты напиши %s.' % config.OWNER_CONTACT if config.UA_CARD_NUMBER else 'Реквизиты ещё не добавлены. Напиши %s.' % config.OWNER_CONTACT}
    if method == 'ton':
        return {'title': 'Оплата TON', 'amount': '%s TON' % config.TON_PRICE, 'card': config.TON_WALLET, 'holder': '', 'note': 'Кошелёк TON. После перевода напиши %s.' % config.OWNER_CONTACT}
    if method == 'crypto':
        return {'title': 'Оплата через CryptoBot', 'card': '', 'holder': '', 'note': 'Шлюз подключён — напиши %s.' % config.OWNER_CONTACT if config.CRYPTOBOT_TOKEN else 'Шлюз будет добавлен. Напиши %s.' % config.OWNER_CONTACT}
    return {'title': 'Оплата', 'card': '', 'holder': '', 'note': ''}

async def api_pay(request):
    user, body = await _auth(request)
    if not user:
        return _cors(web.json_response({'ok': False, 'error': 'bad_init_data'}, status=403))
    method = body.get('method') or 'card'
    return _cors(web.json_response({'ok': True, 'method': method, 'price_stars': config.SUB_PRICE_STARS, 'days': config.SUB_DAYS, 'info': _pay_info(method)}))

async def api_invoice(request):
    """Создаёт invoice-ссылку для оплаты звёздами — открывается прямо в мини аппе."""
    user, body = await _auth(request)
    if not user:
        return _cors(web.json_response({'ok': False, 'error': 'bad_init_data'}, status=403))
    bot = request.app.get('bot')
    if not bot:
        return _cors(web.json_response({'ok': False, 'error': 'no_bot', 'message': 'Оплата звёздами доступна при запуске через бота.'}, status=400))
    try:
        from aiogram.types import LabeledPrice
        link = await bot.create_invoice_link(title='%s — подписка' % config.BOT_NAME, description='Доступ ко всем функциям на %d дней.' % config.SUB_DAYS, payload='subscription_%d' % config.SUB_DAYS, provider_token='', currency='XTR', prices=[LabeledPrice(label='Подписка %d дн.' % config.SUB_DAYS, amount=config.SUB_PRICE_STARS)])
        return _cors(web.json_response({'ok': True, 'link': link}))
    except Exception:
        return _cors(web.json_response({'ok': False, 'error': 'invoice', 'message': 'Не удалось выполнить операцию. Повторите позже'}, status=400))
_WEB_LOGIN = {}

async def _web_login_cleanup(uid):
    rec = _WEB_LOGIN.pop(uid, None)
    if rec and rec.get('app'):
        try:
            await rec['app'].disconnect()
        except Exception:
            pass

async def api_account_delete(request):
    user, body = await _auth(request)
    if not user:
        return web.json_response({'ok': False}, status=403)
    revoked = await runtime.disconnect_account(user['id'], int(body['id']))
    return web.json_response({'ok': revoked, 'message': 'Доступ отозван, аккаунт удалён' if revoked else 'Не удалось подтвердить отзыв. Аккаунт остановлен, данные сохранены для повторной попытки. Завершите сеанс SendFlow в Telegram → Настройки → Устройства, затем повторите удаление.'})

async def _web_finish_login(uid, rec):
    app = rec['app']
    try:
        if len(await asyncio.to_thread(db.get_sessions, uid)) >= config.PAID_MAX_ACCOUNTS:
            raise ValueError('Достигнут лимит аккаунтов')
        ss = await app.export_session_string()
        me = await app.get_me()
        phone = rec.get('phone') or ('+' + me.phone_number if me.phone_number else str(me.id))
        await asyncio.to_thread(db.add_session, uid, phone, rec['api_id'], rec['api_hash'], ss, telegram_user_id=me.id)
        await asyncio.to_thread(db.add_log, uid, 'Добавлен аккаунт %s (из мини аппа)' % phone)
        return _cors(web.json_response({'ok': True, 'done': True, 'phone': phone, 'message': 'Аккаунт %s добавлен!' % phone}))
    except Exception:
        try:
            await app.log_out()
        except Exception:
            pass
        return _cors(web.json_response({'ok': False, 'error': 'save', 'message': 'Не удалось сохранить аккаунт'}, status=400))
    finally:
        await _web_login_cleanup(uid)

async def _api_account_login(request):
    """Пошаговый вход: send_code -> verify_code -> (password)."""
    from pyrogram import Client
    user, body = await _auth(request)
    if not user:
        return _cors(web.json_response({'ok': False, 'error': 'bad_init_data'}, status=403))
    uid = user.get('id')
    step = body.get('step')
    await asyncio.to_thread(product.require_access, uid)
    _rate('login:' + str(uid), 15, 300)
    if not config.DEFAULT_API_ID or not config.DEFAULT_API_HASH:
        raise ValueError('Вход пока не настроен владельцем: нужны DEFAULT_API_ID и DEFAULT_API_HASH')
    if step == 'send_code':
        _rate('codes:' + str(uid), 3, 300)
        if len(await asyncio.to_thread(db.get_sessions, uid)) >= config.PAID_MAX_ACCOUNTS:
            raise ValueError('Достигнут лимит аккаунтов PRO')
    api_id = config.DEFAULT_API_ID
    api_hash = config.DEFAULT_API_HASH
    try:
        if step == 'send_code':
            await _web_login_cleanup(uid)
            phone = (body.get('phone') or '').strip().replace(' ', '')
            if not re.match('^\\+?\\d{10,15}$', phone):
                return _cors(web.json_response({'ok': False, 'error': 'bad_phone', 'message': 'Номер неверный. Пример: +79991234567'}, status=400))
            app = Client('weblogin_%s' % uid, api_id=api_id, api_hash=api_hash, in_memory=True)
            await app.connect()
            sent = await app.send_code(phone)
            _WEB_LOGIN[uid] = {'app': app, 'phone': phone, 'hash': sent.phone_code_hash, 'api_id': api_id, 'api_hash': api_hash, 'expires': time.monotonic() + 300}
            return _cors(web.json_response({'ok': True, 'step': 'code', 'message': 'Код отправлен в Telegram. Введи его.'}))
        rec = _WEB_LOGIN.get(uid)
        if rec and rec.get('expires', 0) < time.monotonic():
            await _web_login_cleanup(uid)
            rec = None
        if not rec:
            return _cors(web.json_response({'ok': False, 'error': 'no_session', 'message': 'Сессия входа потеряна, начни заново.'}, status=400))
        app = rec['app']
        if step == 'verify_code':
            code = re.sub('\\D', '', body.get('code') or '')
            if not code:
                return _cors(web.json_response({'ok': False, 'error': 'no_code', 'message': 'Введи код из Telegram.'}, status=400))
            try:
                await app.sign_in(rec['phone'], rec['hash'], code)
            except Exception as e:
                msg = str(e)
                if 'SESSION_PASSWORD_NEEDED' in msg or 'password' in msg.lower():
                    return _cors(web.json_response({'ok': True, 'step': 'password', 'message': 'Включён пароль 2FA. Введи пароль.'}))
                return _cors(web.json_response({'ok': False, 'error': 'bad_code', 'message': 'Неверный или просроченный код. Попробуйте ещё раз'}, status=400))
            return await _web_finish_login(uid, rec)
        if step == 'password':
            pwd = body.get('password') or ''
            try:
                await app.check_password(pwd)
            except Exception:
                return _cors(web.json_response({'ok': False, 'error': 'bad_password', 'message': 'Неверный пароль. Попробуйте ещё раз'}, status=400))
            return await _web_finish_login(uid, rec)
        return _cors(web.json_response({'ok': False, 'error': 'bad_step'}, status=400))
    except Exception:
        await _web_login_cleanup(uid)
        return _cors(web.json_response({'ok': False, 'error': 'exc', 'message': 'Не удалось выполнить операцию. Повторите позже'}, status=400))
_LOGIN_LOCKS = {}

async def api_account_login(request):
    user, _ = await _auth(request)
    if not user:
        return web.json_response({'ok': False}, status=403)
    uid = user['id']
    lock = _LOGIN_LOCKS.setdefault(uid, asyncio.Lock())
    if lock.locked():
        return web.json_response({'ok': False, 'message': 'Предыдущий запрос ещё выполняется'}, status=409)
    async with lock:
        return await _api_account_login(request)

async def _login_gc(app):

    async def loop():
        while True:
            for uid, rec in list(_WEB_LOGIN.items()):
                if rec.get('expires', 0) < time.monotonic():
                    await _web_login_cleanup(uid)
            await asyncio.sleep(30)
    task = asyncio.create_task(loop())
    yield
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    for uid in list(_WEB_LOGIN):
        await _web_login_cleanup(uid)

async def index(request):
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, 'webapp', 'index.html')
    if os.path.exists(path):
        return web.FileResponse(path)
    return web.Response(text='Mini App', content_type='text/html')

def build_app(bot=None):
    app = web.Application(middlewares=[safety_middleware], client_max_size=128 * 1024)
    app['bot'] = bot
    app.cleanup_ctx.append(_login_gc)
    app.router.add_get('/', index)
    app.router.add_post('/api/auth/verify', api_verify)
    app.router.add_post('/api/state', api_state)
    app.router.add_post('/api/folders', api_folders)
    app.router.add_post('/api/action', api_action)
    app.router.add_post('/api/settings', api_settings)
    app.router.add_post('/api/schedules', api_schedules)
    app.router.add_post('/api/trial', api_trial)
    app.router.add_post('/api/logs', api_logs)
    app.router.add_post('/api/admin', api_admin)
    app.router.add_post('/api/pay', api_pay)
    app.router.add_post('/api/invoice', api_invoice)
    app.router.add_post('/api/account/delete', api_account_delete)
    app.router.add_post('/api/account/login', api_account_login)
    for p in ('/api/auth/verify', '/api/state', '/api/folders', '/api/action', '/api/logs', '/api/admin', '/api/pay', '/api/invoice', '/api/account/delete', '/api/account/login', '/api/settings', '/api/schedules', '/api/trial'):
        app.router.add_route('OPTIONS', p, _options)
    here = os.path.dirname(os.path.abspath(__file__))
    webdir = os.path.join(here, 'webapp')
    if os.path.isdir(webdir):
        app.router.add_static('/static/', webdir)
    return app

def run_standalone():
    web.run_app(build_app(), host=config.WEBAPP_HOST, port=config.WEBAPP_PORT)
if __name__ == '__main__':
    run_standalone()
