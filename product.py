"""Shared product rules. Applies identically to chat, Mini App and scheduler."""
import html
import re
import config
import database as db

FOOTER = 'Отправлено с помощью @SendFlowTgBot'
MAX_CYCLES = 999

def branded(text):
    text = text or ''
    # Never duplicate the exact footer, including when a saved template already has it.
    text = re.sub(r'(?:\n\s*)*' + re.escape(FOOTER) + r'\s*$', '', text).rstrip()
    return (text + '\n\n' if text else '') + FOOTER

def visible_units(text):
    clean = html.unescape(re.sub(r'<[^>]*>', '', text or ''))
    return len(clean.encode('utf-16-le')) // 2

def with_links(text,buttons):
    from urllib.parse import urlsplit
    links=[]
    for button in buttons or []:
        url=str(button.get('url') or '')
        parsed=urlsplit(url)
        if parsed.scheme not in ('https','http','tg') or not (parsed.netloc or parsed.path): raise ValueError('Некорректная ссылка кнопки')
        links.append('<a href="%s">%s</a>' % (html.escape(url,quote=True),html.escape(str(button.get('text') or 'Открыть'))))
    return (text or '')+('\n\n'+'\n'.join(links) if links else '')


def validate_payload(payload):
    kind = payload.get('type', 'text')
    if kind not in ('text', 'photo', 'video', 'forward'):
        raise ValueError('Этот тип сообщения пока не поддерживается')
    if kind == 'forward':
        if not payload.get('from_chat') or not payload.get('msg_id'): raise ValueError('Не задано исходное сообщение')
        return
    if kind in ('photo', 'video'):
        import os
        if not os.path.isfile(payload.get('path') or ''): raise ValueError('Медиафайл недоступен. Загрузите его заново')
    limit = 1024 if kind in ('photo', 'video') else 4096
    for text in [payload.get('text', '')] + list(payload.get('variants') or []):
        if visible_units(branded(with_links(text,payload.get("buttons")))) > limit:
            raise ValueError(f'Слишком длинное сообщение: с подписью допускается до {limit} символов UTF-16')
    if len(payload.get('variants') or []) > 3: raise ValueError('Не более трёх дополнительных вариантов')

def require_access(uid):
    if db.is_banned(uid): raise ValueError('Доступ заблокирован')
    if uid not in config.ADMIN_IDS and not db.is_subscribed(uid): raise ValueError('Нужен активный PRO или пробный период')

def validate_launch(uid, payload, cycles, cooldown, folder_ids=None, account_ids=None):
    require_access(uid)
    validate_payload(payload)
    if not 1 <= int(cycles) <= MAX_CYCLES: raise ValueError('Число циклов: от 1 до 999')
    if not config.MIN_COOLDOWN <= int(cooldown) <= 86400: raise ValueError('Некорректная задержка')
    if len(folder_ids or []) > config.PAID_MAX_FOLDERS: raise ValueError('Слишком много папок')
    sessions = db.get_sessions(uid)
    if account_ids:
        ids = {int(i) for i in account_ids}
        if not ids.issubset({s['id'] for s in sessions}): raise ValueError('Аккаунт не найден')
        sessions = [s for s in sessions if s['id'] in ids]
    if account_ids and any(s.get('status')!='active' for s in sessions): raise ValueError('Выбранный аккаунт отключён или ожидает отзыва доступа')
    sessions=[s for s in sessions if s.get('status')=='active']
    if not sessions: raise ValueError('Сначала подключите активный аккаунт')
    if len(sessions) > config.PAID_MAX_ACCOUNTS: raise ValueError('Выберите аккаунты в пределах лимита PRO')
    return sessions
