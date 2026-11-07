# -*- coding: utf-8 -*-
# Все клавиатуры в одном месте.
# Премиум-эмодзи НА КНОПКАХ — через icon_custom_emoji_id (не в тексте!).

from aiogram import types
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.types import WebAppInfo, KeyboardButton

import config
from emoji import eid


# ===== НИЖНИЕ КНОПКИ (reply-клавиатура под полем ввода) =====
# Навигация по основным разделам. Эмодзи — unicode (премиум-эмодзи
# на reply-кнопках Telegram не поддерживает, это ограничение Bot API).
NAV_BROADCAST = "Рассылка"
NAV_ACCOUNTS = "Аккаунты"
NAV_MENU = "Меню"
NAV_SUB = "Подписка"
NAV_SHOP = "Магазин"
NAV_HELP = "Справочник"


def reply_nav_kb():
    """Постоянная нижняя панель навигации."""
    b = ReplyKeyboardBuilder()
    b.row(KeyboardButton(text=NAV_BROADCAST), KeyboardButton(text=NAV_ACCOUNTS))
    b.row(KeyboardButton(text=NAV_MENU), KeyboardButton(text=NAV_SUB))
    b.row(KeyboardButton(text=NAV_HELP), KeyboardButton(text=NAV_SHOP))
    return b.as_markup(resize_keyboard=True, is_persistent=True,
                       input_field_placeholder="Выберите раздел или отправьте сообщение…")


def btn(text, emoji_key=None, **kw):
    """Кнопка с премиум-иконкой. При несовместимости — без иконки."""
    icon = eid(emoji_key) if (emoji_key and config.PREMIUM_EMOJI) else None
    if icon:
        try:
            return types.InlineKeyboardButton(text=text, icon_custom_emoji_id=icon, **kw)
        except Exception:
            pass
    return types.InlineKeyboardButton(text=text, **kw)


def _help_btn():
    """Справочник: если есть Telegraph-страница — открываем её, иначе текст в чате."""
    if getattr(config, "HELP_URL", ""):
        return btn("Справочник", "check", url=config.HELP_URL)
    return btn("Справочник", "check", callback_data="nav:help")


def _nav(b, back="nav:menu", home=True):
    """Добавляет ряд «Назад / Главное меню»."""
    row = [btn("Назад", "back", callback_data=back)]
    if home and back != "nav:menu":
        row.append(btn("Главное", "home", callback_data="nav:menu"))
    b.row(*row)


# ===== Старт: выбор режима =====
def mode_kb():
    b = InlineKeyboardBuilder()
    if config.WEBAPP_URL:
        b.row(btn("Перейти в Mini App", "app", web_app=WebAppInfo(url=config.WEBAPP_PAGE_URL)))
    b.row(btn("Работать в чате", "chat", callback_data="mode:chat"))
    b.row(btn("Мой профиль", "accounts", callback_data="nav:profile"))
    b.row(btn("Купить подписку", "star", callback_data="nav:sub"))
    return b.as_markup()


# ===== Гейт по подписке на канал =====
def channel_gate_kb():
    b = InlineKeyboardBuilder()
    b.row(btn("Подписаться на канал", "channel", url=config.REQUIRED_CHANNEL_URL))
    b.row(btn("Я подписался — проверить", "ok", callback_data="gate:check"))
    return b.as_markup()


# ===== Главное меню =====
def main_menu_kb(is_admin=False):
    """Строгий, сгруппированный экран: главное действие сверху, далее блоками."""
    b = InlineKeyboardBuilder()
    # 1. Главное действие
    b.row(btn("Запустить рассылку", "rocket", callback_data="bc:start"))
    # 2. Рабочий блок
    b.row(
        btn("Аккаунты", "accounts", callback_data="nav:accounts"),
        btn("Мониторинг", "monitor", callback_data="nav:monitor"),
    )
    b.row(
        btn("Шаблоны", "msg", callback_data="nav:templates"),
        btn("Логи", "logs", callback_data="nav:logs"),
    )
    # 3. Рост и деньги
    b.row(
        btn("Подписка", "star", callback_data="nav:sub"),
        btn("Рефералы", "ref", callback_data="nav:ref"),
    )
    b.row(btn("Профиль", "accounts", callback_data="nav:profile"))
    # 4. Магазин (прямая ссылка на канал) + справочник
    b.row(
        btn("Магазин", "gift", url=config.SHOP_URL),
        _help_btn(),
    )
    b.row(btn("Поддержка", "chat", callback_data="nav:support"))
    if config.WEBAPP_URL:
        b.row(btn("Открыть Mini App", "app", web_app=WebAppInfo(url=config.WEBAPP_PAGE_URL)))
    if is_admin:
        b.row(btn("Админ-панель", "admin", callback_data="nav:admin"))
    return b.as_markup()


# ===== Аккаунты =====
def accounts_kb(sessions):
    b = InlineKeyboardBuilder()
    b.row(btn("Добавить аккаунт", "account", callback_data="acc:add"))
    for s in sessions:
        b.row(btn("Проверить %s" % (s.get("phone") or s["id"]), "key",
                  callback_data="acc:spam:%s" % s["id"]),
              btn("Удалить", "trash", callback_data="acc:del:%s" % s["id"]))
    _nav(b, back="nav:menu")
    return b.as_markup()


# ===== Мониторинг (список рассылок) =====
def monitor_kb(jobs):
    """По каждой активной рассылке — свои кнопки пауза/стоп."""
    b = InlineKeyboardBuilder()
    active = [j for j in jobs if j.status in ("running", "paused")]
    for j in active:
        if j.status == "running":
            b.row(
                btn("⏸ %s" % j.name, "pause", callback_data="mon:pause:%d" % j.id),
                btn("Открыть", "chart", callback_data="mon:open:%d" % j.id),
                btn("🛑", "stop", callback_data="mon:stop:%d" % j.id),
            )
        else:
            b.row(
                btn("▶️ %s" % j.name, "play", callback_data="mon:resume:%d" % j.id),
                btn("Открыть", "chart", callback_data="mon:open:%d" % j.id),
                btn("🛑", "stop", callback_data="mon:stop:%d" % j.id),
            )
    b.row(btn("Обновить", "refresh", callback_data="nav:monitor"))
    _nav(b, back="nav:menu")
    return b.as_markup()


def monitor_job_kb(job):
    """Детальный экран одной рассылки."""
    b = InlineKeyboardBuilder()
    if job and job.status == "running":
        b.row(btn("Пауза", "pause", callback_data="mon:pause:%d" % job.id),
              btn("Стоп", "stop", callback_data="mon:stop:%d" % job.id))
    elif job and job.status == "paused":
        b.row(btn("Продолжить", "play", callback_data="mon:resume:%d" % job.id),
              btn("Стоп", "stop", callback_data="mon:stop:%d" % job.id))
    if job:
        b.row(btn("Обновить", "refresh", callback_data="mon:open:%d" % job.id))
    _nav(b, back="nav:monitor")
    return b.as_markup()


# ===== Подписка / оплата =====
def payment_kb(with_nav=True):
    b = InlineKeyboardBuilder()
    # Автооплата — только Telegram Stars (мгновенно). Карта/крипта — вручную через @zucag.
    b.row(btn("Оплатить · %d ⭐️ Telegram Stars" % config.SUB_PRICE_STARS, "stars", callback_data="pay:stars"))
    b.row(btn("Карта / Крипта — вручную", "card", callback_data="pay:manual"))
    b.row(btn("Написать %s" % config.OWNER_CONTACT, "msg", url=config.OWNER_CONTACT_URL))
    if with_nav:
        _nav(b, back="nav:menu")
    return b.as_markup()


# ===== Админ-панель =====
def admin_kb():
    b = InlineKeyboardBuilder()
    b.row(
        btn("Пользователи", "users", callback_data="admin:users"),
        btn("Аккаунты", "accounts", callback_data="admin:accounts"),
    )
    b.row(
        btn("Профиль юзера", "users", callback_data="admin:userinfo"),
        btn("Логи действий", "logs", callback_data="admin:actions"),
    )
    b.row(
        btn("Выдать подписку", "gift", callback_data="admin:give"),
        btn("Забрать", "ban", callback_data="admin:take"),
    )
    b.row(btn("Массовая выдача", "gift", callback_data="admin:massgive"))
    b.row(
        btn("Забанить", "ban", callback_data="admin:ban"),
        btn("Разбанить", "ok", callback_data="admin:unban"),
    )
    b.row(btn("Сообщение всем", "msg", callback_data="admin:announce"))
    b.row(btn("Обновить", "refresh", callback_data="nav:admin"))
    _nav(b, back="nav:menu")
    return b.as_markup()


def admin_back_kb():
    b = InlineKeyboardBuilder()
    _nav(b, back="nav:admin")
    return b.as_markup()


# ===== Рассылка: выбор папок =====
def folders_kb(folders, chosen):
    """Без чекбокса и эмодзи. Выбранная папка помечается словом ВЫБРАНО."""
    b = InlineKeyboardBuilder()
    for f in folders:
        title = str(f["title"])
        if f["id"] in chosen:
            label = "ВЫБРАНО · %s" % title
            b.row(btn(label, None, callback_data="bc:fold:%s" % f["id"]))
        else:
            b.row(btn(title, "folder", callback_data="bc:fold:%s" % f["id"]))
    if chosen:
        b.row(btn("Дальше · выбрано %d" % len(chosen), "play", callback_data="bc:fold_done"))
    b.row(btn("По всем чатам", "rocket", callback_data="bc:all"))
    b.row(btn("Отмена", "cross", callback_data="nav:menu"))
    return b.as_markup()


# ===== Рассылка: выбор режима =====
def bc_mode_kb():
    """Режим рассылки: текст / медиа+текст / пересыл / текст+кнопки."""
    b = InlineKeyboardBuilder()
    b.row(btn("Только текст", "msg", callback_data="bc:mode:text"))
    b.row(btn("Медиа + текст", "app", callback_data="bc:mode:media"))
    b.row(btn("Пересыл по ссылке", "rocket", callback_data="bc:mode:forward"))
    b.row(btn("Текст + кнопки", "key", callback_data="bc:mode:buttons"))
    b.row(btn("Отмена", "cross", callback_data="nav:menu"))
    return b.as_markup()


def bc_buttons_kb(buttons):
    """Конструктор inline-кнопок под сообщением рассылки."""
    b = InlineKeyboardBuilder()
    for i, bt in enumerate(buttons or []):
        label = bt.get("text") or "Кнопка"
        b.row(btn(label, None, callback_data="bc:btn_del:%d" % i))
    b.row(btn("Добавить кнопку", "account", callback_data="bc:btn_add"))
    if buttons:
        b.row(btn("Готово · кнопок %d" % len(buttons), "play", callback_data="bc:btn_done"))
    b.row(btn("Без кнопок", "cross", callback_data="bc:btn_skip"))
    return b.as_markup()


def cancel_kb():
    b = InlineKeyboardBuilder()
    b.row(btn("Отмена", "cross", callback_data="nav:menu"))
    return b.as_markup()


def confirm_kb(autosub=True, autofolder=None, invisible_tags=False, paid=False, is_admin=False, n_variants=0):
    b = InlineKeyboardBuilder()
    b.row(btn("Запустить рассылку", "rocket", callback_data="bc:launch"))
    b.row(btn(("Автоподписка: ВКЛ" if autosub else "Автоподписка: ВЫКЛ"), "key", callback_data="bc:toggle_autosub"))
    b.row(btn((("Авто-папка: %s" % autofolder) if autofolder else "Авто-папка: выкл"), "folder", callback_data="bc:autofolder"))
    # PRO-функции. Для подписчиков — рабочие тумблеры; без подписки — «замок» как
    # витрина (мотивирует оформить подписку).
    if paid:
        b.row(btn(("Невидимые теги: ВКЛ" if invisible_tags else "Невидимые теги: ВЫКЛ"), "star", callback_data="bc:toggle_invis"))
    else:
        b.row(btn("Невидимые теги — PRO", "star", callback_data="bc:pro"))
    # AI-персонализация — только для PREMIUM (и админа).
    if paid or is_admin:
        b.row(btn("AI-персонализация текста", "star", callback_data="bc:ai"))
    else:
        b.row(btn("AI-персонализация — PRO", "star", callback_data="bc:pro"))
    # Мульти-текст (варианты) — премиум до 3, админ до 5. Снижает спам-блок.
    if paid or is_admin:
        b.row(btn("Варианты текста: %d" % n_variants, "msg", callback_data="bc:addvar"))
    b.row(btn("Сохранить в избранное", "star", callback_data="bc:save_tpl"))
    b.row(btn("Редактировать", "gear", callback_data="bc:edit"))
    _nav(b, back="nav:menu")
    return b.as_markup()


def ai_preview_kb():
    """Клавиатура под AI-предпросмотром: применить / перегенерировать / отмена."""
    b = InlineKeyboardBuilder()
    b.row(btn("Применить", "check", callback_data="bc:ai_apply"))
    b.row(btn("Перегенерировать", "rocket", callback_data="bc:ai_regen"))
    b.row(btn("Отмена", "cross", callback_data="bc:ai_cancel"))
    return b.as_markup()


def edit_kb():
    """Что именно редактировать."""
    b = InlineKeyboardBuilder()
    b.row(btn("Сообщение", "msg", callback_data="bc:edit:msg"))
    b.row(
        btn("Задержку", "timer", callback_data="bc:edit:delay"),
        btn("Циклы", "cycle", callback_data="bc:edit:cycles"),
    )
    b.row(btn("Назад к запуску", "back", callback_data="bc:confirm"))
    return b.as_markup()


def support_kb():
    """Экран поддержки: контакт + назад."""
    b = InlineKeyboardBuilder()
    b.row(btn("Написать в поддержку", "chat", url=config.OWNER_CONTACT_URL))
    if getattr(config, "SHOP_URL", ""):
        b.row(btn("Магазин", "gift", url=config.SHOP_URL))
    _nav(b, back="nav:menu")
    return b.as_markup()


def sub_expired_kb():
    """Клавиатура под сообщением «подписка закончилась»."""
    return payment_kb(with_nav=False)


def templates_kb(templates):
    """Список сохранённых шаблонов рассылки."""
    b = InlineKeyboardBuilder()
    for t in (templates or []):
        b.row(
            btn(str(t.get("name") or "Шаблон"), "msg", callback_data="tpl:use:%s" % t["id"]),
            btn("Удалить", "trash", callback_data="tpl:del:%s" % t["id"]),
        )
    _nav(b, back="nav:menu")
    return b.as_markup()


def back_only_kb(back="nav:menu"):
    b = InlineKeyboardBuilder()
    _nav(b, back=back)
    return b.as_markup()
