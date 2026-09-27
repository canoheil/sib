"""Robust Telegram presentation helpers.

The database is always the authoritative source of game/economy state.
Presentation failures (transient API errors, rate limits, unparseable
Markdown, deleted messages) must NEVER corrupt or roll back committed
state. These helpers provide:

- ``safe_edit_message_text``: retrying, self-healing message editor.
- ``safe_answer_callback``: never-raising callback acknowledgement.
- ``with_refresh_button``: recovery button that re-renders a board from
  the authoritative DB state when the on-screen board went stale.

Supported games and every interactive panel should render through these
helpers instead of calling ``bot.edit_message_text`` directly.
"""

import asyncio
from typing import Awaitable, Callable, Optional

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNotFound,
    TelegramRetryAfter,
)
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from sib.core.logging import logger

REFRESH_CALLBACK_PREFIX = "game_refresh:"
# "message is not modified" == content already current (success).
# "message can't be edited" is NOT in here on purpose: the edit failed and
# the visible content may be stale - it degrades gracefully below instead
# of being masked as a success.
NOT_MODIFIED_MARKERS = ("message is not modified",)
PARSE_ERROR_MARKERS = ("can't parse entities", "unsupported start tag")


def with_refresh_button(
    keyboard: Optional[InlineKeyboardMarkup],
    match_id: int,
) -> InlineKeyboardMarkup:
    """Append a board-refresh row to an existing inline keyboard.

    The refresh callback re-renders the board from the database, giving
    users a safe recovery path whenever the rendered message went stale
    (e.g. after a transient Telegram edit failure).
    """
    rows = list(keyboard.inline_keyboard) if keyboard else []
    if not rows or rows[-1][0].callback_data != f"{REFRESH_CALLBACK_PREFIX}{match_id}":
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔄 بروزرسانی صفحه",
                    callback_data=f"{REFRESH_CALLBACK_PREFIX}{match_id}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _run_edit_attempts(
    edit_factory: Callable[[], Awaitable],
    fallback_factory: Optional[Callable[[], Awaitable]] = None,
    max_attempts: int = 3,
    context: str = "message",
) -> bool:
    """Execute an edit coroutine with retries and graceful degradation.

    ``edit_factory`` must perform the edit. ``fallback_factory`` (optional)
    performs a degraded edit (e.g. without parse_mode) when the primary
    edit is rejected because of unparseable Markdown.
    """
    last_error: Optional[Exception] = None

    for attempt in range(max_attempts):
        try:
            await edit_factory()
            return True
        except TelegramRetryAfter as e:
            last_error = e
            logger.warning(
                f"Flood wait while editing {context}; retrying in {e.retry_after}s"
            )
            await asyncio.sleep(min(float(e.retry_after) + 0.5, 10.0))
        except TelegramBadRequest as e:
            message = str(e).lower()
            if any(marker in message for marker in NOT_MODIFIED_MARKERS):
                # Content already matches: not a failure.
                return True
            if any(marker in message for marker in PARSE_ERROR_MARKERS):
                logger.warning(f"Markdown parse failure for {context}: {e}")
                if fallback_factory is not None:
                    try:
                        await fallback_factory()
                        return True
                    except Exception as fallback_error:
                        last_error = fallback_error
                        logger.warning(
                            f"Fallback edit without parse_mode also failed for {context}: "
                            f"{fallback_error}"
                        )
                break
            logger.warning(f"Non-retryable edit failure for {context}: {e}")
            return False
        except (TelegramForbiddenError, TelegramNotFound) as e:
            logger.warning(f"Cannot edit {context}: {e}")
            return False
        except Exception as e:
            last_error = e
            logger.warning(
                f"Transient edit failure for {context} "
                f"(attempt {attempt + 1}/{max_attempts}): {e}"
            )
            await asyncio.sleep(0.4 * (attempt + 1))

    logger.error(f"Giving up editing {context}: {last_error}")
    return False


async def safe_edit_message_text(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: Optional[str] = "Markdown",
    max_attempts: int = 3,
) -> bool:
    """Edit a message with retries and graceful degradation.

    Returns True when the message content is guaranteed to be current
    (edit succeeded, or the message already had identical content).
    Returns False only after every recovery path was exhausted, in which
    case the stale board can still be fixed through the refresh button.
    """
    return await _run_edit_attempts(
        lambda: bot.edit_message_text(
            text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        ),
        fallback_factory=lambda: bot.edit_message_text(
            text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
            parse_mode=None,
        ),
        max_attempts=max_attempts,
        context=f"message {message_id} in chat {chat_id}",
    )


async def safe_answer_callback(
    callback: CallbackQuery,
    text: Optional[str] = None,
    show_alert: bool = False,
) -> None:
    """Acknowledge a callback query without ever raising."""
    try:
        await callback.answer(text, show_alert=show_alert)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"Failed to answer callback query: {e}")


async def safe_edit_callback_message(
    callback: CallbackQuery,
    bot: Bot,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: Optional[str] = "Markdown",
) -> bool:
    """Edit the message behind a callback query via the robust editor.

    Falls back to the ``Message.edit_text`` API when the message handle
    does not expose chat/message identifiers.
    """
    message = callback.message
    if message is None:
        return False

    message_id = getattr(message, "message_id", None)
    chat_id = None
    chat = getattr(message, "chat", None)
    if chat is not None:
        chat_id = getattr(chat, "id", None)

    if message_id and chat_id:
        return await safe_edit_message_text(
            bot,
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )

    async def _direct_edit():
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)

    async def _direct_edit_plain():
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=None)

    return await _run_edit_attempts(
        _direct_edit,
        fallback_factory=_direct_edit_plain,
        context="callback message",
    )
