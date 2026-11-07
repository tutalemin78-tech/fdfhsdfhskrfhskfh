# -*- coding: utf-8 -*-
import asyncio
import logging
import random
import re
import time

from pyrogram import Client
from pyrogram.enums import ChatType, ParseMode, ChatAction
from pyrogram.errors import (
    FloodWait, PeerFlood, UserBannedInChannel, ChatWriteForbidden,
)
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

import config
import database as db

log = logging.getLogger("sendflow.worker")

JOBS = {}            # job_id -> Job
_SEQ = {"n": 0}
_FOLDERS_CACHE = {}   # session_id -> (ts, folders) — кэш списка папок
_FOLDERS_TTL = 120    # сек: сколько держим кэш папок


from emoji import em


def _next_id():
    _SEQ["n"] += 1
    return _SEQ["n"]


class Job:
    def __init__(self, user_id, cycles, target_type, cooldown, name, payload, chat_list, folder_ids, autosub=True, autofolder=None, invisible_tags=False):
        self.id = _next_id()
        self.invisible_tags = invisible_tags   # платная функция: невидимые упоминания
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
        self.warmup = True       # «Живой режим»: прогрев чата перед отправкой (всегда вкл)
        self.human_mode = True   # «Human Mode»: динамическая задержка + имитация печати
        self.status = "running"  # running | paused | stopped | done
        self.pause_event = asyncio.Event()
        self.pause_event.set()
        self.task = None
        self.accounts = {}       # phone -> stats
        self.created = time.time()
        self.error = None
        self.titles = {}        # кэш id -> название чата (для детальных логов)


def get_job(job_id):
    return JOBS.get(int(job_id)) if job_id is not None else None


def get_user_jobs(user_id, active_only=False):
    js = [j for j in JOBS.values() if j.user_id == user_id]
    if active_only:
        js = [j for j in js if j.status in ("running", "paused")]
    return sorted(js, key=lambda j: j.id)


def pause_job(job_id):
    j = get_job(job_id)
    if j and j.status == "running":
        j.status = "paused"
        j.pause_event.clear()
        return True
    return False


def resume_job(job_id):
    j = get_job(job_id)
    if j and j.status == "paused":
        j.status = "running"
        j.pause_event.set()
        return True
    return False


def stop_job(job_id):
    j = get_job(job_id)
    if j and j.status in ("running", "paused"):
        j.status = "stopped"
        j.pause_event.set()
        if j.task and not j.task.done():
            j.task.cancel()
        return True
    return False


def cleanup_finished(user_id, keep=4):
    """Убираем старые завершённые рассылки, оставляя последние."""
    done = [j for j in get_user_jobs(user_id) if j.status in ("done", "stopped")]
    for j in done[:-keep] if len(done) > keep else []:
        JOBS.pop(j.id, None)


def eta_seconds(job):
    worst = 0
    for st in job.accounts.values():
        done = st["sent"] + st["failed"] + st["paid"]
        rem = max(st["total"] - done, 0)
        worst = max(worst, rem * job.cooldown)
    return int(worst)


def fmt_eta(sec):
    sec = int(sec)
    m, s = divmod(sec, 60)
    h, m = divmod(m, 60)
    if h:
        return "%d:%02d:%02d" % (h, m, s)
    return "%d:%02d" % (m, s)


def _bar(done, total, width=12):
    if total <= 0:
        total = 1
    done = min(done, total)
    filled = int(width * done / total)
    pct = int(100 * done / total)
    return "█" * filled + "▁" * (width - filled), pct


HEAD = {"running": em("play") + " Идёт", "paused": em("pause") + " Пауза",
        "stopped": em("stop") + " Остановлена", "done": em("ok") + " Завершена"}


def render_job(job, short=False):
    if not job:
        return "Рассылка не найдена."
    head = HEAD.get(job.status, job.status)
    total = sum(st["total"] for st in job.accounts.values())
    done = sum(st["sent"] + st["failed"] + st["paid"] for st in job.accounts.values())
    bar, pct = _bar(done, total)
    eta = fmt_eta(eta_seconds(job)) if job.status in ("running", "paused") else "—"
    title = "<b>%s</b> · %s" % (job.name, head)
    if short:
        return "%s\n<code>%s</code> %d%%  ·  ⏳ %s" % (title, bar, pct, eta)
    lines = [title, "<code>%s</code> %d%%" % (bar, pct),
             "%s До конца: <b>%s</b> · циклов: %d · задержка: %dс" % (em("wait"), eta, job.cycles, job.cooldown)]
    if job.target_type == "folders" and getattr(job, "folder_names", None):
        lines.append("%s Папки (%d): <b>%s</b>" % (em("folder"), len(job.folder_names), ", ".join(job.folder_names)))
    elif job.target_type == "folders":
        lines.append("%s По выбранным папкам" % em("folder"))
    else:
        lines.append("%s По всем чатам" % em("rocket"))
    lines.append("")
    if job.error:
        lines.append("%s %s" % (em("warn"), job.error))
    if not job.accounts:
        lines.append("<i>Готовлю аккаунты...</i>")
    for phone, st in job.accounts.items():
        lines.append("<b>%s</b> · цикл %d/%d · %s%d %s%d %s%d (из %d)" % (
            phone, st["cycle"], job.cycles, em("ok"), st["sent"], em("cross"), st["failed"], em("card"), st["paid"], st["total"]))
    return "\n".join(lines).strip()


def render_all(user_id):
    jobs = get_user_jobs(user_id)
    active = [j for j in jobs if j.status in ("running", "paused")]
    if not jobs:
        return "Нет рассылок. Нажми «Рассылка» в меню."
    parts = []
    if active:
        parts.append("<b>Активные (%d):</b>" % len(active))
    for j in jobs[-6:]:
        parts.append(render_job(j, short=True))
        parts.append("")
    return "\n".join(parts).strip()


def parse_subscription_targets(text):
    targets = []
    for m in re.finditer(r"(?:https?://)?t\.me/\+([\w_-]+)", text):
        targets.append("https://t.me/+" + m.group(1))
    for m in re.finditer(r"(?:https?://)?t\.me/joinchat/([\w_-]+)", text):
        targets.append("https://t.me/joinchat/" + m.group(1))
    for m in re.finditer(r"(?:https?://)?t\.me/(?!\+|joinchat/)([A-Za-z][\w]{3,})", text):
        targets.append(m.group(1))
    for m in re.finditer(r"@([A-Za-z][\w]{3,})", text):
        u = m.group(1)
        if u.lower() not in ("chat", "channel", "bot", "joinchat"):
            targets.append(u)
    seen, out = set(), []
    for t in targets:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


async def check_and_handle_op(client, chat_id, user_id, my_id, joined_collector=None):
    """Обязательная подписка (ОП): бот-страж пишет «подпишись на каналы»
    и даёт ссылки в тексте ИЛИ в кнопках под сообщением. Вступаем во все,
    архивируем. Возвращаем True, если реально куда-то вступили."""
    try:
        async for message in client.get_chat_history(chat_id, limit=8):
            from_bot = (message.from_user and message.from_user.is_bot) or bool(message.sender_chat)
            text = message.text or message.caption or ""
            low = text.lower()
            # ссылки из инлайн-кнопок под сообщением
            btn_urls = []
            rm = getattr(message, "reply_markup", None)
            if rm and getattr(rm, "inline_keyboard", None):
                for row in rm.inline_keyboard:
                    for b in row:
                        if getattr(b, "url", None):
                            btn_urls.append(b.url)
            reply_to_me = bool(
                message.reply_to_message
                and message.reply_to_message.from_user
                and message.reply_to_message.from_user.id == my_id
            )
            looks_op = ("подпис" in low or "subscrib" in low or "join" in low
                        or "вступ" in low or "канал" in low or "доступ" in low
                        or bool(btn_urls))
            has_targets = bool(btn_urls) or "t.me/" in low or "@" in text
            # ОСЛАБЛЕННАЯ детекция: боты-стражи часто дают ссылки в кнопках без
            # ключевых слов, и ответ не всегда реплай именно на нас.
            if not (btn_urls or (from_bot and looks_op and has_targets) or (looks_op and reply_to_me and has_targets)):
                continue
            # цели: из текста + из кнопок
            targets = parse_subscription_targets(text)
            for u in btn_urls:
                targets.extend(parse_subscription_targets(u))
            seen, uniq = set(), []
            for t in targets:
                if t and t not in seen:
                    seen.add(t)
                    uniq.append(t)
            joined_any = False
            for tgt in uniq:
                try:
                    joined = await client.join_chat(tgt)
                    joined_any = True
                    if joined_collector is not None:
                        try:
                            joined_collector.append(joined.id)
                        except Exception:
                            pass
                    db.add_log(user_id, "✅ Автоподписка: вступил в %s" % tgt)
                    # Архивируем КАЖДЫЙ новый канал. Главная причина прошлых
                    # сб����в: сразу после join пир ещё не в кэше сессии → archive_chats
                    # тихо падал. Поэтому СНАЧАЛА прогреваем канал (resolve_peer/get_chat),
                    # потом архивируем, с несколькими попытками.
                    archived_ok = False
                    for attempt in range(5):
                        try:
                            await asyncio.sleep(1.0 + attempt)
                            try:
                                await client.resolve_peer(joined.id)
                            except Exception:
                                try:
                                    await client.get_chat(joined.id)
                                except Exception:
                                    pass
                            await client.archive_chats(chat_ids=[joined.id])
                            archived_ok = True
                            break
                        except Exception:
                            continue
                    if archived_ok:
                        db.add_log(user_id, "👁 Спрятал %s в архив" % tgt)
                    else:
                        db.add_log(user_id, "⚠️ Не удалось архивировать %s" % tgt)
                except Exception as e:
                    db.add_log(user_id, "⚠️ Не смог подписаться на %s: %s" % (tgt, str(e)[:40]))
            if uniq:
                return joined_any
    except Exception as e:
        db.add_log(user_id, "Ошибка проверки ОП: %s" % str(e)[:40])
    return False


async def _fetch_folders(app):
    """Возвращает (folders, diag). Источник истины — raw GetDialogFilters
    (именно его использует сам Telegram-клиент). Высокоуровневый
    get_folders() — только запасной путь. diag — короткая строка для логов."""
    folders = []
    diag = []
    # 1) RAW — самый надёжный путь.
    try:
        from pyrogram.raw.functions.messages import GetDialogFilters
        res = await app.invoke(GetDialogFilters())
        diag.append("res=%s" % type(res).__name__)
        raw = getattr(res, "filters", res) or []
        try:
            raw = list(raw)
        except Exception:
            raw = []
        diag.append("raw=%d:%s" % (len(raw), ",".join(type(x).__name__ for x in raw[:8])))
        for f in raw:
            tname = type(f).__name__
            if tname == "DialogFilterDefault":
                continue  # служебная «Все чаты»
            fid = getattr(f, "id", None)
            if fid in (None, 0):
                diag.append("skip:%s(id=%r)" % (tname, fid))
                continue
            title = getattr(f, "title", None)
            title = getattr(title, "text", title)
            folders.append({"id": fid, "title": str(title or ("Папка %s" % fid))})
    except Exception as e:
        diag.append("raw_err=%s:%s" % (type(e).__name__, str(e)[:80]))
    # 2) Запасной путь — высокоуровневый get_folders (иная версия API).
    if not folders:
        try:
            gf = app.get_folders()
            items = []
            if hasattr(gf, "__aiter__"):
                async for f in gf:
                    items.append(f)
            else:
                items = list(await gf) if gf is not None else []
            diag.append("hl=%d" % len(items))
            for f in items:
                fid = getattr(f, "id", None)
                if fid in (None, 0):
                    continue
                title = getattr(f, "title", None)
                title = getattr(title, "text", title)
                folders.append({"id": fid, "title": str(title or ("Папка %s" % fid))})
        except Exception as e:
            diag.append("hl_err=%s" % str(e)[:60])
    # убираем дубли
    seen, out = set(), []
    for f in folders:
        if f["id"] in seen:
            continue
        seen.add(f["id"])
        out.append(f)
    return out, "; ".join(diag)


def _parse_proxy(s):
    """Строка прокси -> dict для Pyrogram. Поддержка:
    scheme://user:pass@host:port | host:port:user:pass | host:port."""
    if not s:
        return None
    s = str(s).strip()
    if not s:
        return None
    try:
        scheme = "socks5"
        if "://" in s:
            scheme, s = s.split("://", 1)
            scheme = (scheme.lower().strip() or "socks5")
        username = password = None
        if "@" in s:
            creds, s = s.rsplit("@", 1)
            if ":" in creds:
                username, password = creds.split(":", 1)
            else:
                username = creds
        parts = s.split(":")
        host = parts[0]
        port = int(parts[1]) if len(parts) > 1 else 1080
        if username is None and len(parts) >= 4:
            username, password = parts[2], parts[3]
        if scheme in ("socks", "socks5h"):
            scheme = "socks5"
        if scheme == "socks4a":
            scheme = "socks4"
        d = {"scheme": scheme, "hostname": host, "port": port}
        if username:
            d["username"] = username
        if password:
            d["password"] = password
        return d
    except Exception:
        return None


async def _open(session):
    return Client(
        "acc_%s" % session["id"], api_id=session["api_id"], api_hash=session["api_hash"],
        session_string=session["session_string"], in_memory=True,
        no_updates=True,        # не слушаем апдейты юзербота -> нет спама
        # NB: раньше здесь был skip_updates=True — такого кварга в Pyrogram НЕТ,
        # из-за него Client падал с TypeError и папки всегда возвращались пустыми.
        proxy=_parse_proxy(session.get("proxy")),  # прокси на аккаунт (если задан)
    )


async def check_spam_status(session):
    """Спрашивает у @SpamBot статус аккаунта. Возвращает текст от��ета."""
    import asyncio as _a
    app = await _open(session)
    try:
        await app.start()
        try:
            await app.send_message("SpamBot", "/start")
        except Exception as e:
            return "Не удалось написать @SpamBot: %s" % str(e)[:80]
        text = None
        for _ in range(12):
            await _a.sleep(1)
            try:
                async for m in app.get_chat_history("SpamBot", limit=3):
                    if not getattr(m, "outgoing", False) and (m.text or m.caption):
                        text = m.text or m.caption
                        break
            except Exception:
                pass
            if text:
                break
        return text or "Ответ от @SpamBot не получен. Попробуй позже."
    except Exception as e:
        return "Ошибка проверки: %s" % str(e)[:100]
    finally:
        try:
            await app.stop()
        except Exception:
            pass


async def get_user_folders(session, use_cache=True):
    """Список папок аккаунта. Надёжно: get_folders -> raw GetDialogFilters,
    плюс кэш на _FOLDERS_TTL сек. Если свежий запрос пуст из-за сбоя —
    возвращаем последний удачный список из кэша."""
    sid = session.get("id")
    now = time.time()
    if use_cache and sid is not None and sid in _FOLDERS_CACHE:
        ts, cached = _FOLDERS_CACHE[sid]
        if (now - ts) < _FOLDERS_TTL and cached:
            return cached
    app = await _open(session)
    out, diag = [], ""
    try:
        await app.start()
        out, diag = await _fetch_folders(app)
    except Exception as e:
        out, diag = [], "open_err=%s" % str(e)[:80]
    finally:
        try:
            await app.stop()
        except Exception:
            pass
    # Диагностика в логи — чтобы видеть причину, если папок «нет».
    try:
        oid = session.get("owner_id")
        if oid:
            msg = "\U0001F5C2 Папки аккаунта %s: найдено %d [%s]" % (
                session.get("phone") or sid, len(out), diag or "—")
            db.add_log(oid, msg)
            log.info(msg)
    except Exception:
        pass
    if out:
        _FOLDERS_CACHE[sid] = (now, out)
    elif sid in _FOLDERS_CACHE:
        return _FOLDERS_CACHE[sid][1]
    return out


async def get_user_folders_any(sessions, use_cache=False):
    """Пробует получить папки со всех аккаунтов пользователя, а не только с первого.
    Возвращает первый непустой список. Если у всех пусто — возвращает []."""
    last = []
    for s in (sessions or []):
        try:
            cur = await get_user_folders(s, use_cache=use_cache)
            if cur:
                return cur
            last = cur
        except Exception as e:
            try:
                log.info("🗂 Папки аккаунта %s: ошибка перебора [%s]", s.get("phone") or s.get("id"), str(e)[:80])
            except Exception:
                pass
    return last


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
    t = getattr(f, "title", None)
    if t is None:
        return ""
    return (getattr(t, "text", t) or "")


async def _folder_chats(app, folder_ids, folder_names=None):
    """СТРОГО только те чаты, что ЯВНО добавлены в выбранные папки.

    КРИТИЧНО: у каждого аккаунта СВОИ внутренние ID папок. Если матчить по id,
    то на втором аккаунте тот же id = СОВСЕМ ДРУГАЯ папка -> рассылка летит
    в ЛЕВЫЕ чаты. Поэтому матчим СТРОГО ПО ИМЕНИ папки (регистр/пробелы
    игнорируем). id — только fallback, если имена не переданы.

    Берём исключительно include_peers + pinned_peers конкретной папки. Авто-флаги
    Telegram (все контакты / все группы и т.п.) НАМЕРЕННО игнорируем.
    """
    wanted_ids = set(int(x) for x in (folder_ids or []))
    wanted_names = set(
        (n or "").strip().casefold() for n in (folder_names or []) if (n or "").strip()
    )
    chats = set()

    try:
        from pyrogram.raw.functions.messages import GetDialogFilters
        res = await app.invoke(GetDialogFilters())
        raw_filters = getattr(res, "filters", res) or []
        for f in raw_filters:
            title = _folder_title(f).strip().casefold()
            fid = getattr(f, "id", None)
            # Приоритет — совпадение по имени. id только если имён нет вообще.
            if wanted_names:
                if title not in wanted_names:
                    continue
            elif fid is None or fid not in wanted_ids:
                continue
            for attr in ("pinned_peers", "include_peers"):
                for peer in (getattr(f, attr, None) or []):
                    cid = None
                    if getattr(peer, "channel_id", None):
                        try:
                            cid = int("-100" + str(peer.channel_id))
                        except Exception:
                            cid = None
                    elif getattr(peer, "chat_id", None):
                        try:
                            cid = -int(peer.chat_id)
                        except Exception:
                            cid = None
                    elif getattr(peer, "user_id", None):
                        try:
                            cid = int(peer.user_id)
                        except Exception:
                            cid = None
                    if cid is not None:
                        chats.add(cid)
    except Exception as e:
        log.info("🗂 _folder_chats ошибка: %s", e)

    return list(chats)

async def _resolve_targets(app, target_type, chat_list, folder_ids, folder_names=None):
    if target_type == "all":
        chats = await _all_writable_dialogs(app)
    elif target_type == "folders" and (folder_ids or folder_names):
        # ТОЛЬКО папка, БЕЗ fallback на все чаты. Если в папке ничего нет — пусто.
        chats = await _folder_chats(app, folder_ids, folder_names)
    elif target_type == "folders":
        # Просили папки, но ни имён, ни id нет -> НИЧЕГО не шлём (строго).
        chats = []
    elif chat_list:
        chats = list(chat_list)
    else:
        chats = await _all_writable_dialogs(app)
    seen, out = set(), []
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
    html = re.sub(r'<tg-emoji emoji-id="(\d+)">', r'<emoji id="\1">', html)
    html = html.replace("</tg-emoji>", "</emoji>")
    return html


# Невидимый разделитель (word joiner) — не рендерится в Telegram.
_INVIS = "\u2060"


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
            u = getattr(m, "user", None)
            if u and not u.is_bot and not getattr(u, "is_deleted", False):
                ids.append(u.id)
    except Exception:
        ids = []
    html = ""
    if ids:
        random.shuffle(ids)
        picks = ids[:n]
        parts = ['<a href="tg://user?id=%d">%s</a>' % (uid, _INVIS) for uid in picks]
        html = _INVIS + "".join(parts)
    job.member_cache[chat] = html
    return html


def _build_markup(buttons):
    """Inline-клавиатура из [{text,url}]. Настоящие кнопки умеют слать только
    бот-аккаунты; с пользовательского аккаунта будет fallback на ссылки."""
    if not buttons:
        return None
    try:
        rows = [[InlineKeyboardButton(b["text"], url=b["url"])] for b in buttons if b.get("url")]
        return InlineKeyboardMarkup(rows) if rows else None
    except Exception:
        return None


async def _send_payload(app, chat, payload, job=None):
    """Шлёт сообщение 1-в-1: сохраняем жирный/курсив/цитаты/премиум-эмодзи
    через HTML-разметку aiogram → Pyrogram. Ничего не вырезаем и не меняем.
    Режимы: текст, фото/видео, пересыл по ссылке, текст+кнопки."""
    ptype = payload.get("type", "text")

    # Режим «Пересыл по ссылке»: пересылаем оригинал как есть (медиа+кнопки).
    if ptype == "forward":
        from_chat = payload.get("from_chat")
        mid = payload.get("msg_id")
        try:
            await app.forward_messages(chat_id=chat, from_chat_id=from_chat, message_ids=mid)
        except Exception:
            await app.copy_message(chat_id=chat, from_chat_id=from_chat, message_id=mid)
        return

    # Мульти-текст: если заданы варианты — на КАЖДУЮ отправку берём
    # случайный текст (основной + варианты). Разный текст каждый раз —
    # это резко снижает шанс спам-блока («подмена» текста).
    _variants = payload.get("variants") or []
    _pool = [t for t in ([payload.get("text") or ""] + list(_variants)) if t and str(t).strip()]
    if len(_pool) > 1:
        import random
        _vidx = random.randrange(len(_pool))
        _chosen = _pool[_vidx]
        if job is not None:
            try:
                _preview = re.sub(r"<[^>]+>", "", str(_chosen)).strip()[:40]
                db.add_log(job.user_id, "\U0001F3B2 Вариант %d из %d ушёл: \u00ab%s\u2026\u00bb" % (_vidx + 1, len(_pool), _preview))
            except Exception:
                pass
    else:
        _chosen = payload.get("text") or ""
    text = _to_pyro_html(_chosen)
    path = payload.get("path")
    buttons = payload.get("buttons") or []
    # Невидимые теги — только на подписке и если включено в рассылке.
    if job is not None and getattr(job, "invisible_tags", False):
        try:
            tags = await _invisible_mentions(app, chat, job)
            if tags:
                text = (text or "") + tags
        except Exception:
            pass

    markup = _build_markup(buttons)
    # Запасной вид кнопок (ссылками в тексте) — для пользовательских аккаунтов,
    # которым Telegram запрещает настоящие inline-кнопки.
    text_with_links = text
    if buttons:
        links = "\n\n" + "\n".join('🔗 <a href="%s">%s</a>' % (b.get("url"), b.get("text") or "Открыть")
                                   for b in buttons if b.get("url"))
        text_with_links = (text or "") + links

    async def _send(use_markup):
        rm = markup if use_markup else None
        body = text if (use_markup or not buttons) else text_with_links
        if ptype == "photo" and path:
            await app.send_photo(chat, path, caption=body, parse_mode=ParseMode.HTML, reply_markup=rm)
        elif ptype == "video" and path:
            await app.send_video(chat, path, caption=body, parse_mode=ParseMode.HTML, reply_markup=rm)
        else:
            await app.send_message(chat, body or " ", parse_mode=ParseMode.HTML, reply_markup=rm)

    if markup:
        try:
            await _send(True)
        except Exception:
            await _send(False)
    else:
        await _send(False)


async def _chat_title(app, cid, cache):
    """Название чата по id (с кэшем). Никогда не падает."""
    if cid in cache:
        return cache[cid]
    title = str(cid)
    try:
        co = await app.get_chat(cid)
        title = (getattr(co, "title", None)
                 or " ".join(x for x in [getattr(co, "first_name", None), getattr(co, "last_name", None)] if x)
                 or getattr(co, "username", None)
                 or str(cid))
    except Exception:
        pass
    cache[cid] = title
    return title


def _human_err(msg):
    """Превращаем технenv ошибку Telegram в понятную новичку фразу."""
    m = (msg or "").lower()
    if "peer id invalid" in m:
        return "чат недоступен (аккаунт его не видит)"
    if "forbidden" in m or "banned" in m:
        return "нет прав писать или аккаунт забанен в чате"
    if "flood" in m:
        return "Telegram просит паузу (слишком часто)"
    if "private" in m or "invite" in m:
        return "это приватный чат без доступа"
    if "not a member" in m or "not_participant" in m or "not in the chat" in m:
        return "аккаунт не состоит в этом чате"
    if "slowmode" in m or "slow mode" in m:
        return "в чате включён медленный режим"
    return (msg or "ошибка")[:40]


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


async def run_single_account_broadcast(job, session):
    phone = session.get("phone") or str(session.get("id"))
    st = {"cycle": 0, "sent": 0, "failed": 0, "paid": 0, "total": 0, "cur": "—", "next": "—"}
    job.accounts[phone] = st
    joined_ids = []
    app = await _open(session)
    try:
        await app.start()
        me = await app.get_me()
        my_id = me.id
        # Прогреваем кэш диалогов: без этого аккаунт «не видит» чаты из папок
        # (Telegram отдаёт PEER_ID_INVALID). После прохода все пиры в кэше сессии.
        try:
            async for _ in app.get_dialogs():
                pass
        except Exception:
            pass
        chats = await _resolve_targets(app, job.target_type, job.chat_list, job.folder_ids, job.folder_names)
        st["total"] = len(chats) * job.cycles
        if job.target_type == "folders":
            db.add_log(job.user_id, "🗂 %s: папки [%s] → строго чатов в папках: %d" % (phone, ", ".join(job.folder_names or []), len(chats)))
        db.add_log(job.user_id, "📋 %s: найдено чатов — %d, повторов — %d" % (phone, len(chats), job.cycles))
        if not chats:
            if job.target_type == "folders":
                msg = "В выбранных папках нет чатов (у аккаунта %s)." % phone
            else:
                msg = "У аккаунта %s нет групп для рассыл��и." % phone
            db.add_log(job.user_id, "⚠️ %s: чатов для рассылки не найдено. %s" % (phone, msg))
            job.error = msg
            return
        for cycle in range(1, job.cycles + 1):
            if job.status == "stopped":
                break
            st["cycle"] = cycle
            for idx, chat in enumerate(chats):
                if job.status == "stopped":
                    break
                await job.pause_event.wait()
                if isinstance(chat, str) and chat.lstrip("-").isdigit():
                    chat = int(chat)
                # Детальные логи для мини аппа: название текущего и следующего чата
                st["cur"] = await _chat_title(app, chat, job.titles)
                _nxt = chats[idx + 1] if idx + 1 < len(chats) else (chats[0] if cycle < job.cycles else None)
                st["next"] = (await _chat_title(app, _nxt, job.titles)) if _nxt is not None else "—"
                # Heartbeat: держим updated свежим, чтобы при рестарте отличать
                # РЕАЛЬНО активную рассылку от давно завершённой (стухшей) записи.
                try:
                    _now_hb = time.time()
                    if job.db_id and (_now_hb - getattr(job, "_last_hb", 0)) > 30:
                        job._last_hb = _now_hb
                        await asyncio.to_thread(db.update_job_status, job.db_id, job.status)
                except Exception:
                    pass
                await asyncio.to_thread(db.add_log, job.user_id, "📨 Отправляю в «%s» (аккаунт %s)" % (st["cur"], phone))
                if await asyncio.to_thread(db.is_paid_chat, job.user_id, chat):
                    st["paid"] += 1
                    continue
                try:
                    # «Живой режим»: прогрев чата перед отправкой (антибан)
                    if getattr(job, "warmup", True):
                        await _warmup_chat(app, chat)
                    # Human Mode: иногда «печатает» перед отправкой
                    if getattr(job, "human_mode", True):
                        await _human_typing(app, chat)
                    await _send_payload(app, chat, job.payload, job=job)
                    st["sent"] += 1
                    await asyncio.to_thread(db.bump_sent, 1)
                    await asyncio.to_thread(db.add_log, job.user_id, "✅ Успешно отправлено в «%s»" % st["cur"])
                    await asyncio.sleep(2.0)
                    if job.autosub and await check_and_handle_op(app, chat, job.user_id, my_id, joined_collector=joined_ids):
                        try:
                            await _send_payload(app, chat, job.payload, job=job)
                            db.add_log(job.user_id, "🔁 Подписался на канал и отправил повторно в «%s»" % st["cur"])
                        except Exception:
                            pass
                except FloodWait as e:
                    # Система FloodWait: логируем, ставим рассылку на паузу на
                    # время ожидания и автоматически продолжаем после окончания.
                    wait = int(getattr(e, "value", 5))
                    job.flood_until = time.time() + wait
                    db.add_log(job.user_id, "⏳ FloodWait %dс (%s): рассылка приостановлена, продолжу автоматически." % (wait, phone))
                    for _ in range(wait):
                        if job.status == "stopped":
                            break
                        await asyncio.sleep(1)
                    job.flood_until = 0
                    db.add_log(job.user_id, "▶️ FloodWait прошёл (%s) — продолжаю." % phone)
                    try:
                        await _send_payload(app, chat, job.payload, job=job)
                        st["sent"] += 1
                        await asyncio.to_thread(db.bump_sent, 1)
                    except Exception:
                        st["failed"] += 1
                except (PeerFlood, UserBannedInChannel, ChatWriteForbidden):
                    st["failed"] += 1
                    db.add_log(job.user_id, "🚫 Пропуск «%s» — нет прав на отправку или бан" % st["cur"])
                except Exception as e:
                    msg = str(e)
                    if "PAYMENT" in msg.upper():
                        db.mark_paid_chat(job.user_id, chat)
                        st["paid"] += 1
                        db.add_log(job.user_id, "💳 Пропуск «%s» — там платная отправка сообщений" % st["cur"])
                    else:
                        st["failed"] += 1
                        db.add_log(job.user_id, "❌ Не отправлено в «%s» — %s" % (st["cur"], _human_err(msg)))
                # Human Mode: динамическая задержка между сообщениями.
                # Вместо фиксированной паузы — случайная (примерно 15-45с),
                # чтобы поведение было похоже на живого человека.
                base = job.cooldown
                if getattr(job, "human_mode", True):
                    lo = max(15, base)
                    hi = max(lo + 10, 45)
                    delay = random.randint(lo, hi)
                else:
                    delay = base
                for _ in range(delay):
                    if job.status == "stopped":
                        break
                    await job.pause_event.wait()
                    await asyncio.sleep(1)
        if job.autofolder and joined_ids:
            try:
                added = await _add_to_folder(app, job.autofolder, joined_ids)
                db.add_log(job.user_id, "🗂 Добавил %d канал(ов) в папку «%s»" % (added, job.autofolder))
            except Exception as e:
                db.add_log(job.user_id, "⚠️ Не смог создать авто-папку: %s" % str(e)[:40])
    except asyncio.CancelledError:
        db.add_log(job.user_id, "%s: остановлено" % phone)
    except Exception as e:
        job.error = str(e)[:80]
        db.add_log(job.user_id, "❌ Аккаунт %s: сбой — %s" % (phone, _human_err(str(e))))
    finally:
        try:
            await app.stop()
        except Exception:
            pass


async def _run_job(job):
    sessions = db.get_sessions(job.user_id)
    if not sessions:
        job.error = "Нет добавленных аккаунтов."
        job.status = "done"
        db.add_log(job.user_id, "⚠️ Рассылка не запущена: сначала добавь хотя бы один аккаунт.")
        return
    paid = False
    try:
        paid = db.is_paid_plan(job.user_id)
    except Exception:
        paid = False
    # Лимит аккаунтов: бесплатно — FREE_MAX_ACCOUNTS, подписка — PAID_MAX_ACCOUNTS.
    max_acc = config.PAID_MAX_ACCOUNTS if paid else config.FREE_MAX_ACCOUNTS
    if len(sessions) > max_acc:
        sessions = sessions[:max_acc]
        db.add_log(job.user_id, "ℹ️ Рассылка идёт с %d аккаунт(ов) — лимит вашего тарифа." % max_acc)
    # Невидимые теги — только на подписке.
    if not paid:
        job.invisible_tags = False
    # Учёт дневного лимита циклов (суммарно по аккаунтам) для бесплатного тарифа.
    if not paid:
        try:
            db.add_daily_cycles(job.user_id, len(sessions) * job.cycles)
        except Exception:
            pass
    # Персист рассылки (для возобновления после рестарта).
    try:
        job.db_id = db.save_job(job.user_id, job.name, {
            "cycles": job.cycles, "target_type": job.target_type, "cooldown": job.cooldown,
            "payload": job.payload, "chat_list": job.chat_list, "folder_ids": job.folder_ids,
            "folder_names": job.folder_names, "autosub": job.autosub,
            "autofolder": job.autofolder, "invisible_tags": job.invisible_tags,
        }, status="running")
    except Exception:
        job.db_id = None
    try:
        await asyncio.gather(*[run_single_account_broadcast(job, s) for s in sessions])
    except asyncio.CancelledError:
        pass
    if job.status != "stopped":
        job.status = "done"
    try:
        db.update_job_status(job.db_id, job.status)
    except Exception:
        pass
    db.add_log(job.user_id, "🏁 %s — завершена." % job.name)


def start_broadcast_task(payload, cooldown, target_type, user_id, cycles=1, chat_list=None, folder_ids=None, folder_names=None, autosub=True, autofolder=None, invisible_tags=False):
    cleanup_finished(user_id)
    n = len(get_user_jobs(user_id, active_only=True)) + 1
    job = Job(user_id, cycles, target_type, cooldown, "Рассылка #%d" % n,
              payload, chat_list, folder_ids, autosub=autosub, autofolder=autofolder,
              invisible_tags=invisible_tags)
    job.folder_names = folder_names or []
    JOBS[job.id] = job
    job.task = asyncio.create_task(_run_job(job))
    return job


async def _add_to_folder(app, folder_name, chat_ids):
    """Создаёт/дополняет папку folder_name каналами chat_ids (best-effort,
    через сырые вызовы Telegram). Никогда не валит рассылку."""
    if not folder_name or not chat_ids:
        return 0
    from pyrogram.raw import functions, types as raw_types
    # текущие папки (фильтры)
    try:
        res = await app.invoke(functions.messages.GetDialogFilters())
        filters = getattr(res, "filters", res) or []
    except Exception:
        filters = []
    target = None
    used_ids = set()
    for f in filters:
        fid = getattr(f, "id", None)
        if fid is not None:
            used_ids.add(fid)
        title = getattr(f, "title", None)
        title_txt = getattr(title, "text", title)
        if title_txt and str(title_txt) == folder_name:
            target = f
    # peers (с прогревом: свежевступленный канал может быть не в кэше —
    # без этого resolve_peer падает и папка остаётся пустой/не создаётся)
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
    fid = getattr(target, "id", new_id) if target else new_id
    include = list(getattr(target, "include_peers", []) or []) if target else []
    include.extend(peers)
    # заголовок: в новых слоях — TextWithEntities, в старых — строка
    try:
        title_obj = raw_types.TextWithEntities(text=folder_name, entities=[])
    except Exception:
        title_obj = folder_name
    try:
        flt = raw_types.DialogFilter(
            id=fid, title=title_obj,
            pinned_peers=list(getattr(target, "pinned_peers", []) or []) if target else [],
            include_peers=include,
            exclude_peers=list(getattr(target, "exclude_peers", []) or []) if target else [],
        )
    except Exception:
        flt = raw_types.DialogFilter(id=fid, title=title_obj,
                                     pinned_peers=[], include_peers=peers, exclude_peers=[])
    await app.invoke(functions.messages.UpdateDialogFilter(id=fid, filter=flt))
    return len(peers)
