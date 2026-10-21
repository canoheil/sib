# BS1-IMPL — Battleship Friendly Match Privacy (Ephemeral Radar)

**Phase:** implementation · **Date:** 2026-10-21
**Predecessor:** `docs/games/BS1-RECON.md` (recon + design)
**Baseline:** `1b2bc5d` · branch `arena/2f8c7cb4-sib`
**Runtime:** `aiogram 3.31.0`, Bot API ≥ 10.3 (`EphemeralMessageParameters`)

---

## 1. The bug, in one paragraph

A friendly match keeps both teams in ONE group and shares ONE board message
(`services/friendly.py:291-297`). The old renderer elected “the player on turn”
as the viewer (`state_viewer_id`, `services/friendly.py:131-150`) and rebuilt
that single message from that player's side, so **each player's radar and a
live targeting grid were published to the whole group**, and the perspective
flipped on every shot. That behaviour is gone for Battleship.

## 2. The new two-surface UI

### 2.1 Group message = neutral public board

`BattleshipEngine.render_public()` + `get_public_keyboard()`:

* rosters of both friendly teams;
* both fleets' remaining cells (symmetric, derivable from public hits);
* the ordered, attributed shot history — the only place a shot is described:

  `• آرش به B4 شلیک کرد: 🌊 آب` / `💥 برخورد` / `🔥 غرق شد (3 خانه)`

* whose turn it is;
* a hint that the radar is private;
* **keyboard: exactly two buttons, nothing else**

  | Button | Callback | Style |
  |---|---|---|
  | `🎯 رادار من` | `bs_radar:{match_id}` | success |
  | `🔄 بروزرسانی` | `game_refresh:{match_id}` | primary |

No grid, no `bs_shot`/`bs_action` callbacks, no `شما`/`حریف` wording — the same
string is rendered for everybody.

### 2.2 Private panel = ephemeral radar

`BattleshipEngine.render_radar_panel()` + `get_radar_panel_keyboard()`:

```
🎯 **رادار شما (نوبت شماست)**
🟢 یک خانه روی رادار زیر را هدف بگیر:

  A B C D E F G H
1 · · 🌊 · · · · ·
…
🛡️ ناوگان شما: 12/14
🎯 ناوگان دشمن: 9/14
🎯 شما به B4 زدید: 💥 برخورد
```

The grid is 8×8 with `A–H` / `1–8` labels (the game's real board; the brief's
“10×10” was an assumption — the engine has always been 8×8, 14 hull cells).
Cells call `bs_shot:{match}:{r}:{c}` **only for the player on turn**; the panel
footer carries `🔄 بروزرسانی رادار` (`bs_radar`) and `✖️ بستن` (`bs_close`).

Delivery (`services/battleship_radar.py`):

```python
await bot.send_message(
    chat_id=<group id>,
    text=…, reply_markup=<grid>, parse_mode="Markdown",
    ephemeral_message_parameters=EphemeralMessageParameters(
        receiver_user_id=<player id>,
        callback_query_id=<this callback>,
        replace_callback_query_message=True,   # panel replaces the tapped board
    ),
)
```

A player may only open a panel for **themselves** and only if they are a
participant of that friendly match; spectators get a private alert and nothing
is sent anywhere.

## 3. Flow of one shot

1. The player taps `🎯 رادار من` → `handle_bs_radar` → participation check →
   ephemeral panel **replaces the group board in their client only**.
2. They tap a cell → `bs_shot:{match}:{r}:{c}` → `handle_bs_shot` →
   `process_game_action` (unchanged Phase A: row lock → validate → apply →
   commit; the DB stays authoritative).
3. Phase B order is deliberate:
   1. **the private panel** is edited in place (`editEphemeralMessageText`) with
      the result and a read-only grid — the ephemeral window is 15 s and the
      player is looking at it;
   2. **then the public group board** is edited with the new history line, the
      new turn and the new fleet counts;
   3. a bot turn, when applicable, is already scheduled by the shared dispatcher
      (`schedule_bot_turn`, before presentation) and re-renders only the public
      board (`services/game_sync.py`).

## 4. Fallback ladder (privacy-preserving at every level)

| Level | Mechanism | Trigger |
|---|---|---|
| 1 | edit the panel the tap came from (ephemeral or DM) | re-tap / 🔄 inside the panel |
| 2 | ephemeral send with `replace_callback_query_message=True` | opening the panel from the group board |
| 3 | ephemeral send with `replace_…=False` | level 2 rejected |
| 4 | **normal DM** (`chat_id=user.id`) with the same grid | ephemeral unavailable (privacy settings, expired window, API error) |
| 5 | `callback.answer(..., show_alert=True)`, ≤ 200 chars, grid **bounds only** (`A1 تا H8`) + a nudge to press 🔄 | DM also failed |

`False`/`True` are swapped in levels 2–3 when the callback originated **from** an
ephemeral message, as the Bot API requires. No level ever posts the radar into
the group, and delivery failures are logged (`Events.BATTLESHIP_RADAR_EVENT`).

## 5. Cleanup of the old logic

* `state_viewer_id` / `viewer_audience` are **no longer used for Battleship** in
  any of the four friendly renderers: `handlers/games/common.py`
  (`_present_friendly_board`, `render_board_for`, `handle_game_refresh`),
  `handlers/friendly.py::render_friendly_board`, `services/game_sync.py`.
  They remain untouched for RPS/High Card (blind-commit boards), which the
  brief protects.
* Stale / forged callbacks:
  * wrong-turn or already-played cell → engine rejects → the private panel is
    refreshed **read-only** and the group board is re-rendered neutral;
  * taps on legacy group grids (`bs_action:`) still play, but are answered with
    a private panel + neutral board (no grid in the group);
  * spectators / non-participants → alert only, zero sends;
  * an ephemeral id is **never** written to `group1_message_id` /
    `group2_message_id` (that would orphan the group board behind `message_id=0`).

## 6. Files

| File | Change |
|---|---|
| `games/battleship/engine.py` | `friendly_private_radar` flag, attributed `shot_log` in `apply_action`, `render_public`, `get_public_keyboard`, `render_radar_panel`, `get_radar_panel_keyboard`, small helpers |
| `services/battleship_radar.py` (**new**) | public/panel views, ephemeral → DM → alert delivery, panel edit/close, callback parsing |
| `handlers/games/battleship.py` | `bs_radar`, `bs_shot`, `bs_close`, legacy `bs_action` |
| `handlers/games/common.py` | public-board presentation (panel first), radar recovery render, refresh branch |
| `handlers/friendly.py`, `services/game_sync.py` | friendly boards render the neutral public view |
| `core/logging.py` | `BATTLESHIP_RADAR_EVENT` |
| `tests/games/test_bs1_friendly_radar.py` (**new**) | 10 privacy/flow tests |

## 7. Tests

| Test | Locks down |
|---|---|
| `test_group_board_has_no_grid_buttons` | neutral text + exactly two buttons, no grid cells, no shot callbacks |
| `test_group_board_edit_after_shot_has_no_grid` | the message edited in the group after a shot stays grid-free |
| `test_ephemeral_radar_is_sent_to_active_player` | `receiver_user_id`/`callback_query_id`/`replace=True`, 64 `bs_shot` cells, group untouched |
| `test_waiting_player_gets_a_read_only_panel` | own radar, no live grid |
| `test_shot_updates_both_ephemeral_and_group_messages` | panel result line, public history line, committed state, panel edited before group |
| `test_fallback_to_dm_if_ephemeral_fails` | 2 ephemeral attempts → DM; total failure → ≤200-char alert, never the group |
| `test_stale_callback_does_not_leak_radar` | rejected shot, read-only panel, neutral group, board pointer intact |
| `test_spectator_callback_is_refused_without_delivery` | no delivery to non-players |
| `test_legacy_group_grid_tap_is_answered_privately` | old boards keep working without leaking |
| `test_panel_close_deletes_the_ephemeral_message` | `deleteEphemeralMessage` only |

Suites run: the new file (10 passed), `tests/test_battleship*.py` + `tests/games`
(73 passed). The full suite has pre-existing failures (missing image assets,
alembic head/config, unrelated party/session tests) that reproduce identically
on the base commit.

## 8. Known limits

* Ephemeral delivery is **best-effort**: not guaranteed, and only to the client
  application that triggered the tap. Everything verifiable lives on the public
  board, so a lost whisper never hides match state.
* A player's stale panel cannot be refreshed from a bot turn / timeout (its
  identity only exists inside that player's own callbacks); it self-heals on
  their next tap because its cells are inert when it is not their turn.
* The brief's level-5 text mentions the grid bounds instead of a typed
  `شلیک C4` command; typed shooting is **not** implemented in this phase (it
  would need a text router and match lookup). See BS1-RECON §4, decision 3.
