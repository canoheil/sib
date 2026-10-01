"""Shared navigation convention (context-aware back buttons).

A screen's Back target is encoded as the PARENT SCREEN'S CALLBACK DATA
attached to the entry callback:

    apple_battle:group_games_panel   -> entered from the games menu
    apple_battle:main_menu           -> entered from the main menu
    apple_wiki:apple_cards           -> entered from the cards panel

The origin is only recognized when it is a KNOWN screen callback (allow
list) - game codes and numeric args never collide with it. Parsing rules
are shared by every handler - no ad-hoc per-page logic.
"""

from typing import Optional, Tuple

MAIN_MENU = "main_menu"

# Screens that may appear as a contextual Back target (origin).
KNOWN_SCREENS = frozenset(
    {
        MAIN_MENU,
        "group_games_panel",
        "group_active_games",
        "create_new_party_menu",
        "apple_cards",
        "apple_shop",
        "apple_profile",
        "apple_battle",
        "apple_trade",
        "apple_wiki",
        "apple_garden",
        "help_menu",
    }
)


def with_origin(callback: str, origin: Optional[str] = None) -> str:
    """Build a callback carrying its parent screen (omitted for main)."""
    if not origin or origin == MAIN_MENU:
        return callback
    return f"{callback}:{origin}"


def parse_callback(
    data: Optional[str], *, arg_segments: int = 0
) -> Tuple[str, Optional[str]]:
    """Split 'screen[:args][:origin]' into (callback_base, origin).

    The trailing segment is the origin only when it is a KNOWN screen
    callback; otherwise it is a normal handler argument. ``arg_segments``
    declares how many handler-argument segments the callback carries —
    those are NEVER reinterpreted as the origin, even when an argument
    happens to share its name with a known screen (e.g.
    ``help_page:apple_battle``: the topic ``apple_battle`` is the Apple
    Battle screen's own callback name).
    """
    if not data:
        return "", None
    parts = data.split(":")
    origin: Optional[str] = None
    fixed = 1 + max(0, arg_segments)
    if len(parts) > fixed and parts[-1] in KNOWN_SCREENS:
        origin = parts[-1]
        parts = parts[:-1]
    return ":".join(parts), origin


def back_label_for(origin: Optional[str]) -> str:
    """Friendly Persian label for the contextual back button."""
    if origin == "apple_cards":
        return "↩️ بازگشت به کارت‌های من"
    if origin == "apple_profile":
        return "↩️ بازگشت به پروفایل من"
    if origin == "group_games_panel":
        return "↩️ بازگشت به بازی‌ها"
    if origin == "group_active_games":
        return "↩️ بازگشت به بازی‌های فعال"
    return "↩️ بازگشت به منوی اصلی"


def back_target(origin: Optional[str]) -> str:
    return origin or MAIN_MENU
