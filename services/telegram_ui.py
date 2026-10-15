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
import re
from typing import Any, Awaitable, Callable, Optional, Tuple

import aiogram
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
# Recovery-row colors: Battleship keeps its long-standing green refresh row,
# every other board uses the standard blue one (never transparent).
REFRESH_BUTTON_STYLE = "primary"
BATTLESHIP_REFRESH_BUTTON_STYLE = "success"


def board_refresh_style(game_code: Optional[str]) -> str:
    """Glass style of a board's «🔄 بروزرسانی صفحه» row for ``game_code``."""
    if game_code == "battleship":
        return BATTLESHIP_REFRESH_BUTTON_STYLE
    return REFRESH_BUTTON_STYLE


# "message is not modified" == content already current (success).
# "message can't be edited" is NOT in here on purpose: the edit failed and
# the visible content may be stale - it degrades gracefully below instead
# of being masked as a success.
NOT_MODIFIED_MARKERS = ("message is not modified",)
PARSE_ERROR_MARKERS = ("can't parse entities", "unsupported start tag")
# Only an explicit type mismatch justifies replacing an unknown-type message.
# A permission/error/expired message is NOT evidence of a type change.
TYPE_MISMATCH_MARKERS = (
    "there is no text in the message to edit",
    "there is no media in the message to edit",
    "message is not a media message",
)

# Telegram counts caption length in UTF-16 code units (not Python characters).
CAPTION_LIMIT = 1024
_CAPTION_SHORTENED = "\n… (متن برای زیرنویس تلگرام کوتاه شد) …\n"


def caption_length(text: str) -> int:
    """Length as counted by Telegram for photo captions."""
    return len(text.encode("utf-16-le")) // 2


def _caption_slice(text: str, max_units: int, *, from_end: bool = False) -> str:
    """Take a prefix/suffix without cutting a UTF-16 surrogate pair."""
    chars = reversed(text) if from_end else iter(text)
    result = []
    units = 0
    for char in chars:
        size = 2 if ord(char) > 0xFFFF else 1
        if units + size > max_units:
            break
        result.append(char)
        units += size
    return "".join(reversed(result) if from_end else result)


def photo_caption(text: str) -> str:
    """Fit a PHOTO target into a Telegram caption without changing its type.

    Keep both the beginning (title/context) and end (usually stats/actions),
    visibly marking the omitted middle. A photo cannot display more than
    1024 UTF-16 units in one caption; silently treating it as a text target
    would delete/reopen the panel on every long-card navigation.
    """
    if caption_length(text) <= CAPTION_LIMIT:
        return text
    available = CAPTION_LIMIT - caption_length(_CAPTION_SHORTENED)
    head_units = available * 2 // 3
    return (
        _caption_slice(text, head_units)
        + _CAPTION_SHORTENED
        + _caption_slice(text, available - head_units, from_end=True)
    )


def _version_parts(value: object) -> tuple[int, ...]:
    """Parse numeric version components without adding a packaging dependency."""
    parts = re.findall(r"\d+", str(value))
    return tuple(int(part) for part in parts) if parts else (0,)


def _inline_button_has_style_field() -> bool:
    fields = getattr(InlineKeyboardButton, "model_fields", None)
    if fields is None:  # Pydantic v1 compatibility for older aiogram installs.
        fields = getattr(InlineKeyboardButton, "__fields__", {})
    return "style" in fields


AIROGRAM_VERSION = getattr(aiogram, "__version__", "0")
BOT_API_VERSION = getattr(aiogram, "__api_version__", "0")
APPLE_MINE_WORK_BUTTON_SUPPORTS_STYLE = (
    _version_parts(AIROGRAM_VERSION) >= (3, 30, 0)
    and _version_parts(BOT_API_VERSION) >= (9, 0)
    and _inline_button_has_style_field()
)
_WORK_BUTTON_PATH_LOGGED = False


def _log_work_button_path(*, uses_style: bool) -> None:
    global _WORK_BUTTON_PATH_LOGGED
    if _WORK_BUTTON_PATH_LOGGED:
        return
    path = "Bot API success style" if uses_style else "Persian green-circle label fallback"
    logger.info(
        "Apple Mine work button uses %s (aiogram=%s, Bot API schema=%s)",
        path,
        AIROGRAM_VERSION,
        BOT_API_VERSION,
    )
    _WORK_BUTTON_PATH_LOGGED = True


def work_button_kb(*, supports_style: bool) -> InlineKeyboardMarkup:
    """Build the work button using the green style when the client supports it."""
    label = "⛏ کار کردن در معدن"
    if supports_style:
        try:
            button = InlineKeyboardButton(
                text=label,
                callback_data="mine:work",
                style="success",
            )
        except TypeError:
            _log_work_button_path(uses_style=False)
            button = InlineKeyboardButton(
                text=f"🟢 {label}",
                callback_data="mine:work",
            )
        else:
            _log_work_button_path(uses_style=True)
    else:
        _log_work_button_path(uses_style=False)
        button = InlineKeyboardButton(
            text=f"🟢 {label}",
            callback_data="mine:work",
        )
    return InlineKeyboardMarkup(inline_keyboard=[[button]])


def with_refresh_button(
    keyboard: Optional[InlineKeyboardMarkup],
    match_id: int,
    *,
    refresh_style: str = REFRESH_BUTTON_STYLE,
) -> InlineKeyboardMarkup:
    """Append a board-refresh row to an existing inline keyboard.

    The refresh callback re-renders the board from the database, giving
    users a safe recovery path whenever the rendered message went stale
    (e.g. after a transient Telegram edit failure). The row is blue by
    default; ``refresh_style`` lets a game apply its own button color
    without changing other games (Battleship passes ``success``).
    """
    rows = list(keyboard.inline_keyboard) if keyboard else []
    refresh_callback = f"{REFRESH_CALLBACK_PREFIX}{match_id}"
    if rows and rows[-1][0].callback_data == refresh_callback:
        # Keep the helper idempotent while honoring an explicitly requested
        # style if a keyboard already contains this refresh row.
        refresh_button = rows[-1][0]
        if refresh_button.style != refresh_style:
            rows[-1] = [refresh_button.model_copy(update={"style": refresh_style})]
    else:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔄 بروزرسانی صفحه",
                    callback_data=refresh_callback,
                    style=refresh_style,
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _run_edit_attempts(
    edit_factory: Callable[[], Awaitable],
    fallback_factory: Optional[Callable[[], Awaitable]] = None,
    max_attempts: int = 3,
    context: str = "message",
    on_type_mismatch: Optional[Callable[[], None]] = None,
    on_edited: Optional[Callable[[Any], None]] = None,
) -> bool:
    """Execute an edit coroutine with retries and graceful degradation.

    ``edit_factory`` must perform the edit. ``fallback_factory`` (optional)
    performs a degraded edit (e.g. without parse_mode) when the primary
    edit is rejected because of unparseable Markdown.
    """
    last_error: Optional[Exception] = None

    for attempt in range(max_attempts):
        try:
            edited = await edit_factory()
            if on_edited is not None:
                on_edited(edited)
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
                        edited = await fallback_factory()
                        if on_edited is not None:
                            on_edited(edited)
                        return True
                    except Exception as fallback_error:
                        last_error = fallback_error
                        logger.warning(
                            f"Fallback edit without parse_mode also failed for {context}: "
                            f"{fallback_error}"
                        )
                break
            if on_type_mismatch and any(marker in message for marker in TYPE_MISMATCH_MARKERS):
                on_type_mismatch()
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
    on_type_mismatch: Optional[Callable[[], None]] = None,
    on_edited: Optional[Callable[[Any], None]] = None,
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
        on_type_mismatch=on_type_mismatch,
        on_edited=on_edited,
    )


async def safe_edit_message_caption(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    parse_mode: Optional[str] = "Markdown",
    max_attempts: int = 3,
    on_edited: Optional[Callable[[Any], None]] = None,
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
        on_edited=on_edited,
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


async def _send_fallback(
    bot,
    chat_id,
    text,
    reply_markup=None,
    parse_mode="Markdown",
    disable_notification: bool = False,
):
    """Send a plain text message (retrying without parse_mode on failure).

    ``disable_notification`` is forwarded only when set, so existing callers
    keep exactly the same Bot API call shape.
    """
    extra = {"disable_notification": True} if disable_notification else {}
    try:
        return await bot.send_message(
            chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode, **extra
        )
    except Exception:
        return await bot.send_message(
            chat_id, text, reply_markup=reply_markup, parse_mode=None, **extra
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
    on_type_mismatch: Optional[Callable[[], None]] = None,
    on_edited: Optional[Callable[[Any], None]] = None,
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
        on_type_mismatch=on_type_mismatch,
        on_edited=on_edited,
    )


async def safe_send_screen(
    bot: Bot,
    chat_id: int,
    text: str,
    photo=None,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: str | None = "Markdown",
    photo_fallback_to_text: bool = True,
    disable_notification: bool = False,
):
    """Send a screen; optionally fall back to text when photo send fails.

    A new chat/remote group needs a text fallback. When REPLACING a text
    message, however, a failed photo send should instead edit that existing
    text message in place (not delete it and send another text message).

    ``disable_notification`` sends a silent message (it still appears in the
    chat). It is forwarded only when True, so default calls are unchanged.
    """
    extra = {"disable_notification": True} if disable_notification else {}
    if photo is not None:
        caption = photo_caption(text)
        try:
            return await bot.send_photo(
                chat_id, photo, caption=caption,
                reply_markup=reply_markup, parse_mode=parse_mode, **extra,
            )
        except TelegramBadRequest as exc:
            if parse_mode and any(marker in str(exc).lower() for marker in PARSE_ERROR_MARKERS):
                # Shortening a long caption can split Markdown entities.
                # Retry the PHOTO without formatting before degrading to TEXT.
                try:
                    return await bot.send_photo(
                        chat_id, photo, caption=caption,
                        reply_markup=reply_markup, parse_mode=None, **extra,
                    )
                except Exception as plain_exc:
                    logger.warning("photo screen without Markdown failed: %s", plain_exc)
            else:
                logger.warning("send photo screen failed: %s", exc)
        except Exception as exc:
            logger.warning("send photo screen failed: %s", exc)
        if not photo_fallback_to_text:
            return None
    try:
        return await _send_fallback(
            bot, chat_id, text, reply_markup, parse_mode, disable_notification=disable_notification
        )
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
        photo_fallback_to_text=False,
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
    on_edited: Optional[Callable[[Any], None]] = None,
) -> Tuple[bool, Optional[Message]]:
    """The single message-transition engine.

    Renders ``(text, keyboard, photo)`` onto the message at
    ``(chat_id, message_id)`` — regardless of whether that message is
    currently TEXT or PHOTO.

    * PHOTO → PHOTO and TEXT → TEXT are edited in place. When
      ``current_is_photo`` is unknown, the target type is edited first;
      only an explicit Telegram type-mismatch response warrants replacing.
      Other edit failures leave the live message untouched for retry.
    * TEXT → PHOTO / PHOTO → TEXT replace the message (Telegram cannot
      convert types in place): send the new screen, delete the old one.

    Returns ``(ok, replacement)`` where ``replacement`` is the new Message
    when the message was replaced (callers MUST move their live reference
    to it), or None when the original message still shows the screen.

    A PHOTO target stays a PHOTO target even if the full text exceeds the
    caption limit; only its displayed caption is visibly shortened.
    """
    target_photo = photo
    caption = photo_caption(text) if photo is not None else text
    type_mismatch = False

    def _mark_type_mismatch() -> None:
        nonlocal type_mismatch
        type_mismatch = True

    on_type_mismatch = _mark_type_mismatch if current_is_photo is None else None

    async def _in_place_edit() -> bool:
        if target_photo is not None:
            return await safe_edit_message_media(
                bot,
                chat_id,
                message_id,
                target_photo,
                caption,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                on_type_mismatch=on_type_mismatch,
                on_edited=on_edited,
            )
        return await safe_edit_message_text(
            bot,
            chat_id,
            message_id,
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            on_type_mismatch=on_type_mismatch,
            on_edited=on_edited,
        )

    if current_is_photo is not None:
        target_is_photo = target_photo is not None
        if current_is_photo == target_is_photo:
            # Same type: pure in-place edit (no orphans, id stays stable).
            ok = await _in_place_edit()
            return (True, None) if ok else (False, None)
        # Type change: the in-place edit is impossible - replace directly.
    else:
        # Unknown current type: a failed edit is not proof of a type change.
        # Do not replace on a transient failure, permissions issue or stale id.
        if await _in_place_edit():
            return True, None
        if not type_mismatch:
            return False, None

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
        on_edited=on_edited,
    )
    if not ok:
        ok = await safe_edit_message_text(
            bot,
            chat_id,
            message_id,
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            on_edited=on_edited,
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

    Returns the Message now displaying the screen. ``callback.message`` is
    refreshed with Telegram's edit result even for an in-place media swap;
    on replacement it is retargeted to the NEW message id before returning.
    Panel claiming, banner caching and follow-up edits see the live screen.
    """
    message = callback.message
    if message is None:
        return None

    message_id = getattr(message, "message_id", None)
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None) if chat is not None else None

    if message_id and chat_id:
        def _retarget_edited(edited: Any) -> None:
            # Telegram returns the updated Message for an in-place edit,
            # but the callback's immutable snapshot still has the OLD photo
            # file_id. Banner caching and subsequent renders need the new one.
            edited_chat = getattr(edited, "chat", None)
            if (
                isinstance(getattr(edited, "message_id", None), int)
                and edited.message_id == message_id
                and getattr(edited_chat, "id", None) == chat_id
            ):
                _retarget_message_reference(callback, edited)

        ok, replacement = await render_screen(
            bot,
            chat_id,
            message_id,
            text,
            photo=photo,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            current_is_photo=_is_photo_message(message),
            on_edited=_retarget_edited,
        )
        if not ok:
            return None
        if replacement is not None:
            _retarget_message_reference(callback, replacement)
        return callback.message

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
