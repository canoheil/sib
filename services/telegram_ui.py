"""Robust Telegram presentation helpers.

The database is always the authoritative source of game/economy state.
Presentation failures (transient API errors, rate limits, unparseable
Markdown, deleted messages) must NEVER corrupt or roll back committed
state. These helpers provide:

- ``safe_edit_message_text``: retrying, self-healing message editor.
- ``safe_answer_callback``: never-raising callback acknowledgement.
- ``with_refresh_button``: recovery button that re-renders a board from
  the authoritative DB state when the on-screen board went stale.
- ``render_screen`` / ``render_callback_screen``: the ONE message
  transition engine. Every screen transition (TEXT↔TEXT, TEXT↔PHOTO,
  PHOTO↔PHOTO) goes through here so that each screen always ends up with
  exactly its own media + caption + keyboard — a previous screen's photo
  can never leak into the next screen.

Transition rules (the whole point of this module):

* same message type → in-place edit (``edit_message_text`` /
  ``edit_message_media``);
* different message type (Telegram cannot convert text↔photo in place)
  → **replace**: send the new screen, delete the old message, and hand
  the caller the NEW message so the live message reference always points
  at the message that is actually on screen (never a stale message_id).

Supported games and every interactive panel should render through these
helpers instead of calling ``bot.edit_message_text`` directly.
"""

import asyncio
from typing import Any, Awaitable, Callable, Optional, Tuple

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

# Telegram caption hard limit (photo screens cannot show more text).
CAPTION_LIMIT = 1024


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
        except (TypeError, AttributeError) as e:
            # Programming-level error (bad call signature / dead handle):
            # retrying can never help - fail fast so callers can degrade.
            logger.warning(f"Edit of {context} failed outright: {e}")
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


async def safe_edit_message_caption(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: Optional[str] = "Markdown",
    max_attempts: int = 3,
) -> bool:
    """Edit a PHOTO message's caption (the art itself is kept)."""
    return await _run_edit_attempts(
        lambda: bot.edit_message_caption(
            caption=text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        ),
        fallback_factory=lambda: bot.edit_message_caption(
            caption=text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
            parse_mode=None,
        ),
        max_attempts=max_attempts,
        context=f"caption of message {message_id} in chat {chat_id}",
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


async def _send_fallback(bot, chat_id, text, reply_markup=None, parse_mode="Markdown"):
    """Send a plain text message (retrying without parse_mode on failure)."""
    try:
        return await bot.send_message(
            chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode
        )
    except Exception:
        return await bot.send_message(
            chat_id, text, reply_markup=reply_markup, parse_mode=None
        )


async def safe_edit_message_media(
    bot: Bot,
    chat_id: int,
    message_id: int,
    photo: Any,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: Optional[str] = "Markdown",
    max_attempts: int = 3,
) -> bool:
    """Swap a message's media (photo→photo) with an updated caption."""
    from aiogram.types import InputMediaPhoto

    def _factory():
        media = InputMediaPhoto(media=photo, caption=text or "", parse_mode=parse_mode)
        return bot.edit_message_media(
            media=media,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
        )

    def _fallback():
        media = InputMediaPhoto(media=photo, caption=text or "", parse_mode=None)
        return bot.edit_message_media(
            media=media,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
        )

    return await _run_edit_attempts(
        _factory,
        fallback_factory=_fallback,
        max_attempts=max_attempts,
        context=f"media of message {message_id} in chat {chat_id}",
    )


async def safe_send_screen(
    bot: Bot,
    chat_id: int,
    text: str,
    photo=None,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = "Markdown",
):
    """Send a photo+caption screen, falling back to text only.

    Used where a text→photo transition is impossible on edit: callers send
    a fresh message instead. Returns the sent Message (or None).
    """
    if photo is not None:
        try:
            return await bot.send_photo(
                chat_id,
                photo,
                caption=text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
        except Exception as exc:
            logger.debug("send photo screen failed: %s", exc)
    try:
        return await _send_fallback(bot, chat_id, text, reply_markup, parse_mode)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("send screen failed entirely: %s", exc)
        return None


def _is_photo_message(message: Any) -> bool:
    """True when the message on screen is a PHOTO message."""
    content_type = getattr(message, "content_type", None)
    content_type = getattr(content_type, "value", content_type)
    return content_type == "photo"


def _retarget_message_reference(holder: Any, new_message: Message) -> None:
    """Move a live message reference (e.g. ``callback.message``) to the
    replacement message, in place.

    Telegram cannot convert a text message into a photo message (or vice
    versa) in place, so the renderer replaces the message. Everything the
    handler does afterwards (panel claiming, further edits) must see the
    NEW message_id — a stale id must never outlive the replacement.
    """
    try:
        holder.message = new_message
    except Exception:  # frozen aiogram models
        object.__setattr__(holder, "message", new_message)


async def _replace_message(
    bot: Bot,
    chat_id: int,
    old_message_id: Optional[int],
    text: str,
    photo: Optional[Any],
    reply_markup: Optional[InlineKeyboardMarkup],
    parse_mode: Optional[str],
) -> Optional[Message]:
    """Replace a message with a freshly sent screen.

    Sends the new message first (never leave the chat empty), then deletes
    the old one (never leave orphan messages behind). Returns the new
    message, or None when sending failed.
    """
    sent = await safe_send_screen(
        bot,
        chat_id,
        text,
        photo=photo,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
    )
    if sent is None:
        return None
    new_id = getattr(sent, "message_id", None)
    if old_message_id and new_id and new_id != old_message_id:
        try:
            await bot.delete_message(chat_id, old_message_id)
        except Exception as exc:
            logger.warning(
                f"Could not delete replaced message {old_message_id} "
                f"in chat {chat_id}: {exc}"
            )
    return sent


async def render_screen(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    photo: Optional[Any] = None,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: Optional[str] = "Markdown",
    current_is_photo: Optional[bool] = None,
) -> Tuple[bool, Optional[Message]]:
    """The single message-transition engine.

    Renders ``(text, keyboard, photo)`` onto the message at
    ``(chat_id, message_id)`` — regardless of whether that message is
    currently TEXT or PHOTO.

    * PHOTO → PHOTO and TEXT → TEXT are edited in place. When
      ``current_is_photo`` is unknown, the natural in-place edit for the
      TARGET type is attempted first: it can only succeed on the matching
      message type (Telegram enforces this), so a failure means either a
      type change or a dead message — both are handled by replacing.
    * TEXT → PHOTO / PHOTO → TEXT replace the message (Telegram cannot
      convert types in place): send the new screen, delete the old one.

    Returns ``(ok, replacement)`` where ``replacement`` is the new Message
    when the message was replaced (callers MUST move their live reference
    to it), or None when the original message still shows the screen.

    ``photo`` that does not fit a Telegram caption degrades the target to
    a text screen (presentation must never break the flow).
    """
    target_photo = photo if (photo is not None and len(text) <= CAPTION_LIMIT) else None

    async def _in_place_edit() -> bool:
        if target_photo is not None:
            return await safe_edit_message_media(
                bot,
                chat_id,
                message_id,
                target_photo,
                text,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
        return await safe_edit_message_text(
            bot,
            chat_id,
            message_id,
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )

    if current_is_photo is not None:
        target_is_photo = target_photo is not None
        if current_is_photo == target_is_photo:
            # Same type: pure in-place edit (no orphans, id stays stable).
            ok = await _in_place_edit()
            return (True, None) if ok else (False, None)
        # Type change: the in-place edit is impossible - replace directly.
    else:
        # Unknown current type: the in-place edit can only succeed on the
        # matching type; anything else falls through to replacement.
        if await _in_place_edit():
            return True, None

    # Replace: send the new screen, delete the old message, move on.
    sent = await _replace_message(
        bot,
        chat_id,
        message_id,
        text,
        target_photo,
        reply_markup,
        parse_mode,
    )
    if sent is not None:
        return True, sent

    # Degraded in-place edit so the message is never left dead. This can
    # keep the wrong media (only when replacing failed entirely) but the
    # text/keyboard always end up current.
    ok = await safe_edit_message_caption(
        bot,
        chat_id,
        message_id,
        text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
    )
    if not ok:
        ok = await safe_edit_message_text(
            bot,
            chat_id,
            message_id,
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
    return (True, None) if ok else (False, None)


async def render_callback_screen(
    callback: CallbackQuery,
    bot: Bot,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: Optional[str] = "Markdown",
    photo: Optional[Any] = None,
) -> Optional[Message]:
    """Render a screen onto the message behind a callback query.

    Returns the Message now displaying the screen. When a replacement
    happened, ``callback.message`` is retargeted to the new message BEFORE
    returning, so anything the handler does afterwards (panel claiming,
    follow-up edits) references the live message.
    """
    message = callback.message
    if message is None:
        return None

    message_id = getattr(message, "message_id", None)
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None) if chat is not None else None

    if message_id and chat_id:
        ok, replacement = await render_screen(
            bot,
            chat_id,
            message_id,
            text,
            photo=photo,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            current_is_photo=_is_photo_message(message),
        )
        if not ok:
            return None
        if replacement is not None:
            _retarget_message_reference(callback, replacement)
            return replacement
        return message

    # Legacy fallback: the handle has no chat/message identifiers.
    async def _direct_edit():
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=parse_mode)

    async def _direct_edit_plain():
        await message.edit_text(text, reply_markup=reply_markup, parse_mode=None)

    ok = await _run_edit_attempts(
        _direct_edit,
        fallback_factory=_direct_edit_plain,
        context="callback message",
    )
    return message if ok else None


async def safe_edit_callback_message(
    callback: CallbackQuery,
    bot: Bot,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: Optional[str] = "Markdown",
    photo=None,
) -> bool:
    """Show a screen on the message behind a callback query.

    Works no matter whether the current message is TEXT or PHOTO and no
    matter whether the target screen carries media: the transition engine
    edits, swaps media, or replaces the message as needed. With ``photo``
    the target is a photo screen (its own media + caption); without it the
    target is a text screen — a stale photo from a previous screen never
    survives into it.

    Returns True when the screen is current on the live message. After a
    replacement ``callback.message`` points at the new message.
    """
    return (
        await render_callback_screen(
            callback,
            bot,
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            photo=photo,
        )
        is not None
    )


async def safe_edit_screen(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = "Markdown",
    photo=None,
    current_is_photo: Optional[bool] = None,
) -> Optional[int]:
    """Render a screen onto a (chat_id, message_id) pair of unknown type.

    Same transition rules as ``render_screen``. Returns the message_id
    that now displays the screen (a replacement changes it!), or None on
    failure. Callers that store message ids (match boards) MUST persist
    the returned id.
    """
    ok, replacement = await render_screen(
        bot,
        chat_id,
        message_id,
        text,
        photo=photo,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
        current_is_photo=current_is_photo,
    )
    if not ok:
        return None
    return getattr(replacement, "message_id", None) or message_id
