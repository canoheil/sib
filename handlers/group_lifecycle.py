"""Register groups and send the one-time Persian welcome on bot membership changes."""

import json
from typing import Any

from aiogram import Bot, Router
from aiogram.types import ChatMemberUpdated
from sqlalchemy import func, update
from sqlalchemy.dialects import mysql, sqlite

from sib.core.logging import logger
from sib.database.connection import get_session
from sib.database.models import AdminLog, Group

router = Router(name="group_lifecycle")

_WELCOME_FROM = {"left", "kicked", "restricted"}
_WELCOME_TO = {"member", "administrator"}
_REMOVED_TO = {"left", "kicked"}
_WELCOME_TEXT = "🍏 سلام! من سیب‌ام. چند پیام در گروه رد و بدل کنید تا آماده شوم."


def _status_value(member: Any) -> str | None:
    status = getattr(member, "status", None)
    return getattr(status, "value", status)


def _actor_id(update_event: ChatMemberUpdated) -> int:
    actor = getattr(update_event, "from_user", None)
    actor_id = getattr(actor, "id", None)
    if not isinstance(actor_id, int) or isinstance(actor_id, bool):
        return 0
    return actor_id


def _upsert_group_statement(session, *, group_id: int, title: str, username: str | None):
    """Build an atomic dialect-specific upsert, preserving welcomed_at on duplicates."""
    values = {
        "id": group_id,
        "title": title,
        "username": username,
        "is_active": True,
    }
    dialect = session.get_bind().dialect.name
    if dialect in ("mysql", "mariadb"):
        statement = mysql.insert(Group).values(**values)
        return statement.on_duplicate_key_update(
            title=statement.inserted.title,
            is_active=True,
        )
    if dialect == "sqlite":  # used by isolated lifecycle tests
        statement = sqlite.insert(Group).values(**values)
        return statement.on_conflict_do_update(
            index_elements=[Group.id],
            set_={"title": statement.excluded.title, "is_active": True},
        )
    raise RuntimeError(f"Unsupported group lifecycle database dialect: {dialect}")


async def _auto_register(update_event: ChatMemberUpdated, bot: Bot) -> None:
    chat = update_event.chat
    group_id = chat.id
    title = chat.title or ""
    username = getattr(chat, "username", None)
    actor_id = _actor_id(update_event)

    claimed_welcome = False
    try:
        async with get_session() as session:
            await session.execute(_upsert_group_statement(
                session,
                group_id=group_id,
                title=title,
                username=username,
            ))
            session.add(AdminLog(
                admin_id=actor_id,
                action="group_auto_registered",
                details=json.dumps(
                    {"group_id": group_id, "title": title},
                    ensure_ascii=False,
                ),
            ))
            # Claim the first welcome atomically before making the Telegram call.
            # This prevents duplicate messages from repeated/concurrent updates.
            result = await session.execute(
                update(Group)
                .where(Group.id == group_id, Group.welcomed_at.is_(None))
                .values(welcomed_at=func.now())
            )
            claimed_welcome = result.rowcount == 1
    except Exception:
        logger.exception("Group auto-registration failed: %s", group_id)
        return

    logger.info("Group auto-registered: %s %s", group_id, title)
    if claimed_welcome:
        try:
            await bot.send_message(
                chat_id=group_id,
                text=_WELCOME_TEXT,
                parse_mode=None,
            )
        except Exception:
            # Registration and the one-time claim remain committed even if the
            # bot cannot post (for example, if it lacks send-message rights).
            logger.exception("Group welcome message failed: %s", group_id)


async def _auto_deactivate(update_event: ChatMemberUpdated) -> None:
    group_id = update_event.chat.id
    try:
        async with get_session() as session:
            await session.execute(
                update(Group)
                .where(Group.id == group_id)
                .values(is_active=False)
            )
            session.add(AdminLog(
                admin_id=_actor_id(update_event),
                action="group_deactivated",
                details=json.dumps({"group_id": group_id}, ensure_ascii=False),
            ))
    except Exception:
        logger.exception("Group auto-deactivation failed: %s", group_id)
        return

    logger.info("Group auto-deactivated: %s", group_id)


@router.my_chat_member()
async def handle_my_chat_member(update: ChatMemberUpdated, bot: Bot) -> None:
    """Process bot membership changes without letting failures escape dispatch."""
    try:
        chat_type = getattr(update.chat.type, "value", update.chat.type)
        if chat_type not in ("group", "supergroup"):
            return

        old_member = update.old_chat_member
        new_member = update.new_chat_member
        new_user = getattr(new_member, "user", None)
        old_user = getattr(old_member, "user", None)
        if (
            new_user is None
            or not getattr(new_user, "is_bot", False)
            or getattr(old_user, "id", None) != getattr(new_user, "id", None)
        ):
            return

        old_status = _status_value(old_member)
        new_status = _status_value(new_member)
        if old_status == new_status:
            return

        if old_status in _WELCOME_FROM and new_status in _WELCOME_TO:
            await _auto_register(update, bot)
        elif new_status in _REMOVED_TO:
            await _auto_deactivate(update)
    except Exception:
        group_id = getattr(getattr(update, "chat", None), "id", "unknown")
        logger.exception("Group lifecycle update failed: %s", group_id)
