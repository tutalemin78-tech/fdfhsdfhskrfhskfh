"""Session encryption and short-lived Mini App authentication. No secret logging."""
import base64
import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qsl
from cryptography.fernet import Fernet, MultiFernet, InvalidToken

PREFIX = 'enc:v1:'
def cipher():
    keys = [k.strip() for k in os.environ.get('SESSION_ENCRYPTION_KEYS', '').split(',') if k.strip()]
    if not keys:
        raise RuntimeError('Задайте SESSION_ENCRYPTION_KEYS; инструкция в README_UPDATE.md')
    return MultiFernet([Fernet(k.encode('ascii')) for k in keys])

def encrypt(value):
    if value is None or value == '':
        return value
    if str(value).startswith(PREFIX):
        value = decrypt(value)
    return PREFIX + cipher().encrypt(str(value).encode()).decode()

def decrypt(value):
    if value is None or value == '':
        return value
    if not str(value).startswith(PREFIX):
        raise RuntimeError('Обнаружена незашифрованная сессия: выполните миграцию')
    try:
        return cipher().decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken:
        raise RuntimeError('Невозможно расшифровать сессию: проверьте ключ') from None

def verify_init_data(raw, token, max_age=900, now=None):
    if not isinstance(raw, str) or len(raw) > 16384 or not token:
        return None
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
        parsed = dict(pairs)
        if len(parsed) != len(pairs):
            return None
        received = parsed.pop('hash', '')
        check = '\n'.join(f'{k}={parsed[k]}' for k in sorted(parsed))
        secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received):
            return None
        now = time.time() if now is None else now
        age = now - int(parsed['auth_date'])
        if age < -30 or age > min(max(int(max_age), 1), 86400):
            return None
        user = json.loads(parsed['user'])
        if not isinstance(user, dict) or type(user.get('id')) is not int or user['id'] <= 0:
            return None
        return user
    except (ValueError, TypeError, KeyError):
        return None

def issue_token(user, bot_token, ttl=3600):
    payload = base64.urlsafe_b64encode(json.dumps({'id': user['id'], 'exp': int(time.time()) + ttl}, separators=(',', ':')).encode()).decode()
    sig = hmac.new(bot_token.encode(), ('sendflow-web-v1:' + payload).encode(), hashlib.sha256).hexdigest()
    return payload + '.' + sig

def read_token(value, bot_token):
    try:
        payload, sig = value.split('.', 1)
        expected = hmac.new(bot_token.encode(), ('sendflow-web-v1:' + payload).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected): return None
        obj = json.loads(base64.urlsafe_b64decode(payload))
        if obj['exp'] <= time.time() or type(obj['id']) is not int: return None
        return {'id': obj['id']}
    except (ValueError, TypeError, KeyError):
        return None
