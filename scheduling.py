# -*- coding: utf-8 -*-
"""Persistent timezone-aware scheduler. Missed jobs are paused, never bulk-replayed."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import database as db

def parse_local(value, tzname):
    zone = ZoneInfo(tzname)
    naive = datetime.fromisoformat(value)
    if naive.tzinfo is not None:
        raise ValueError('Укажите местное время без часового смещения')
    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)
    if first.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != naive:
        raise ValueError('Такого местного времени нет из-за перехода на летнее время')
    if first.utcoffset() != second.utcoffset():
        raise ValueError('Время неоднозначно из-за перевода часов. Выберите другое время')
    return first.astimezone(timezone.utc)

def next_occurrence(previous, tzname, kind, weekdays, now=None):
    if kind == 'once':
        return None
    zone = ZoneInfo(tzname)
    wall = previous.astimezone(zone)
    now = now or datetime.now(timezone.utc)
    for days in range(1, 370):
        candidate = wall.replace(tzinfo=None) + timedelta(days=days)
        if kind == 'weekly' and candidate.weekday() not in weekdays:
            continue
        try:
            value = parse_local(candidate.isoformat(), tzname)
        except ValueError:
            continue
        if value > now:
            return value
    raise ValueError('Не удалось рассчитать следующую отправку')

def create(uid, launch, local_at, tzname, repeat_kind='once', weekdays=None):
    import product
    product.validate_launch(uid, launch['payload'], launch['cycles'], launch['cooldown'], launch.get('folder_ids'), launch.get('account_ids'))
    when = parse_local(local_at, tzname)
    if when <= datetime.now(timezone.utc) + timedelta(seconds=30):
        raise ValueError('Выберите время минимум на 30 секунд позже текущего')
    if repeat_kind not in ('once', 'daily', 'weekly'):
        raise ValueError('Неизвестный повтор')
    weekdays = sorted(set((int(x) for x in weekdays or [])))
    if repeat_kind == 'weekly' and (not weekdays or any((x not in range(7) for x in weekdays))):
        raise ValueError('Выберите дни недели')
    if repeat_kind == 'weekly' and when.astimezone(ZoneInfo(tzname)).weekday() not in weekdays:
        raise ValueError('Первая дата должна соответствовать выбранному дню недели')
    with db.transaction() as cur:
        db._lock_user(cur, uid)
        cur.execute("SELECT count(*) AS n FROM scheduled_jobs WHERE user_id=%s AND status IN ('pending','paused','dispatching')", (uid,))
        if cur.fetchone()['n'] >= 50:
            raise ValueError('Не более 50 активных расписаний')
        cur.execute('INSERT INTO scheduled_jobs(user_id,config,next_at,timezone,repeat_kind,weekdays) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id', (uid, json.dumps(launch, ensure_ascii=False), when, tzname, repeat_kind, json.dumps(weekdays)))
        return cur.fetchone()['id']

def list_for(uid):
    rows = db._fetchall("SELECT id,next_at,timezone,repeat_kind,weekdays,status,last_error,config FROM scheduled_jobs WHERE user_id=%s AND status NOT IN ('cancelled','done') ORDER BY next_at LIMIT 50", (uid,))
    return [{'id': r['id'], 'next_at': r['next_at'].isoformat(), 'timezone': r['timezone'], 'repeat_kind': r['repeat_kind'], 'weekdays': json.loads(r['weekdays']), 'status': r['status'], 'last_error': r['last_error'], 'text': json.loads(r['config']).get('payload', {}).get('text', '')[:120]} for r in rows]

def cancel(uid, sid):
    return bool(db._fetchone("UPDATE scheduled_jobs SET status='cancelled' WHERE id=%s AND user_id=%s AND status NOT IN ('cancelled','done') RETURNING id", (sid, uid)))

def reschedule(uid, sid, local_at, tzname):
    when = parse_local(local_at, tzname)
    if when <= datetime.now(timezone.utc) + timedelta(seconds=30):
        raise ValueError('Укажите будущее время')
    row = db._fetchone('SELECT repeat_kind,weekdays FROM scheduled_jobs WHERE id=%s AND user_id=%s', (sid, uid))
    if not row:
        raise ValueError('Расписание не найдено')
    if row['repeat_kind'] == 'weekly' and when.astimezone(ZoneInfo(tzname)).weekday() not in json.loads(row['weekdays']):
        raise ValueError('Дата не соответствует дням недели')
    return bool(db._fetchone("UPDATE scheduled_jobs SET next_at=%s,timezone=%s,status='pending',last_error=NULL WHERE id=%s AND user_id=%s AND status IN ('pending','paused') RETURNING id", (when, tzname, sid, uid)))

async def loop(bot):
    import userbot_worker as worker
    await asyncio.to_thread(db._exec, "UPDATE scheduled_jobs SET status='paused',last_error='Запуск прерван перезапуском. Проверьте мониторинг и перенесите задачу' WHERE status='dispatching'")
    while True:
        try:
            with_claim = await asyncio.to_thread(db._fetchall, "UPDATE scheduled_jobs SET status='dispatching' WHERE id IN (SELECT id FROM scheduled_jobs WHERE status='pending' AND next_at<=now() ORDER BY next_at FOR UPDATE SKIP LOCKED LIMIT 20) RETURNING *")
            for row in with_claim:
                try:
                    if (datetime.now(timezone.utc) - row['next_at']).total_seconds() > 120:
                        raise ValueError('Время отправки пропущено. Перенесите задачу — автоматической отправки не было')
                    launch = json.loads(row['config'])
                    await worker.start_broadcast_task(user_id=row['user_id'], **launch)
                    nxt = next_occurrence(row['next_at'], row['timezone'], row['repeat_kind'], json.loads(row['weekdays']))
                    await asyncio.to_thread(db._exec, 'UPDATE scheduled_jobs SET status=%s,next_at=COALESCE(%s,next_at),last_error=NULL WHERE id=%s', ('pending' if nxt else 'done', nxt, row['id']))
                    await bot.send_message(row['user_id'], f"🕒 Рассылка по расписанию #{row['id']} запущена. Результат — в мониторинге.")
                except Exception as exc:
                    text = str(exc) if isinstance(exc, ValueError) else 'Ошибка запуска. Проверьте мониторинг и перенесите задачу'
                    await asyncio.to_thread(db._exec, "UPDATE scheduled_jobs SET status='paused',last_error=%s WHERE id=%s", (text[:250], row['id']))
                    try:
                        await bot.send_message(row['user_id'], f"⚠️ Расписание #{row['id']} приостановлено. {text}", parse_mode=None)
                    except Exception:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception:
            import logging
            logging.getLogger('sendflow.schedule').warning('Scheduler iteration failed')
        await asyncio.sleep(5)
