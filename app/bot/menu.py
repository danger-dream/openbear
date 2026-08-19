"""命令菜单 —— 启动时清理旧菜单 + 注册自有命令。"""
from __future__ import annotations

from aiogram import Bot
from aiogram.types import BotCommand

from app.logging import get_logger

log = get_logger("bot.menu")

_COMMANDS = [
    BotCommand(command="status", description="查看 OpenBear 总体状态"),
    BotCommand(command="restart", description="重启 OpenBear 服务"),
    BotCommand(command="web", description="查看 Web 管理入口"),
    BotCommand(command="memory", description="记忆服务设置"),
]


async def setup_menu(bot: Bot) -> None:
    """先清旧菜单，再注册自有命令（失败不阻断启动）。"""
    try:
        await bot.delete_my_commands()
        await bot.set_my_commands(_COMMANDS)
        log.info("命令菜单已注册", 命令数=len(_COMMANDS))
    except Exception as e:
        log.warning("命令菜单注册失败(忽略)", 错误=str(e)[:120])
