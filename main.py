# -*- coding: utf-8 -*-
import asyncio
import time
import uuid
import html
import product
import scheduling
import account_runtime as runtime
import json
import logging
import os
import re
from datetime import datetime
from aiogram import Bot, Dispatcher, F, types
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.client.default import DefaultBotProperties
from aiogram.types import LabeledPrice
from aiohttp import web
import config
import database as db
import keyboards as kb
import userbot_worker as worker
import ai
from emoji import em
from webapp_server import build_app
from deployment_startup import open_frontend, wait_for_primary
logging.basicConfig(level=logging.INFO)
log = logging.getLogger('sendflow')
config.require_token()
bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
MEDIA_DIR = os.path.join(config.DATA_DIR, 'media')
os.makedirs(MEDIA_DIR, exist_ok=True)
_LOGIN_CLIENTS = {}
_LOGIN_BUSY = set()
_AUTO_MON = {}
_LOGIN_EXPIRY = {}

async def _cleanup_login(uid):
    client = _LOGIN_CLIENTS.pop(uid, None)
    _LOGIN_EXPIRY.pop(uid, None)
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass

async def _login_cleanup_loop():
    while True:
        for uid, expiry in list(_LOGIN_EXPIRY.items()):
            if expiry < time.monotonic():
                await _cleanup_login(uid)
        await asyncio.sleep(30)

class GuardMiddleware:

    async def __call__(self, handler, event, data):
        user = getattr(event, 'from_user', None)
        message = event if isinstance(event, types.Message) else getattr(event, 'message', None)
        if not user or user.is_bot:
            return
        if message and message.chat.type != 'private':
            if isinstance(event, types.Message):
                await event.answer('Управление SendFlow доступно только в личном чате с ботом')
            return
        if await asyncio.to_thread(db.is_banned, user.id) and (not getattr(event, 'successful_payment', None)):
            if isinstance(event, types.CallbackQuery):
                await event.answer('Доступ заблокирован', show_alert=True)
            return
        state = data.get('state')
        if state:
            current = await state.get_state()
            if isinstance(event, types.Message) and current in ('AddAccount:code', 'AddAccount:password', 'AddAccount:api_hash', 'AddAccount:proxy'):
                try:
                    await event.delete()
                except Exception:
                    pass
            if current and current.startswith('AddAccount:') and (isinstance(event, types.CallbackQuery) and (event.data or '').startswith(('nav:', 'mode:')) or (isinstance(event, types.Message) and (event.text or '').startswith(('/start', '/menu', '/cancel')))):
                await _cleanup_login(user.id)
                await state.clear()
        try:
            return await handler(event, data)
        except ValueError as exc:
            text = html.escape(str(exc))[:700]
            if isinstance(event, types.CallbackQuery):
                await event.answer('Проверьте данные и повторите', show_alert=True)
            elif isinstance(event, types.Message):
                await event.answer(text)
        except Exception as exc:
            log.warning('Handler failed (%s)', type(exc).__name__)
            if isinstance(event, types.CallbackQuery):
                try:
                    await event.answer('Не удалось выполнить действие. Повторите позже', show_alert=True)
                except Exception:
                    pass
            elif isinstance(event, types.Message):
                await event.answer('Не удалось выполнить действие. Повторите позже или обратитесь в поддержку')
dp.message.middleware(GuardMiddleware())
dp.callback_query.middleware(GuardMiddleware())

class AddAccount(StatesGroup):
    api_id = State()
    api_hash = State()
    proxy = State()
    phone = State()
    code = State()
    password = State()

class Broadcast(StatesGroup):
    message = State()
    link = State()
    btn_name = State()
    btn_url = State()
    delay = State()
    cycles = State()
    variant = State()

class Schedule(StatesGroup):
    date = State()
    repeat = State()

class AdminOps(StatesGroup):
    give_target = State()
    give_days = State()
    take_target = State()
    announce = State()
    massgive_ids = State()
    massgive_days = State()
    ban_target = State()
    unban_target = State()
    info_target = State()

class SaveTpl(StatesGroup):
    name = State()

class AutoFolder(StatesGroup):
    name = State()

def is_admin(uid):
    return uid in config.ADMIN_IDS

def has_access(uid):
    return not db.is_banned(uid) and (is_admin(uid) or db.is_subscribed(uid))

def _fmt_left(uid):
    exp = db.subscription_expiry(uid)
    if not exp:
        return 'нет подписки'
    delta = exp - datetime.now()
    if delta.total_seconds() <= 0:
        return 'истекла'
    days = delta.days
    hours = delta.seconds // 3600
    if days > 0:
        return 'ещё %d дн. %d ч.' % (days, hours)
    return 'ещё %d ч.' % hours

def _cancel_auto_mon(chat_id):
    t = _AUTO_MON.pop(chat_id, None)
    if t and (not t.done()):
        t.cancel()

async def show(event, text, markup=None):
    """Редактирует сообщение (из callback) или отправляет новое."""
    if isinstance(event, types.CallbackQuery):
        _cancel_auto_mon(event.message.chat.id)
        try:
            await event.message.edit_text(text, reply_markup=markup)
        except Exception:
            try:
                await event.message.answer(text, reply_markup=markup)
            except Exception:
                pass
        try:
            await event.answer()
        except Exception:
            pass
    else:
        await event.answer(text, reply_markup=markup)
_BANNERS = {'menu': 'assets/banner_menu.png', 'profile': 'assets/banner_profile.png', 'accounts': 'assets/banner_accounts.png', 'broadcast': 'assets/banner_broadcast.png', 'monitor': 'assets/banner_monitor.png', 'sub': 'assets/banner_sub.png'}

async def show_banner(event, key, text, markup=None):
    """Показать раздел с баннером (фото + подпись). Фолбэк — обычный текст."""
    path = _BANNERS.get(key)
    if not (path and os.path.exists(path)):
        await show(event, text, markup)
        return
    cap = text if len(text) <= 1024 else text[:1019] + '…'
    photo = types.FSInputFile(path)
    if isinstance(event, types.CallbackQuery):
        _cancel_auto_mon(event.message.chat.id)
        try:
            await event.answer()
        except Exception:
            pass
        try:
            await event.message.delete()
        except Exception:
            pass
        try:
            await bot.send_photo(event.message.chat.id, photo, caption=cap, reply_markup=markup)
            return
        except Exception:
            pass
        await show(event, text, markup)
    else:
        try:
            await event.answer_photo(photo, caption=cap, reply_markup=markup)
        except Exception:
            await event.answer(text, reply_markup=markup)

async def check_channel_sub(user_id):
    if not config.REQUIRED_CHANNEL:
        return True
    try:
        member = await bot.get_chat_member(config.REQUIRED_CHANNEL, user_id)
        return member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR)
    except Exception as e:
        log.warning('channel check failed: %s', e)
        return False

def menu_text(uid):
    sub = 'админ' if is_admin(uid) else _fmt_left(uid)
    return '%s <b>%s</b>\n\n%s Подписка: <b>%s</b>\n%s Аккаунтов: <b>%d</b>\n\nВыбери раздел ниже — в любом экране есть кнопки «Назад» и «Главное».' % (em('rocket'), config.BOT_NAME, em('star'), sub, em('accounts'), len(db.get_sessions(uid)))

async def open_menu(event, uid):
    await show_banner(event, 'menu', await asyncio.to_thread(menu_text, uid), kb.main_menu_kb(is_admin(uid)))

async def gate_or_menu(event, uid):
    if await asyncio.to_thread(db.is_banned, uid):
        await show(event, '%s Доступ к боту заблокирован администратором.' % em('ban'), None)
        return
    if await asyncio.to_thread(has_access, uid):
        await open_menu(event, uid)
        return
    text = '%s Чтобы начать, подпишись на наш канал.\n\n%s После подписки ты получишь <b>%d дня бесплатной подписки</b>.\nНажми «Я подписался» после подписки.' % (em('channel'), em('gift'), config.FREE_TRIAL_DAYS)
    await show(event, text, kb.channel_gate_kb())

@dp.message(Command('trial'))
async def cmd_trial(message: types.Message):
    uid = message.from_user.id
    await asyncio.to_thread(db.track_user, uid, message.from_user.username, message.from_user.first_name)
    if not await check_channel_sub(uid):
        await message.answer('Подпишитесь на канал и нажмите «Я подписался»', reply_markup=kb.channel_gate_kb())
        return
    if await asyncio.to_thread(db.is_subscribed, uid):
        await message.answer('PRO уже действует')
        return
    exp = await asyncio.to_thread(db.grant_trial, uid, 4)
    await message.answer(f'🎁 Полный PRO активирован до {exp:%d.%m.%Y %H:%M} UTC' if exp else 'Пробный период уже использован', reply_markup=kb.payment_kb())

@dp.message(Command('recipients'))
async def cmd_recipients(message: types.Message, state: FSMContext):
    data = await state.get_data()
    items = data.get('recipient_summary') or []
    if not items:
        await message.answer('Сначала сформируйте предпросмотр рассылки')
        return
    content = '\n'.join((f"Аккаунт #{x['account_id']} · {x['chat_id']} · {x['title']}" for x in items))
    await message.answer_document(types.BufferedInputFile(content.encode(), filename='recipients.txt'), caption='Фактический список получателей предпросмотра')

@dp.message(Command('testsend'))
async def cmd_testsend(message: types.Message, state: FSMContext):
    await asyncio.to_thread(product.require_access, message.from_user.id)
    data = await state.get_data()
    if not data.get('payload'):
        await message.answer('Сначала подготовьте сообщение')
        return
    sessions = await asyncio.to_thread(db.get_sessions, message.from_user.id)
    if not sessions:
        await message.answer('Нет аккаунтов')
        return
    app = await worker._open(sessions[0])
    try:
        await app.start()
        async with runtime.send_lock(sessions[0]['id']):
            await worker._send_payload(app, 'me', data['payload'])
        await message.answer('Тест отправлен в Избранное первого подключённого аккаунта. Выбрать другой можно в Mini App')
    finally:
        await app.stop()

@dp.message(Command('stopall'))
async def cmd_stopall(message: types.Message):
    for job in worker.get_user_jobs(message.from_user.id, active_only=True):
        worker.stop_job(job.id, message.from_user.id)
    await message.answer('Остановка всех рассылок запрошена. Расписания отменяются отдельно в Mini App')

@dp.message(Command('privacy'))
async def cmd_privacy(message: types.Message):
    await message.answer('Сессии, API hash и прокси шифруются отдельным ключом сервера. Сервер использует сессию для отправки и обработки входящих событий. Метаданные рассылок и короткие фрагменты отправленных сообщений сохраняются для уведомлений. Содержимое входящих сообщений не сохраняется в БД, но при включённых уведомлениях попадает в ваш чат с ботом. Пароль 2FA не записывается в БД. Для отзыва доступа используйте удаление аккаунта и раздел «Устройства» Telegram. Остаточный риск взлома сервера сохраняется; рекомендуем рабочий аккаунт. Полная политика и контакт оператора должны быть опубликованы владельцем до публичного запуска.')

@dp.message(Command('start'))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    u = message.from_user
    new_user = await asyncio.to_thread(db.track_user, u.id, u.username, u.first_name)
    parts = (message.text or '').split(maxsplit=1)
    if len(parts) > 1:
        m = re.search('(\\d{4,})', parts[1])
        if m:
            ref_id = int(m.group(1))
            exp = await asyncio.to_thread(db.award_referral, ref_id, u.id, config.REF_HOURS, eligible=new_user)
            if exp:
                try:
                    await bot.send_message(ref_id, f'🎁 Новый пользователь по вашей ссылке! +{config.REF_HOURS} ч PRO. Действует до {exp:%d.%m.%Y %H:%M} (время сервера UTC).')
                except Exception:
                    pass
    try:
        await message.answer('%s Панель навигации активирована.' % em('rocket'), reply_markup=kb.reply_nav_kb())
    except Exception:
        pass
    uname = '@' + u.username if u.username else u.first_name or 'друг'
    uname = uname.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    _hand = '<tg-emoji emoji-id="%s">👋</tg-emoji>' % config.HELLO_EMOJI_ID
    _tail = '\n\n<b>Выбери, как тебе удобнее работать:</b>'
    text_premium = '%s Привет, %s!%s' % (_hand, uname, _tail)
    text_plain = '👋 Привет, %s!%s' % (uname, _tail)
    _banner = config.START_BANNER if getattr(config, 'START_BANNER', '') else 'assets/logo.png'

    async def _send_start(caption):
        if _banner and os.path.exists(_banner):
            await message.answer_photo(types.FSInputFile(_banner), caption=caption, reply_markup=kb.mode_kb())
        else:
            await message.answer(caption, reply_markup=kb.mode_kb())
    try:
        await _send_start(text_premium)
    except Exception:
        try:
            await _send_start(text_plain)
        except Exception:
            await message.answer(text_plain, reply_markup=kb.mode_kb())

@dp.message(Command('menu'))
async def cmd_menu(message: types.Message, state: FSMContext):
    await state.clear()
    await gate_or_menu(message, message.from_user.id)

@dp.callback_query(F.data == 'mode:chat')
async def cb_mode_chat(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await gate_or_menu(c, c.from_user.id)

@dp.callback_query(F.data == 'gate:check')
async def cb_gate_check(c: types.CallbackQuery):
    uid = c.from_user.id
    if await asyncio.to_thread(has_access, uid):
        await open_menu(c, uid)
        return
    subbed = await check_channel_sub(uid)
    if not subbed:
        await c.answer('Ты ещё не подписан на канал — подпишись и повтори.', show_alert=True)
        return
    if not await asyncio.to_thread(db.trial_used, uid):
        exp = await asyncio.to_thread(db.grant_trial, uid, config.FREE_TRIAL_DAYS)
        if exp:
            await c.answer('Зачислено %d дня бесплатно!' % config.FREE_TRIAL_DAYS, show_alert=True)
        await open_menu(c, uid)
    else:
        text = '%s Бесплатный период уже использован.\n\nОформи подписку, чтобы продолжить:' % em('warn')
        await show(c, text, kb.payment_kb())

@dp.callback_query(F.data == 'nav:menu')
async def cb_menu(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await gate_or_menu(c, c.from_user.id)

def accounts_text(uid):
    sessions = db.get_sessions(uid)
    if not sessions:
        body = 'У тебя пока нет аккаунтов. Добавь первый — он нужен для рассылки.'
    else:
        body = '\n'.join(('%s <b>%s</b> · %s' % (em('green'), s.get('phone') or s['id'], s.get('status', 'active')) for s in sessions))
    return '%s <b>Аккаунты</b>\n\n%s' % (em('accounts'), body)

@dp.callback_query(F.data == 'nav:accounts')
async def cb_accounts(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await show_banner(c, 'accounts', await asyncio.to_thread(accounts_text, c.from_user.id), kb.accounts_kb(await asyncio.to_thread(db.get_sessions, c.from_user.id)))

@dp.callback_query(F.data == 'nav:profile')
async def cb_profile(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    uid = c.from_user.id
    sessions = await asyncio.to_thread(db.get_sessions, uid)
    sub_exp = await asyncio.to_thread(db.subscription_expiry, uid)
    if is_admin(uid):
        sub = 'Администратор (безлимит)'
    elif sub_exp and sub_exp > datetime.now():
        sub = 'активна до %s' % sub_exp.strftime('%d.%m.%Y %H:%M')
    else:
        sub = 'не активна'
    plan = 'Premium' if await asyncio.to_thread(_is_paid, uid) else 'Free'
    acc_lines = '\n'.join(('%s %s · %s' % (em('green'), s.get('phone') or s['id'], s.get('status', 'active')) for s in sessions)) or 'нет аккаунтов'
    text = '%s <b>Профиль</b>\n\n%s ID: <code>%d</code>\n%s Тариф: <b>%s</b>\n%s Подписка: <b>%s</b>\n%s Аккаунтов: <b>%d</b>\n%s Рефералов: <b>%d</b>\n%s Шаблонов: <b>%d</b>\n\n<b>Твои аккаунты:</b>\n%s' % (em('accounts'), em('key'), uid, em('star'), plan, em('ok'), sub, em('accounts'), len(sessions), em('ref'), await asyncio.to_thread(db.count_referrals, uid), em('msg'), await asyncio.to_thread(db.count_templates, uid), acc_lines)
    await show_banner(c, 'profile', text, kb.back_only_kb('nav:menu'))

def _is_paid(uid):
    return is_admin(uid) or db.is_paid_plan(uid)

@dp.callback_query(F.data == 'acc:add')
async def cb_acc_add(c: types.CallbackQuery, state: FSMContext):
    uid = c.from_user.id
    await asyncio.to_thread(product.require_access, uid)
    await _cleanup_login(uid)
    max_acc = config.PAID_MAX_ACCOUNTS if await asyncio.to_thread(_is_paid, uid) else config.FREE_MAX_ACCOUNTS
    if len(await asyncio.to_thread(db.get_sessions, uid)) >= max_acc:
        if await asyncio.to_thread(_is_paid, uid):
            await show(c, '%s Достигнут лимит аккаунтов подписки (%d). Удали лишний, чтобы добавить новый.' % (em('warn'), max_acc), kb.back_only_kb('nav:accounts'))
        else:
            await show(c, '%s На бесплатном тарифе можно подключить до <b>%d</b> аккаунтов.\n\n%s На подписке — до <b>%d</b> аккаунтов и возможности PRO.' % (em('warn'), config.FREE_MAX_ACCOUNTS, em('star'), config.PAID_MAX_ACCOUNTS), kb.payment_kb())
        return
    if config.SKIP_API_PROMPT and config.DEFAULT_API_ID and config.DEFAULT_API_HASH:
        await state.update_data(api_id=config.DEFAULT_API_ID, api_hash=config.DEFAULT_API_HASH)
        await state.set_state(AddAccount.proxy)
        await show(c, _PROXY_PROMPT, kb.cancel_kb())
    else:
        await state.set_state(AddAccount.api_id)
        await show(c, '%s Шаг 1/3. Отправь <b>API_ID</b> (цифры, с my.telegram.org).' % em('key'), kb.cancel_kb())

@dp.message(AddAccount.api_id)
async def st_api_id(message: types.Message, state: FSMContext):
    txt = (message.text or '').strip()
    if not txt.isdigit() or len(txt) < 5:
        await message.answer('API_ID — это число (обычно 7–8 цифр). Попробуй ещё раз.', reply_markup=kb.cancel_kb())
        return
    await state.update_data(api_id=int(txt))
    await state.set_state(AddAccount.api_hash)
    await message.answer('%s Шаг 2/3. Теперь отправь <b>API_HASH</b>.' % em('key'), reply_markup=kb.cancel_kb())

@dp.message(AddAccount.api_hash)
async def st_api_hash(message: types.Message, state: FSMContext):
    txt = (message.text or '').strip()
    if len(txt) < 10:
        await message.answer('Похоже, это не API_HASH. Попробуй ещё раз.', reply_markup=kb.cancel_kb())
        return
    await state.update_data(api_hash=txt)
    await state.set_state(AddAccount.proxy)
    await message.answer(_PROXY_PROMPT, reply_markup=kb.cancel_kb())
_PROXY_PROMPT = '🔑 Пришли <b>прокси</b> для этого аккаунта (необязательно; не гарантирует защиту от ограничений).\n\nФормат: <code>socks5://user:pass@host:port</code> или <code>host:port</code>.\nЕсли прокси не нужен — отправь /skip.'

@dp.message(AddAccount.proxy)
async def st_proxy(message: types.Message, state: FSMContext):
    raw = (message.text or '').strip()
    if raw.lower() in ('/skip', 'skip', '-', 'нет'):
        await state.update_data(proxy=None)
    else:
        if worker._parse_proxy(raw) is None:
            await message.answer('Не понял прокси. Формат: socks5://user:pass@host:port или host:port. Или /skip.', reply_markup=kb.cancel_kb())
            return
        await state.update_data(proxy=raw)
    await state.set_state(AddAccount.phone)
    await message.answer('%s Отправь номер телефона в формате +7XXXXXXXXXX.' % em('phone'), reply_markup=kb.cancel_kb())

@dp.message(AddAccount.phone)
async def st_phone(message: types.Message, state: FSMContext):
    phone = (message.text or '').strip().replace(' ', '')
    if not re.match('^\\+?\\d{10,15}$', phone):
        await message.answer('Номер неверный. Пример: +79991234567', reply_markup=kb.cancel_kb())
        return
    data = await state.get_data()
    from pyrogram import Client
    app = Client('login_%s' % message.from_user.id, api_id=data['api_id'], api_hash=data['api_hash'], in_memory=True, proxy=worker._parse_proxy(data.get('proxy')))
    try:
        await app.connect()
        sent = await app.send_code(phone)
        await state.update_data(phone=phone, phone_code_hash=sent.phone_code_hash)
        _LOGIN_CLIENTS[message.from_user.id] = app
        _LOGIN_EXPIRY[message.from_user.id] = time.monotonic() + 300
        await state.set_state(AddAccount.code)
        await message.answer('%s Код отправлен в Telegram. Введи его С ПРОБЕЛАМИ, например: 1 2 3 4 5.' % em('ok'), reply_markup=kb.cancel_kb())
    except Exception:
        try:
            await app.disconnect()
        except Exception:
            pass
        await state.clear()
        await message.answer('%s Ошибка: %s' % (em('cross'), 'операция не выполнена; проверьте данные и попробуйте снова'), reply_markup=kb.back_only_kb('nav:accounts'))

@dp.message(AddAccount.code)
async def st_code(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    code = re.sub('\\D', '', message.text or '')
    data = await state.get_data()
    app = _LOGIN_CLIENTS.get(uid)
    if _LOGIN_EXPIRY.get(uid, 0) < time.monotonic():
        await _cleanup_login(uid)
        app = None
    if not app:
        await state.clear()
        await message.answer('Сессия входа потеряна, начни заново.', reply_markup=kb.back_only_kb('nav:accounts'))
        return
    if uid in _LOGIN_BUSY:
        await message.answer('⏳ Уже проверяю предыдущий код — подожди пару секунд, не вводи повторно.')
        return
    _LOGIN_BUSY.add(uid)
    await message.answer('⏳ Проверяю код, подожди...')
    try:
        await app.sign_in(data['phone'], data['phone_code_hash'], code)
        await _finish_login(message, state, app, data)
    except Exception as e:
        msg = str(e)
        if 'SESSION_PASSWORD_NEEDED' in msg or 'password' in msg.lower():
            await state.set_state(AddAccount.password)
            await message.answer('%s Включён пароль 2FA. Отправь пароль.' % em('key'), reply_markup=kb.cancel_kb())
        else:
            await message.answer('%s Неверный код: %s\nПопробуй ещё раз.' % (em('cross'), 'код неверен или просрочен'), reply_markup=kb.cancel_kb())
    finally:
        _LOGIN_BUSY.discard(uid)

@dp.message(AddAccount.password)
async def st_password(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    data = await state.get_data()
    app = _LOGIN_CLIENTS.get(uid)
    if _LOGIN_EXPIRY.get(uid, 0) < time.monotonic():
        await _cleanup_login(uid)
        app = None
    if not app:
        await state.clear()
        await message.answer('Сессия входа потеряна.', reply_markup=kb.back_only_kb('nav:accounts'))
        return
    if uid in _LOGIN_BUSY:
        await message.answer('⏳ Уже проверяю пароль — подожди, не вводи повторно.')
        return
    _LOGIN_BUSY.add(uid)
    await message.answer('⏳ Проверяю пароль, подожди...')
    try:
        await app.check_password(message.text or '')
        await _finish_login(message, state, app, data)
    except Exception:
        await message.answer('%s Неверный пароль: %s' % (em('cross'), 'операция не выполнена'), reply_markup=kb.cancel_kb())
    finally:
        _LOGIN_BUSY.discard(uid)

async def _finish_login(message, state, app, data):
    try:
        if len(await asyncio.to_thread(db.get_sessions, message.from_user.id)) >= config.PAID_MAX_ACCOUNTS:
            raise ValueError('Достигнут лимит аккаунтов')
        ss = await app.export_session_string()
        me = await app.get_me()
        phone = data.get('phone') or '+' + (me.phone_number or '') or str(me.id)
        await asyncio.to_thread(db.add_session, message.from_user.id, phone, data['api_id'], data['api_hash'], ss, proxy=data.get('proxy'), telegram_user_id=me.id)
        await asyncio.to_thread(db.add_log, message.from_user.id, 'Добавлен аккаунт %s' % phone)
        await message.answer('%s Аккаунт <b>%s</b> успешно добавлен!' % (em('ok'), phone), reply_markup=kb.back_only_kb('nav:accounts'))
    except Exception:
        await message.answer('%s Ошибка сохранения: %s' % (em('cross'), 'операция не выполнена'), reply_markup=kb.back_only_kb('nav:accounts'))
    finally:
        try:
            await app.disconnect()
        except Exception:
            pass
        _LOGIN_CLIENTS.pop(message.from_user.id, None)
        _LOGIN_EXPIRY.pop(message.from_user.id, None)
        await state.clear()

@dp.callback_query(F.data.startswith('acc:del:'))
async def cb_acc_del(c: types.CallbackQuery):
    sid = int(c.data.split(':')[-1])
    await c.answer('Отключаю аккаунт…')
    revoked = await runtime.disconnect_account(c.from_user.id, sid)
    if not revoked:
        await show(c, '⚠️ Отзыв доступа не подтверждён. Задачи остановлены. Завершите сеанс SendFlow: Telegram → Настройки → Устройства. Затем повторите удаление.', kb.back_only_kb('nav:accounts'))
        return
    await show(c, await asyncio.to_thread(accounts_text, c.from_user.id), kb.accounts_kb(await asyncio.to_thread(db.get_sessions, c.from_user.id)))

@dp.callback_query(F.data.startswith('acc:spam:'))
async def cb_acc_spam(c: types.CallbackQuery):
    sid = int(c.data.split(':')[-1])
    sessions = [s for s in await asyncio.to_thread(db.get_sessions, c.from_user.id) if s['id'] == sid]
    if not sessions:
        await c.answer('Аккаунт не найден', show_alert=True)
        return
    await c.answer('Проверяю через @SpamBot, подожди...')
    try:
        status = await worker.check_spam_status(sessions[0])
    except Exception:
        status = 'Ошибка: %s' % 'операция не выполнена'
    phone = sessions[0].get('phone') or sid
    await show(c, '%s <b>@SpamBot — %s</b>\n\n%s' % (em('accounts'), phone, status), kb.back_only_kb('nav:accounts'))

@dp.callback_query(F.data == 'nav:logs')
async def cb_logs(c: types.CallbackQuery):
    logs = await asyncio.to_thread(db.get_logs, c.from_user.id, 25)
    if not logs:
        body = 'Пока пусто.'
    else:
        body = '\n'.join(('<code>%s</code> %s' % (l['ts'], l['text']) for l in logs))
    await show(c, '%s <b>Логи</b>\n\n%s' % (em('logs'), body), kb.back_only_kb('nav:menu'))

@dp.callback_query(F.data == 'nav:monitor')
async def cb_monitor(c: types.CallbackQuery):
    uid = c.from_user.id
    jobs = worker.get_user_jobs(uid)
    text = '%s <b>Мониторинг</b>\n\n%s' % (em('monitor'), worker.render_all(uid))
    await show(c, text, kb.monitor_kb(jobs))

async def _auto_monitor(chat_id, message_id, job_id):
    """Живое ��бновление экрана рассылки каждые 5с (таймер до конца)."""
    try:
        for _ in range(240):
            await asyncio.sleep(5)
            if _AUTO_MON.get(chat_id) is not asyncio.current_task():
                break
            job = worker.get_job(job_id, chat_id)
            if not job:
                break
            text = '%s <b>Мониторинг</b>\n\n%s' % (em('monitor'), worker.render_job(job))
            try:
                await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=kb.monitor_job_kb(job))
            except Exception:
                pass
            if job.status not in ('running', 'paused'):
                break
    except asyncio.CancelledError:
        pass
    finally:
        if _AUTO_MON.get(chat_id) is asyncio.current_task():
            _AUTO_MON.pop(chat_id, None)

@dp.callback_query(F.data.startswith('mon:open:'))
async def cb_mon_open(c: types.CallbackQuery):
    job_id = int(c.data.split(':')[-1])
    job = worker.get_job(job_id, c.from_user.id)
    text = '%s <b>Мониторинг</b>\n\n%s' % (em('monitor'), worker.render_job(job))
    await show(c, text, kb.monitor_job_kb(job))
    chat_id = c.message.chat.id
    if job and job.status in ('running', 'paused'):
        _AUTO_MON[chat_id] = asyncio.create_task(_auto_monitor(chat_id, c.message.message_id, job_id))

async def _mon_action(c, action):
    job_id = int(c.data.split(':')[-1])
    {'pause': worker.pause_job, 'resume': worker.resume_job, 'stop': worker.stop_job}[action](job_id, c.from_user.id)
    await c.answer({'pause': 'Пауза', 'resume': 'Продолжаем', 'stop': 'Остановлено'}[action])
    job = worker.get_job(job_id, c.from_user.id)
    text = '%s <b>Мониторинг</b>\n\n%s' % (em('monitor'), worker.render_job(job))
    await show(c, text, kb.monitor_job_kb(job))
    chat_id = c.message.chat.id
    if job and job.status in ('running', 'paused'):
        _AUTO_MON[chat_id] = asyncio.create_task(_auto_monitor(chat_id, c.message.message_id, job_id))

@dp.callback_query(F.data.startswith('mon:pause:'))
async def cb_mon_pause(c: types.CallbackQuery):
    await _mon_action(c, 'pause')

@dp.callback_query(F.data.startswith('mon:resume:'))
async def cb_mon_resume(c: types.CallbackQuery):
    await _mon_action(c, 'resume')

@dp.callback_query(F.data.startswith('mon:stop:'))
async def cb_mon_stop(c: types.CallbackQuery):
    await _mon_action(c, 'stop')

def sub_text(uid):
    return f"⭐ <b>SendFlow PRO</b>\nМеньше ручной работы — больше контроля над публикациями.\n\nСтатус: <b>{('Администратор' if is_admin(uid) else _fmt_left(uid))}</b>\n<b>{config.SUB_PRICE_STARS} ⭐ за {config.SUB_DAYS} дней</b> — без автоматического продления.\n\n✓ До {config.PAID_MAX_ACCOUNTS} аккаунтов\n✓ Текст, фото, видео и копирование поста по ссылке\n✓ Основной текст + до 3 вариантов\n✓ Публикации по расписанию\n✓ Уведомления об ответах\n✓ Уведомления о личных сообщениях и автоответ\n✓ Шаблоны и мониторинг\n\n🎁 <b>Первые 4 дня — полный PRO бесплатно</b>, один раз на пользователя.\nНажмите /trial для активации. Расписание и автоответ — в Mini App.\n\nК каждому сообщению добавляется подпись @SendFlowTgBot. Ограничения Telegram сохраняются.\nAI доступен, если настроен владельцем. На пользовательских аккаунтах кнопки превращаются в ссылки.\n\nОплата Stars — автоматически; карта/крипта — через {html.escape(config.OWNER_CONTACT)}."

@dp.callback_query(F.data == 'nav:sub')
async def cb_sub(c: types.CallbackQuery):
    await show_banner(c, 'sub', await asyncio.to_thread(sub_text, c.from_user.id), kb.payment_kb())

@dp.callback_query(F.data == 'pay:stars')
async def cb_pay_stars(c: types.CallbackQuery):
    prices = [LabeledPrice(label='Подписка %d дн.' % config.SUB_DAYS, amount=config.SUB_PRICE_STARS)]
    try:
        await bot.send_invoice(chat_id=c.from_user.id, title='%s — подписка' % config.BOT_NAME, description='Доступ ко всем функциям на %d дней.' % config.SUB_DAYS, payload='subscription_%d' % config.SUB_DAYS, provider_token='', currency='XTR', prices=prices)
        await c.answer()
    except Exception:
        await c.answer('Не удалось выставить счёт: %s' % 'операция не выполнена', show_alert=True)

@dp.callback_query(F.data == 'pay:manual')
async def cb_pay_manual(c: types.CallbackQuery):
    text = '%s <b>Оплата картой или криптой — вручную</b>\n\nАвтооплата в боте — только через <b>Telegram Stars</b> ⭐️ (мгновенно).\n\nХочешь оплатить <b>картой</b> (РФ/УКР) или <b>криптой</b> (TON, USDT и др.)?\nНапиши %s — активируем подписку вручную после оплаты.' % (em('card'), config.OWNER_CONTACT)
    await show(c, text, kb.back_only_kb('nav:sub'))

@dp.message(Command('stats'))
async def cmd_stats(message: types.Message):
    try:
        today = config.SOCIAL_PROOF_BASE + await asyncio.to_thread(db.sent_today)
        total = config.SOCIAL_PROOF_TOTAL_BASE + await asyncio.to_thread(db.sent_total)
    except Exception:
        today, total = (config.SOCIAL_PROOF_BASE, config.SOCIAL_PROOF_TOTAL_BASE)
    text = '%s <b>Статистика %s</b>\n\n%s За сегодня отправлено: <b>%s</b> сообщений\n%s Всего за всё время: <b>%s</b> сообщений\n\n<i>Статистика подтверждённых отправок %s.</i>' % (em('chart'), config.BOT_NAME, em('rocket'), '{:,}'.format(today).replace(',', '\u202f'), em('star'), '{:,}'.format(total).replace(',', '\u202f'), config.BOT_NAME)
    await message.answer(text, reply_markup=kb.back_only_kb('nav:menu'))

@dp.pre_checkout_query()
async def pre_checkout(q: types.PreCheckoutQuery):
    valid = q.invoice_payload == f'subscription_{config.SUB_DAYS}' and q.currency == 'XTR' and (q.total_amount == config.SUB_PRICE_STARS) and (not await asyncio.to_thread(db.is_banned, q.from_user.id))
    await q.answer(ok=valid, error_message=None if valid else 'Счёт устарел или недоступен. Создайте новый в разделе PRO')

@dp.message(F.successful_payment)
async def on_paid(message: types.Message):
    payment = message.successful_payment
    if payment.currency != 'XTR' or payment.total_amount != config.SUB_PRICE_STARS or payment.invoice_payload != f'subscription_{config.SUB_DAYS}':
        await message.answer('Платёж требует проверки. Напишите в поддержку и укажите дату оплаты')
        return
    exp = await asyncio.to_thread(db.apply_stars_payment, message.from_user.id, payment.telegram_payment_charge_id, payment.total_amount, payment.currency, config.SUB_DAYS)
    if exp is None:
        await message.answer('Этот платёж уже учтён. Статус доступен в разделе PRO')
        return
    await message.answer(f'⭐ PRO активирован до <b>{exp:%d.%m.%Y %H:%M}</b> (UTC). Спасибо за поддержку SendFlow!', reply_markup=kb.main_menu_kb(is_admin(message.from_user.id)))

@dp.callback_query(F.data == 'nav:admin')
async def cb_admin(c: types.CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer('Только для админа', show_alert=True)
        return
    active_bc = sum((1 for j in worker.JOBS.values() if getattr(j, 'status', '') in ('running', 'paused')))
    text = '%s <b>Админ-панель — дашборд</b>\n\n%s Пользователей всего: <b>%d</b>\n%s Активных подписок: <b>%d</b>\n%s Платных подписок: <b>%d</b>\n%s Аккаунтов подключено: <b>%d</b>\n%s Активных рассылок сейчас: <b>%d</b>\n\n%s <b>Выручка (Stars):</b>\n• За 30 дней: <b>%d ⭐️</b>\n• За всё время: <b>%d ⭐️</b>' % (em('admin'), em('users'), await asyncio.to_thread(db.count_users), em('star'), await asyncio.to_thread(db.count_active_subs), em('star'), await asyncio.to_thread(db.count_paid_subs), em('accounts'), len(await asyncio.to_thread(db.get_all_sessions)), em('rocket'), active_bc, em('star'), await asyncio.to_thread(db.revenue_stars, 30), await asyncio.to_thread(db.revenue_stars, None))
    await show(c, text, kb.admin_kb())

@dp.callback_query(F.data == 'admin:users')
async def cb_admin_users(c: types.CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer('Только для админа', show_alert=True)
        return
    users = (await asyncio.to_thread(db.get_all_users))[:40]
    rows = []
    for u in users:
        uname = '@' + u['username'] if u.get('username') else '—'
        sub = '✅' if await asyncio.to_thread(db.is_subscribed, u['telegram_id']) else '—'
        rows.append('<code>%s</code> %s %s' % (u['telegram_id'], uname, sub))
    body = '\n'.join(rows) or 'Пусто.'
    await show(c, '%s <b>Пользователи</b> (%d)\n\n%s' % (em('users'), await asyncio.to_thread(db.count_users), body), kb.admin_back_kb())

@dp.callback_query(F.data == 'admin:accounts')
async def cb_admin_accounts(c: types.CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer('Только для админа', show_alert=True)
        return
    sessions = (await asyncio.to_thread(db.get_all_sessions))[:50]
    rows = ['%s <b>%s</b> · владелец <code>%s</code>' % (em('green'), s.get('phone') or s['id'], s['owner_id']) for s in sessions]
    body = '\n'.join(rows) or 'Пусто.'
    await show(c, '%s <b>Аккаунты</b> (%d)\n\n%s' % (em('accounts'), len(await asyncio.to_thread(db.get_all_sessions)), body), kb.admin_back_kb())

async def _start_broadcast_flow(event, uid, state):
    if not await asyncio.to_thread(has_access, uid):
        await show(event, '%s Для рассылки нужна активная подписка.' % em('warn'), kb.payment_kb())
        return
    if not await asyncio.to_thread(db.get_sessions, uid):
        await show(event, '%s Сначала добавь хотя бы один аккаунт.' % em('warn'), kb.back_only_kb('nav:accounts'))
        return
    if isinstance(event, types.CallbackQuery):
        await event.answer('Загружаю папки...')
    await state.clear()
    sessions = await asyncio.to_thread(db.get_sessions, uid)
    folders = []
    try:
        folders = await worker.get_user_folders_any(sessions, use_cache=False)
    except Exception as e:
        log.warning('folders error: %s', e)
    await state.update_data(folders=folders, chosen=[])
    if folders:
        await show(event, '%s <b>Выбери папки</b> для рассылки (можно несколько) или по всем чатам:' % em('folder'), kb.folders_kb(folders, []))
    else:
        await show(event, '%s <b>Папки у аккаунта не найдены.</b>\n\nМожешь разослать <b>по всем чатам</b> — это все группы и супергруппы, где состоит аккаунт.\n\n<i>Я проверил все подключённые аккаунты. Если папки есть в Telegram, но тут пусто — открой Логи и пришли строку 🗂 Папки аккаунта.</i>' % em('folder'), kb.folders_kb([], []))

@dp.callback_query(F.data == 'bc:start')
async def cb_bc_start(c: types.CallbackQuery, state: FSMContext):
    await _start_broadcast_flow(c, c.from_user.id, state)

@dp.callback_query(F.data.startswith('bc:fold:'))
async def cb_bc_fold(c: types.CallbackQuery, state: FSMContext):
    fid = int(c.data.split(':')[-1])
    data = await state.get_data()
    chosen = list(data.get('chosen', []))
    folders = data.get('folders', [])
    if fid in chosen:
        chosen.remove(fid)
    else:
        max_f = config.PAID_MAX_FOLDERS if await asyncio.to_thread(_is_paid, c.from_user.id) else config.FREE_MAX_FOLDERS
        if len(chosen) >= max_f:
            await c.answer('Максимум %d папок на вашем тарифе' % max_f, show_alert=True)
            return
        chosen.append(fid)
    await state.update_data(chosen=chosen)
    await show(c, '%s <b>Выбери папки</b> (выбрано: %d):' % (em('folder'), len(chosen)), kb.folders_kb(folders, chosen))

@dp.callback_query(F.data == 'bc:all')
async def cb_bc_all(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(target_type='all', chosen=[])
    await _ask_mode(c, state)

@dp.callback_query(F.data == 'bc:fold_done')
async def cb_bc_fold_done(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get('chosen'):
        await c.answer('Выбери хотя бы одну папку', show_alert=True)
        return
    await state.update_data(target_type='folders')
    await _ask_mode(c, state)

async def _ask_mode(event, state):
    """Показывает выбор режима рассылки после выбора цели (папки/все чаты)."""
    await state.set_state(None)
    await show(event, '%s <b>Выбери режим рассылки</b>\n\n%s <b>Только текст</b> — обычное текстовое сообщение\n%s <b>Медиа + текст</b> — фото или видео с подписью\n%s <b>Пересыл по ссылке</b> — бот перешлёт пост по ссылке (сохранит кнопки оригинала)\n%s <b>Текст + кнопки</b> — сообщение с кнопками-ссылками под ним' % (em('rocket'), em('msg'), em('app'), em('rocket'), em('key')), kb.bc_mode_kb())

@dp.callback_query(F.data == 'bc:mode:text')
async def cb_mode_text(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(bc_mode='text', buttons=[])
    await state.set_state(Broadcast.message)
    await show(c, '%s Отправь <b>текст</b> для рассылки.\n\nФорматирование (жирный, курсив, цитаты, эмодзи) сохранится 1-в-1.' % em('msg'), kb.cancel_kb())

@dp.callback_query(F.data == 'bc:mode:media')
async def cb_mode_media(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(bc_mode='media', buttons=[])
    await state.set_state(Broadcast.message)
    await show(c, '%s Отправь <b>фото или видео</b> (можно с подписью).' % em('app'), kb.cancel_kb())

@dp.callback_query(F.data == 'bc:mode:forward')
async def cb_mode_forward(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(bc_mode='forward', buttons=[])
    await state.set_state(Broadcast.link)
    await show(c, '%s Пришли <b>ссылку на сообщение</b>, которое нужно разослать.\n\nНапример: <code>https://t.me/durov/123</code>\nБот перешлёт его во все выбранные чаты (с медиа и кнопками оригинала).' % em('rocket'), kb.cancel_kb())

@dp.callback_query(F.data == 'bc:mode:buttons')
async def cb_mode_buttons(c: types.CallbackQuery, state: FSMContext):
    if not (await asyncio.to_thread(_is_paid, c.from_user.id) or is_admin(c.from_user.id)):
        await c.answer('🔒 Рассылка с кнопками — только для PREMIUM. Оформи подписку!', show_alert=True)
        return
    await state.update_data(bc_mode='buttons', buttons=[])
    await state.set_state(Broadcast.message)
    await show(c, '%s Сначала отправь <b>текст</b> сообщения (можно с фото/видео).\n\nПотом добавим кнопки-ссылки под ним.' % em('key'), kb.cancel_kb())

def _parse_msg_link(link):
    """Парсит ссылку на сообщение Telegram → (from_chat, msg_id)."""
    link = (link or '').strip()
    m = re.search('t\\.me/c/(\\d+)/(?:\\d+/)?(\\d+)', link)
    if m:
        return (int('-100' + m.group(1)), int(m.group(2)))
    m = re.search('t\\.me/([A-Za-z0-9_]+)/(?:\\d+/)?(\\d+)', link)
    if m:
        return (m.group(1), int(m.group(2)))
    return (None, None)

@dp.message(Broadcast.link)
async def st_bc_link(message: types.Message, state: FSMContext):
    from_chat, msg_id = _parse_msg_link(message.text or '')
    if not from_chat or not msg_id:
        await message.answer('%s Не похоже на ссылку на сообщение. Пришли ссылку вида https://t.me/canal/123' % em('warn'), reply_markup=kb.cancel_kb())
        return
    await state.update_data(payload={'type': 'forward', 'from_chat': from_chat, 'msg_id': msg_id, 'link': (message.text or '').strip(), 'text': ''})
    data = await state.get_data()
    if data.get('editing'):
        await state.update_data(editing=False)
        await show_confirm(message, state)
        return
    await state.set_state(Broadcast.delay)
    await message.answer('%s Задержка между сообщениями в секундах (рекомендую ≈30)?' % em('timer'), reply_markup=kb.cancel_kb())

@dp.callback_query(F.data == 'bc:btn_add')
async def cb_btn_add(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(Broadcast.btn_name)
    await show(c, '%s Введи <b>название кнопки</b> (что будет написано на кнопке):' % em('key'), kb.cancel_kb())

@dp.message(Broadcast.btn_name)
async def st_btn_name(message: types.Message, state: FSMContext):
    name = (message.text or '').strip()
    if not name:
        await message.answer('Название не может быть пустым. Введи текст кнопки:', reply_markup=kb.cancel_kb())
        return
    await state.update_data(_btn_name=name)
    await state.set_state(Broadcast.btn_url)
    await message.answer('%s Теперь пришли <b>ссылку</b>, куда ведёт кнопка (https://…):' % em('rocket'), reply_markup=kb.cancel_kb())

@dp.message(Broadcast.btn_url)
async def st_btn_url(message: types.Message, state: FSMContext):
    url = (message.text or '').strip()
    if not (url.startswith('http://') or url.startswith('https://') or url.startswith('tg://')):
        await message.answer('%s Ссылка должна начинаться с http:// или https://. Попробуй ещё раз:' % em('warn'), reply_markup=kb.cancel_kb())
        return
    data = await state.get_data()
    buttons = list(data.get('buttons', []))
    buttons.append({'text': data.get('_btn_name', 'Открыть'), 'url': url})
    await state.update_data(buttons=buttons)
    await state.set_state(None)
    await message.answer('%s Кнопка добавлена. Можно добавить ещё или нажать «Готово».' % em('ok'), reply_markup=kb.bc_buttons_kb(buttons))

@dp.callback_query(F.data.startswith('bc:btn_del:'))
async def cb_btn_del(c: types.CallbackQuery, state: FSMContext):
    i = int(c.data.split(':')[-1])
    data = await state.get_data()
    buttons = list(data.get('buttons', []))
    if 0 <= i < len(buttons):
        buttons.pop(i)
    await state.update_data(buttons=buttons)
    await show(c, '%s <b>Кнопки сообщения</b>' % em('key'), kb.bc_buttons_kb(buttons))

@dp.callback_query(F.data.in_({'bc:btn_done', 'bc:btn_skip'}))
async def cb_btn_done(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    buttons = data.get('buttons', []) if c.data == 'bc:btn_done' else []
    payload = dict(data.get('payload') or {'type': 'text', 'text': ''})
    payload['buttons'] = buttons
    await state.update_data(payload=payload, buttons=buttons)
    if data.get('editing'):
        await state.update_data(editing=False)
        await show_confirm(c, state)
        return
    await state.set_state(Broadcast.delay)
    await show(c, '%s Задержка между сообщениями в секундах (рекомендую ≈30)?' % em('timer'), kb.cancel_kb())

async def show_confirm(event, state):
    data = await state.get_data()
    uid = event.from_user.id
    try:
        product.validate_payload(data.get('payload') or {})
        sessions = await asyncio.to_thread(db.get_sessions, uid)
        if len(sessions) > config.PAID_MAX_ACCOUNTS:
            raise ValueError('Слишком много аккаунтов. Выберите нужные через Mini App')
        targets, summary = await worker.prepare_targets(uid, data.get('target_type', 'all'), data.get('chosen'), account_ids=[x['id'] for x in sessions])
        for session in sessions:
            app = await worker._open(session)
            try:
                await app.start()
                await worker.materialize_payload(app, data['payload'])
            finally:
                await app.stop()
        await state.update_data(targets=targets, recipient_summary=summary, account_ids=[x['id'] for x in sessions], preview_at=time.monotonic())
        names = '\n'.join(('• ' + html.escape(x['title'][:70]) for x in summary[:12]))
        count = len({x['chat_id'] for x in summary})
        text = f"✅ <b>Проверьте отправку</b>\nАккаунтов: {len(sessions)}\nУникальных чатов: {count}\nЦиклов: {data.get('cycles', 1)}\nЗадержка: {data.get('delay', 30)} с\n\n{names}\n\nПолный список: /recipients\nТест в Избранное: /testsend\nПодпись: Отправлено с помощью @SendFlowTgBot\nОбщие группы: одна отправка на цикл. Личные сообщения исключены.\nПубликацию можно запланировать кнопкой ниже. Выбор аккаунтов — в Mini App."
        await show(event, text, kb.confirm_kb(False, None, False, await asyncio.to_thread(db.is_paid_plan, uid), is_admin(uid), len(data['payload'].get('variants') or [])))
    except ValueError as exc:
        await show(event, html.escape(str(exc)), kb.back_only_kb('nav:menu'))

@dp.callback_query(F.data == 'bc:toggle_invis')
async def cb_toggle_invis(c: types.CallbackQuery, state: FSMContext):
    await c.answer('Скрытые упоминания отключены: отправка не должна незаметно пинговать людей', show_alert=True)

@dp.callback_query(F.data == 'bc:ai')
async def cb_bc_ai(c: types.CallbackQuery, state: FSMContext):
    if not (await asyncio.to_thread(_is_paid, c.from_user.id) or is_admin(c.from_user.id)):
        await c.answer('🔒 AI-персонализация — только для PREMIUM. Оформи подписку!', show_alert=True)
        return
    if not ai.is_configured():
        await c.answer('AI не настроен: добавь AI_API_KEY в .env (бесплатно на console.groq.com).', show_alert=True)
        return
    data = await state.get_data()
    payload = data.get('payload') or {}
    text = (payload.get('text') or '').strip()
    if not text:
        await c.answer('Сначала введи текст сообщения.', show_alert=True)
        return
    await c.answer('Улучшаю текст…')
    new_text = await ai.personalize(text, style='sell')
    if not new_text:
        await c.answer('AI сейчас недоступен, попробуй ещё раз.', show_alert=True)
        return
    await state.update_data(ai_preview=new_text)
    await _show_ai_preview(c, new_text)

async def _show_ai_preview(c, new_text):
    """Показывает новый текст от AI с кнопками Применить/Перегенерировать/Отмена.
    Если в тексте невалидный HTML — показываем экранированный, чтобы не упасть."""
    head = '%s <b>AI улучшил текст.</b> Новый вариант:\n\n' % em('star')
    tail = '\n\n<i>Применить этот вариант или перегенерировать?</i>'
    try:
        await show(c, head + (new_text or '') + tail, kb.ai_preview_kb())
    except Exception:
        import html as _html
        await show(c, head + _html.escape(new_text or '') + tail, kb.ai_preview_kb())

@dp.callback_query(F.data == 'bc:ai_apply')
async def cb_bc_ai_apply(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    new_text = data.get('ai_preview')
    if not new_text:
        await c.answer('Нет готового варианта — нажми AI-персонализацию снова.', show_alert=True)
        return
    payload = data.get('payload') or {}
    payload['text'] = new_text
    await state.update_data(payload=payload, ai_preview=None)
    await c.answer('Применено')
    await show_confirm(c, state)

@dp.callback_query(F.data == 'bc:ai_regen')
async def cb_bc_ai_regen(c: types.CallbackQuery, state: FSMContext):
    if not (await asyncio.to_thread(_is_paid, c.from_user.id) or is_admin(c.from_user.id)):
        await c.answer('🔒 AI-персонализация — только для PREMIUM. Оформи подписку!', show_alert=True)
        return
    if not ai.is_configured():
        await c.answer('AI не настроен: добавь AI_API_KEY в .env.', show_alert=True)
        return
    data = await state.get_data()
    payload = data.get('payload') or {}
    text = (payload.get('text') or '').strip()
    if not text:
        await c.answer('Сначала введи текст сообщения.', show_alert=True)
        return
    await c.answer('Генерирую заново…')
    new_text = await ai.personalize(text, style='sell')
    if not new_text:
        await c.answer('AI сейчас недоступен, попробуй ещё раз.', show_alert=True)
        return
    await state.update_data(ai_preview=new_text)
    await _show_ai_preview(c, new_text)

@dp.callback_query(F.data == 'bc:ai_cancel')
async def cb_bc_ai_cancel(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(ai_preview=None)
    await c.answer('Отменено')
    await show_confirm(c, state)

def _max_variants(uid):
    return 3

@dp.callback_query(F.data == 'bc:addvar')
async def cb_bc_addvar(c: types.CallbackQuery, state: FSMContext):
    uid = c.from_user.id
    cap = _max_variants(uid)
    if cap <= 0:
        await c.answer('Разные тексты — функция подписки.', show_alert=True)
        return
    data = await state.get_data()
    payload = data.get('payload') or {}
    cur = len([v for v in payload.get('variants') or [] if v and str(v).strip()])
    if cur >= cap:
        await c.answer('Достигнут лимит вариантов: %d.' % cap, show_alert=True)
        return
    await state.set_state(Broadcast.variant)
    await show(c, '%s Пришли ещё один <b>вариант текста</b> (добавлено %d из %d).\n\nНа каждую отправку бот будет случайно выбирать один из текстов — это помогает чередовать содержание объявлений.' % (em('msg'), cur, cap), kb.cancel_kb())

@dp.message(Broadcast.variant)
async def st_bc_variant(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    cap = _max_variants(uid)
    new_text = message.html_text or message.text or message.caption or ''
    if not new_text.strip():
        await message.answer('Пустой текст. Пришли вариант текстом.', reply_markup=kb.cancel_kb())
        return
    data = await state.get_data()
    payload = data.get('payload') or {}
    variants = [v for v in payload.get('variants') or [] if v and str(v).strip()]
    if len(variants) >= cap:
        await state.set_state(None)
        await message.answer('%s Достигнут лимит вариантов.' % em('warn'))
        await show_confirm(message, state)
        return
    variants.append(new_text)
    payload['variants'] = variants
    await state.update_data(payload=payload)
    await state.set_state(None)
    await message.answer('%s Вариант добавлен (всего текстов: %d).' % (em('ok'), len(variants) + 1))
    await show_confirm(message, state)

@dp.callback_query(F.data == 'bc:pro')
async def cb_bc_pro(c: types.CallbackQuery):
    await show(c, await asyncio.to_thread(sub_text, c.from_user.id), kb.payment_kb())

@dp.message(Broadcast.message)
async def st_bc_message(message: types.Message, state: FSMContext):
    data0 = await state.get_data()
    buttons = data0.get('buttons', [])
    payload = {'type': 'text', 'text': message.html_text or '', 'buttons': buttons}
    if message.photo:
        path = os.path.join(MEDIA_DIR, 'ph_%s.jpg' % uuid.uuid4().hex)
        await bot.download(message.photo[-1], destination=path)
        payload = {'type': 'photo', 'path': path, 'text': message.html_text or message.caption or '', 'buttons': buttons}
    elif message.video:
        path = os.path.join(MEDIA_DIR, 'vd_%s.mp4' % uuid.uuid4().hex)
        await bot.download(message.video, destination=path)
        payload = {'type': 'video', 'path': path, 'text': message.html_text or message.caption or '', 'buttons': buttons}
    await state.update_data(payload=payload)
    data = await state.get_data()
    if data.get('bc_mode') == 'buttons' and (not data.get('editing')):
        await state.set_state(None)
        await message.answer('%s Текст сохранён. Теперь добавь <b>кнопки</b> со ссылками (или пропусти).' % em('key'), reply_markup=kb.bc_buttons_kb(buttons))
        return
    if data.get('editing'):
        await state.update_data(editing=False)
        await show_confirm(message, state)
        return
    await state.set_state(Broadcast.delay)
    await message.answer('%s Задержка между сообщениями в секундах (рекомендую ≈30)?' % em('timer'), reply_markup=kb.cancel_kb())

@dp.message(Broadcast.delay)
async def st_bc_delay(message: types.Message, state: FSMContext):
    txt = (message.text or '').strip()
    if not txt.isdigit():
        await message.answer('Введи число секунд, например 30.', reply_markup=kb.cancel_kb())
        return
    delay = max(int(txt), config.MIN_COOLDOWN)
    await state.update_data(delay=delay)
    data = await state.get_data()
    if data.get('editing'):
        await state.update_data(editing=False)
        await show_confirm(message, state)
        return
    await state.set_state(Broadcast.cycles)
    await message.answer('%s <b>Сколько раз отправить сообщение?</b>\n\nВведи количество повторов (циклов) — это сколько раз бот пройдёт по всем выбранным чатам и отправит твоё сообщение.\n\nНапример: <b>1</b> — один проход, <b>10</b> — десять проходов. Можно от 1 до 999.' % em('cycle'), reply_markup=kb.cancel_kb())

@dp.message(Broadcast.cycles)
async def st_bc_cycles(message: types.Message, state: FSMContext):
    txt = (message.text or '').strip()
    if not txt.isdigit() or not 1 <= int(txt) <= 999:
        await message.answer('%s Нужно целое число <b>от 1 до 999</b>. Попробуй ещё раз.' % em('warn'), reply_markup=kb.cancel_kb())
        return
    await state.update_data(cycles=int(txt), editing=False)
    await show_confirm(message, state)

@dp.callback_query(F.data == 'bc:confirm')
async def cb_bc_confirm(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await show_confirm(c, state)

@dp.callback_query(F.data == 'bc:edit')
async def cb_bc_edit(c: types.CallbackQuery):
    await show(c, '%s Что редактируем?' % em('gear'), kb.edit_kb())

@dp.callback_query(F.data == 'bc:edit:msg')
async def cb_bc_edit_msg(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(editing=True)
    await state.set_state(Broadcast.message)
    await show(c, '%s Отправь новое сообщение (текст/фото/видео):' % em('msg'), kb.cancel_kb())

@dp.callback_query(F.data == 'bc:edit:delay')
async def cb_bc_edit_delay(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(editing=True)
    await state.set_state(Broadcast.delay)
    await show(c, '%s Введи новую задержку в секундах:' % em('timer'), kb.cancel_kb())

@dp.callback_query(F.data == 'bc:edit:cycles')
async def cb_bc_edit_cycles(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(editing=True)
    await state.set_state(Broadcast.cycles)
    await show(c, '%s Введи новое количество циклов — <b>от 1 до 999</b>:' % em('cycle'), kb.cancel_kb())

@dp.callback_query(F.data == 'bc:launch')
async def cb_bc_launch(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    uid = c.from_user.id
    if not data.get('targets') or time.monotonic() - data.get('preview_at', 0) > 600:
        await c.answer('Обновите предпросмотр', show_alert=True)
        await show_confirm(c, state)
        return
    await state.update_data(targets=None)
    try:
        job = await worker.start_broadcast_task(payload=data['payload'], cooldown=data.get('delay', 30), target_type=data.get('target_type', 'all'), user_id=uid, cycles=data.get('cycles', 1), folder_ids=data.get('chosen') or None, account_ids=data['account_ids'], targets=data['targets'], dedupe=True)
    except ValueError as exc:
        await c.answer(str(exc)[:180], show_alert=True)
        return
    await state.clear()
    await c.answer('Рассылка запущена')
    await show(c, worker.render_job(job), kb.monitor_job_kb(job))

@dp.callback_query(F.data == 'bc:schedule')
async def cb_schedule(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get('targets') or time.monotonic() - data.get('preview_at', 0) > 600:
        await c.answer('Обновите предпросмотр', show_alert=True)
        return
    await state.set_state(Schedule.date)
    await show(c, '🕒 Укажите дату, время и часовой пояс одной строкой.\nНапример: <code>2026-12-20 18:30 Europe/Berlin</code>\n\nСервер должен работать в момент отправки. Пропущенные публикации приостанавливаются.', kb.cancel_kb())

@dp.message(Schedule.date)
async def st_schedule_date(message: types.Message, state: FSMContext):
    parts = (message.text or '').split()
    if len(parts) != 3:
        await message.answer('Формат: ГГГГ-ММ-ДД ЧЧ:ММ Europe/Berlin')
        return
    value = parts[0] + 'T' + parts[1]
    try:
        scheduling.parse_local(value, parts[2])
    except Exception:
        await message.answer('Не удалось распознать дату или часовой пояс. Проверьте формат и переход часов')
        return
    await state.update_data(schedule_local=value, schedule_zone=parts[2])
    await state.set_state(Schedule.repeat)
    await message.answer('Как повторять? Отправьте <b>один раз</b>, <b>ежедневно</b> или дни недели: <code>пн ср пт</code>.', reply_markup=kb.cancel_kb())

@dp.message(Schedule.repeat)
async def st_schedule_repeat(message: types.Message, state: FSMContext):
    text = (message.text or '').strip().lower()
    days = {'пн': 0, 'вт': 1, 'ср': 2, 'чт': 3, 'пт': 4, 'сб': 5, 'вс': 6}
    weekdays = []
    if text in ('один раз', 'один', 'once'):
        kind = 'once'
    elif text in ('ежедневно', 'каждый день', 'daily'):
        kind = 'daily'
    else:
        words = text.replace(',', ' ').split()
        if not words or any((x not in days for x in words)):
            await message.answer('Укажите «один раз», «ежедневно» или дни: пн вт ср чт пт сб вс')
            return
        kind = 'weekly'
        weekdays = sorted({days[x] for x in words})
    data = await state.get_data()
    if not data.get('targets'):
        await state.clear()
        await message.answer('Предпросмотр потерян. Настройте рассылку заново')
        return
    launch = {'payload': data['payload'], 'cooldown': data.get('delay', 30), 'cycles': data.get('cycles', 1), 'target_type': data.get('target_type', 'all'), 'folder_ids': data.get('chosen') or None, 'account_ids': data['account_ids'], 'targets': data['targets'], 'dedupe': True}
    sid = await asyncio.to_thread(scheduling.create, message.from_user.id, launch, data['schedule_local'], data['schedule_zone'], kind, weekdays)
    await state.clear()
    await message.answer(f'✅ Расписание #{sid} сохранено. Управление и перенос — в Mini App. В момент запуска проверяются PRO и доступность аккаунтов.', reply_markup=kb.main_menu_kb(is_admin(message.from_user.id)))

@dp.message(F.web_app_data)
async def on_webapp_data(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    raw = message.web_app_data.data if message.web_app_data else ''
    action = raw
    try:
        parsed = json.loads(raw)
        action = parsed.get('action', raw)
    except Exception:
        pass
    if action in ('broadcast', 'bc:start'):
        await _start_broadcast_flow(message, uid, state)
    elif action in ('monitor', 'nav:monitor'):
        await message.answer('%s <b>Мониторинг</b>\n\n%s' % (em('monitor'), worker.render_all(uid)), reply_markup=kb.monitor_kb(worker.get_user_jobs(uid)))
    elif action in ('accounts', 'acc:add'):
        await message.answer(await asyncio.to_thread(accounts_text, uid), reply_markup=kb.accounts_kb(await asyncio.to_thread(db.get_sessions, uid)))
    elif action in ('sub', 'subscribe', 'nav:sub'):
        await message.answer(await asyncio.to_thread(sub_text, uid), reply_markup=kb.payment_kb())
    elif action in ('templates', 'nav:templates'):
        await message.answer('%s <b>Шаблоны рассылки</b>' % em('msg'), reply_markup=kb.templates_kb(await asyncio.to_thread(db.get_templates, uid)))
    elif action in ('ref', 'nav:ref'):
        try:
            me = await bot.me()
            link = 'https://t.me/%s?start=%d' % (me.username, uid)
        except Exception:
            link = 'ссылка недоступна'
        await message.answer('%s <b>Рефералы</b>\nТвоя ссылка:\n<code>%s</code>\nПриглашено: <b>%d</b>' % ('🤝', link, await asyncio.to_thread(db.count_referrals, uid)), reply_markup=kb.back_only_kb('nav:menu'))
    elif action in ('help', 'nav:help'):
        await message.answer(help_text(), reply_markup=kb.back_only_kb('nav:menu'))
    else:
        await gate_or_menu(message, uid)

def help_text():
    return f'<b>SendFlow — справочник обновления</b>\n\n<b>4 дня полного PRO</b>\n/trial — активация один раз после подписки на канал. Все продуктовые функции PRO доступны в пробном периоде.\n\n<b>Рассылка</b>\nТекст, одиночное фото или видео с подписью, копирование поста по ссылке. Основной текст + до 3 дополнительных вариантов: выбор случайный. Ссылки из кнопок отображаются в тексте. Защищённые посты и альбомы по ссылке не копируются.\nВыберите аккаунты и папки, проверьте получателей и тест в Избранное. Личные сообщения исключены; общие группы по умолчанию получают одно сообщение за цикл. Подпись добавляется всегда через пустую строку: Отправлено с помощью @SendFlowTgBot.\n\n<b>Расписание — в чате и Mini App</b>\nОдин раз, ежедневно или по дням недели. В чате нажмите «Запланировать» после предпросмотра; в Mini App выберите «По расписанию». Укажите часовой пояс. Пропущенные публикации не отправляются пачкой — перенесите их вручную.\n\n<b>Ответы и личные сообщения</b>\nВ разделе «Аккаунты» Mini App настройте уведомления об ответах на ваши рассылки, уведомления о личных сообщениях и автоответ. Уведомление содержит цитату и кнопку. Ссылка на групповой ответ требует доступа к чату; в личных сообщениях кнопка открывает диалог или профиль. Переключитесь в нужный Telegram-аккаунт. Автоответ — не чаще одного раза в 24 часа каждому собеседнику, всегда либо вне рабочих часов. Он тоже содержит подпись SendFlow. Ботам и себе не отвечает.\n\n<b>Работа сервиса</b>\nДля уведомлений сервер должен работать постоянно. PRO должен быть активен. Telegram может ограничивать отправку; гарантии «антибана» нет. После сбоя неподтверждённые отправки не повторяются автоматически.\n\n<b>Безопасность</b>\nСессия даёт доступ к аккаунту. Рекомендуем отдельный рабочий аккаунт. Отключить: Аккаунты → Удалить. Если отзыв не подтверждён — Telegram → Настройки → Устройства. Коды и пароли не отправляйте поддержке. /privacy — хранение данных.\n\n/recipients — список получателей; /testsend — тест; /stopall — остановить всё.\nРефералы: новый пользователь по ссылке даёт +{config.REF_HOURS} ч PRO, один раз. Повторный вход и приглашение себя бонуса не дают.\nПоддержка: {html.escape(config.OWNER_CONTACT)}'

@dp.message(Command('help'))
async def cmd_help(message: types.Message, state: FSMContext):
    await message.answer(help_text(), reply_markup=kb.back_only_kb('nav:menu'))

@dp.callback_query(F.data == 'nav:help')
async def cb_help(c: types.CallbackQuery):
    await show(c, help_text(), kb.back_only_kb('nav:menu'))

def support_text():
    return f'<b>Поддержка SendFlow</b>\n\n• Вход: проверьте номер, свежий код и пароль 2FA.\n• Расписание и уведомления: нужны активный PRO и работающий сервер.\n• Пропущенную публикацию перенесите в разделе расписаний.\n• Telegram может ограничивать отправки; гарантии «антибана» нет.\n• Если отзыв доступа не подтверждён, завершите сеанс в Telegram → Настройки → Устройства.\n\nПоддержка: {html.escape(config.OWNER_CONTACT)}'

@dp.callback_query(F.data == 'nav:support')
async def cb_support(c: types.CallbackQuery):
    await show(c, support_text(), kb.support_kb())

@dp.callback_query(F.data == 'nav:ref')
async def cb_ref(c: types.CallbackQuery):
    uid = c.from_user.id
    try:
        me = await bot.me()
        link = 'https://t.me/%s?start=%d' % (me.username, uid)
    except Exception:
        link = 'ссылка недоступна'
    cnt = await asyncio.to_thread(db.count_referrals, uid)
    lb_lines = []
    medals = {1: '🥇', 2: '🥈', 3: '🥉'}
    for i, r in enumerate(await asyncio.to_thread(db.referral_leaderboard, 5), 1):
        nm = r.get('first_name') or ('@' + r['username'] if r.get('username') else 'ID %s' % r.get('uid'))
        lb_lines.append('%s %s — <b>%d</b>' % (medals.get(i, '%d.' % i), nm, r.get('n', 0)))
    lb = '\n'.join(lb_lines) if lb_lines else 'Пока пусто — стань первым!'
    rank, rn = await asyncio.to_thread(db.referral_rank, uid)
    rank_line = '\n\nТвоё место в рейтинге: <b>#%d</b>' % rank if rank else ''
    text = '%s <b>Реферальная программа</b>\n\nПриглашай друзей по своей ссылке — за каждого получишь <b>+%d часов</b> подписки!\n\nПриглашено: <b>%d</b>%s\n\n%s <b>Топ пригласивших:</b>\n%s\n\nТвоя ссылка:\n<code>%s</code>' % ('🤝', config.REF_HOURS, cnt, rank_line, em('chart'), lb, link)
    await show(c, text, kb.back_only_kb('nav:menu'))

@dp.callback_query(F.data == 'nav:templates')
async def cb_templates(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    tpls = await asyncio.to_thread(db.get_templates, c.from_user.id)
    if tpls:
        body = 'Выбери шаблон, чтобы сразу перейти к запуску, или удали ненужный.'
    else:
        body = 'Пока нет сохранённых шаблонов. Создай рассылку и на экране подтверждения нажми «Сохранить в избранное».'
    await show(c, '%s <b>Шаблоны рассылки</b>\n\n%s' % (em('msg'), body), kb.templates_kb(tpls))

@dp.callback_query(F.data.startswith('tpl:del:'))
async def cb_tpl_del(c: types.CallbackQuery):
    tid = int(c.data.split(':')[-1])
    await asyncio.to_thread(db.delete_template, tid, c.from_user.id)
    await c.answer('Шаблон удалён')
    tpls = await asyncio.to_thread(db.get_templates, c.from_user.id)
    await show(c, '%s <b>Шаблоны рассылки</b>' % em('msg'), kb.templates_kb(tpls))

@dp.callback_query(F.data.startswith('tpl:use:'))
async def cb_tpl_use(c: types.CallbackQuery, state: FSMContext):
    tid = int(c.data.split(':')[-1])
    if not await asyncio.to_thread(has_access, c.from_user.id):
        await show(c, '%s Для рассылки нужна активная подписка.' % em('warn'), kb.payment_kb())
        return
    t = await asyncio.to_thread(db.get_template, tid, c.from_user.id)
    if not t:
        await c.answer('Шаблон не найден', show_alert=True)
        return
    try:
        snap = json.loads(t['payload'])
    except Exception:
        snap = {}
    await state.clear()
    await state.update_data(**snap)
    await c.answer('Шаблон загружен')
    await show_confirm(c, state)

@dp.callback_query(F.data == 'bc:save_tpl')
async def cb_save_tpl(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get('payload'):
        await c.answer('Сначала настрой рассылку', show_alert=True)
        return
    if not await asyncio.to_thread(_is_paid, c.from_user.id) and await asyncio.to_thread(db.count_templates, c.from_user.id) >= config.FREE_MAX_TEMPLATES:
        await show(c, '%s На бесплатном тарифе можно хранить до <b>%d</b> шаблонов.\n\n%s На подписке — до 100 шаблонов.' % (em('warn'), config.FREE_MAX_TEMPLATES, em('star')), kb.payment_kb())
        return
    await state.set_state(SaveTpl.name)
    await show(c, '%s Введи название шаблона (например «Оффер на канал»):' % em('star'), kb.back_only_kb('bc:confirm'))

@dp.message(SaveTpl.name)
async def st_save_tpl_name(message: types.Message, state: FSMContext):
    name = (message.text or '').strip()[:60] or 'Шаблон'
    data = await state.get_data()
    snap = {'payload': data.get('payload'), 'target_type': data.get('target_type', 'all'), 'chosen': data.get('chosen') or [], 'folders': data.get('folders') or [], 'delay': data.get('delay', 30), 'cycles': data.get('cycles', 1), 'autosub': data.get('autosub', False), 'autofolder': data.get('autofolder')}
    await asyncio.to_thread(db.add_template, message.from_user.id, name, json.dumps(snap, ensure_ascii=False))
    await state.set_state(None)
    await message.answer('%s Шаблон «%s» сохранён в избранное!' % (em('ok'), name), reply_markup=kb.back_only_kb('nav:menu'))

@dp.callback_query(F.data == 'bc:toggle_autosub')
async def cb_toggle_autosub(c: types.CallbackQuery, state: FSMContext):
    await c.answer('Автоматические вступления отключены. Подпишитесь на нужные каналы самостоятельно', show_alert=True)

@dp.callback_query(F.data == 'bc:autofolder')
async def cb_autofolder(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get('autofolder'):
        await state.update_data(autofolder=None)
        await c.answer('Авто-папка выключена')
        await show_confirm(c, state)
        return
    await state.set_state(AutoFolder.name)
    await show(c, '%s Введи название папки, куда складывать ВСЕ каналы, на которые подпишемся во время рассылки. Папка создастся автоматически.' % em('folder'), kb.back_only_kb('bc:confirm'))

@dp.message(AutoFolder.name)
async def st_autofolder_name(message: types.Message, state: FSMContext):
    name = (message.text or '').strip()[:40]
    await state.set_state(None)
    if name:
        await state.update_data(autofolder=name)
    await show_confirm(message, state)

def _resolve_admin_target(s):
    s = (s or '').strip()
    if s.startswith('@') or not s.lstrip('-').isdigit():
        row = db.get_user_by_username(s)
        return row['telegram_id'] if row else None
    return int(s)

@dp.callback_query(F.data == 'admin:give')
async def cb_admin_give(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer('Только для админа', show_alert=True)
        return
    await state.set_state(AdminOps.give_target)
    await show(c, '%s Кому выдать подписку? Введи <b>ID</b> или <b>@username</b>:' % em('gift'), kb.cancel_kb())

@dp.message(AdminOps.give_target)
async def st_give_target(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    await state.update_data(give_target=(message.text or '').strip())
    await state.set_state(AdminOps.give_days)
    await message.answer('На сколько дней? Введи число (например 30):', reply_markup=kb.cancel_kb())

@dp.message(AdminOps.give_days)
async def st_give_days(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    txt = (message.text or '').strip()
    if not txt.isdigit():
        await message.answer('Нужно число дней.', reply_markup=kb.cancel_kb())
        return
    days = int(txt)
    data = await state.get_data()
    target = _resolve_admin_target(data.get('give_target'))
    await state.clear()
    if not target:
        await message.answer('%s Пользователь не найден. Он должен был хотя бы раз запустить бота.' % em('warn'), reply_markup=kb.back_only_kb('nav:admin'))
        return
    exp = await asyncio.to_thread(db.set_subscription, target, days, source='manual')
    await asyncio.to_thread(db.add_log, target, 'Админ выдал подписку на %d дн.' % days)
    await asyncio.to_thread(db.add_admin_action, message.from_user.id, 'give', target, '%d дн.' % days)
    try:
        await bot.send_message(target, '%s Тебе выдали подписку на %d дн.! Активна до <b>%s</b>.' % (em('gift'), days, exp.strftime('%d.%m.%Y')))
    except Exception:
        pass
    await message.answer('%s Готово. Подписка для <code>%s</code> до <b>%s</b>.' % (em('ok'), target, exp.strftime('%d.%m.%Y')), reply_markup=kb.back_only_kb('nav:admin'))

@dp.callback_query(F.data == 'admin:take')
async def cb_admin_take(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer('Только для админа', show_alert=True)
        return
    await state.set_state(AdminOps.take_target)
    await show(c, '%s У кого забрать подписку? Введи <b>ID</b> или <b>@username</b>:' % em('ban'), kb.cancel_kb())

@dp.message(AdminOps.take_target)
async def st_take_target(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    target = _resolve_admin_target((message.text or '').strip())
    await state.clear()
    if not target:
        await message.answer('%s Пользователь не найден.' % em('warn'), reply_markup=kb.back_only_kb('nav:admin'))
        return
    await asyncio.to_thread(db.remove_subscription, target)
    await asyncio.to_thread(db.add_log, target, 'Админ забрал подписку')
    await asyncio.to_thread(db.add_admin_action, message.from_user.id, 'take', target, 'снята подписка')
    try:
        await bot.send_message(target, '%s Твоя подписка отменена администратором.' % em('warn'))
    except Exception:
        pass
    await message.answer('%s Подписка у <code>%s</code> снята.' % (em('ok'), target), reply_markup=kb.back_only_kb('nav:admin'))

@dp.callback_query(F.data == 'admin:announce')
async def cb_admin_announce(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer('Только для админа', show_alert=True)
        return
    await state.set_state(AdminOps.announce)
    await show(c, '%s Введи сообщение — отправлю его ВСЕМ, кто запускал бота.\n<i>Поддерживается форматирование Telegram.</i>' % em('msg'), kb.cancel_kb())

@dp.message(AdminOps.announce)
async def st_announce(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    await state.clear()
    text = message.html_text or message.text or ''
    users = await asyncio.to_thread(db.get_all_users)
    sent = 0
    failed = 0
    await message.answer('%s Рассылаю сообщение %d пользователям…' % (em('rocket'), len(users)))
    for u in users:
        try:
            await bot.send_message(u['telegram_id'], text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    await asyncio.to_thread(db.add_admin_action, message.from_user.id, 'announce', None, 'доставлено %d' % sent)
    await message.answer('%s Готово. Доставлено: %d, не дошло: %d.' % (em('ok'), sent, failed), reply_markup=kb.back_only_kb('nav:admin'))

@dp.callback_query(F.data == 'admin:massgive')
async def cb_admin_massgive(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer('Только для админа', show_alert=True)
        return
    await state.set_state(AdminOps.massgive_ids)
    await show(c, '%s <b>Массовая выдача подписки</b>\n\nПришли список ID через пробел, запятую или с новой строки. Можно и @username.' % em('gift'), kb.cancel_kb())

@dp.message(AdminOps.massgive_ids)
async def st_massgive_ids(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    await state.update_data(massgive_ids=message.text or '')
    await state.set_state(AdminOps.massgive_days)
    await message.answer('На сколько дней выдать всем? Введи число (например 30):', reply_markup=kb.cancel_kb())

@dp.message(AdminOps.massgive_days)
async def st_massgive_days(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    txt = (message.text or '').strip()
    if not txt.isdigit():
        await message.answer('Нужно число дней.', reply_markup=kb.cancel_kb())
        return
    days = int(txt)
    data = await state.get_data()
    await state.clear()
    raw = data.get('massgive_ids', '')
    tokens = re.split('[\\s,;]+', raw.strip())
    ok = 0
    fail = 0
    for tok in tokens:
        if not tok:
            continue
        target = _resolve_admin_target(tok)
        if not target:
            fail += 1
            continue
        try:
            exp = await asyncio.to_thread(db.set_subscription, target, days, source='manual')
            await asyncio.to_thread(db.add_log, target, 'Массовая выдача подписки на %d дн.' % days)
            await asyncio.to_thread(db.add_admin_action, message.from_user.id, 'massgive', target, '%d дн.' % days)
            ok += 1
            try:
                await bot.send_message(target, '%s Тебе выдали подписку на %d дн.! Активна до <b>%s</b>.' % (em('gift'), days, exp.strftime('%d.%m.%Y')))
            except Exception:
                pass
        except Exception:
            fail += 1
    await message.answer('%s Готово. Выдано: <b>%d</b>, не удалось: <b>%d</b>.' % (em('ok'), ok, fail), reply_markup=kb.back_only_kb('nav:admin'))

@dp.callback_query(F.data == 'admin:ban')
async def cb_admin_ban(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer('Только для админа', show_alert=True)
        return
    await state.set_state(AdminOps.ban_target)
    await show(c, '%s Кого забан��ть? Введи <b>ID</b> или <b>@username</b>:' % em('ban'), kb.cancel_kb())

@dp.message(AdminOps.ban_target)
async def st_ban_target(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    target = _resolve_admin_target((message.text or '').strip())
    await state.clear()
    if not target:
        await message.answer('%s Пользователь не найден.' % em('warn'), reply_markup=kb.back_only_kb('nav:admin'))
        return
    await asyncio.to_thread(db.ban_user, target)
    await asyncio.to_thread(db.add_log, target, 'Забанен администратором')
    await asyncio.to_thread(db.add_admin_action, message.from_user.id, 'ban', target, '')
    try:
        await bot.send_message(target, '%s Доступ к боту заблокирован администратором.' % em('ban'))
    except Exception:
        pass
    accs = await asyncio.to_thread(db.get_sessions, target)
    logs = await asyncio.to_thread(db.get_logs, target, 8)
    info = '\n'.join(('• %s — %s' % (l['ts'], l['text']) for l in logs)) or '—'
    await message.answer('%s Пользователь <code>%s</code> забанен.\n\n%s Аккаунтов: <b>%d</b>\n%s Последние логи:\n%s' % (em('ok'), target, em('accounts'), len(accs), em('monitor'), info), reply_markup=kb.back_only_kb('nav:admin'))

@dp.callback_query(F.data == 'admin:unban')
async def cb_admin_unban(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer('Только для админа', show_alert=True)
        return
    await state.set_state(AdminOps.unban_target)
    await show(c, '%s Кого разбанить? Введи <b>ID</b> или <b>@username</b>:' % em('ok'), kb.cancel_kb())

@dp.message(AdminOps.unban_target)
async def st_unban_target(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    target = _resolve_admin_target((message.text or '').strip())
    await state.clear()
    if not target:
        await message.answer('%s Пользователь не найден.' % em('warn'), reply_markup=kb.back_only_kb('nav:admin'))
        return
    await asyncio.to_thread(db.unban_user, target)
    await asyncio.to_thread(db.add_log, target, 'Разбанен администратором')
    await asyncio.to_thread(db.add_admin_action, message.from_user.id, 'unban', target, '')
    try:
        await bot.send_message(target, '%s Доступ к боту восстановлен.' % em('ok'))
    except Exception:
        pass
    await message.answer('%s Пользователь <code>%s</code> разбанен.' % (em('ok'), target), reply_markup=kb.back_only_kb('nav:admin'))

@dp.callback_query(F.data == 'admin:userinfo')
async def cb_admin_userinfo(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer('Только для админа', show_alert=True)
        return
    await state.set_state(AdminOps.info_target)
    await show(c, '%s Чей профиль показать? Введи <b>ID</b> или <b>@username</b>:' % em('users'), kb.cancel_kb())

@dp.message(AdminOps.info_target)
async def st_info_target(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    target = _resolve_admin_target((message.text or '').strip())
    await state.clear()
    if not target:
        await message.answer('%s Пользователь не найден.' % em('warn'), reply_markup=kb.back_only_kb('nav:admin'))
        return
    accs = await asyncio.to_thread(db.get_sessions, target)
    logs = await asyncio.to_thread(db.get_logs, target, 10)
    sub = '✅ активна' if await asyncio.to_thread(db.is_subscribed, target) else '— нет'
    banned = '🚫 ДА' if await asyncio.to_thread(db.is_banned, target) else 'нет'
    acc_lines = '\n'.join(('• %s (%s)' % (a.get('phone', '?'), a.get('status', '?')) for a in accs)) or '—'
    log_lines = '\n'.join(('• %s — %s' % (l['ts'], l['text']) for l in logs)) or '—'
    await message.answer('%s <b>Профиль</b> <code>%s</code>\n\nПодписка: %s\nБан: %s\n\n%s <b>Аккаунты (%d):</b>\n%s\n\n%s <b>Логи:</b>\n%s' % (em('users'), target, sub, banned, em('accounts'), len(accs), acc_lines, em('monitor'), log_lines), reply_markup=kb.back_only_kb('nav:admin'))

@dp.callback_query(F.data == 'admin:actions')
async def cb_admin_actions(c: types.CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer('Только для админа', show_alert=True)
        return
    acts = await asyncio.to_thread(db.get_admin_actions, 30)
    if not acts:
        body = 'Пока нет записей.'
    else:
        names = {'give': 'выдал подписку', 'take': 'снял подписку', 'massgive': 'массовая выдача', 'ban': 'забанил', 'unban': 'разбанил', 'announce': 'рассылка всем'}
        lines = []
        for a in acts:
            when = (a.get('ts') or '')[:16].replace('T', ' ')
            act = names.get(a.get('action'), a.get('action'))
            detail = ' — ' + a['detail'] if a.get('detail') else ''
            lines.append('• %s: <b>%s</b> → <code>%s</code>%s' % (when, act, a.get('target_id'), detail))
        body = '\n'.join(lines)
    await show(c, '%s <b>Логи действий админов</b>\n\n%s' % (em('monitor'), body), kb.back_only_kb('nav:admin'))

@dp.message(F.text == kb.NAV_BROADCAST)
async def nav_txt_broadcast(message: types.Message, state: FSMContext):
    await state.clear()
    await _start_broadcast_flow(message, message.from_user.id, state)

@dp.message(F.text == kb.NAV_ACCOUNTS)
async def nav_txt_accounts(message: types.Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    await message.answer(await asyncio.to_thread(accounts_text, uid), reply_markup=kb.accounts_kb(await asyncio.to_thread(db.get_sessions, uid)))

@dp.message(F.text == kb.NAV_MENU)
async def nav_txt_menu(message: types.Message, state: FSMContext):
    await state.clear()
    await gate_or_menu(message, message.from_user.id)

@dp.message(F.text == kb.NAV_SUB)
async def nav_txt_sub(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(await asyncio.to_thread(sub_text, message.from_user.id), reply_markup=kb.payment_kb())

@dp.message(F.text == kb.NAV_HELP)
async def nav_txt_help(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(help_text(), reply_markup=kb.back_only_kb('nav:menu'))

@dp.message(F.text == kb.NAV_SHOP)
async def nav_txt_shop(message: types.Message, state: FSMContext):
    await message.answer('%s Магазин: %s' % (em('gift'), config.SHOP_URL))

async def subscription_watcher():
    while True:
        try:
            for uid in await asyncio.to_thread(db.get_expired_to_notify):
                try:
                    await bot.send_message(uid, '%s Твоя подписка закончилась.\n\nПродли её, чтобы продолжить работать:' % em('warn'), reply_markup=kb.sub_expired_kb())
                except Exception:
                    pass
                await asyncio.to_thread(db.mark_notified, uid)
        except Exception as e:
            log.warning('watcher: %s', e)
        try:
            now = datetime.now()
            for uid, exp_str in await asyncio.to_thread(db.get_active_trials):
                try:
                    exp = datetime.fromisoformat(exp_str)
                except Exception:
                    continue
                hours_left = (exp - now).total_seconds() / 3600.0
                if hours_left <= 0:
                    continue
                if hours_left <= 24:
                    kind, human = ('trial_1', 'меньше суток')
                elif hours_left <= 48:
                    kind, human = ('trial_2', '2 дня')
                elif hours_left <= 72:
                    kind, human = ('trial_3', '3 дня')
                else:
                    continue
                if await asyncio.to_thread(db.reminder_sent, uid, kind):
                    continue
                await asyncio.to_thread(db.mark_reminder, uid, kind)
                try:
                    await bot.send_message(uid, '%s До конца пробного периода осталось <b>%s</b>.\n\nОформи подписку, чтобы не потерять возможности PRO, до %d аккаунтов и PRO-функции:' % (em('wait'), human, config.PAID_MAX_ACCOUNTS), reply_markup=kb.sub_expired_kb())
                except Exception:
                    pass
        except Exception as e:
            log.warning('trial funnel: %s', e)
        await asyncio.sleep(60)

async def _resume_jobs():
    for row in await asyncio.to_thread(db.get_resumable_jobs):
        cfg = row.get('config') or {}
        if not cfg.get('targets'):
            await asyncio.to_thread(db.update_job_status, row['id'], 'stopped')
            try:
                await bot.send_message(row['user_id'], 'Старая рассылка остановлена: проверьте получателей и запустите её заново в новой версии')
            except Exception:
                pass
            continue
        try:
            await worker.start_broadcast_task(user_id=row['user_id'], resume_db_id=row['id'], resume_status=row['status'], **cfg)
        except Exception:
            await asyncio.to_thread(db.update_job_status, row['id'], 'stopped')
            try:
                await bot.send_message(row['user_id'], 'Не удалось восстановить рассылку. Проверьте подписку и аккаунты; повторного запуска не было')
            except Exception:
                pass

async def main():
    runner=None
    tasks=[]
    primary=False
    state=None
    worker.SHUTTING_DOWN=False
    try:
        runner,state=await open_frontend(bot,build_app)
        await wait_for_primary()
        primary=True
        await asyncio.to_thread(db.init_db)
        config.HELP_URL=''
        tasks=[asyncio.create_task(subscription_watcher()),asyncio.create_task(runtime.listener_loop(bot)),asyncio.create_task(scheduling.loop(bot)),asyncio.create_task(_login_cleanup_loop())]
        await _resume_jobs()
        await bot.delete_webhook(drop_pending_updates=False)
        state['ready']=True
        state['message']='Готово'
        log.info('Bot started: %s',config.BOT_NAME)
        await dp.start_polling(bot)
    finally:
        if state is not None:
            state['ready']=False
            state['message']='Сервис перезапускается. Повторите запрос через несколько секунд'
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        worker.SHUTTING_DOWN=True
        for job in worker.JOBS.values():
            if job.task and not job.task.done(): job.task.cancel()
        await asyncio.gather(*(job.task for job in worker.JOBS.values() if job.task),return_exceptions=True)
        await runtime.shutdown()
        for uid in list(_LOGIN_CLIENTS): await _cleanup_login(uid)
        if primary: await asyncio.to_thread(db.release_instance_lock)
        if runner: await runner.cleanup()
        await bot.session.close()

if __name__=='__main__':
    asyncio.run(main())
