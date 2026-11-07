# -*- coding: utf-8 -*-
"""Telegram keyboards. Use Telegram custom emoji icons; keep labels text-only."""
from aiogram import types
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.types import WebAppInfo, KeyboardButton
import config
from emoji import eid

NAV_BROADCAST = "Рассылка"
NAV_ACCOUNTS = "Аккаунты"
NAV_MENU = "Меню"
NAV_SUB = "PRO"
# Kept for compatibility with the legacy text-navigation handler; not shown in menus.
NAV_SHOP = "Магазин"
NAV_HELP = "Справочник"


def reply_nav_kb():
    b = ReplyKeyboardBuilder()
    b.row(KeyboardButton(text=NAV_BROADCAST), KeyboardButton(text=NAV_ACCOUNTS))
    b.row(KeyboardButton(text=NAV_MENU), KeyboardButton(text=NAV_SUB))
    b.row(KeyboardButton(text=NAV_HELP))
    return b.as_markup(resize_keyboard=True, is_persistent=True,
                        input_field_placeholder="Выберите раздел или отправьте сообщение…")


def btn(text, emoji_key=None, **kw):
    """Use a Telegram custom-emoji button icon when enabled; never add emoji fallback text."""
    icon = eid(emoji_key) if (emoji_key and config.PREMIUM_EMOJI) else None
    if icon:
        try:
            return types.InlineKeyboardButton(text=text, icon_custom_emoji_id=icon, **kw)
        except Exception:
            pass
    return types.InlineKeyboardButton(text=text, **kw)


def _help_btn():
    if getattr(config, "HELP_URL", ""):
        return btn("Справочник", "check", url=config.HELP_URL)
    return btn("Справочник", "check", callback_data="nav:help")


def _nav(b, back="nav:menu", home=True):
    row = [btn("Назад", "back", callback_data=back)]
    if home and back != "nav:menu":
        row.append(btn("Главное меню", "home", callback_data="nav:menu"))
    b.row(*row)


def mode_kb():
    b = InlineKeyboardBuilder()
    if config.WEBAPP_URL:
        b.row(btn("Открыть Mini App", "app", web_app=WebAppInfo(url=config.WEBAPP_PAGE_URL)))
    b.row(btn("Работать в чате", "chat", callback_data="mode:chat"))
    b.row(btn("Мой профиль", "accounts", callback_data="nav:profile"))
    b.row(btn("Подписка PRO", "star", callback_data="nav:sub"))
    return b.as_markup()


def channel_gate_kb():
    b = InlineKeyboardBuilder()
    b.row(btn("Подписаться на канал", "channel", url=config.REQUIRED_CHANNEL_URL))
    b.row(btn("Я подписался — проверить", "ok", callback_data="gate:check"))
    return b.as_markup()


def main_menu_kb(is_admin=False):
    b = InlineKeyboardBuilder()
    b.row(btn("Создать рассылку", "rocket", callback_data="bc:start"))
    b.row(btn("Аккаунты", "accounts", callback_data="nav:accounts"),
          btn("Мониторинг", "monitor", callback_data="nav:monitor"))
    b.row(btn("Шаблоны", "msg", callback_data="nav:templates"),
          btn("История", "logs", callback_data="nav:logs"))
    b.row(btn("Подписка PRO", "star", callback_data="nav:sub"),
          btn("Рефералы", "ref", callback_data="nav:ref"))
    b.row(btn("Профиль", "accounts", callback_data="nav:profile"), _help_btn())
    b.row(btn("Связаться с нами", "chat", callback_data="nav:support"))
    if config.WEBAPP_URL:
        b.row(btn("Открыть Mini App", "app", web_app=WebAppInfo(url=config.WEBAPP_PAGE_URL)))
    if is_admin:
        b.row(btn("Панель администратора", "admin", callback_data="nav:admin"))
    return b.as_markup()


def accounts_kb(sessions):
    b = InlineKeyboardBuilder()
    b.row(btn("Добавить аккаунт", "account", callback_data="acc:add"))
    for s in sessions:
        b.row(btn("Проверить %s" % (s.get("phone") or s["id"]), "key", callback_data="acc:spam:%s" % s["id"]),
              btn("Удалить", "trash", callback_data="acc:del:%s" % s["id"]))
    _nav(b)
    return b.as_markup()


def monitor_kb(jobs):
    b = InlineKeyboardBuilder()
    for j in [x for x in jobs if x.status in ("running", "paused")]:
        if j.status == "running":
            b.row(btn("Пауза · %s" % j.name, "pause", callback_data="mon:pause:%d" % j.id),
                  btn("Открыть", "chart", callback_data="mon:open:%d" % j.id),
                  btn("Стоп", "stop", callback_data="mon:stop:%d" % j.id))
        else:
            b.row(btn("Продолжить · %s" % j.name, "play", callback_data="mon:resume:%d" % j.id),
                  btn("Открыть", "chart", callback_data="mon:open:%d" % j.id),
                  btn("Стоп", "stop", callback_data="mon:stop:%d" % j.id))
    b.row(btn("Обновить", "refresh", callback_data="nav:monitor"))
    _nav(b)
    return b.as_markup()


def monitor_job_kb(job):
    b = InlineKeyboardBuilder()
    if job and job.status == "running":
        b.row(btn("Пауза", "pause", callback_data="mon:pause:%d" % job.id), btn("Стоп", "stop", callback_data="mon:stop:%d" % job.id))
    elif job and job.status == "paused":
        b.row(btn("Продолжить", "play", callback_data="mon:resume:%d" % job.id), btn("Стоп", "stop", callback_data="mon:stop:%d" % job.id))
    if job:
        b.row(btn("Обновить", "refresh", callback_data="mon:open:%d" % job.id))
    _nav(b, back="nav:monitor")
    return b.as_markup()


def payment_kb(with_nav=True):
    b = InlineKeyboardBuilder()
    b.row(btn("Оплатить · %d Telegram Stars" % config.SUB_PRICE_STARS, "stars", callback_data="pay:stars"))
    b.row(btn("Карта или криптовалюта", "card", callback_data="pay:manual"))
    b.row(btn("Связаться с поддержкой", "msg", url=config.OWNER_CONTACT_URL))
    if with_nav:
        _nav(b)
    return b.as_markup()


def admin_kb():
    b = InlineKeyboardBuilder()
    b.row(btn("Пользователи", "users", callback_data="admin:users"), btn("Аккаунты", "accounts", callback_data="admin:accounts"))
    b.row(btn("Профиль пользователя", "users", callback_data="admin:userinfo"), btn("Журнал действий", "logs", callback_data="admin:actions"))
    b.row(btn("Выдать PRO", "gift", callback_data="admin:give"), btn("Снять PRO", "ban", callback_data="admin:take"))
    b.row(btn("Массовая выдача", "gift", callback_data="admin:massgive"))
    b.row(btn("Заблокировать", "ban", callback_data="admin:ban"), btn("Разблокировать", "ok", callback_data="admin:unban"))
    b.row(btn("Сообщение всем", "msg", callback_data="admin:announce"))
    b.row(btn("Обновить", "refresh", callback_data="nav:admin"))
    _nav(b)
    return b.as_markup()


def admin_back_kb():
    b = InlineKeyboardBuilder(); _nav(b, back="nav:admin"); return b.as_markup()


def folders_kb(folders, chosen):
    b = InlineKeyboardBuilder()
    for f in folders:
        title = str(f["title"])
        label = ("Выбрано · " if f["id"] in chosen else "") + title
        b.row(btn(label, None if f["id"] in chosen else "folder", callback_data="bc:fold:%s" % f["id"]))
    if chosen:
        b.row(btn("Далее · выбрано %d" % len(chosen), "play", callback_data="bc:fold_done"))
    b.row(btn("Все чаты", "rocket", callback_data="bc:all"), btn("Отмена", "cross", callback_data="nav:menu"))
    return b.as_markup()


def bc_mode_kb():
    b = InlineKeyboardBuilder()
    b.row(btn("Только текст", "msg", callback_data="bc:mode:text"))
    b.row(btn("Медиа и текст", "app", callback_data="bc:mode:media"))
    b.row(btn("Копировать пост по ссылке", "rocket", callback_data="bc:mode:forward"))
    b.row(btn("Текст и кнопки", "key", callback_data="bc:mode:buttons"))
    b.row(btn("Отмена", "cross", callback_data="nav:menu"))
    return b.as_markup()


def bc_buttons_kb(buttons):
    b = InlineKeyboardBuilder()
    for i, bt in enumerate(buttons or []):
        b.row(btn(str(bt.get("text") or "Кнопка"), None, callback_data="bc:btn_del:%d" % i))
    b.row(btn("Добавить кнопку", "account", callback_data="bc:btn_add"))
    if buttons:
        b.row(btn("Готово · кнопок %d" % len(buttons), "play", callback_data="bc:btn_done"))
    b.row(btn("Без кнопок", "cross", callback_data="bc:btn_skip"))
    return b.as_markup()


def cancel_kb():
    b = InlineKeyboardBuilder(); b.row(btn("Отмена", "cross", callback_data="nav:menu")); return b.as_markup()


def confirm_kb(autosub=False, autofolder=None, invisible_tags=False, paid=False, is_admin=False, n_variants=0):
    b = InlineKeyboardBuilder()
    b.row(btn("Запустить по проверенному списку", "rocket", callback_data="bc:launch"))
    b.row(btn("Запланировать", "timer", callback_data="bc:schedule"))
    b.row(btn("Улучшить текст с AI", "rocket", callback_data="bc:ai"))
    b.row(btn("Варианты текста: %d" % n_variants, "msg", callback_data="bc:addvar"))
    b.row(btn("Сохранить шаблон", "star", callback_data="bc:save_tpl"))
    b.row(btn("Редактировать", "gear", callback_data="bc:edit"))
    if config.WEBAPP_URL:
        b.row(btn("Таймеры и настройки аккаунтов", "timer", web_app=WebAppInfo(url=config.WEBAPP_PAGE_URL)))
    _nav(b)
    return b.as_markup()


def ai_preview_kb():
    b = InlineKeyboardBuilder()
    b.row(btn("Применить", "check", callback_data="bc:ai_apply"))
    b.row(btn("Создать другой вариант", "rocket", callback_data="bc:ai_regen"))
    b.row(btn("Отмена", "cross", callback_data="bc:ai_cancel"))
    return b.as_markup()


def edit_kb():
    b = InlineKeyboardBuilder()
    b.row(btn("Сообщение", "msg", callback_data="bc:edit:msg"))
    b.row(btn("Задержка", "timer", callback_data="bc:edit:delay"), btn("Циклы", "cycle", callback_data="bc:edit:cycles"))
    b.row(btn("Назад к запуску", "back", callback_data="bc:confirm"))
    return b.as_markup()


def support_kb():
    b = InlineKeyboardBuilder(); b.row(btn("Связаться с нами", "chat", callback_data="nav:support")); _nav(b); return b.as_markup()


def sub_expired_kb():
    return payment_kb(with_nav=False)


def templates_kb(templates):
    b = InlineKeyboardBuilder()
    for t in templates or []:
        b.row(btn(str(t.get("name") or "Шаблон"), "msg", callback_data="tpl:use:%s" % t["id"]),
              btn("Удалить", "trash", callback_data="tpl:del:%s" % t["id"]))
    _nav(b)
    return b.as_markup()


def back_only_kb(back="nav:menu"):
    b = InlineKeyboardBuilder(); _nav(b, back=back); return b.as_markup()
