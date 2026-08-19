"""入口 —— 建 Bot/Dispatcher、注册中间件与路由、polling/webhook 启动。"""
from __future__ import annotations

import asyncio
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiohttp import web

from app.bot import admin
from app.bot.menu import setup_menu
from app.bot.whitelist import WhitelistMiddleware
from app.config import get_config
from app.logging import get_logger, setup_logging
from app.restart_notify import send_pending_restart_completion_notices
from app.services import Services

log = get_logger("main")


def build_dispatcher(svc: Services) -> Dispatcher:
    dp = Dispatcher()
    dp["svc"] = svc
    wl = WhitelistMiddleware(svc.config.telegram.whitelist_ids)
    dp.message.middleware(wl)
    dp.callback_query.middleware(wl)
    # 只注册轻量管理 router。旧对话、媒体、渠道/设置/会话/工具面板不再接入 Dispatcher。
    dp.include_router(admin.router)
    return dp


async def _drain_startup_backlog(bot: Bot, svc: Services) -> None:
    """消费 polling backlog，但不重放旧消息。

    TG 不再作为对话客户端；启动时只推进 getUpdates offset，避免重启前积压的
    普通文本、/new 或媒体消息在新进程里触发 Agent run。
    """
    offset: int | None = None
    drained = 0
    while True:
        updates = await bot.get_updates(offset=offset, timeout=0, limit=100, allowed_updates=["message"])
        if not updates:
            break
        drained += len(updates)
        offset = max(int(upd.update_id) for upd in updates) + 1
    if drained:
        log.info("已消费 Telegram 启动积压消息，不做重放", 数量=drained)


async def run() -> None:
    config = get_config()
    setup_logging(config.log_level)
    errors = config.validate_for_startup()
    if errors:
        for e in errors:
            log.error("启动配置校验失败", 原因=e)
        sys.exit(1)

    log.info("OpenBear 启动中", 模式=config.telegram.mode, 主力模型=config.models.primary)
    bot = Bot(config.telegram.bot_token, default=DefaultBotProperties(parse_mode=None))
    svc = Services(config, bot)
    await svc.startup()
    dp = build_dispatcher(svc)
    await setup_menu(bot)
    await send_pending_restart_completion_notices(bot, svc)

    try:
        if config.telegram.mode == "webhook":
            from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
            host = config.telegram.webhook_host.rstrip("/")
            secret = config.telegram.webhook_secret
            url = f"{host}/tg/{secret}"
            await bot.set_webhook(url, secret_token=secret, drop_pending_updates=False)
            app = web.Application()
            SimpleRequestHandler(dispatcher=dp, bot=bot,
                                 secret_token=secret).register(app, path=f"/tg/{secret}")
            setup_application(app, dp, bot=bot)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, host="0.0.0.0", port=config.telegram.webhook_port)
            await site.start()
            log.info("Webhook 已启动", 端口=config.telegram.webhook_port)
            await asyncio.Event().wait()
        else:
            await bot.delete_webhook(drop_pending_updates=False)
            await _drain_startup_backlog(bot, svc)
            log.info("Polling 模式启动")
            await dp.start_polling(bot, handle_as_tasks=True)
    finally:
        await svc.shutdown()
        await bot.session.close()
        log.info("OpenBear 已退出")


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        pass


if __name__ == "__main__":
    main()
