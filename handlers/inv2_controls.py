"""Private admin-only invasion-v2 commands with durable per-command auditing."""
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from sib.admin.auth import IsAdminFilter, is_admin
from sib.core.logging import logger
from sib.database.connection import get_session
from sib.database.models import AdminLog
from sib.services.invasion_v2 import admin_controls as ops
from sib.services.invasion_v2.presentation import _telegram

router = Router(name="inv2_controls")
router.message.filter(IsAdminFilter())
router.callback_query.filter(IsAdminFilter())

COMMANDS = {
    "inv2_status": (ops.status, 0, 0),
    "inv2_toggle": (ops.toggle, 1, 1),
    "inv2_force_spawn": (ops.force_spawn, 1, 3),
    "inv2_force_end": (ops.force_end, 2, 2),
    "inv2_regions": (ops.regions, 0, 0),
    "inv2_reset_region": (ops.reset_region, 1, 1),
    "inv2_mute": (ops.mute, 2, 2),
    "inv2_unmute": (ops.unmute, 1, 1),
    "inv2_group_info": (ops.group_info, 1, 1),
    "inv2_invasions": (ops.invasions, 0, 1),
    "inv2_config": (ops.config, 0, 0),
    "inv2_set": (ops.set_config, 2, 2),
    "inv2_health": (ops.health, 0, 0),
}


def _private(event) -> bool:
    chat = getattr(event, "chat", None) or getattr(getattr(event, "message", None), "chat", None)
    chat_type = getattr(chat, "type", None)
    return getattr(chat_type, "value", chat_type) == "private"


async def _execute(admin_id: int, command: str, args: list[str], func, min_args: int, max_args: int) -> str:
    """Audit with the action in the same transaction; never expose tracebacks."""
    try:
        async with get_session() as session:
            session.add(AdminLog(admin_id=admin_id, action=command,
                                 details=" ".join(args)[:500]))
            if not min_args <= len(args) <= max_args:
                return f"⚠️ شمار ورودی‌ها درست نیست؛ راهنمای /{command} را ببینید."
            try:
                return await func(session, *args)
            except (ValueError, OverflowError, TypeError):
                return "⚠️ ورودی نامعتبر است؛ شناسه‌ها و مقادیر را بررسی کنید."
    except Exception:
        logger.exception("inv2 admin command failed: %s", command)
        return "⚠️ عملیات انجام نشد؛ بعداً دوباره تلاش کنید."


async def _run(admin_id: int, command: str, args: list[str]) -> str:
    return await _execute(admin_id, command, args, *COMMANDS[command])


@router.message(Command(*COMMANDS.keys()))
async def inv2_command(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        if _private(message):
            await _telegram(None, message.answer, text="دسترسی ندارید.", parse_mode=None)
        return
    if not _private(message):
        return  # never send admin state or trigger an action from a group
    parts = (message.text or "").split()
    command = parts[0].split("@", 1)[0].lstrip("/") if parts else ""
    if command not in COMMANDS:
        return
    response = await _run(message.from_user.id, command, parts[1:])
    # Config commit must precede invalidation; a failed/invalid write cannot
    # claim to change the value. Other admin operations do not change the flag.
    if command in ("inv2_toggle", "inv2_set") and response.startswith(("⚔️ تهاجم", "⚙️ inv2_")):
        from sib.services.maintenance.service import invalidate_invasion_config_cache
        invalidate_invasion_config_cache()
    await _telegram(None, message.answer, text=response[:4000], parse_mode=None)


@router.message(Command("inv2_simulate"))
async def simulate_command(message: Message) -> None:
    if not message.from_user or not is_admin(message.from_user.id):
        if _private(message):
            await _telegram(None, message.answer, text="دسترسی ندارید.", parse_mode=None)
        return
    if not _private(message):
        return
    parts = (message.text or "").split()
    args = parts[1:]
    # Accept the old Phase 5 optional user-count argument for compatibility;
    # Phase 6 always reports N=1,3,5,10, never an arbitrary supplied count.
    if len(args) == 3:
        try:
            int(args[2])
        except ValueError:
            pass
        else:
            args = args[:2]
    from sib.services.invasion_v2.simulation import simulate
    result = await _execute(message.from_user.id, "inv2_simulate", args, simulate, 1, 2)
    await _telegram(None, message.answer, text=result[:4000], parse_mode=None)
