# -*- coding: utf-8 -*-
import asyncio
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

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("sendflow")

# Проверяем токен ДО создания бота — иначе aiogram кинет непонятное "Token is invalid!"
config.require_token()

bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

MEDIA_DIR = "media"
os.makedirs(MEDIA_DIR, exist_ok=True)

_LOGIN_CLIENTS = {}
_LOGIN_BUSY = set()  # анти-дубль: пока проверяем код/пароль, повторный ввод игнорируем
_AUTO_MON = {}  # chat_id -> task живого обновления монитора


# ===================== СОСТОЯНИЯ =====================
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


# ===================== ХЕЛПЕРЫ =====================
def is_admin(uid):
    return uid in config.ADMIN_IDS


def has_access(uid):
    return is_admin(uid) or db.is_subscribed(uid)


def _fmt_left(uid):
    exp = db.subscription_expiry(uid)
    if not exp:
        return "нет подписки"
    delta = exp - datetime.now()
    if delta.total_seconds() <= 0:
        return "истекла"
    days = delta.days
    hours = delta.seconds // 3600
    if days > 0:
        return "ещё %d дн. %d ч." % (days, hours)
    return "ещё %d ч." % hours


def _cancel_auto_mon(chat_id):
    t = _AUTO_MON.pop(chat_id, None)
    if t and not t.done():
        t.cancel()


async def show(event, text, markup=None):
    """Редактирует сообщение (из callback) или отправляет новое."""
    if isinstance(event, types.CallbackQuery):
        # уходя с экрана — гасим живой авто-апдейт монитора, иначе он перезапишет новый экран
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


_BANNERS = {
    "menu": "assets/banner_menu.png",
    "profile": "assets/banner_profile.png",
    "accounts": "assets/banner_accounts.png",
    "broadcast": "assets/banner_broadcast.png",
    "monitor": "assets/banner_monitor.png",
    "sub": "assets/banner_sub.png",
}


async def show_banner(event, key, text, markup=None):
    """Показать раздел с баннером (фото + подпись). Фолбэк — обычный текст."""
    path = _BANNERS.get(key)
    if not (path and os.path.exists(path)):
        await show(event, text, markup)
        return
    cap = text if len(text) <= 1024 else text[:1019] + "\u2026"
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
    try:
        member = await bot.get_chat_member(config.REQUIRED_CHANNEL, user_id)
        return member.status in (
            ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR,
        )
    except Exception as e:
        log.warning("channel check failed: %s", e)
        return False


# ===================== ЭКРАНЫ =====================
def menu_text(uid):
    sub = "админ" if is_admin(uid) else _fmt_left(uid)
    return (
        "%s <b>%s</b>\n\n"
        "%s Подписка: <b>%s</b>\n"
        "%s Аккаунтов: <b>%d</b>\n\n"
        "Выбери раздел ниже — в любом экране есть кнопки «Назад» и «Главное»."
    ) % (em("rocket"), config.BOT_NAME, em("star"), sub,
         em("accounts"), len(db.get_sessions(uid)))


async def open_menu(event, uid):
    await show_banner(event, "menu", menu_text(uid), kb.main_menu_kb(is_admin(uid)))


async def gate_or_menu(event, uid):
    if db.is_banned(uid):
        await show(event, "%s Доступ к боту заблокирован администратором." % em("ban"), None)
        return
    if has_access(uid):
        await open_menu(event, uid)
        return
    text = (
        "%s Чтобы начать, подпишись на наш канал.\n\n"
        "%s После подписки ты получишь <b>%d дня бесплатной подписки</b>.\n"
        "Нажми «Я подписался» после подписки."
    ) % (em("channel"), em("gift"), config.FREE_TRIAL_DAYS)
    await show(event, text, kb.channel_gate_kb())


# ===================== /start =====================
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    u = message.from_user
    db.track_user(u.id, u.username, u.first_name)
    # Реферальная ссылка: /start ref123 или /start 123
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1:
        m = re.search(r"(\d{4,})", parts[1])
        if m:
            ref_id = int(m.group(1))
            if ref_id != u.id and db.add_referral(ref_id, u.id):
                try:
                    exp = db.add_subscription_hours(ref_id, config.REF_HOURS, source="ref")
                    db.add_log(ref_id, "🎁 +%d ч за приглашённого" % config.REF_HOURS)
                    await bot.send_message(
                        ref_id,
                        "%s По твоей ссылке зашёл новый пользователь!\n+%d ч подписки. Активна до <b>%s</b>." % (
                            em("gift"), config.REF_HOURS, exp.strftime("%d.%m.%Y %H:%M")))
                except Exception:
                    pass
    # Нижняя панель навигации (reply-клавиатура) — выставляем один раз.
    try:
        await message.answer("%s Панель навигации активирована." % em("rocket"), reply_markup=kb.reply_nav_kb())
    except Exception:
        pass
    uname = ("@" + u.username) if u.username else (u.first_name or "друг")
    uname = uname.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    _hand = '<tg-emoji emoji-id="%s">👋</tg-emoji>' % config.HELLO_EMOJI_ID
    _tail = "\n\n<b>Выбери, как тебе удобнее работать:</b>"
    text_premium = "%s Привет, %s!%s" % (_hand, uname, _tail)
    text_plain = "👋 Привет, %s!%s" % (uname, _tail)
    _banner = config.START_BANNER if getattr(config, "START_BANNER", "") else "assets/logo.png"

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


@dp.message(Command("menu"))
async def cmd_menu(message: types.Message, state: FSMContext):
    await state.clear()
    await gate_or_menu(message, message.from_user.id)


# ===================== НАВИГАЦИЯ =====================
@dp.callback_query(F.data == "mode:chat")
async def cb_mode_chat(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await gate_or_menu(c, c.from_user.id)


@dp.callback_query(F.data == "gate:check")
async def cb_gate_check(c: types.CallbackQuery):
    uid = c.from_user.id
    if has_access(uid):
        await open_menu(c, uid)
        return
    subbed = await check_channel_sub(uid)
    if not subbed:
        await c.answer("Ты ещё не подписан на канал — подпишись и повтори.", show_alert=True)
        return
    if not db.trial_used(uid):
        exp = db.grant_trial(uid, config.FREE_TRIAL_DAYS)
        if exp:
            await c.answer("Зачислено %d дня бесплатно!" % config.FREE_TRIAL_DAYS, show_alert=True)
        await open_menu(c, uid)
    else:
        text = ("%s Бесплатный период уже использован.\n\nОформи подписку, чтобы продолжить:") % em("warn")
        await show(c, text, kb.payment_kb())


@dp.callback_query(F.data == "nav:menu")
async def cb_menu(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await gate_or_menu(c, c.from_user.id)


# ===================== АККАУНТЫ =====================
def accounts_text(uid):
    sessions = db.get_sessions(uid)
    if not sessions:
        body = "У тебя пока нет аккаунтов. Добавь первый — он нужен для рассылки."
    else:
        body = "\n".join("%s <b>%s</b> · %s" % (em("green"), s.get("phone") or s["id"], s.get("status", "active"))
                         for s in sessions)
    return "%s <b>Аккаунты</b>\n\n%s" % (em("accounts"), body)


@dp.callback_query(F.data == "nav:accounts")
async def cb_accounts(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await show_banner(c, "accounts", accounts_text(c.from_user.id), kb.accounts_kb(db.get_sessions(c.from_user.id)))


@dp.callback_query(F.data == "nav:profile")
async def cb_profile(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    uid = c.from_user.id
    sessions = db.get_sessions(uid)
    sub_exp = db.subscription_expiry(uid)
    if is_admin(uid):
        sub = "Администратор (безлимит)"
    elif sub_exp and sub_exp > datetime.now():
        sub = "активна до %s" % sub_exp.strftime("%d.%m.%Y %H:%M")
    else:
        sub = "не активна"
    plan = "Premium" if _is_paid(uid) else "Free"
    acc_lines = "\n".join("%s %s · %s" % (em("green"), s.get("phone") or s["id"], s.get("status", "active"))
                          for s in sessions) or "нет аккаунтов"
    text = (
        "%s <b>Профиль</b>\n\n"
        "%s ID: <code>%d</code>\n"
        "%s Тариф: <b>%s</b>\n"
        "%s Подписка: <b>%s</b>\n"
        "%s Аккаунтов: <b>%d</b>\n"
        "%s Рефералов: <b>%d</b>\n"
        "%s Шаблонов: <b>%d</b>\n\n"
        "<b>Твои аккаунты:</b>\n%s"
    ) % (em("accounts"), em("key"), uid, em("star"), plan, em("ok"), sub,
         em("accounts"), len(sessions), em("ref"), db.count_referrals(uid),
         em("msg"), db.count_templates(uid), acc_lines)
    await show_banner(c, "profile", text, kb.back_only_kb("nav:menu"))


def _is_paid(uid):
    return is_admin(uid) or db.is_paid_plan(uid)


@dp.callback_query(F.data == "acc:add")
async def cb_acc_add(c: types.CallbackQuery, state: FSMContext):
    uid = c.from_user.id
    # Лимит аккаунтов по тарифу: бесплатно — 2, подписка — 5.
    max_acc = config.PAID_MAX_ACCOUNTS if _is_paid(uid) else config.FREE_MAX_ACCOUNTS
    if len(db.get_sessions(uid)) >= max_acc:
        if _is_paid(uid):
            await show(c, "%s Достигнут лимит аккаунтов подписки (%d). Удали лишний, чтобы добавить новый." % (em("warn"), max_acc), kb.back_only_kb("nav:accounts"))
        else:
            await show(c, "%s На бесплатном тарифе можно подключить до <b>%d</b> аккаунтов.\n\n%s На подписке — до <b>%d</b> аккаунтов и безлимит рассылок." % (em("warn"), config.FREE_MAX_ACCOUNTS, em("star"), config.PAID_MAX_ACCOUNTS), kb.payment_kb())
        return
    if config.SKIP_API_PROMPT and config.DEFAULT_API_ID and config.DEFAULT_API_HASH:
        await state.update_data(api_id=config.DEFAULT_API_ID, api_hash=config.DEFAULT_API_HASH)
        await state.set_state(AddAccount.proxy)
        await show(c, _PROXY_PROMPT, kb.cancel_kb())
    else:
        await state.set_state(AddAccount.api_id)
        await show(c, "%s Шаг 1/3. Отправь <b>API_ID</b> (цифры, с my.telegram.org)." % em("key"), kb.cancel_kb())


@dp.message(AddAccount.api_id)
async def st_api_id(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip()
    if not txt.isdigit() or len(txt) < 5:
        await message.answer("API_ID — это число (обычно 7–8 цифр). Попробуй ещё раз.", reply_markup=kb.cancel_kb())
        return
    await state.update_data(api_id=int(txt))
    await state.set_state(AddAccount.api_hash)
    await message.answer("%s Шаг 2/3. Теперь отправь <b>API_HASH</b>." % em("key"), reply_markup=kb.cancel_kb())


@dp.message(AddAccount.api_hash)
async def st_api_hash(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip()
    if len(txt) < 10:
        await message.answer("Похоже, это не API_HASH. Попробуй ещё раз.", reply_markup=kb.cancel_kb())
        return
    await state.update_data(api_hash=txt)
    await state.set_state(AddAccount.proxy)
    await message.answer(_PROXY_PROMPT, reply_markup=kb.cancel_kb())


_PROXY_PROMPT = (
    "\U0001F511 Пришли <b>прокси</b> для этого аккаунта (рекомендуется — снижает риск блокировки).\n\n"
    "Формат: <code>socks5://user:pass@host:port</code> или <code>host:port</code>.\n"
    "Если прокси не нужен — отправь /skip."
)


@dp.message(AddAccount.proxy)
async def st_proxy(message: types.Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() in ("/skip", "skip", "-", "нет"):
        await state.update_data(proxy=None)
    else:
        if worker._parse_proxy(raw) is None:
            await message.answer("Не понял прокси. Формат: socks5://user:pass@host:port или host:port. Или /skip.", reply_markup=kb.cancel_kb())
            return
        await state.update_data(proxy=raw)
    await state.set_state(AddAccount.phone)
    await message.answer("%s Отправь номер телефона в формате +7XXXXXXXXXX." % em("phone"), reply_markup=kb.cancel_kb())


@dp.message(AddAccount.phone)
async def st_phone(message: types.Message, state: FSMContext):
    phone = (message.text or "").strip().replace(" ", "")
    if not re.match(r"^\+?\d{10,15}$", phone):
        await message.answer("Номер неверный. Пример: +79991234567", reply_markup=kb.cancel_kb())
        return
    data = await state.get_data()
    from pyrogram import Client
    app = Client("login_%s" % message.from_user.id, api_id=data["api_id"],
                 api_hash=data["api_hash"], in_memory=True,
                 proxy=worker._parse_proxy(data.get("proxy")))
    try:
        await app.connect()
        sent = await app.send_code(phone)
        await state.update_data(phone=phone, phone_code_hash=sent.phone_code_hash)
        _LOGIN_CLIENTS[message.from_user.id] = app
        await state.set_state(AddAccount.code)
        await message.answer("%s Код отправлен в Telegram. Введи его С ПРОБЕЛАМИ, например: 1 2 3 4 5." % em("ok"),
                             reply_markup=kb.cancel_kb())
    except Exception as e:
        try:
            await app.disconnect()
        except Exception:
            pass
        await state.clear()
        await message.answer("%s Ошибка: %s" % (em("cross"), str(e)[:120]), reply_markup=kb.back_only_kb("nav:accounts"))


@dp.message(AddAccount.code)
async def st_code(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    code = re.sub(r"\D", "", message.text or "")
    data = await state.get_data()
    app = _LOGIN_CLIENTS.get(uid)
    if not app:
        await state.clear()
        await message.answer("Сессия входа потеряна, начни заново.", reply_markup=kb.back_only_kb("nav:accounts"))
        return
    if uid in _LOGIN_BUSY:
        await message.answer("⏳ Уже проверяю предыдущий код — подожди пару секунд, не вводи повторно.")
        return
    _LOGIN_BUSY.add(uid)
    await message.answer("⏳ Проверяю код, подожди...")
    try:
        await app.sign_in(data["phone"], data["phone_code_hash"], code)
        await _finish_login(message, state, app, data)
    except Exception as e:
        msg = str(e)
        if "SESSION_PASSWORD_NEEDED" in msg or "password" in msg.lower():
            await state.set_state(AddAccount.password)
            await message.answer("%s Включён пароль 2FA. Отправь пароль." % em("key"), reply_markup=kb.cancel_kb())
        else:
            await message.answer("%s Неверный код: %s\nПопробуй ещё раз." % (em("cross"), msg[:80]), reply_markup=kb.cancel_kb())
    finally:
        _LOGIN_BUSY.discard(uid)


@dp.message(AddAccount.password)
async def st_password(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    data = await state.get_data()
    app = _LOGIN_CLIENTS.get(uid)
    if not app:
        await state.clear()
        await message.answer("Сессия входа потеряна.", reply_markup=kb.back_only_kb("nav:accounts"))
        return
    if uid in _LOGIN_BUSY:
        await message.answer("⏳ Уже проверяю пароль — подожди, не вводи повторно.")
        return
    _LOGIN_BUSY.add(uid)
    await message.answer("⏳ Проверяю пароль, подожди...")
    try:
        await app.check_password((message.text or "").strip())
        await _finish_login(message, state, app, data)
    except Exception as e:
        await message.answer("%s Неверный пароль: %s" % (em("cross"), str(e)[:80]), reply_markup=kb.cancel_kb())
    finally:
        _LOGIN_BUSY.discard(uid)


async def _finish_login(message, state, app, data):
    try:
        ss = await app.export_session_string()
        me = await app.get_me()
        phone = data.get("phone") or ("+" + (me.phone_number or "")) or str(me.id)
        db.add_session(message.from_user.id, phone, data["api_id"], data["api_hash"], ss, proxy=data.get("proxy"))
        db.add_log(message.from_user.id, "Добавлен аккаунт %s" % phone)
        await message.answer("%s Аккаунт <b>%s</b> успешно добавлен!" % (em("ok"), phone),
                             reply_markup=kb.back_only_kb("nav:accounts"))
    except Exception as e:
        await message.answer("%s Ошибка сохранения: %s" % (em("cross"), str(e)[:100]),
                             reply_markup=kb.back_only_kb("nav:accounts"))
    finally:
        try:
            await app.disconnect()
        except Exception:
            pass
        _LOGIN_CLIENTS.pop(message.from_user.id, None)
        await state.clear()


@dp.callback_query(F.data.startswith("acc:del:"))
async def cb_acc_del(c: types.CallbackQuery):
    sid = int(c.data.split(":")[-1])
    db.delete_session(sid, owner_id=c.from_user.id)
    await c.answer("Аккаунт удалён")
    await show(c, accounts_text(c.from_user.id), kb.accounts_kb(db.get_sessions(c.from_user.id)))


@dp.callback_query(F.data.startswith("acc:spam:"))
async def cb_acc_spam(c: types.CallbackQuery):
    sid = int(c.data.split(":")[-1])
    sessions = [s for s in db.get_sessions(c.from_user.id) if s["id"] == sid]
    if not sessions:
        await c.answer("Аккаунт не найден", show_alert=True)
        return
    await c.answer("Проверяю через @SpamBot, подожди...")
    try:
        status = await worker.check_spam_status(sessions[0])
    except Exception as e:
        status = "Ошибка: %s" % str(e)[:100]
    phone = sessions[0].get("phone") or sid
    await show(c, "%s <b>@SpamBot — %s</b>\n\n%s" % (em("accounts"), phone, status), kb.back_only_kb("nav:accounts"))


# ===================== ЛОГИ =====================
@dp.callback_query(F.data == "nav:logs")
async def cb_logs(c: types.CallbackQuery):
    logs = db.get_logs(c.from_user.id, 25)
    if not logs:
        body = "Пока пусто."
    else:
        body = "\n".join("<code>%s</code> %s" % (l["ts"], l["text"]) for l in logs)
    await show(c, "%s <b>Логи</b>\n\n%s" % (em("logs"), body), kb.back_only_kb("nav:menu"))


# ===================== МОНИТОРИНГ =====================
@dp.callback_query(F.data == "nav:monitor")
async def cb_monitor(c: types.CallbackQuery):
    uid = c.from_user.id
    jobs = worker.get_user_jobs(uid)
    text = "%s <b>Мониторинг</b>\n\n%s" % (em("monitor"), worker.render_all(uid))
    await show(c, text, kb.monitor_kb(jobs))


async def _auto_monitor(chat_id, message_id, job_id):
    """Живое ��бновление экрана рассылки каждые 5с (таймер до конца)."""
    try:
        for _ in range(240):  # до ~20 минут
            await asyncio.sleep(5)
            # если пользователь ушёл с экрана — этот таск уже не актуален
            if _AUTO_MON.get(chat_id) is not asyncio.current_task():
                break
            job = worker.get_job(job_id)
            if not job:
                break
            text = "%s <b>Мониторинг</b>\n\n%s" % (em("monitor"), worker.render_job(job))
            try:
                await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id,
                                            reply_markup=kb.monitor_job_kb(job))
            except Exception:
                pass
            if job.status not in ("running", "paused"):
                break
    except asyncio.CancelledError:
        pass
    finally:
        if _AUTO_MON.get(chat_id) is asyncio.current_task():
            _AUTO_MON.pop(chat_id, None)


@dp.callback_query(F.data.startswith("mon:open:"))
async def cb_mon_open(c: types.CallbackQuery):
    job_id = int(c.data.split(":")[-1])
    job = worker.get_job(job_id)
    text = "%s <b>Мониторинг</b>\n\n%s" % (em("monitor"), worker.render_job(job))
    await show(c, text, kb.monitor_job_kb(job))
    # запускаем живое обновление (show уже погасил предыдущий таск)
    chat_id = c.message.chat.id
    if job and job.status in ("running", "paused"):
        _AUTO_MON[chat_id] = asyncio.create_task(
            _auto_monitor(chat_id, c.message.message_id, job_id))


async def _mon_action(c, action):
    job_id = int(c.data.split(":")[-1])
    {"pause": worker.pause_job, "resume": worker.resume_job, "stop": worker.stop_job}[action](job_id)
    await c.answer({"pause": "Пауза", "resume": "Продолжаем", "stop": "Остановлено"}[action])
    job = worker.get_job(job_id)
    text = "%s <b>Мониторинг</b>\n\n%s" % (em("monitor"), worker.render_job(job))
    await show(c, text, kb.monitor_job_kb(job))
    chat_id = c.message.chat.id
    if job and job.status in ("running", "paused"):
        _AUTO_MON[chat_id] = asyncio.create_task(
            _auto_monitor(chat_id, c.message.message_id, job_id))


@dp.callback_query(F.data.startswith("mon:pause:"))
async def cb_mon_pause(c: types.CallbackQuery):
    await _mon_action(c, "pause")


@dp.callback_query(F.data.startswith("mon:resume:"))
async def cb_mon_resume(c: types.CallbackQuery):
    await _mon_action(c, "resume")


@dp.callback_query(F.data.startswith("mon:stop:"))
async def cb_mon_stop(c: types.CallbackQuery):
    await _mon_action(c, "stop")


# ===================== ПОДПИСКА / ОПЛАТА =====================
def sub_text(uid):
    e = em
    return (
        "%s <b>Подписка PREMIUM</b>\n\n"
        "Статус: <b>%s</b>\n"
        "Цена: <b>%d ⭐️</b> за %d дней\n\n"
        "<b>Что даёт PREMIUM:</b>\n"
        "%s Безлимит рассылок (без дневного лимита циклов)\n"
        "%s Больше аккаунтов одновременно (до %d)\n"
        "%s AI-персонализация текста (продающий текст за секунды)\n"
        "%s Рассылка с кнопками-ссылками\n"
        "%s Невидимые теги (скрытые упоминания для охвата)\n"
        "%s Быстрее: меньше задержка между сообщениями\n"
        "%s Больше папок в рассылке (до %d)\n"
        "%s Приоритетная поддержка\n\n"
        "<b>Оплата:</b>\n"
        "%s <b>Telegram Stars</b> — автоматически и мгновенно\n"
        "%s Карта / крипта — вручную через %s\n\n"
        "Выбери способ оплаты:"
    ) % (e("star"), ("админ" if is_admin(uid) else _fmt_left(uid)),
         config.SUB_PRICE_STARS, config.SUB_DAYS,
         e("ok"), e("accounts"), config.PAID_MAX_ACCOUNTS, e("rocket"),
         e("check"), e("star"), e("timer"), e("folder"), config.PAID_MAX_FOLDERS,
         e("ok"), e("star"), e("card"), config.OWNER_CONTACT)


@dp.callback_query(F.data == "nav:sub")
async def cb_sub(c: types.CallbackQuery):
    await show_banner(c, "sub", sub_text(c.from_user.id), kb.payment_kb())


@dp.callback_query(F.data == "pay:stars")
async def cb_pay_stars(c: types.CallbackQuery):
    prices = [LabeledPrice(label="Подписка %d дн." % config.SUB_DAYS, amount=config.SUB_PRICE_STARS)]
    try:
        await bot.send_invoice(
            chat_id=c.from_user.id,
            title="%s — подписка" % config.BOT_NAME,
            description="Доступ ко всем функциям на %d дней." % config.SUB_DAYS,
            payload="subscription_%d" % config.SUB_DAYS,
            provider_token="",
            currency="XTR",
            prices=prices,
        )
        await c.answer()
    except Exception as e:
        await c.answer("Не удалось выставить счёт: %s" % str(e)[:80], show_alert=True)


@dp.callback_query(F.data == "pay:manual")
async def cb_pay_manual(c: types.CallbackQuery):
    # Карта/крипта — только вручную через @zucag. Автооплата — только Stars.
    text = ("%s <b>Оплата картой или криптой — вручную</b>\n\n"
            "Автооплата в боте — только через <b>Telegram Stars</b> ⭐️ (мгновенно).\n\n"
            "Хочешь оплатить <b>картой</b> (РФ/УКР) или <b>криптой</b> (TON, USDT и др.)?\n"
            "Напиши %s — активируем подписку вручную после оплаты.") % (
        em("card"), config.OWNER_CONTACT)
    await show(c, text, kb.back_only_kb("nav:sub"))


@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    # Публичная статистика (Social Proof): витринная база + реальные отправки.
    try:
        today = config.SOCIAL_PROOF_BASE + db.sent_today()
        total = config.SOCIAL_PROOF_TOTAL_BASE + db.sent_total()
    except Exception:
        today, total = config.SOCIAL_PROOF_BASE, config.SOCIAL_PROOF_TOTAL_BASE
    text = (
        "%s <b>Статистика %s</b>\n\n"
        "%s За сегодня отправлено: <b>%s</b> сообщений\n"
        "%s Всего за всё время: <b>%s</b> сообщений\n\n"
        "<i>Присоединяйся к тысячам пользователей %s!</i>"
    ) % (em("chart"), config.BOT_NAME, em("rocket"),
         "{:,}".format(today).replace(",", "\u202f"), em("star"),
         "{:,}".format(total).replace(",", "\u202f"), config.BOT_NAME)
    await message.answer(text, reply_markup=kb.back_only_kb("nav:menu"))


@dp.pre_checkout_query()
async def pre_checkout(q: types.PreCheckoutQuery):
    await q.answer(ok=True)


@dp.message(F.successful_payment)
async def on_paid(message: types.Message):
    exp = db.set_subscription(message.from_user.id, config.SUB_DAYS, source="stars")
    db.add_log(message.from_user.id, "Оплачена подписка (Stars)")
    try:
        db.log_payment(message.from_user.id, message.successful_payment.total_amount, "XTR", "stars")
    except Exception:
        pass
    await message.answer("%s Подписка активна до <b>%s</b>!" % (em("ok"), exp.strftime("%d.%m.%Y")),
                         reply_markup=kb.main_menu_kb(is_admin(message.from_user.id)))


# ===================== АДМИН-ПАНЕЛЬ =====================
@dp.callback_query(F.data == "nav:admin")
async def cb_admin(c: types.CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Только для админа", show_alert=True)
        return
    active_bc = sum(1 for j in worker.JOBS.values() if getattr(j, "status", "") in ("running", "paused"))
    text = ("%s <b>Админ-панель — дашборд</b>\n\n"
            "%s Пользователей всего: <b>%d</b>\n"
            "%s Активных подписок: <b>%d</b>\n"
            "%s Платных подписок: <b>%d</b>\n"
            "%s Аккаунтов подключено: <b>%d</b>\n"
            "%s Активных рассылок сейчас: <b>%d</b>\n\n"
            "%s <b>Выручка (Stars):</b>\n"
            "• За 30 дней: <b>%d ⭐️</b>\n"
            "• За всё время: <b>%d ⭐️</b>") % (
        em("admin"), em("users"), db.count_users(),
        em("star"), db.count_active_subs(),
        em("star"), db.count_paid_subs(),
        em("accounts"), len(db.get_all_sessions()),
        em("rocket"), active_bc,
        em("star"), db.revenue_stars(30), db.revenue_stars(None))
    await show(c, text, kb.admin_kb())


@dp.callback_query(F.data == "admin:users")
async def cb_admin_users(c: types.CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Только для админа", show_alert=True)
        return
    users = db.get_all_users()[:40]
    rows = []
    for u in users:
        uname = ("@" + u["username"]) if u.get("username") else "—"
        sub = "✅" if db.is_subscribed(u["telegram_id"]) else "—"
        rows.append("<code>%s</code> %s %s" % (u["telegram_id"], uname, sub))
    body = "\n".join(rows) or "Пусто."
    await show(c, "%s <b>Пользователи</b> (%d)\n\n%s" % (em("users"), db.count_users(), body),
               kb.admin_back_kb())


@dp.callback_query(F.data == "admin:accounts")
async def cb_admin_accounts(c: types.CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Только для админа", show_alert=True)
        return
    sessions = db.get_all_sessions()[:50]
    rows = ["%s <b>%s</b> · владелец <code>%s</code>" % (em("green"), s.get("phone") or s["id"], s["owner_id"])
            for s in sessions]
    body = "\n".join(rows) or "Пусто."
    await show(c, "%s <b>Аккаунты</b> (%d)\n\n%s" % (em("accounts"), len(db.get_all_sessions()), body),
               kb.admin_back_kb())


# ===================== РАССЫЛКА =====================
async def _start_broadcast_flow(event, uid, state):
    if not has_access(uid):
        await show(event, "%s Для рассылки нужна активная подписка." % em("warn"), kb.payment_kb())
        return
    if not db.get_sessions(uid):
        await show(event, "%s Сначала добавь хотя бы один аккаунт." % em("warn"),
                   kb.back_only_kb("nav:accounts"))
        return
    if isinstance(event, types.CallbackQuery):
        await event.answer("Загружаю папки...")
    await state.clear()
    sessions = db.get_sessions(uid)
    folders = []
    try:
        # ВАЖНО: не только sessions[0]. У пользователя может быть 2+ аккаунта,
        # и первый может быть без папок/битый, а папки есть на другом.
        folders = await worker.get_user_folders_any(sessions, use_cache=False)
    except Exception as e:
        log.warning("folders error: %s", e)
    await state.update_data(folders=folders, chosen=[])
    if folders:
        await show(event, "%s <b>Выбери папки</b> для рассылки (можно несколько) или по всем чатам:" % em("folder"),
                   kb.folders_kb(folders, []))
    else:
        # Папок нет/не нашлись — НЕ уходим молча в «все чаты».
        # Всегда показываем экран выбора с кнопкой «По всем чатам», чтобы
        # пользователь сам решал и видел, что происходит.
        await show(event, "%s <b>Папки у аккаунта не найдены.</b>\n\n"
                          "Можешь разослать <b>по всем чатам</b> — это все группы и супергруппы, "
                          "где состоит аккаунт.\n\n"
                          "<i>Я проверил все подключённые аккаунты. Если папки есть в Telegram, "
                          "но тут пусто — открой Логи и пришли строку 🗂 Папки аккаунта.</i>" % em("folder"),
                   kb.folders_kb([], []))


@dp.callback_query(F.data == "bc:start")
async def cb_bc_start(c: types.CallbackQuery, state: FSMContext):
    await _start_broadcast_flow(c, c.from_user.id, state)


@dp.callback_query(F.data.startswith("bc:fold:"))
async def cb_bc_fold(c: types.CallbackQuery, state: FSMContext):
    fid = int(c.data.split(":")[-1])
    data = await state.get_data()
    chosen = list(data.get("chosen", []))
    folders = data.get("folders", [])
    if fid in chosen:
        chosen.remove(fid)
    else:
        max_f = config.PAID_MAX_FOLDERS if _is_paid(c.from_user.id) else config.FREE_MAX_FOLDERS
        if len(chosen) >= max_f:
            await c.answer("Максимум %d папок на вашем тарифе" % max_f, show_alert=True)
            return
        chosen.append(fid)
    await state.update_data(chosen=chosen)
    await show(c, "%s <b>Выбери папки</b> (выбрано: %d):" % (em("folder"), len(chosen)),
               kb.folders_kb(folders, chosen))


@dp.callback_query(F.data == "bc:all")
async def cb_bc_all(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(target_type="all", chosen=[])
    await _ask_mode(c, state)


@dp.callback_query(F.data == "bc:fold_done")
async def cb_bc_fold_done(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("chosen"):
        await c.answer("Выбери хотя бы одну папку", show_alert=True)
        return
    await state.update_data(target_type="folders")
    await _ask_mode(c, state)


async def _ask_mode(event, state):
    """Показывает выбор режима рассылки после выбора цели (папки/все чаты)."""
    await state.set_state(None)
    await show(event, "%s <b>Выбери режим рассылки</b>\n\n"
                      "%s <b>Только текст</b> — обычное текстовое сообщение\n"
                      "%s <b>Медиа + текст</b> — фото или видео с подписью\n"
                      "%s <b>Пересыл по ссылке</b> — бот перешлёт пост по ссылке (сохранит кнопки оригинала)\n"
                      "%s <b>Текст + кнопки</b> — сообщение с кнопками-ссылками под ним" % (
                      em("rocket"), em("msg"), em("app"), em("rocket"), em("key")),
               kb.bc_mode_kb())


@dp.callback_query(F.data == "bc:mode:text")
async def cb_mode_text(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(bc_mode="text", buttons=[])
    await state.set_state(Broadcast.message)
    await show(c, "%s Отправь <b>текст</b> для рассылки.\n\nФорматирование (жирный, курсив, цитаты, эмодзи) сохранится 1-в-1." % em("msg"),
               kb.cancel_kb())


@dp.callback_query(F.data == "bc:mode:media")
async def cb_mode_media(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(bc_mode="media", buttons=[])
    await state.set_state(Broadcast.message)
    await show(c, "%s Отправь <b>фото или видео</b> (можно с подписью)." % em("app"),
               kb.cancel_kb())


@dp.callback_query(F.data == "bc:mode:forward")
async def cb_mode_forward(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(bc_mode="forward", buttons=[])
    await state.set_state(Broadcast.link)
    await show(c, "%s Пришли <b>ссылку на сообщение</b>, которое нужно разослать.\n\n"
                  "Например: <code>https://t.me/durov/123</code>\n"
                  "Бот перешлёт его во все выбранные чаты (с медиа и кнопками оригинала)." % em("rocket"),
               kb.cancel_kb())


@dp.callback_query(F.data == "bc:mode:buttons")
async def cb_mode_buttons(c: types.CallbackQuery, state: FSMContext):
    # Режим «текст + кнопки» пока доступен только админи��тратору.
    if not (_is_paid(c.from_user.id) or is_admin(c.from_user.id)):
        await c.answer("🔒 Рассылка с кнопками — только для PREMIUM. Оформи подписку!", show_alert=True)
        return
    await state.update_data(bc_mode="buttons", buttons=[])
    await state.set_state(Broadcast.message)
    await show(c, "%s Сначала отправь <b>текст</b> сообщения (можно с фото/видео).\n\n"
                  "Потом добавим кнопки-ссылки под ним." % em("key"),
               kb.cancel_kb())


def _parse_msg_link(link):
    """Парсит ссылку на сообщение Telegram → (from_chat, msg_id)."""
    link = (link or "").strip()
    m = re.search(r"t\.me/c/(\d+)/(?:\d+/)?(\d+)", link)
    if m:
        return int("-100" + m.group(1)), int(m.group(2))
    m = re.search(r"t\.me/([A-Za-z0-9_]+)/(?:\d+/)?(\d+)", link)
    if m:
        return m.group(1), int(m.group(2))
    return None, None


@dp.message(Broadcast.link)
async def st_bc_link(message: types.Message, state: FSMContext):
    from_chat, msg_id = _parse_msg_link(message.text or "")
    if not from_chat or not msg_id:
        await message.answer("%s Не похоже на ссылку на сообщение. Пришли ссылку вида https://t.me/canal/123" % em("warn"),
                             reply_markup=kb.cancel_kb())
        return
    await state.update_data(payload={"type": "forward", "from_chat": from_chat, "msg_id": msg_id,
                                     "link": (message.text or "").strip(), "text": ""})
    data = await state.get_data()
    if data.get("editing"):
        await state.update_data(editing=False)
        await show_confirm(message, state)
        return
    await state.set_state(Broadcast.delay)
    await message.answer("%s Задержка между сообщениями в секундах (рекомендую ≈30)?" % em("timer"),
                         reply_markup=kb.cancel_kb())


@dp.callback_query(F.data == "bc:btn_add")
async def cb_btn_add(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(Broadcast.btn_name)
    await show(c, "%s Введи <b>название кнопки</b> (что будет написано на кнопке):" % em("key"),
               kb.cancel_kb())


@dp.message(Broadcast.btn_name)
async def st_btn_name(message: types.Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("Название не может быть пустым. Введи текст кнопки:", reply_markup=kb.cancel_kb())
        return
    await state.update_data(_btn_name=name)
    await state.set_state(Broadcast.btn_url)
    await message.answer("%s Теперь пришли <b>ссылку</b>, куда ведёт кнопка (https://…):" % em("rocket"),
                         reply_markup=kb.cancel_kb())


@dp.message(Broadcast.btn_url)
async def st_btn_url(message: types.Message, state: FSMContext):
    url = (message.text or "").strip()
    if not (url.startswith("http://") or url.startswith("https://") or url.startswith("tg://")):
        await message.answer("%s Ссылка должна начинаться с http:// или https://. Попробуй ещё раз:" % em("warn"),
                             reply_markup=kb.cancel_kb())
        return
    data = await state.get_data()
    buttons = list(data.get("buttons", []))
    buttons.append({"text": data.get("_btn_name", "Открыть"), "url": url})
    await state.update_data(buttons=buttons)
    await state.set_state(None)
    await message.answer("%s Кнопка добавлена. Можно добавить ещё или нажать «Готово»." % em("ok"),
                         reply_markup=kb.bc_buttons_kb(buttons))


@dp.callback_query(F.data.startswith("bc:btn_del:"))
async def cb_btn_del(c: types.CallbackQuery, state: FSMContext):
    i = int(c.data.split(":")[-1])
    data = await state.get_data()
    buttons = list(data.get("buttons", []))
    if 0 <= i < len(buttons):
        buttons.pop(i)
    await state.update_data(buttons=buttons)
    await show(c, "%s <b>Кнопки сообщения</b>" % em("key"), kb.bc_buttons_kb(buttons))


@dp.callback_query(F.data.in_({"bc:btn_done", "bc:btn_skip"}))
async def cb_btn_done(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    buttons = data.get("buttons", []) if c.data == "bc:btn_done" else []
    payload = dict(data.get("payload") or {"type": "text", "text": ""})
    payload["buttons"] = buttons
    await state.update_data(payload=payload, buttons=buttons)
    if data.get("editing"):
        await state.update_data(editing=False)
        await show_confirm(c, state)
        return
    await state.set_state(Broadcast.delay)
    await show(c, "%s Задержка между сообщениями в секундах (рекомендую ≈30)?" % em("timer"),
              kb.cancel_kb())


async def show_confirm(event, state):
    data = await state.get_data()
    tt = {"all": "по всем чатам", "folders": "по выбранным папкам"}.get(data.get("target_type"), "по всем чатам")
    p = data.get("payload") or {}
    ptype = {"text": "текст", "photo": "фото", "video": "видео", "forward": "пересыл по ссылке"}.get(p.get("type"), "—")
    _btns = p.get("buttons") or []
    if _btns:
        ptype += " + %d кнопк(и)" % len(_btns)
    uid = event.from_user.id
    autosub = data.get("autosub", True)
    autofolder = data.get("autofolder")
    paid = _is_paid(uid)
    invisible_tags = bool(data.get("invisible_tags", False)) and paid
    invis_line = ""
    if paid:
        invis_line = "\nНевидимые теги: <b>%s</b>" % ("вкл" if invisible_tags else "выкл")
    text = ("%s <b>Проверь и запускай</b>\n\n"
            "Цель: <b>%s</b>\nСообщение: <b>%s</b>\nЗадержка: <b>%d с</b>\nЦиклов: <b>%d</b>\nАккаунтов: <b>%d</b>\n"
            "Автоподписка: <b>%s</b>\nАвто-папка: <b>%s</b>%s") % (
        em("ok"), tt, ptype, data.get("delay", 30), data.get("cycles", 1), len(db.get_sessions(uid)),
        ("вкл" if autosub else "выкл"), (autofolder or "выкл"), invis_line)
    _nvar = len([v for v in (p.get("variants") or []) if v and str(v).strip()])
    await show(event, text, kb.confirm_kb(autosub, autofolder, invisible_tags, paid, is_admin(uid), _nvar))


@dp.callback_query(F.data == "bc:toggle_invis")
async def cb_toggle_invis(c: types.CallbackQuery, state: FSMContext):
    if not _is_paid(c.from_user.id):
        await c.answer("Невидимые теги доступны по подписке", show_alert=True)
        return
    data = await state.get_data()
    cur = bool(data.get("invisible_tags", False))
    await state.update_data(invisible_tags=(not cur))
    await c.answer("Невидимые теги " + ("включены" if not cur else "выключены"))
    await show_confirm(c, state)


@dp.callback_query(F.data == "bc:ai")
async def cb_bc_ai(c: types.CallbackQuery, state: FSMContext):
    # AI-персонализация работает ТОЛЬКО у админа. Для остальных — «в разработке».
    if not (_is_paid(c.from_user.id) or is_admin(c.from_user.id)):
        await c.answer("🔒 AI-персонализация — только для PREMIUM. Оформи подписку!", show_alert=True)
        return
    if not ai.is_configured():
        await c.answer("AI не настроен: добавь AI_API_KEY в .env (бесплатно на console.groq.com).", show_alert=True)
        return
    data = await state.get_data()
    payload = data.get("payload") or {}
    text = (payload.get("text") or "").strip()
    if not text:
        await c.answer("Сначала введи текст сообщения.", show_alert=True)
        return
    await c.answer("Улучшаю текст…")
    new_text = await ai.personalize(text, style="sell")
    if not new_text:
        await c.answer("AI сейчас недоступен, попробуй ещё раз.", show_alert=True)
        return
    await state.update_data(ai_preview=new_text)
    await _show_ai_preview(c, new_text)


async def _show_ai_preview(c, new_text):
    """Показывает новый текст от AI с кнопками Применить/Перегенерировать/Отмена.
    Если в тексте невалидный HTML — показываем экранированный, чтобы не упасть."""
    head = "%s <b>AI улучшил текст.</b> Новый вариант:\n\n" % em("star")
    tail = "\n\n<i>Применить этот вариант или перегенерировать?</i>"
    try:
        await show(c, head + (new_text or "") + tail, kb.ai_preview_kb())
    except Exception:
        import html as _html
        await show(c, head + _html.escape(new_text or "") + tail, kb.ai_preview_kb())


@dp.callback_query(F.data == "bc:ai_apply")
async def cb_bc_ai_apply(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    new_text = data.get("ai_preview")
    if not new_text:
        await c.answer("Нет готового варианта — нажми AI-персонализацию снова.", show_alert=True)
        return
    payload = data.get("payload") or {}
    payload["text"] = new_text
    await state.update_data(payload=payload, ai_preview=None)
    await c.answer("Применено")
    await show_confirm(c, state)


@dp.callback_query(F.data == "bc:ai_regen")
async def cb_bc_ai_regen(c: types.CallbackQuery, state: FSMContext):
    if not (_is_paid(c.from_user.id) or is_admin(c.from_user.id)):
        await c.answer("🔒 AI-персонализация — только для PREMIUM. Оформи подписку!", show_alert=True)
        return
    if not ai.is_configured():
        await c.answer("AI не настроен: добавь AI_API_KEY в .env.", show_alert=True)
        return
    data = await state.get_data()
    payload = data.get("payload") or {}
    text = (payload.get("text") or "").strip()
    if not text:
        await c.answer("Сначала введи текст сообщения.", show_alert=True)
        return
    await c.answer("Генерирую заново…")
    new_text = await ai.personalize(text, style="sell")
    if not new_text:
        await c.answer("AI сейчас недоступен, попробуй ещё раз.", show_alert=True)
        return
    await state.update_data(ai_preview=new_text)
    await _show_ai_preview(c, new_text)


@dp.callback_query(F.data == "bc:ai_cancel")
async def cb_bc_ai_cancel(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(ai_preview=None)
    await c.answer("Отменено")
    await show_confirm(c, state)


def _max_variants(uid):
    return 3


@dp.callback_query(F.data == "bc:addvar")
async def cb_bc_addvar(c: types.CallbackQuery, state: FSMContext):
    uid = c.from_user.id
    cap = _max_variants(uid)
    if cap <= 0:
        await c.answer("Разные тексты — функция подписки.", show_alert=True)
        return
    data = await state.get_data()
    payload = data.get("payload") or {}
    cur = len([v for v in (payload.get("variants") or []) if v and str(v).strip()])
    if cur >= cap:
        await c.answer("Достигнут лимит вариантов: %d." % cap, show_alert=True)
        return
    await state.set_state(Broadcast.variant)
    await show(c, "%s Пришли ещё один <b>вариант текста</b> (добавлено %d из %d).\n\n"
                  "На каждую отправку бот будет случайно выбирать один из текстов — это снижает риск спам-блока." % (
                  em("msg"), cur, cap), kb.cancel_kb())


@dp.message(Broadcast.variant)
async def st_bc_variant(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    cap = _max_variants(uid)
    new_text = message.html_text or message.text or message.caption or ""
    if not new_text.strip():
        await message.answer("Пустой текст. Пришли вариант текстом.", reply_markup=kb.cancel_kb())
        return
    data = await state.get_data()
    payload = data.get("payload") or {}
    variants = [v for v in (payload.get("variants") or []) if v and str(v).strip()]
    if len(variants) >= cap:
        await state.set_state(None)
        await message.answer("%s Достигнут лимит вариантов." % em("warn"))
        await show_confirm(message, state)
        return
    variants.append(new_text)
    payload["variants"] = variants
    await state.update_data(payload=payload)
    await state.set_state(None)
    await message.answer("%s Вариант добавлен (всего текстов: %d)." % (em("ok"), len(variants) + 1))
    await show_confirm(message, state)


@dp.callback_query(F.data == "bc:pro")
async def cb_bc_pro(c: types.CallbackQuery):
    text = ("%s <b>PRO-функции</b>\n\n"
            "%s <b>Невидимые теги</b> — каждое сообщение незаметно упоминает участников чата — выглядит как обычный пост, но охват выше.\n"
            "%s <b>AI-персонализация</b> — бот улучшит текст: сделает его продающим, официальным или дружелюбным (3 стиля).\n"
            "%s Безлимит рассылок, до %d аккаунтов, приоритет в очереди и безлимит шаблонов.\n\n"
            "Оформи подписку, чтобы открыть всё:") % (
        em("star"), em("check"), em("check"), em("rocket"), config.PAID_MAX_ACCOUNTS)
    await show(c, text, kb.payment_kb())


@dp.message(Broadcast.message)
async def st_bc_message(message: types.Message, state: FSMContext):
    data0 = await state.get_data()
    buttons = data0.get("buttons", [])
    payload = {"type": "text", "text": message.html_text or "", "buttons": buttons}
    if message.photo:
        path = os.path.join(MEDIA_DIR, "ph_%d.jpg" % message.from_user.id)
        await bot.download(message.photo[-1], destination=path)
        payload = {"type": "photo", "path": path, "text": message.html_text or message.caption or "", "buttons": buttons}
    elif message.video:
        path = os.path.join(MEDIA_DIR, "vd_%d.mp4" % message.from_user.id)
        await bot.download(message.video, destination=path)
        payload = {"type": "video", "path": path, "text": message.html_text or message.caption or "", "buttons": buttons}
    await state.update_data(payload=payload)
    data = await state.get_data()
    # Режим «текст + кнопки»: после текста собираем кнопки.
    if data.get("bc_mode") == "buttons" and not data.get("editing"):
        await state.set_state(None)
        await message.answer("%s Текст сохранён. Теперь добавь <b>кнопки</b> со ссылками (или пропусти)." % em("key"),
                             reply_markup=kb.bc_buttons_kb(buttons))
        return
    if data.get("editing"):
        await state.update_data(editing=False)
        await show_confirm(message, state)
        return
    await state.set_state(Broadcast.delay)
    await message.answer("%s Задержка между сообщениями в секундах (рекомендую ≈30)?" % em("timer"),
                         reply_markup=kb.cancel_kb())


@dp.message(Broadcast.delay)
async def st_bc_delay(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip()
    if not txt.isdigit():
        await message.answer("Введи число секунд, например 30.", reply_markup=kb.cancel_kb())
        return
    delay = max(int(txt), config.MIN_COOLDOWN)
    await state.update_data(delay=delay)
    data = await state.get_data()
    if data.get("editing"):
        await state.update_data(editing=False)
        await show_confirm(message, state)
        return
    await state.set_state(Broadcast.cycles)
    await message.answer("%s <b>Сколько раз отправить сообщение?</b>\n\nВведи количество повторов (циклов) — это сколько раз бот пройдёт по всем выбранным чатам и отправит твоё сообщение.\n\nНапример: <b>1</b> — один проход, <b>10</b> — десять проходов. Можно от 1 до 999." % em("cycle"),
                         reply_markup=kb.cancel_kb())


@dp.message(Broadcast.cycles)
async def st_bc_cycles(message: types.Message, state: FSMContext):
    txt = (message.text or "").strip()
    # Лимит снят: разрешаем от 1 до 100000 циклов (фактически без ограничения).
    if not txt.isdigit() or not (1 <= int(txt) <= 100000):
        await message.answer("%s Нужно целое число <b>от 1 до 100000</b>. Попробуй ещё раз." % em("warn"), reply_markup=kb.cancel_kb())
        return
    await state.update_data(cycles=int(txt), editing=False)
    await show_confirm(message, state)


@dp.callback_query(F.data == "bc:confirm")
async def cb_bc_confirm(c: types.CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await show_confirm(c, state)


@dp.callback_query(F.data == "bc:edit")
async def cb_bc_edit(c: types.CallbackQuery):
    await show(c, "%s Что редактируем?" % em("gear"), kb.edit_kb())


@dp.callback_query(F.data == "bc:edit:msg")
async def cb_bc_edit_msg(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(editing=True)
    await state.set_state(Broadcast.message)
    await show(c, "%s Отправь новое сообщение (текст/фото/видео):" % em("msg"), kb.cancel_kb())


@dp.callback_query(F.data == "bc:edit:delay")
async def cb_bc_edit_delay(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(editing=True)
    await state.set_state(Broadcast.delay)
    await show(c, "%s Введи новую задержку в секундах:" % em("timer"), kb.cancel_kb())


@dp.callback_query(F.data == "bc:edit:cycles")
async def cb_bc_edit_cycles(c: types.CallbackQuery, state: FSMContext):
    await state.update_data(editing=True)
    await state.set_state(Broadcast.cycles)
    await show(c, "%s Введи новое количество циклов — <b>от 1 до 100000</b>:" % em("cycle"), kb.cancel_kb())


@dp.callback_query(F.data == "bc:launch")
async def cb_bc_launch(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    payload = data.get("payload")
    if not payload:
        await c.answer("Сначала настрой рассылку", show_alert=True)
        return
    uid = c.from_user.id
    cycles = data.get("cycles", 1)
    n_sessions = max(1, len(db.get_sessions(uid)))
    # Дневной лимит циклов для бесплатного тарифа (суммарно по аккаунтам).
    if not _is_paid(uid):
        need = n_sessions * cycles
        left = db.daily_cycles_left(uid, config.FREE_DAILY_CYCLES)
        if need > left:
            await c.answer()
            await show(c, "%s <b>Дневной лимит исчерпан</b>\n\n"
                         "На бесплатном тарифе — <b>%d циклов в день</b> (осталось: %d).\n"
                         "Этой рассылке нужно %d.\n\n"
                         "%s Подожди до <b>00:00</b> — лимит обновится, или оформи подписку — безлимит рассылок." % (
                         em("warn"), config.FREE_DAILY_CYCLES, left, need, em("star")), kb.payment_kb())
            return
    _folders = data.get("folders") or []
    _chosen = data.get("chosen") or []
    _folder_names = [str(f.get("title")) for f in _folders if f.get("id") in _chosen]
    job = worker.start_broadcast_task(
        payload=payload,
        cooldown=data.get("delay", 30),
        target_type=data.get("target_type", "all"),
        user_id=uid,
        cycles=cycles,
        chat_list=None,
        folder_ids=data.get("chosen") or None,
        folder_names=_folder_names or None,
        autosub=data.get("autosub", True),
        autofolder=data.get("autofolder"),
        invisible_tags=bool(data.get("invisible_tags", False)) and _is_paid(uid),
    )
    await state.clear()
    await c.answer("Рассылка запущена!")
    # Отдельное уведомление о запуске
    try:
        await bot.send_message(c.from_user.id,
                               "%s <b>%s</b> запущена!\nГотовлю аккаунты и считаю чаты…" % (em("rocket"), job.name))
    except Exception:
        pass
    text = "%s <b>Мониторинг</b>\n\n%s" % (em("monitor"), worker.render_job(job))
    await show(c, text, kb.monitor_job_kb(job))
    chat_id = c.message.chat.id
    if job and job.status in ("running", "paused"):
        _AUTO_MON[chat_id] = asyncio.create_task(
            _auto_monitor(chat_id, c.message.message_id, job.id))


# ===================== MINI APP DATA =====================
@dp.message(F.web_app_data)
async def on_webapp_data(message: types.Message, state: FSMContext):
    uid = message.from_user.id
    raw = message.web_app_data.data if message.web_app_data else ""
    action = raw
    try:
        parsed = json.loads(raw)
        action = parsed.get("action", raw)
    except Exception:
        pass
    if action in ("broadcast", "bc:start"):
        await _start_broadcast_flow(message, uid, state)
    elif action in ("monitor", "nav:monitor"):
        await message.answer("%s <b>Мониторинг</b>\n\n%s" % (em("monitor"), worker.render_all(uid)),
                             reply_markup=kb.monitor_kb(worker.get_user_jobs(uid)))
    elif action in ("accounts", "acc:add"):
        await message.answer(accounts_text(uid), reply_markup=kb.accounts_kb(db.get_sessions(uid)))
    elif action in ("sub", "subscribe", "nav:sub"):
        await message.answer(sub_text(uid), reply_markup=kb.payment_kb())
    elif action in ("templates", "nav:templates"):
        await message.answer("%s <b>Шаблоны рассылки</b>" % em("msg"),
                             reply_markup=kb.templates_kb(db.get_templates(uid)))
    elif action in ("ref", "nav:ref"):
        try:
            me = await bot.me()
            link = "https://t.me/%s?start=%d" % (me.username, uid)
        except Exception:
            link = "ссылка недоступна"
        await message.answer("%s <b>Рефералы</b>\nТвоя ссылка:\n<code>%s</code>\nПриглашено: <b>%d</b>" % (
            "🤝", link, db.count_referrals(uid)), reply_markup=kb.back_only_kb("nav:menu"))
    elif action in ("help", "nav:help"):
        await message.answer(help_text(), reply_markup=kb.back_only_kb("nav:menu"))
    else:
        await gate_or_menu(message, uid)


# ===================== СПРАВОЧНИК / HELP =====================
def help_text():
    return (
        "<b>%s — справочник</b>\n\n"
        "Это бот для массовых рассылок по твоим группам и каналам с твоих Telegram-аккаун��ов. "
        "Ниже — все возможности простыми словами.\n\n"
        "<b>Аккаунты</b>\n"
        "• Несколько Telegram-аккаунтов (на подписке — без лимита)\n"
        "• Свой прокси для каждого аккаунта (SOCKS5/HTTP) — меньше риск блокировок\n"
        "• Проверка аккаунта через @SpamBot прямо из бота\n\n"
        "<b>Рассылка</b>\n"
        "• По всем чатам или строго по выбранным папкам (можно несколько)\n"
        "• Режимы: только текст; медиа + текст (фото/видео с подписью); пересыл поста по ссылке; текст с кнопками-ссылками\n"
        "• До 3 разных текстов сразу — н�� каждую отправку берётся случайный (защита от спам-фильтра)\n"
        "• AI-улучшение текста под продажу\n"
        "• Число повторов (циклов) и задержка между отправками\n"
        "• Форматирование 1-в-1: жирный, курсив, подчёркнутый, зачёркнутый, спойлер, цитаты, код, ссылки и премиум-эмодзи\n\n"
        "<b>Умные функции</b>\n"
        "• Авто-вступление: если чат требует подписку на каналы — бот сам вступит и отправит сообщение повторно\n"
        "• Авто-папка: все каналы, куда бот вступил при рассылке, складываются в отдельную папку\n"
        "• Живой мониторинг: прогресс в %%, отправлено, текущий и следующий чат, пауза/продолжение/стоп\n"
        "• Шаблоны: сохрани готовую рассылку и запускай в один тап\n"
        "• Логи всех действий\n\n"
        "<b>Аккаунт и рост</b>\n"
        "• Профиль: тариф, срок подписки, аккаунты, рефералы, шаблоны\n"
        "• Рефералы: за каждого друга по твоей ссылке +%d ч подписки\n"
        "• Подписка: Telegram Stars, карта, TON, крипта\n"
        "• Mini App: управление рассылками с у����обного экрана\n\n"
        "<b>Как начать</b>\n"
        "1. Добавь аккаунт: номер телефона → код из Telegram → пароль 2FA (если включён)\n"
        "2. Нажми «Рассылка» и выбери папки или «Все чаты»\n"
        "3. Напиши сообщение, задай задержку и число повторов\n"
        "4. Запусти и следи за ходом во вкладке «Мониторинг»\n\n"
        "По оплате и вопросам пиши: %s"
    ) % (config.BOT_NAME, config.REF_HOURS, config.OWNER_CONTACT)


@dp.message(Command("help"))
async def cmd_help(message: types.Message, state: FSMContext):
    await message.answer(help_text(), reply_markup=kb.back_only_kb("nav:menu"))


@dp.callback_query(F.data == "nav:help")
async def cb_help(c: types.CallbackQuery):
    await show(c, help_text(), kb.back_only_kb("nav:menu"))


# ===================== ПОДДЕРЖКА =====================
def support_text():
    e = em
    return (
        "%s <b>Центр поддержки</b>\n\n"
        "%s <b>Частые во��росы:</b>\n"
        "• <b>Аккаунт не добавляется?</b> Проверь номер и код, при 2FA введи пароль.\n"
        "• <b>Рассылка остановилась?</b> Возможен FloodWait — бот сам продолжит после паузы.\n"
        "• <b>Дневной лимит?</b> На бесплатном — %d циклов/день. Обновляется в 00:00.\n"
        "• <b>Как убрать лимиты?</b> Оформи подписку — безлимит рассылок и до %d аккаунтов.\n\n"
        "%s Не нашёл ответ? Напиши нам — поможем."
    ) % (e("chat"), e("check"), config.FREE_DAILY_CYCLES, config.PAID_MAX_ACCOUNTS, e("ok"))


@dp.callback_query(F.data == "nav:support")
async def cb_support(c: types.CallbackQuery):
    await show(c, support_text(), kb.support_kb())


# ===================== РЕФЕРАЛЫ =====================
@dp.callback_query(F.data == "nav:ref")
async def cb_ref(c: types.CallbackQuery):
    uid = c.from_user.id
    try:
        me = await bot.me()
        link = "https://t.me/%s?start=%d" % (me.username, uid)
    except Exception:
        link = "ссылка недоступна"
    cnt = db.count_referrals(uid)
    # Лидерборд: топ-5 пригласивших + место пользователя.
    lb_lines = []
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for i, r in enumerate(db.referral_leaderboard(5), 1):
        nm = r.get("first_name") or (("@" + r["username"]) if r.get("username") else ("ID %s" % r.get("uid")))
        lb_lines.append("%s %s — <b>%d</b>" % (medals.get(i, "%d." % i), nm, r.get("n", 0)))
    lb = ("\n".join(lb_lines)) if lb_lines else "Пока пусто — стань первым!"
    rank, rn = db.referral_rank(uid)
    rank_line = ("\n\nТвоё место в рейтинге: <b>#%d</b>" % rank) if rank else ""
    text = ("%s <b>Реферальная программа</b>\n\n"
            "Приглашай друзей по своей ссылке — за каждого получишь <b>+%d часов</b> подписки!\n\n"
            "Приглашено: <b>%d</b>%s\n\n"
            "%s <b>Топ пригласивших:</b>\n%s\n\n"
            "Твоя ссылка:\n<code>%s</code>") % ("🤝", config.REF_HOURS, cnt, rank_line, em("chart"), lb, link)
    await show(c, text, kb.back_only_kb("nav:menu"))


# ===================== ШАБЛОНЫ =====================
@dp.callback_query(F.data == "nav:templates")
async def cb_templates(c: types.CallbackQuery, state: FSMContext):
    await state.clear()
    tpls = db.get_templates(c.from_user.id)
    if tpls:
        body = "Выбери шаблон, чтобы сразу перейти к запуску, или удали ненужный."
    else:
        body = "Пока нет сохранённых шаблонов. Создай рассылку и на экране подтверждения нажми «Сохранить в избранное»."
    await show(c, "%s <b>Шаблоны рассылки</b>\n\n%s" % (em("msg"), body), kb.templates_kb(tpls))


@dp.callback_query(F.data.startswith("tpl:del:"))
async def cb_tpl_del(c: types.CallbackQuery):
    tid = int(c.data.split(":")[-1])
    db.delete_template(tid, c.from_user.id)
    await c.answer("Шаблон удалён")
    tpls = db.get_templates(c.from_user.id)
    await show(c, "%s <b>Шаблоны рассылки</b>" % em("msg"), kb.templates_kb(tpls))


@dp.callback_query(F.data.startswith("tpl:use:"))
async def cb_tpl_use(c: types.CallbackQuery, state: FSMContext):
    tid = int(c.data.split(":")[-1])
    if not has_access(c.from_user.id):
        await show(c, "%s Для рассылки нужна активная подписка." % em("warn"), kb.payment_kb())
        return
    t = db.get_template(tid, c.from_user.id)
    if not t:
        await c.answer("Шаблон не найден", show_alert=True)
        return
    try:
        snap = json.loads(t["payload"])
    except Exception:
        snap = {}
    await state.clear()
    await state.update_data(**snap)
    await c.answer("Шаблон загружен")
    await show_confirm(c, state)


@dp.callback_query(F.data == "bc:save_tpl")
async def cb_save_tpl(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if not data.get("payload"):
        await c.answer("Сначала настрой рассылку", show_alert=True)
        return
    # Лимит шаблонов: бесплатно — 5, подписка — безлимит.
    if not _is_paid(c.from_user.id) and db.count_templates(c.from_user.id) >= config.FREE_MAX_TEMPLATES:
        await show(c, "%s На бесплатном тарифе можно хранить до <b>%d</b> шаблонов.\n\n%s На подписке — безлимит шаблонов." % (em("warn"), config.FREE_MAX_TEMPLATES, em("star")), kb.payment_kb())
        return
    await state.set_state(SaveTpl.name)
    await show(c, "%s Введи название шаблона (например «Оффер на канал»):" % em("star"), kb.back_only_kb("bc:confirm"))


@dp.message(SaveTpl.name)
async def st_save_tpl_name(message: types.Message, state: FSMContext):
    name = ((message.text or "").strip()[:60]) or "Шаблон"
    data = await state.get_data()
    snap = {
        "payload": data.get("payload"),
        "target_type": data.get("target_type", "all"),
        "chosen": data.get("chosen") or [],
        "folders": data.get("folders") or [],
        "delay": data.get("delay", 30),
        "cycles": data.get("cycles", 1),
        "autosub": data.get("autosub", True),
        "autofolder": data.get("autofolder"),
    }
    db.add_template(message.from_user.id, name, json.dumps(snap, ensure_ascii=False))
    await state.set_state(None)
    await message.answer("%s Шаблон «%s» сохранён в избранное!" % (em("ok"), name),
                         reply_markup=kb.back_only_kb("nav:menu"))


# ===================== АВТОПОДПИСКА / АВТО-ПАПКА =====================
@dp.callback_query(F.data == "bc:toggle_autosub")
async def cb_toggle_autosub(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    cur = data.get("autosub", True)
    await state.update_data(autosub=(not cur))
    await c.answer("Автоподписка " + ("включена" if not cur else "выключена"))
    await show_confirm(c, state)


@dp.callback_query(F.data == "bc:autofolder")
async def cb_autofolder(c: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("autofolder"):
        await state.update_data(autofolder=None)
        await c.answer("Авто-папка выключена")
        await show_confirm(c, state)
        return
    await state.set_state(AutoFolder.name)
    await show(c, "%s Введи название папки, куда складывать ВСЕ каналы, на которые "
                  "подпишемся во время рассылки. Папка создастся автоматически." % em("folder"), kb.back_only_kb("bc:confirm"))


@dp.message(AutoFolder.name)
async def st_autofolder_name(message: types.Message, state: FSMContext):
    name = (message.text or "").strip()[:40]
    await state.set_state(None)
    if name:
        await state.update_data(autofolder=name)
    await show_confirm(message, state)


# ===================== АДМИН: ВЫДАЧА / ЗАБРАТЬ / РАССЫЛКА =====================
def _resolve_admin_target(s):
    s = (s or "").strip()
    if s.startswith("@") or not s.lstrip("-").isdigit():
        row = db.get_user_by_username(s)
        return (row["telegram_id"] if row else None)
    return int(s)


@dp.callback_query(F.data == "admin:give")
async def cb_admin_give(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("Только для админа", show_alert=True)
        return
    await state.set_state(AdminOps.give_target)
    await show(c, "%s Кому выдать подписку? Введи <b>ID</b> или <b>@username</b>:" % em("gift"), kb.cancel_kb())


@dp.message(AdminOps.give_target)
async def st_give_target(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    await state.update_data(give_target=(message.text or "").strip())
    await state.set_state(AdminOps.give_days)
    await message.answer("На сколько дней? Введи число (например 30):", reply_markup=kb.cancel_kb())


@dp.message(AdminOps.give_days)
async def st_give_days(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    txt = (message.text or "").strip()
    if not txt.isdigit():
        await message.answer("Нужно число дней.", reply_markup=kb.cancel_kb())
        return
    days = int(txt)
    data = await state.get_data()
    target = _resolve_admin_target(data.get("give_target"))
    await state.clear()
    if not target:
        await message.answer("%s Пользователь не найден. Он должен был хотя бы раз запустить бота." % em("warn"),
                             reply_markup=kb.back_only_kb("nav:admin"))
        return
    exp = db.set_subscription(target, days, source="manual")
    db.add_log(target, "Админ выдал подписку на %d дн." % days)
    db.add_admin_action(message.from_user.id, "give", target, "%d дн." % days)
    try:
        await bot.send_message(target, "%s Тебе выдали подписку на %d дн.! Активна до <b>%s</b>." % (
            em("gift"), days, exp.strftime("%d.%m.%Y")))
    except Exception:
        pass
    await message.answer("%s Готово. Подписка для <code>%s</code> до <b>%s</b>." % (
        em("ok"), target, exp.strftime("%d.%m.%Y")), reply_markup=kb.back_only_kb("nav:admin"))


@dp.callback_query(F.data == "admin:take")
async def cb_admin_take(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("Только для админа", show_alert=True)
        return
    await state.set_state(AdminOps.take_target)
    await show(c, "%s У кого забрать подписку? Введи <b>ID</b> или <b>@username</b>:" % em("ban"), kb.cancel_kb())


@dp.message(AdminOps.take_target)
async def st_take_target(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    target = _resolve_admin_target((message.text or "").strip())
    await state.clear()
    if not target:
        await message.answer("%s Пользователь не найден." % em("warn"), reply_markup=kb.back_only_kb("nav:admin"))
        return
    db.remove_subscription(target)
    db.add_log(target, "Админ забрал подписку")
    db.add_admin_action(message.from_user.id, "take", target, "снята подписка")
    try:
        await bot.send_message(target, "%s Твоя подписка отменена администратором." % em("warn"))
    except Exception:
        pass
    await message.answer("%s Подписка у <code>%s</code> снята." % (em("ok"), target),
                         reply_markup=kb.back_only_kb("nav:admin"))


@dp.callback_query(F.data == "admin:announce")
async def cb_admin_announce(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("Только для админа", show_alert=True)
        return
    await state.set_state(AdminOps.announce)
    await show(c, "%s Введи сообщение — отправлю его ВСЕМ, кто запускал бота.\n"
                  "<i>Поддерживается форматирование Telegram.</i>" % em("msg"), kb.cancel_kb())


@dp.message(AdminOps.announce)
async def st_announce(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    await state.clear()
    text = message.html_text or message.text or ""
    users = db.get_all_users()
    sent = 0
    failed = 0
    await message.answer("%s Рассылаю сообщение %d пользователям…" % (em("rocket"), len(users)))
    for u in users:
        try:
            await bot.send_message(u["telegram_id"], text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    db.add_admin_action(message.from_user.id, "announce", None, "доставлено %d" % sent)
    await message.answer("%s Готово. Доставлено: %d, не дошло: %d." % (em("ok"), sent, failed),
                         reply_markup=kb.back_only_kb("nav:admin"))


# ===================== АДМИН: МАССОВАЯ ВЫДАЧА =====================
@dp.callback_query(F.data == "admin:massgive")
async def cb_admin_massgive(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("Только для админа", show_alert=True)
        return
    await state.set_state(AdminOps.massgive_ids)
    await show(c, "%s <b>Массовая выдача подписки</b>\n\nПришли список ID через пробел, запятую или с новой строки. Можно и @username." % em("gift"), kb.cancel_kb())


@dp.message(AdminOps.massgive_ids)
async def st_massgive_ids(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    await state.update_data(massgive_ids=(message.text or ""))
    await state.set_state(AdminOps.massgive_days)
    await message.answer("На сколько дней выдать всем? Введи число (например 30):", reply_markup=kb.cancel_kb())


@dp.message(AdminOps.massgive_days)
async def st_massgive_days(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    txt = (message.text or "").strip()
    if not txt.isdigit():
        await message.answer("Нужно число дней.", reply_markup=kb.cancel_kb())
        return
    days = int(txt)
    data = await state.get_data()
    await state.clear()
    raw = data.get("massgive_ids", "")
    tokens = re.split(r"[\s,;]+", raw.strip())
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
            exp = db.set_subscription(target, days, source="manual")
            db.add_log(target, "Массовая выдача подписки на %d дн." % days)
            db.add_admin_action(message.from_user.id, "massgive", target, "%d дн." % days)
            ok += 1
            try:
                await bot.send_message(target, "%s Тебе выдали подписку на %d дн.! Активна до <b>%s</b>." % (
                    em("gift"), days, exp.strftime("%d.%m.%Y")))
            except Exception:
                pass
        except Exception:
            fail += 1
    await message.answer("%s Готово. Выдано: <b>%d</b>, не удалось: <b>%d</b>." % (em("ok"), ok, fail),
                         reply_markup=kb.back_only_kb("nav:admin"))


# ===================== АДМИН: БАН / РАЗБАН =====================
@dp.callback_query(F.data == "admin:ban")
async def cb_admin_ban(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("Только для админа", show_alert=True)
        return
    await state.set_state(AdminOps.ban_target)
    await show(c, "%s Кого забан��ть? Введи <b>ID</b> или <b>@username</b>:" % em("ban"), kb.cancel_kb())


@dp.message(AdminOps.ban_target)
async def st_ban_target(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    target = _resolve_admin_target((message.text or "").strip())
    await state.clear()
    if not target:
        await message.answer("%s Пользователь не найден." % em("warn"), reply_markup=kb.back_only_kb("nav:admin"))
        return
    db.ban_user(target)
    db.add_log(target, "Забанен администратором")
    db.add_admin_action(message.from_user.id, "ban", target, "")
    try:
        await bot.send_message(target, "%s Доступ к боту заблокирован администратором." % em("ban"))
    except Exception:
        pass
    accs = db.get_sessions(target)
    logs = db.get_logs(target, 8)
    info = "\n".join("• %s — %s" % (l["ts"], l["text"]) for l in logs) or "—"
    await message.answer(
        "%s Пользователь <code>%s</code> забанен.\n\n%s Аккаунтов: <b>%d</b>\n%s Последние логи:\n%s" % (
            em("ok"), target, em("accounts"), len(accs), em("monitor"), info),
        reply_markup=kb.back_only_kb("nav:admin"))


@dp.callback_query(F.data == "admin:unban")
async def cb_admin_unban(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("Только для админа", show_alert=True)
        return
    await state.set_state(AdminOps.unban_target)
    await show(c, "%s Кого разбанить? Введи <b>ID</b> или <b>@username</b>:" % em("ok"), kb.cancel_kb())


@dp.message(AdminOps.unban_target)
async def st_unban_target(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    target = _resolve_admin_target((message.text or "").strip())
    await state.clear()
    if not target:
        await message.answer("%s Пользователь не найден." % em("warn"), reply_markup=kb.back_only_kb("nav:admin"))
        return
    db.unban_user(target)
    db.add_log(target, "Разбанен администратором")
    db.add_admin_action(message.from_user.id, "unban", target, "")
    try:
        await bot.send_message(target, "%s Доступ к боту восстановлен." % em("ok"))
    except Exception:
        pass
    await message.answer("%s Пользователь <code>%s</code> разбанен." % (em("ok"), target),
                         reply_markup=kb.back_only_kb("nav:admin"))


# ===================== АДМИН: ПРОСМОТР ЮЗЕРА =====================
@dp.callback_query(F.data == "admin:userinfo")
async def cb_admin_userinfo(c: types.CallbackQuery, state: FSMContext):
    if not is_admin(c.from_user.id):
        await c.answer("Только для админа", show_alert=True)
        return
    await state.set_state(AdminOps.info_target)
    await show(c, "%s Чей профиль показать? Введи <b>ID</b> или <b>@username</b>:" % em("users"), kb.cancel_kb())


@dp.message(AdminOps.info_target)
async def st_info_target(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        await state.clear()
        return
    target = _resolve_admin_target((message.text or "").strip())
    await state.clear()
    if not target:
        await message.answer("%s Пользователь не найден." % em("warn"), reply_markup=kb.back_only_kb("nav:admin"))
        return
    accs = db.get_sessions(target)
    logs = db.get_logs(target, 10)
    sub = "✅ активна" if db.is_subscribed(target) else "— нет"
    banned = "🚫 ДА" if db.is_banned(target) else "нет"
    acc_lines = "\n".join("• %s (%s)" % (a.get("phone", "?"), a.get("status", "?")) for a in accs) or "—"
    log_lines = "\n".join("• %s — %s" % (l["ts"], l["text"]) for l in logs) or "—"
    await message.answer(
        "%s <b>Профиль</b> <code>%s</code>\n\nПодписка: %s\nБан: %s\n\n%s <b>Аккаунты (%d):</b>\n%s\n\n%s <b>Логи:</b>\n%s" % (
            em("users"), target, sub, banned, em("accounts"), len(accs), acc_lines, em("monitor"), log_lines),
        reply_markup=kb.back_only_kb("nav:admin"))


# ===================== АДМИН: ЛОГИ ДЕЙСТВИЙ =====================
@dp.callback_query(F.data == "admin:actions")
async def cb_admin_actions(c: types.CallbackQuery):
    if not is_admin(c.from_user.id):
        await c.answer("Только для админа", show_alert=True)
        return
    acts = db.get_admin_actions(30)
    if not acts:
        body = "Пока нет записей."
    else:
        names = {"give": "выдал подписку", "take": "снял подписку", "massgive": "массовая выдача",
                 "ban": "забанил", "unban": "разбанил", "announce": "рассылка всем"}
        lines = []
        for a in acts:
            when = (a.get("ts") or "")[:16].replace("T", " ")
            act = names.get(a.get("action"), a.get("action"))
            detail = (" — " + a["detail"]) if a.get("detail") else ""
            lines.append("• %s: <b>%s</b> → <code>%s</code>%s" % (when, act, a.get("target_id"), detail))
        body = "\n".join(lines)
    await show(c, "%s <b>Логи действий админов</b>\n\n%s" % (em("monitor"), body), kb.back_only_kb("nav:admin"))


# ===================== ФОНОВЫЙ ТАСК: ИСТЕЧЕНИЕ ПОДПИСКИ =====================
# ===================== НИЖНЯЯ ПАНЕЛЬ (reply-навигация) =====================
# Регистрируются после FSM-хендлеров: пока идёт ввод (например, текст
# рассылки) — приоритет у FSM, иначе кнопки работают как навигация.
@dp.message(F.text == kb.NAV_BROADCAST)
async def nav_txt_broadcast(message: types.Message, state: FSMContext):
    await state.clear()
    await _start_broadcast_flow(message, message.from_user.id, state)


@dp.message(F.text == kb.NAV_ACCOUNTS)
async def nav_txt_accounts(message: types.Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id
    await message.answer(accounts_text(uid), reply_markup=kb.accounts_kb(db.get_sessions(uid)))


@dp.message(F.text == kb.NAV_MENU)
async def nav_txt_menu(message: types.Message, state: FSMContext):
    await state.clear()
    await gate_or_menu(message, message.from_user.id)


@dp.message(F.text == kb.NAV_SUB)
async def nav_txt_sub(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(sub_text(message.from_user.id), reply_markup=kb.payment_kb())


@dp.message(F.text == kb.NAV_HELP)
async def nav_txt_help(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(help_text(), reply_markup=kb.back_only_kb("nav:menu"))


@dp.message(F.text == kb.NAV_SHOP)
async def nav_txt_shop(message: types.Message, state: FSMContext):
    await message.answer("%s Магазин: %s" % (em("gift"), config.SHOP_URL))


async def subscription_watcher():
    while True:
        try:
            for uid in db.get_expired_to_notify():
                try:
                    await bot.send_message(
                        uid,
                        "%s Твоя подписка закончилась.\n\nПродли её, чтобы продолжить работать:" % em("warn"),
                        reply_markup=kb.sub_expired_kb(),
                    )
                except Exception:
                    pass
                db.mark_notified(uid)
        except Exception as e:
            log.warning("watcher: %s", e)
        # Воронка: напоминания о конце триала (за 3 / 2 / 1 день).
        try:
            now = datetime.now()
            for uid, exp_str in db.get_active_trials():
                try:
                    exp = datetime.fromisoformat(exp_str)
                except Exception:
                    continue
                hours_left = (exp - now).total_seconds() / 3600.0
                if hours_left <= 0:
                    continue
                if hours_left <= 24:
                    kind, human = "trial_1", "меньше суток"
                elif hours_left <= 48:
                    kind, human = "trial_2", "2 дня"
                elif hours_left <= 72:
                    kind, human = "trial_3", "3 дня"
                else:
                    continue
                if db.reminder_sent(uid, kind):
                    continue
                db.mark_reminder(uid, kind)
                try:
                    await bot.send_message(
                        uid,
                        "%s До конца пробного периода осталось <b>%s</b>.\n\n"
                        "Оформи подписку, чтобы не потерять безлимит рассылок, до %d аккаунтов и PRO-функции:" % (
                            em("wait"), human, config.PAID_MAX_ACCOUNTS),
                        reply_markup=kb.sub_expired_kb(),
                    )
                except Exception:
                    pass
        except Exception as e:
            log.warning("trial funnel: %s", e)
        await asyncio.sleep(60)


async def _resume_jobs():
    """Возобновляет рассылки, прерванные рестартом бота (Render и др.)."""
    await asyncio.sleep(4)
    try:
        jobs = db.get_resumable_jobs()
    except Exception as e:
        log.warning("resume read: %s", e)
        return
    # Порог свежести: возобновляем ТОЛЬКО рассылки, у которых heartbeat (updated)
    # обновлялся недавно. Давно завершённые/стухшие записи (бот лежал,
    # рассылка давно кончилась) закрываем МОЛЧА — без сообщения пользователю.
    RESUME_MAX_AGE = 15 * 60  # 15 минут
    _now = datetime.now()
    for j in jobs:
        # Старую запись закрываем, чтобы не возобновлять повторно.
        try:
            db.update_job_status(j["id"], "superseded")
        except Exception:
            pass
        cfg = j.get("config") or {}
        if not cfg.get("payload"):
            continue
        # Реально ли рассылка была активна прямо перед рестартом?
        try:
            _upd = j.get("updated") or j.get("created")
            _age = (_now - datetime.fromisoformat(_upd)).total_seconds() if _upd else 1e9
        except Exception:
            _age = 1e9
        if _age > RESUME_MAX_AGE:
            # Стухшая запись — ничего не запускаем и НИЧЕГО не пишем.
            continue
        try:
            worker.start_broadcast_task(
                payload=cfg.get("payload"),
                cooldown=cfg.get("cooldown", 30),
                target_type=cfg.get("target_type", "all"),
                user_id=j["user_id"],
                cycles=cfg.get("cycles", 1),
                chat_list=cfg.get("chat_list"),
                folder_ids=cfg.get("folder_ids"),
                folder_names=cfg.get("folder_names"),
                autosub=cfg.get("autosub", True),
                autofolder=cfg.get("autofolder"),
                invisible_tags=cfg.get("invisible_tags", False),
            )
            try:
                await bot.send_message(j["user_id"],
                    "%s Рассылка возобновлена после перезапуска бота." % em("refresh"))
            except Exception:
                pass
        except Exception as e:
            log.warning("resume job %s: %s", j.get("id"), e)


# ===================== ЗАПУСК =====================
async def main():
    db.init_db()
    # Справоч��ик Telegraph: один раз создаём страницу и кэшируем ссылку
    try:
        import make_telegraph
        _hu = make_telegraph.ensure_help_page(config.help_cache_path())
        if _hu:
            config.HELP_URL = _hu
            log.info("Telegraph справочник: %s", _hu)
        else:
            log.info("Telegraph: ссылка не создана (нет пакета/сети) — справочник откроется в чате")
    except Exception as e:
        log.warning("telegraph: %s", e)
    runner = None
    try:
        webapp = build_app(bot)
        runner = web.AppRunner(webapp)
        await runner.setup()
        site = web.TCPSite(runner, config.WEBAPP_HOST, config.WEBAPP_PORT)
        await site.start()
        log.info("Mini App server on %s:%s", config.WEBAPP_HOST, config.WEBAPP_PORT)
    except Exception as e:
        log.warning("web server not started: %s", e)
    asyncio.create_task(subscription_watcher())
    asyncio.create_task(_resume_jobs())
    log.info("Bot started: %s", config.BOT_NAME)
    try:
        try:
            await bot.delete_webhook(drop_pending_updates=True)
        except Exception:
            pass
        await dp.start_polling(bot)
    finally:
        if runner:
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
