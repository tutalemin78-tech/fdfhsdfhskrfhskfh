# -*- coding: utf-8 -*-
"""Бесплатная AI-персонализация текста рассылки.

Работает с любым OpenAI-совместимым API. По умолчанию — Groq
(бесплатный и быстрый). Ключ кладётся в .env:  AI_API_KEY=gsk_...
Где взять бесплатно: https://console.groq.com  ->  API Keys  ->  Create API Key.
Можно подменить провайдера через AI_BASE_URL и AI_MODEL (см. config.py).

Важно: текст рассылки уходит в Telegram с parse_mode=HTML, поэтому AI обязан
возвращать готовую HTML-разметку Telegram (<b>, <i>, <u>, <s>, <blockquote>),
а НЕ markdown (никаких ** и __ — они отрендерятся как обычные звёздочки).
"""
import re
import random
import aiohttp
import config

try:
    from emoji import em as _em
except Exception:
    def _em(_k):
        return ""

SYSTEM_PROMPT = (
    "Ты — элитный копирайтер и редактор продающих Telegram-рассылок мирового уровня. "
    "Твоя задача — переписать присланный текст так, чтобы он стал в РАЗЫ сильнее, "
    "цепляющим, премиальным и неотразимым, сохранив исходный смысл и ЯЗЫК оригинала.\n"
    "Сделай так, чтобы с первой строки невозможно было пройти мимо.\n\n"
    "ПРАВИЛА ОФОРМЛЕНИЯ (только HTML-разметка Telegram, НЕ markdown):\n"
    "- <b>жирным</b> выделяй заголовок и ключевую выгоду;\n"
    "- <i>курсивом</i> — эмоции и акценты;\n"
    "- <u>подчёркиванием</u> — важные детали;\n"
    "- <s>зачёркиванием</s> — старую цену или «было», если уместно;\n"
    "- <blockquote>цитатой</blockquote> — главный оффер или гарантию;\n"
    "- <code>моноширинным</code> — промокод, цифры или условия, если есть.\n"
    "Используй РАЗНЫЕ виды форматирования вместе — текст должен выглядеть дорого и живо.\n\n"
    "СТРУКТУРА:\n"
    "1) мощный цепляющий заголовок с эмодзи;\n"
    "2) 2-4 коротких пункта с выгодами (каждый начинается с эмодзи);\n"
    "3) лёгкое усиление доверия (соц. доказательство или гарантия);\n"
    "4) чёткий и сильный призыв к действию в конце.\n\n"
    "ЭМОДЗИ: добавляй уместные эмодзи в заголовок и в начало пунктов "
    "(🚀 ⭐️ ✅ 🎁 💬 📊 🔥 💎 🔑 ⏱) — они сделают текст ярким и премиальным. "
    "Не переусердствуй: примерно 1 эмодзи на строку-пункт.\n\n"
    "Каждый раз выдавай ЗАМЕТНО НОВЫЙ вариант: другой заголовок, другие формулировки, "
    "другой порядок строк. Никогда не повторяй предыдущий вариант.\n"
    "Верни ТОЛЬКО готовый текст рассылки в HTML. Без пояснений, без markdown "
    "(никаких ** или __), без тройных кавычек и без обрамляющих кавычек."
)

STYLE_HINTS = {
    "sell": "Стиль: продающий, цепляющий, с сильным призывом к действию.",
    "official": "Стиль: официальный, деловой, уважительный.",
    "friendly": "Стиль: дружелюбный, живой, на «ты», с лёгкими эмодзи.",
}


def is_configured():
    """AI готов к работе только если задан ключ."""
    return bool(getattr(config, "AI_API_KEY", ""))


def _clean(out):
    """Подчищаем ответ модели: убираем ```-ограждения и markdown-жирный/курсив,
    чтобы в Telegram HTML всё рендерилось корректно."""
    out = (out or "").strip()
    if out.startswith("```"):
        out = re.sub(r"^```[a-zA-Z]*\n?", "", out)
        out = re.sub(r"\n?```$", "", out).strip()
    # markdown -> HTML (на случай если модель всё же добавила ** или __)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out, flags=re.S)
    out = re.sub(r"__(.+?)__", r"<u>\1</u>", out, flags=re.S)
    return out.strip()


# unicode-эмодзи -> ключ премиум-эмодзи (emoji.py). После генерации подменяем
# обычные эмодзи на премиальные (анимированные) теги — текст выглядит «дорого».
_PREMIUM_MAP = {
    "🚀": "rocket", "⭐️": "star", "⭐": "star", "✅": "ok", "✔️": "check",
    "🎁": "gift", "💬": "chat", "📊": "chart", "⚠️": "warn", "⏱": "timer",
    "⏳": "wait", "🔁": "cycle", "📁": "folder", "🔑": "key", "💳": "card",
    "🪙": "crypto", "🤝": "ref", "👥": "accounts",
}


def _premiumize(text):
    """Заменяет обычные эмодзи на премиум-теги Telegram (если включены)."""
    if not text or not getattr(config, "PREMIUM_EMOJI", False):
        return text
    for ch, key in _PREMIUM_MAP.items():
        if ch in text:
            tag = _em(key)
            if tag and tag != ch:
                text = text.replace(ch, tag)
    return text


async def personalize(text, style="sell"):
    """Возвращает улучшенный HTML-текст или None (если AI не настроен/недоступен).

    Каждый вызов даёт новый вариант: высокая температура + случайный «нонс»,
    который заставляет модель не повторяться при «Перегенерировать».
    """
    if not text or not text.strip():
        return None
    if not is_configured():
        return None
    hint = STYLE_HINTS.get(style, STYLE_HINTS["sell"])
    nonce = random.randint(1000, 999999)
    payload = {
        "model": config.AI_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT + " " + hint},
            {"role": "user", "content": text},
            {"role": "system", "content": "Сгенерируй свежий вариант №%d, отличающийся от любых предыдущих." % nonce},
        ],
        "temperature": 1.0,
        "top_p": 0.95,
    }
    headers = {
        "Authorization": "Bearer %s" % config.AI_API_KEY,
        "Content-Type": "application/json",
    }
    url = config.AI_BASE_URL.rstrip("/") + "/chat/completions"
    try:
        timeout = aiohttp.ClientTimeout(total=45)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(url, json=payload, headers=headers) as r:
                if r.status != 200:
                    return None
                data = await r.json()
                choices = data.get("choices") or []
                if not choices:
                    return None
                out = (choices[0].get("message") or {}).get("content") or ""
                out = _clean(out)
                out = _premiumize(out)
                return out or None
    except Exception:
        return None
