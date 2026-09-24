"""Global error handling for Telegram updates."""

from aiogram import Router
from aiogram.types import ErrorEvent
from sib.core.errors import sibException, MESSAGES
from sib.core.logging import logger

router = Router(name="error_handler")


@router.errors()
async def global_error_handler(event: ErrorEvent):
    """Global error handler for all unhandled exceptions in updates."""
    exception = event.exception
    update = event.update

    # Domain exceptions
    if isinstance(exception, sibException):
        logger.warning(f"sibException handled: {exception.message}")
        if update.message:
            await update.message.reply(exception.message)
        elif update.callback_query:
            await update.callback_query.answer(exception.message, show_alert=True)
        return

    # Unexpected exceptions
    logger.exception(f"Unhandled exception during update {update.update_id}: {exception}")

    if update.message:
        await update.message.reply(MESSAGES["generic_error"])
    elif update.callback_query:
        await update.callback_query.answer(MESSAGES["generic_error"], show_alert=True)
