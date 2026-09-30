# STEP 2 — Group menu: 🎮 Games + 📖 Guide

Status: DONE (full suite green; `tests/test_group_menu_games_guide.py` 5 tests).

- `keyboards/common.py::get_group_welcome_keyboard()` — 🎮 بازی‌ها
  (`group_games_panel`), 📖 راهنما (`help_menu`), existing buttons kept.
- `handlers/start.py` — origin-aware help (`_help_keyboard(origin)`,
  `help_page:<topic>:<origin>`); back label/target derive from origin.
- Navigation reuses `render_screen`/`render_callback_screen`; no navigation
  path cancels or forfeits a live match (asserted in the tests).
