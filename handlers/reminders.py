"""«🔔 یادآوری‌ها» — the player's own on/off switch for come-back messages.

The reminders in ``services/reminders.py`` are the only messages the bot sends
without being asked, so the player must be able to stop them in one tap. The
button lives on the private profile (its ``🔔``/``🔕`` label always shows the
CURRENT state) and answers with an alert, so the switch is never silent about
what it just did.

Private chats only: the profile panel is personal, and a group message is not
the place to change a personal setting.
"""

from aiogram import Bot, F, Router
from aiogram.types import CallbackQuery

from sib.core.logging import logger
from sib.database.connection import get_session
from sib.database.models import UserReminderState
from sib.services.reminders import ReminderService
from sib.services.telegram_ui import safe_answer_callback

router = Router(name="reminders")

REMINDERS_ON_ALERT = "🔔 یادآوری‌ها روشن شد؛ روزی حداکثر یک پیام."
REMINDERS_OFF_ALERT = "🔕 یادآوری‌ها خاموش شد؛ دیگه پیام نمی‌فرستیم."
REMINDERS_FAILURE_ALERT = "تنظیم یادآوری‌ها ذخیره نشد؛ دوباره امتحان کن."
PRIVATE_ONLY_ALERT = "این تنظیم فقط در چت خصوصی است."


@router.callback_query(F.data == "reminders_toggle")
async def handle_reminders_toggle(callback: CallbackQuery, bot: Bot) -> None:
    """Flip the caller's reminder preference and re-render the profile."""
    user = callback.from_user
    if user is None:
        return

    chat = getattr(callback.message, "chat", None) if callback.message else None
    if getattr(chat, "type", None) != "private":
        await safe_answer_callback(callback, PRIVATE_ONLY_ALERT, show_alert=True)
        return

    try:
        async with get_session() as session:
            state = await session.get(UserReminderState, user.id)
            was_off = bool(
                state is not None and (state.opted_out or state.blocked_at is not None)
            )
            now_off = await ReminderService.set_opted_out(session, user.id, not was_off)
    except Exception:
        logger.exception("reminders toggle failed user=%s", user.id)
        await safe_answer_callback(callback, REMINDERS_FAILURE_ALERT, show_alert=True)
        return

    alert = REMINDERS_OFF_ALERT if now_off else REMINDERS_ON_ALERT

    await safe_answer_callback(callback, alert, show_alert=True)

    # Re-render the profile so the button label matches the new state.
    if callback.message is not None:
        from sib.handlers.start import handle_apple_profile

        await handle_apple_profile(callback, bot)
