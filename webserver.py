"""Tiny health-check web server + optional keep-alive pinger.

* Render Web Services must listen on $PORT, otherwise the deploy is marked as failed.
* Render FREE services spin down after 15 minutes without inbound HTTP traffic, so the bot
  pings its own public URL every few minutes (KEEP_ALIVE=true). Not needed on a VPS or on
  a paid Render instance.
"""
import asyncio
import logging

import aiohttp
from aiohttp import web

import config
from tasks import TASKS
from utils import uptime

log = logging.getLogger("web")


async def _index(_request):
    return web.Response(text=f"{config.BOT_NAME} is alive and running!")


async def _health(_request):
    return web.json_response({"status": "ok", "uptime": uptime(), "active_tasks": len(TASKS), "version": config.VERSION})


async def start_web_server() -> web.AppRunner:
    app = web.Application()
    app.add_routes([web.get("/", _index), web.get("/health", _health)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, config.HOST, config.PORT).start()
    log.info("Health server listening on %s:%s", config.HOST, config.PORT)
    return runner


async def keep_alive_loop() -> None:
    url = f"{config.APP_URL}/health"
    log.info("Keep-alive enabled -> pinging %s every %ss", url, config.PING_INTERVAL)
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            await asyncio.sleep(config.PING_INTERVAL)
            try:
                async with session.get(url) as response:
                    log.debug("keep-alive ping -> %s", response.status)
            except Exception as exc:
                log.warning("keep-alive ping failed: %s", exc)
