"""Render rolling-deploy handover: HTTP liveness before exclusive bot ownership."""
import asyncio
import logging
import time
from aiohttp import web
import config
import database as db
import security

log=logging.getLogger('sendflow.startup')

async def open_frontend(bot, build_app):
    # Fail clearly on missing settings before advertising HTTP liveness.
    db.validate_lock_connection()
    security.cipher()
    state={'ready':False,'message':'Обновление сервиса: ожидаю остановки предыдущего экземпляра'}
    @web.middleware
    async def gate(request,handler):
        if request.path=='/healthz':
            # Liveness only, deliberately not a promise that polling is active.
            return web.json_response({'status':'ready' if state['ready'] else 'starting'},headers={'Cache-Control':'no-store'})
        if request.path=='/readyz':
            return web.json_response({'ready':state['ready']},status=200 if state['ready'] else 503,headers={'Cache-Control':'no-store'})
        if request.path.startswith('/api/') and not state['ready']:
            return web.json_response({'ok':False,'error':'starting','message':state['message']},status=503,headers={'Retry-After':'5','Cache-Control':'no-store'})
        return await handler(request)
    app=build_app(bot)
    app.middlewares.insert(0,gate)
    runner=web.AppRunner(app)
    await runner.setup()
    try:
        await web.TCPSite(runner,config.WEBAPP_HOST,config.WEBAPP_PORT).start()
    except BaseException:
        await runner.cleanup()
        raise
    log.info('HTTP запущен на порту %s; /healthz — liveness, /readyz — готовность бота',config.WEBAPP_PORT)
    return runner,state

async def wait_for_primary(timeout=180,interval=3):
    deadline=time.monotonic()+timeout
    warned=False
    while True:
        try:
            await asyncio.to_thread(db.acquire_instance_lock)
            return
        except db.InstanceAlreadyRunning:
            if not warned:
                log.warning('Ожидаю завершения старого процесса. Polling, рассылки и таймеры этого экземпляра ещё не запущены')
                warned=True
            if time.monotonic()>=deadline:
                raise RuntimeError('Блокировка занята более 180 секунд. Остановите другой сервис SendFlow с этой базой; не отключайте защиту') from None
            await asyncio.sleep(interval)
