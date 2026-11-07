# -*- coding: utf-8 -*-
"""
Центральная конфигурация SendFlow.

Все секреты берутся из переменных окружения (.env локально / Render Environment).
В коде НЕТ боевых токенов и реквизитов — это безопасно для публичного репозитория.
Для локального теста положи .env рядом (см. .env.example).
"""
import os

try:
    from dotenv import load_dotenv
    _dotenv_ok = load_dotenv()  # читает .env рядом с файлом
    if not _dotenv_ok:
        print("[config] ⚠ файл .env не найден рядом с main.py — беру переменные из окружения")
except ImportError:
    print("[config] ⚠⚠ БИБЛИОТЕКА python-dotenv НЕ УСТАНОВЛЕНА — файл .env НЕ будет прочитан!")
    print("[config]    Исправь так:  pip install -r requirements.txt")


def _bool(name, default="1"):
    return os.getenv(name, default) not in ("0", "false", "False", "", "no", "NO")


def _int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default


def _ids(s):
    out = []
    for p in (s or "").replace(";", ",").split(","):
        p = p.strip()
        if p.lstrip("-").isdigit():
            out.append(int(p))
    return out


# ===================== БОТ =====================
# Токен обязателен. Локально клади его в .env (BOT_TOKEN=...).
def _clean_token(raw):
    """Вычищает токен от частых ошибок копи-паста: кавычек,
    пробелов, переносов строки, BOM и невидимых символов."""
    t = (raw or "").strip()
    for q in ('"', "'", "«", "»", "“", "”", "`"):
        t = t.strip(q)
    for ch in ("\ufeff", "\u200b", "\u200e", "\u200f", " ", "\t", "\r", "\n"):
        t = t.replace(ch, "")
    return t


BOT_TOKEN = _clean_token(os.getenv("BOT_TOKEN", ""))
BOT_NAME = os.getenv("BOT_NAME", "SendFlow")


def require_token():
    """Понятная ошибка вместо невнятного краша."""
    if not BOT_TOKEN:
        raise RuntimeError(
            "\n❌ BOT_TOKEN пустой — бот не видит токен.\n"
            "Причина обычно одна из двух:\n"
            "  1) Не установлены библиотеки — выполни:  pip install -r requirements.txt\n"
            "  2) Файл назван не .env (а .env.txt) или лежит не рядом с main.py\n"
            "     Внутри .env должна быть строка:  BOT_TOKEN=токен_от_@BotFather"
        )
    import re as _re
    if not _re.fullmatch(r"\d{6,}:[A-Za-z0-9_-]{30,}", BOT_TOKEN):
        _m = BOT_TOKEN[:8] + "..." + BOT_TOKEN[-4:] if len(BOT_TOKEN) > 14 else BOT_TOKEN
        raise RuntimeError(
            "\n❌ BOT_TOKEN кривого формата (получено: %s)\n"
            "Нужный вид: 1234567890:ABCdefGhi... (цифры, двоеточие, буквы/цифры).\n"
            "Скопируй токен из @BotFather целиком, без пробелов и кавычек." % _m
        )


# ===================== АДМИНЫ =====================
# По умолчанию список пуст — задаётся через ADMIN_IDS="123,456".
ADMIN_IDS = _ids(os.getenv("ADMIN_IDS", "477250712"))

# ===================== ОБЯЗАТЕЛЬНАЯ ПОДПИСКА НА КАНАЛ =====================
# Бот ДОЛЖЕН быть админом этого канала, чтобы проверять подписку.
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@akvingr")
REQUIRED_CHANNEL_URL = os.getenv("REQUIRED_CHANNEL_URL", "https://t.me/akvingr")
FREE_TRIAL_DAYS = _int("FREE_TRIAL_DAYS", 3)

# ===================== МАГАЗИН =====================
SHOP_URL = os.getenv("SHOP_URL", "https://t.me/SendFlowShop")

# ===================== ПОДПИСКА / ОПЛАТА =====================
SUB_PRICE_STARS = _int("SUB_PRICE_STARS", 50)
SUB_DAYS = _int("SUB_DAYS", 30)

# Реквизиты ручной оплаты (пустые по умолчанию — заполни через env).
CARD_NUMBER = os.getenv("CARD_NUMBER", "")
CARD_HOLDER = os.getenv("CARD_HOLDER", "")
CARD_PRICE_RUB = _int("CARD_PRICE_RUB", 100)
UA_CARD_NUMBER = os.getenv("UA_CARD_NUMBER", "")
UA_CARD_HOLDER = os.getenv("UA_CARD_HOLDER", "")
UA_CARD_PRICE_UAH = _int("UA_CARD_PRICE_UAH", 60)
TON_WALLET = os.getenv("TON_WALLET", "")
TON_PRICE = os.getenv("TON_PRICE", "1")
CRYPTOBOT_TOKEN = os.getenv("CRYPTOBOT_TOKEN", "")
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "zucag")

# ===================== ТАРИФЫ =====================
# Бесплатный план (триал/рефералы):
FREE_MAX_ACCOUNTS = _int("FREE_MAX_ACCOUNTS", 2)      # макс. аккаунтов одновременно
FREE_DAILY_CYCLES = _int("FREE_DAILY_CYCLES", 200)    # лимит циклов в сутки (суммарно)
FREE_MAX_FOLDERS = _int("FREE_MAX_FOLDERS", 3)        # макс. папок в рассылке
FREE_MAX_TEMPLATES = _int("FREE_MAX_TEMPLATES", 5)    # макс. сохранённых шаблонов
FREE_COOLDOWN = _int("FREE_COOLDOWN", 3)              # мин. задержка между сообщениями

# Платный план:
PAID_MAX_ACCOUNTS = _int("PAID_MAX_ACCOUNTS", 5)      # макс. аккаунтов на подписке
PAID_MAX_FOLDERS = _int("PAID_MAX_FOLDERS", 50)       # фактически «без лимита»
PAID_COOLDOWN = _int("PAID_COOLDOWN", 1)              # подписчикам — быстрее

# Обратная совместимость со старым кодом
TRIAL_MAX_ACCOUNTS = FREE_MAX_ACCOUNTS
MAX_FOLDERS = FREE_MAX_FOLDERS
MIN_COOLDOWN = FREE_COOLDOWN
MAX_CONCURRENT_JOBS = _int("MAX_CONCURRENT_JOBS", 5)

# ===================== API_ID / API_HASH =====================
DEFAULT_API_ID = _int("DEFAULT_API_ID", 2040)
DEFAULT_API_HASH = os.getenv("DEFAULT_API_HASH", "b18441a1ff607e10a989891a5462e627")
SKIP_API_PROMPT = _bool("SKIP_API_PROMPT", "1")

# ===================== MINI APP =====================
WEBAPP_HOST = os.getenv("WEBAPP_HOST", "0.0.0.0")
WEBAPP_PORT = int(os.getenv("PORT") or os.getenv("WEBAPP_PORT") or "8080")


def _detect_webapp_url():
    """Адрес мини аппа. WEBAPP_URL имеет приоритет, иначе берём с облака."""
    u = os.getenv("WEBAPP_URL", "").strip()
    if u:
        return u.rstrip("/")
    u = os.getenv("RENDER_EXTERNAL_URL", "").strip()
    if u:
        return u.rstrip("/")
    for var in ("RAILWAY_PUBLIC_DOMAIN", "RAILWAY_STATIC_URL", "PUBLIC_DOMAIN", "APP_URL", "VIRTUAL_HOST"):
        d = os.getenv(var, "").strip()
        if d:
            d = d.replace("https://", "").replace("http://", "").rstrip("/")
            return "https://" + d
    return ""


WEBAPP_URL = _detect_webapp_url()
WEBAPP_PAGE_URL = (WEBAPP_URL + "/static/index.html?v=3") if WEBAPP_URL else ""
WEBAPP_API_URL = os.getenv("WEBAPP_API_URL", "").strip()
# Разрешённые Origin для CORS мини аппа (через запятую). По умолчанию — свой домен.
WEBAPP_ALLOWED_ORIGINS = [o.strip() for o in os.getenv("WEBAPP_ALLOWED_ORIGINS", WEBAPP_URL).split(",") if o.strip()]
# Максимальный возраст initData (анти-replay), сек. 0 = не проверять.
INITDATA_MAX_AGE = _int("INITDATA_MAX_AGE", 86400)

# ===================== ПРЕМИУМ-ЭМОДЗИ =====================
PREMIUM_EMOJI = _bool("PREMIUM_EMOJI", "1")
# Премиум-эмодзи «рука машет» для приветствия на старте (пак TgAndroidIcons).
HELLO_EMOJI_ID = "5906995262378741881"

# ===================== ПУБЛИЧНАЯ СТАТИСТИКА (/stats, Social Proof) =====================
# Базовые «витринные» числа, чтобы инструмент выглядел популярным.
# Реальные отправки бота прибавляются сверху.
SOCIAL_PROOF_BASE = _int("SOCIAL_PROOF_BASE", 1500000)        # за сегодня
SOCIAL_PROOF_TOTAL_BASE = _int("SOCIAL_PROOF_TOTAL_BASE", 87000000)  # за всё время

# ===================== AI-ПЕРСОНАЛИЗАЦИЯ =====================
# Бесплатный рабочий вариант — Groq (OpenAI-совместимый).
# Где взять ключ бесплатно: https://console.groq.com  →  API Keys  →  Create API Key
# Потом положи в .env:  AI_API_KEY=gsk_...
# Можно заменить провайдера: AI_BASE_URL + AI_MODEL (любой OpenAI-совместимый).
AI_API_KEY = os.getenv("AI_API_KEY", "").strip()
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.groq.com/openai/v1").strip()
AI_MODEL = os.getenv("AI_MODEL", "llama-3.3-70b-versatile").strip()

# ===================== КОНТАКТ / РЕФЕРАЛЫ =====================
OWNER_CONTACT = os.getenv("OWNER_CONTACT", "@zucag")
OWNER_CONTACT_URL = os.getenv("OWNER_CONTACT_URL", "https://t.me/zucag")
REF_HOURS = _int("REF_HOURS", 6)

# ===================== ХРАНИЛИЩЕ =====================
# На Render укажи DATA_DIR на постоянный диск (например /var/data),
# иначе при перезапуске данные стираются.
DATA_DIR = os.getenv("DATA_DIR", "").strip()


# ===================== СПРАВОЧНИК (TELEGRAPH) =====================
# Ссылка на Telegraph-страницу справочника. Создаётся автоматически
# при первом запуске (make_telegraph.ensure_help_page) и кэшируется.
HELP_URL = os.getenv("HELP_URL", "").strip()


def help_cache_path():
    base = DATA_DIR if DATA_DIR else "."
    return os.path.join(base, "telegraph_help.json")
