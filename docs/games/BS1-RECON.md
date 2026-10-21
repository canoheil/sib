# BS1-RECON — Battleship Friendly Match Privacy Fix

**Phase:** design and reconnaissance only — **no code written in this phase** · **Date:** 2026-10-21
**Inspected baseline:** `1b2bc5d` (`Merge pull request #86 …`), branch `arena/2f8c7cb4-sib`
**Runtime assumptions verified:** `aiogram 3.31.0` (the pin in `requirements.txt:1`) installed and introspected locally; Telegram Bot API surface as of the **10.3** changelog (2026-08-24) ephemeral-message rework.
**Scope of this change:** this document only. No handlers, engines, services, keyboards, migrations, configuration, assets or database rows are modified. Everything described as “proposed” is future work for the implementation phase.

Paths are relative to the repository root. At runtime the root modules are importable under the `sib.*` namespace (`sib.games.battleship.engine` is `games/battleship/engine.py`).

---

## 0. Proposed design at a glance

1. **Split the friendly Battleship UI into exactly two surfaces.** A **public, viewer-neutral group board** (rosters, both fleets' remaining cells, an ordered shot log, whose turn, timer) and a **private ephemeral targeting panel** (one team's radar + live 8×8 grid) that only the player on turn can open.
2. **The public board stops rendering any radar grid and carries no shot callbacks.** Its only game action is “🎯 رادار من” (`bs_radar:{match_id}`), which is harmless for spectators (they get a private toast) and for the waiting player (they get their own read-only panel).
3. **The panel is a direct response to the player's own tap:** `sendMessage` with `ephemeral_message_parameters` = `receiver_user_id`, `callback_query_id`, `replace_callback_query_message=True`, plus the grid as `reply_markup`. The presser sees the panel *in place of* the group board; nobody else's client changes.
4. **Shots are tapped inside the panel** (`bs_shot:{match_id}:{r}:{c}`) and still go through the existing authoritative transaction (`process_game_action`). After commit, the panel is edited in place (`editEphemeralMessageText`) and only then is the public group board edited.
5. **No schema change, no new persistence.** The ephemeral message identity always arrives in the callback update (`chat_id`, `from.id`, `message.ephemeral_message_id`), so nothing about a panel is ever stored in the `Match` row. In particular, an ephemeral id must never be written to `group1_message_id` / `group2_message_id`.
6. **Fallback ladder, privacy-preserving at every level:** ephemeral-with-replace → ephemeral-without-replace → normal DM → **typed coordinate in the group** → alert + hint. Never fall back to posting the grid in the group.
7. **Timeout safety.** The 90-second Battleship turn timer forfeits the match (`core/config.py:77`, `services/timeout/service.py:76-105`). The implementation must make sure a failed panel delivery can never cost a player the match (coordinate fallback plus a timeout policy decision, §3.7).

---

## 1. TASK 1 — Reconnaissance

### 1.1 Where the implementation lives

| Concern | Location |
|---|---|
| Rules engine (8×8, fleet `[4,3,3,2,2]` = 14 cells, alternating shots) | `games/battleship/engine.py` — class `BattleshipEngine:14`, `GRID_SIZE:29`, `SHIP_SIZES:30` |
| Automatic fleet placement | `generate_fleet():33-69`, called from `create_initial_state():71-130` at `:103-104` |
| Turn model | `create_initial_state()` (`turn_order:105-110`, `current_player_id:112`), `validate_action():132-157` (turn check `:141`, bounds `:146`, duplicate cell `:152-157`), `apply_action():159-212` (hit/miss `:183-199`, win `:201-205`, turn advance `:206-210`) |
| Text renderer | `render_for_group():271-363` |
| Keyboard renderer | `get_keyboard():365-404` |
| Radar/marker labels | `radar_labels():246-269`, markers `EMPTY/MISS/HIT/SUNK:236-239`, coordinate helper `_coord_label():242-244` |
| Telegram callback handler | `handlers/games/battleship.py:10-18` (`bs_action:{match}:{r}:{c}` → `process_game_action`) |
| Central action dispatcher | `handlers/games/common.py:268-...` (`process_game_action`), friendly presentation `:735-798`, refresh `:800-895` |
| Friendly-match plumbing | `services/friendly.py:86-166` (audiences/viewer helpers), `:249-385` (`start_friendly_match`) |
| Other code that renders friendly boards | `handlers/friendly.py:63-106`, `services/game_sync.py:78-125`, `sib/bots/runner.py:381-387` |
| Timeout / forfeit | `services/timeout/service.py:60-140` |
| Bot AI | `sib/bots/strategies/battleship.py` (hunt/target, never repeats a cell) |

### 1.2 Ship placement is fully automatic — confirmed

`create_initial_state()` generates two independent fleets (`:103-104`) with `generate_fleet()` (`:33-69`): random orientation, random origin, 1000 attempts, no overlap. The fleets live in `state["teams"]["A"|"B"]["ships"]` (`:114-124`), and nothing in the handlers, keyboards or the matchmaking flow ever asks a player to place or confirm a ship.

**Therefore the privacy leak can only exist in the shooting phase**, exactly as the brief states. There is no placement-phase UI to fix.

### 1.3 How the board is rendered in the group during shooting

Two renderers, both **perspective-relative**:

* `render_for_group(state, for_group_id, for_player_id)` resolves “my side” by finding the player whose `group_id` equals `for_group_id` (`engine.py:276-278`) and then renders **that team's** radar (`radar_labels`, `:246-269`), **that team's** intact-cell count and **that team's** turn prompt.
* `get_keyboard(state, match_id, for_player_id)` resolves the same team (`:373-376`) and makes cells tappable **only for the player whose turn it is** (`:397-402`).

Text template (`engine.py:353-363`), with the viewer-specific parts marked:

| Line | Source | Viewer-specific? |
|---|---|---|
| `🚢 **جنگ کشتیها (Battleship)** 🍏` | `:355` | no |
| rosters `⚔️ تیم…` / `🆚 تیم…` | `teams_line:312-320` | no |
| `🎯 **رادار شلیکهای شما به سمت دشمن:**` + 9×9 ASCII grid | `:356-357` | **yes** (the viewer's whole guess map) |
| `🛡️ سلولهای سالم ناوگان شما: {my_remaining}/14` | `:358` | **yes** |
| `🎯 سلولهای سالم ناوگان دشمن: {opp_remaining}/14` | `:359` | yes (mirror image of the above) |
| last shot `🎯 C4` + `💥 برخورد!` / `🌊 خطا!` | `last_shot_line:300-308`, printed `:360` | no (global, public by design) |
| `📢 آخرین رویداد: …` | `last_event:309`, `:361` | no |
| `🟢 نوبت شماست!` / `🟢 نوبت X است!` / `⏳ در انتظار شلیک X...` | `games/base.py:19-42` via `:344-351` | **yes** |

Keyboard shape (`engine.py:383-404`): 9 rows × 9 buttons — a header row `A..H` (green, red corner `:384-389`), row labels `1..8`, and 64 cells. A shot cell carries `🌊/💥/🔥` with `noop`; an untouched cell carries `·` and `bs_action:{match}:{r}:{c}` **only when the viewer is the current player** (`:400-402`).

### 1.4 Why a friendly match collapses both sides onto ONE message

1. **One group, one message.** `start_friendly_match()` sets `group1_id == group2_id == party.group_id` (`services/friendly.py:291-297`), and the friendly handler stores the same message id in both columns (`handlers/friendly.py:109-126`). The board is the edited party card (`handlers/friendly.py:274-310`).
2. **Team ambivalence is worked around with synthetic audiences.** Because both teams carry the real chat id, `apply_friendly_state()` (`services/friendly.py:98-111`, called at `:347`) rewrites the serialized players' `group_id`: team 1 keeps the real (negative) chat id, team 2 gets `abs(group_id) * 1000 + 2` (`friendly_audiences():86-95`). This is what makes `render_for_group` able to render “team 2's” board at all.
3. **A single viewer is elected for every render:** `state_viewer_id()` returns **the player on turn** (`services/friendly.py:131-150`), with `min(human_ids)` as the no-turn fallback; `shared_board_viewer()` wraps it (`:153-166`). That viewer's audience is passed as `for_group_id` (`_friendly_board_target`, `handlers/games/common.py:155-172`).
4. **Every friendly renderer follows the same rule:** `_present_friendly_board` (`common.py:735-798`), `render_board_for` (`:175-250`), the refresh path (`:857-866`), `render_friendly_board` (`handlers/friendly.py:63-106`), `sync_match_messages` (`services/game_sync.py:78-113`), and the bot-turn renderer that calls it (`sib/bots/runner.py:381-387`).

**Net effect:** the group message is a *shared physical surface carrying one player's private perspective*, and the perspective flips on every single shot.

### 1.5 Trace — what actually happens when Player A fires at C4

1. A taps `bs_action:{match}:2:3` on the group board (`engine.py:401`) → `handlers/games/battleship.py:10-18`.
2. Phase A (DB only, row locked, no Telegram awaits — `common.py:280-299`): load, deserialize, `validate_action` passes the turn check (`engine.py:141`), `apply_action` writes `teams.A.shots_taken["2,3"]`, sets `last_shot`/`last_event`, and **advances the turn to B** (`engine.py:159-212`), `MatchResultService` only if finished, then `commit()` before any presentation (`common.py:437`).
3. Phase B: `is_friendly` → `_present_friendly_board()` (`common.py:595-605`, `:735-798`). `_friendly_board_target()` now returns **viewer = B** (`services/friendly.py:131-150`).
4. The *group* message is replaced with **B's** board: B's radar, B's fleet health labelled “you”, a live B keyboard, and (because the audience has exactly one member, `games/base.py:40-41`) the literal line **`🟢 نوبت شماست!`** — read by both players.
5. A watches their own board disappear; B watches their board appear. During A's turn, the very same message had been showing **A's** radar and **A's** live grid.

| Surface | During A's turn | After A's shot |
|---|---|---|
| Group message radar | A's guess map (both players) | B's guess map (both players) |
| Group message “سلولهای سالم ناوگان شما” | A's fleet (both read it) | B's fleet (both read it) |
| Live targeting keyboard in the group | A's grid, tappable by anyone | B's grid, tappable by anyone |
| Turn prompt | `نوبت A` (both) | `🟢 نوبت شماست!` (both) |
| B sees their own board? | no | yes |

### 1.6 Findings — why it is unplayable

**P1 — Each player's radar is published to the whole group (the reported leak).**
While A is on turn, the shared message shows A's complete target map: every cell A has already tried, with hit/miss/sunk markers (`radar_labels:246-269`, printed at `:356-357`), because the render audience is “the player on turn” (`friendly.py:131-150` → `common.py:750-756` → `common.py:772-780`). When the turn flips, the message shows B's map instead. So each player's guesses — the one genuinely private thing in Battleship, *what you know and what you have ruled out* — are displayed to the opponent.

*Severity note, precisely:* no hidden hull cell is exposed. `radar_labels` only labels cells that were already shot (`:255-269`), and ships are never rendered anywhere; the existing regression test `test_enemy_ship_positions_not_revealed_in_keyboard` (`tests/test_battleship_board_ux.py`) still holds. The leak is the **knowledge state and the live target options**, not the fleet layout.

**P2 — The private grid is interactive and public.**
The current player's live keyboard lives inside the shared message (`engine.py:400-402`). Any group member can press a cell. The server correctly rejects them (`engine.py:141` → `common.py:479-500`), but the UI actively invites the wrong person to act, and every rejected tap triggers a full board re-render and a “این مسابقه… / حرکت نامعتبر” alert (`common.py:479-500`, `:502-532`).

**P3 — The waiting player has no board at all.**
The moment a player shoots, their radar and fleet status are removed from the message and replaced by the opponent's. The only way to see “your” side is to be on turn, which inverts the information the labels claim to show and makes planning impossible.

**P4 — The labels lie to half the group.**
`سلولهای سالم ناوگان شما` is computed from the elected viewer (`engine.py:281-282`, `:358`), i.e. from the player on turn. The other player therefore reads *the opponent's* fleet health as “yours” and their own as “enemy”, and the turn line says `نوبت شماست!` to both (`games/base.py:40-41`).

**P5 — Refresh renders to the wrong person.**
`handle_game_refresh()` lets **any member of the friendly group** press 🔄 (`common.py:838-846`, explicitly documented as “public group information”) and then renders `state_viewer_id(state, human_ids, user.id)`, which returns the **current player**, not the presser (`friendly.py:131-150`, `common.py:857-866`). A spectator — or the waiting opponent — is served the active player's radar together with a live grid.

**P6 — Stale/duplicate handling assumes a personal board.**
`_duplicate_feedback()` (`common.py:256-266`) and the silent-duplicate special cases (`:352-372`) only cover RPS and High Card; Battleship double taps and stale-grid taps fall into noisy recovery renders. In a group this turns ordinary mis-taps into visible churn.

**Answering the brief's two direct questions:**

* *“When Player A shoots, does the message update and reveal their target coordinate?”* — **Yes, and that part is correct and should stay public.** `last_shot` (`engine.py:193`/`:197`) and `last_event` (`:199`) are global, and the coordinate is printed (`:300-308`). A public coordinate is not the bug; a public radar + public keyboard is.
* *“Or their own board to Player B?”* — **Yes, both directions, by design of the flip.** While A is on turn, B sees A's radar and A's live grid; after A's shot the message becomes B's board for everyone. Neither player ever has a private surface of their own.

### 1.7 Ephemeral messages: how High Card does it today

High Card is the only place in the codebase that uses the Bot API ephemeral channel (`grep receiver_user_id`): `handlers/games/common.py:539`, plus the marriage/love flows which use an unrelated `receiver_user_id` column name.

The mechanics:

1. `handlers/games/high_card.py:20-29` calls `process_game_action(..., ephemeral_card=True)` for draw and swap.
2. In Phase A — **inside** the locked transaction, with no Telegram call — the handler captures the private text and the target group (`common.py:375-390`).
3. In Phase B it sends: `bot.send_message(chat_id=<group>, text=<card>, receiver_user_id=<user.id>, parse_mode=None)` (`common.py:530-540`).
4. Any exception degrades to a `callback.answer(..., show_alert=True)` alert (`common.py:541-556`); the neutral group render continues regardless.
5. The group board stays neutral because the High Card group renderer never includes the private card.

Tests pin the contract: `tests/games/test_g2_friendly_reveal.py:164-201` asserts the send carries `chat_id` + `receiver_user_id` + `parse_mode=None`; `:203-219` asserts the group text is neutral; `:251-280` asserts the alert fallback and that a failed ephemeral send does not block the group edit.

**Bot API / aiogram facts that constrain any new use** (Bot API 10.2 introduced ephemeral messages on 2026-07-14; 10.3 on 2026-08-24 replaced the flat parameters with `ephemeral_message_parameters`; verified against the installed aiogram 3.31.0):

| Rule | Consequence for Battleship |
|---|---|
| `receiver_user_id` and `callback_query_id` on the send methods are **deprecated** in favour of `ephemeral_message_parameters` (`aiogram/methods/send_message.py:61-68`); aiogram still accepts the flat form. `replace_callback_query_message` exists **only** on `EphemeralMessageParameters`. | High Card's flat call still works, but new code must use the object because the “replace in place” behaviour is only reachable there. |
| Any bot may send an ephemeral message **within 15 seconds** of an eligible incoming action, and it is delivered **to the exact client application that triggered the action**. | The panel must be an immediate response to the player's own tap. It can never be a reliable push to the other player, nor to the player's second device. |
| A bot that is a **chat administrator** may whisper to any non-bot member at any time, delivery still not guaranteed. | At best an optional “your turn” nicety. Never load-bearing. |
| `replace_callback_query_message=True` shows the ephemeral message **in place of the originating message** for that user; it must be `False` for callbacks that came **from** an ephemeral message. | This is exactly the desired “private overlay on the group board” and also dictates the post-shot behaviour (edit the panel, do not re-send it as a replacement). |
| `Message.message_id` is **0** for ephemeral messages; `Message.ephemeral_message_id` addresses them; editing/deleting require `(chat_id, receiver_user_id, ephemeral_message_id)` via `editEphemeralMessageText` / `editEphemeralMessageReplyMarkup` / `editEphemeralMessageCaption` / `editEphemeralMessageMedia` / `deleteEphemeralMessage` (`aiogram/client/bot.py:6558+`). Ids may be reused after expiry. | No existing “edit the message I was pressed on” helper may be reused for a panel, and an ephemeral id must never be persisted as the group board pointer. |
| A reply to an ephemeral message must itself be ephemeral, within 15 s. Not supported in channels or for business accounts. | Groups/supergroups only — which is where friendly matches live. |

### 1.8 Can we reuse the High Card pattern exactly?

**Reuse the shape, extend the mechanics.** The pattern's contract (commit first, best-effort private delivery, alert fallback, neutral public board, tests) transfers directly. The *implementation* is not sufficient, for five concrete reasons:

1. **Payload weight.** High Card whispers one inert text line (~30 chars, no keyboard). Battleship needs an 81-button grid, so the send must carry `reply_markup`, which High Card never exercises on an ephemeral message.
2. **Lifecycle.** High Card sends once and forgets. Battleship needs the same panel to be **edited in place** (shot result, stale-turn collapse, teammate read-only view), i.e. `editEphemeralMessageText` / `editEphemeralMessageReplyMarkup` — methods High Card never calls.
3. **The 15-second clock.** High Card does not pass `callback_query_id`; it is “fast enough” by accident. Battleship's post-shot presentation currently runs through `safe_edit_screen`, whose retry ladder can sleep through flood waits for up to ~30 s (`services/telegram_ui.py:242-311`: 3 attempts, `min(retry_after+0.5, 10.0)` each). If the panel send is sequenced after the group edit, it will miss the window. **Ordering becomes a hard requirement**, not a detail.
4. **The fallback is too small.** `callback.answer(text=…)` is capped at 200 characters, so a grid cannot ride an alert. Battleship needs a DM fallback and a coordinate fallback.
5. **No forfeit risk in High Card.** Battleship forfeits on the 90 s turn timeout (`core/config.py:77`, `services/timeout/service.py:76-105`), so a failed panel is not cosmetic — it loses the match.

---

## 2. TASK 2 — Design proposal

### 2.1 Principles

* **One public surface, one private surface.** Nothing viewer-specific may ever be written to the group message again.
* **Public = what everyone may know.** Rosters, both fleets' intact-cell counts, the ordered shot log, whose turn, the timer, the rematch button. Note that fleet health is derivable from public hits, so publishing both numbers symmetrically leaks nothing.
* **Private = one team's radar.** The 8×8 map of their own shots plus the actionable grid. Delivered only to a member of the team on turn; read-only for that team's other members.
* **The panel is a convenience, not the source of truth.** Every user-visible *fact* lives on the public board, so a lost whisper never hides game state.
* **Coordinates are public by design.** Only the *panel* is private. This single insight unlocks a fully playable last-resort fallback (§2.6, level 3).

### 2.2 Surface 1 — the public group board

Text (sample, single message, edited in place):

```
🚢 جنگ کشتیها (Battleship) — بازی رفیقانه

⚔️ تیم سرخ: آرش، سارا
🆚 تیم سبز: مهدی، نگار

🎯 نوبت: آرش (تیم سرخ) ⏱ ۹۰ ثانیه
📊 ناوگان: سرخ ۱۲/۱۴ — سبز ۹/۱۴

📜 شلیکهای این نبرد:
۱) آرش → C4: 🌊 خطا
۲) مهدی → E5: 💥 برخورد
۳) سارا → E6: 🔥 غرق (کشتی ۲ خانهای)
```

Rules for this surface:

* **No ASCII radar**, no second-person fleet phrasing, no live grid.
* Shot log: ordered, attributed, capped (last ~10 entries plus “و N شلیک قبلی”); the log itself becomes the game's public memory.
* Turn prompt always **names** the player (`🟢 نوبت آرش است!`), never “نوبت شماست” — reuse the honest-turn contract the codebase already documents (`games/base.py:19-42`) but bypass its “audience has one member” exception for friendly matches.
* Persistent hint on the first turns: `برای دیدن رادار، «🎯 رادار من» را بزن؛ فقط خودت میبینی.`
* Keyboard (identical for every viewer, so nothing is personalised in public):
  * row 1: `🎯 رادار من` → `bs_radar:{match_id}`
  * row 2: `🔄 بروزرسانی صفحه` → `game_refresh:{match_id}` (existing helper, `services/telegram_ui.py:207-236`)
  * finished: `🔁 بازی مجدد` (`get_friendly_rematch_keyboard`, `keyboards/party.py`)

### 2.3 Surface 2 — the private targeting panel

Text (sample):

```
🎯 رادار شما — تیم سرخ
🟢 نوبت شماست! یک خانه را هدف بگیر.

    A B C D E F G H
 1  · · 🌊 · · · · ·
 2  · · 💥 · · · · ·
 3  · · · · · 🔥 · ·
 …
📊 ناوگان دشمن: ۹/۱۴
🎯 آخرین شلیک شما: C4 — خطا
```

Read-only variant when it is not the viewer's turn (teammate opening it, or a stale panel):
`⏳ نوبت آرش است — فقط تماشا.` with no `bs_shot` callbacks.

Keyboard: the same 9×9 coordinate grid as today (`engine.py:383-404`) but with `bs_shot:{match}:{r}:{c}` on untouched cells, `noop` on shot cells, plus a footer row `🔄 بروزرسانی پنل` (`bs_radar`) and `✖️ بستن` (`bs_close` → `deleteEphemeralMessage`). 82 buttons is well inside Telegram's limits and identical to today's shape, so nothing about the rendering changes visually apart from *who* can see it.

### 2.4 Exact step-by-step flow

Notation: **A** = player on turn (team 1), **B** = waiting player (team 2), **G** = the group.

**Phase 0 — match start**
1. `friendly_start` renders the **public** board into the party message (`handlers/friendly.py:274-310`) and stores that id (`:109-126`). No panel is sent; no one has tapped anything, so no ephemeral message is possible.
2. If the first turn belongs to a bot, the bot task plays normally and only the public board is refreshed (`sib/bots/runner.py:381-387` → `services/game_sync.py`).

**Phase 1 — A opens the radar**
3. A taps `🎯 رادار من` on the public board (`bs_radar:{match}`).
4. Handler order (strict): (a) `answerCallbackQuery` immediately, (b) read-only DB load of match + state, (c) authorisation check *participant*, (d) compute the panel text/keyboard, (e) send the ephemeral panel with `receiver_user_id=A`, `callback_query_id=<this update>`, `replace_callback_query_message=True`, `reply_markup=<grid>`.
5. Result on A's client: the panel **replaces** the group board. Result on B's client and on every other member's: nothing changes at all. The group message itself is untouched.
6. Spectator taps the same button → private alert only (`این بازی رفیقانه است؛ فقط بازیکنها رادار دارند`), no panel, no group edit.
7. Teammate of A in 2v2/4v4 taps → read-only panel for the same team.

**Phase 2 — A shoots**
8. A taps a cell in the panel (`bs_shot:{match}:{r}:{c}`).
9. Handler: `answerCallbackQuery` immediately → Phase A transaction (row lock, `validate_action`, `apply_action`, commit) — unchanged from today (`handlers/games/common.py:280-433`). A stale panel is rejected by the engine's turn check (`engine.py:141`).
10. Phase B, in this order:
    a. **Panel first** (15 s window + the tapper is looking at it): `editEphemeralMessageText` on `(chat_id, A, ephemeral_message_id)` with the shot result and the panel collapsed to “شلیک شد — منتظر B”.
    b. **Then the public board**: edit `group1_message_id` with the new history line, new turn, new fleet numbers.
    c. Then schedule a bot turn if the next player is a bot (`common.py:569`), never awaited.
11. B's client now shows the public board: `🎯 نوبت: مهدی` and a new log line. B taps `🎯 رادار من` → their own panel replaces it locally. A's client is unchanged.

**Phase 3 — repeat until finished**
12. The finishing shot uses the existing neutral finale (`shape_friendly_final_text`, `services/friendly.py:188-205`; `get_friendly_rematch_keyboard`) so the result names the winning **team**, not “you”. Any open panel is edited to `🏁 بازی تمام شد — برنده: تیم سبز` with only a close button (or deleted with `deleteEphemeralMessage`).

**Edge flows**

| Situation | Behaviour |
|---|---|
| Stale panel tap (turn already moved, double tap, timeout) | Engine rejects → alert `این پنل قدیمی است` + panel edited to the read-only current-turn view + public board re-rendered from DB |
| Non-participant | Private alert; no panel; no group edit |
| Bot turn | No panel possible; public board shows `⏳ نوبت بات`; `sync_match_messages` renders the neutral board |
| Turn timeout | Forfeit unchanged (`services/timeout/service.py:76-105`); public board final; any open panel is stale and self-heals on next tap |
| 🔄 on the public board | Re-render the public board from DB only — never opens a radar |
| 🔄 inside the panel | `editEphemeralMessageText` for that same viewer, from DB |
| Restart / recovery / maintenance | Public board only (`services/game_sync.py`, `services/maintenance/service.py:199-203`) |
| Ephemeral unavailable | Level 1-4 of §2.6 |

### 2.5 Callback namespace

| Callback data | Found on | Effect |
|---|---|---|
| `bs_radar:{match_id}` | public board + panel footer + `replace_callback_query_message` re-open | Send or edit the caller's private panel |
| `bs_shot:{match_id}:{r}:{c}` | private panel cells only | Apply the shot through `process_game_action` |
| `bs_close:{match_id}` | private panel | `deleteEphemeralMessage` |
| `game_refresh:{match_id}` | both surfaces (existing prefix, `services/telegram_ui.py:47`) | Branch on whether `message.ephemeral_message_id` is present |
| `bs_action:{match_id}:{r}:{c}` | **legacy** keyboards already delivered | Transitional alias: answer with `پنل بازی بهروز شد؛ روی «رادار من» بزن` and re-render the public board. Remove after one release. |
| `noop` | labels/corners/shot cells | unchanged |

All strings stay far under Telegram's 64-byte `callback_data` limit (the longest is 25 bytes).

### 2.6 The ephemeral fallback ladder

| Level | Mechanism | Trigger | Privacy | Notes |
|---|---|---|---|---|
| 0 | `sendMessage` + `ephemeral_message_parameters{receiver_user_id, callback_query_id, replace_callback_query_message=True}` + grid | happy path | perfect | Panel replaces the board for that user only |
| 1 | Same, `replace_callback_query_message=False` | Level 0 rejected, or the callback originated from an ephemeral message | perfect | A separate whisper; slightly noisier |
| 2 | Normal DM: `chat_id=user.id`, same text + grid | Ephemeral send raised `TelegramBadRequest` / `TelegramForbidden` / any non-retryable error | perfect | Requires the user to have started the bot; `TelegramForbiddenError` → level 3. Buttons work identically; handlers must not assume `callback.message.chat.id` is the match group |
| 3 | **Typed coordinate in the group**: public board advertises `اگر پنل باز نشد بنویس: شلیک C4`; a text handler resolves `(match, player, r, c)` and reuses the same transaction | Level 2 unavailable | **safe** — the coordinate is public the instant the shot lands; the panel content is never posted | Keeps the game playable when every private channel fails; needs one text handler and the same turn/participant checks |
| 4 | `answerCallbackQuery(show_alert=True)` with a ≤200-char hint pointing at 🔄 and the typed syntax + a structured log event + a counter | all of the above failed | safe | Never contains the grid |

**Hard rule:** no level may ever post the radar/panel into the group. If nothing works, the player is told *how* to play, not shown the board.

**Product decision to take (see §4):** whether to pre-flight DM availability at match start (a one-time “برای بازی ربات را استارت کن” DM) so level 3 is rare, or to keep the flow silent and rely on the hint line.

### 2.7 Change map (design only)

| File | Change |
|---|---|
| `games/battleship/engine.py` | Split rendering into a **public** renderer (no radar, no per-side “you”, ordered log, symmetric fleet numbers) and a **team panel** renderer (today's view). Add `get_public_keyboard(match_id)` and keep the grid builder as the panel keyboard. Append a per-shot `shot_log` entry in `apply_action` (`by`, `team`, `r`, `c`, `result`, `sunk_size`). Declare a capability flag (e.g. `friendly_private_panel = True`) so the dispatcher can branch generically instead of hard-coding `game_code == "battleship"`. |
| `handlers/games/battleship.py` | New callbacks `bs_radar`, `bs_shot`, `bs_close`; keep `bs_action` as a transitional alias. |
| `handlers/games/common.py` | In the friendly branch, if the engine declares a private panel: render the **public** board, deliver the panel to the actor, and never call `_edit_game_callback` for an ephemeral message. Guard every `callback.message.message_id` use against the ephemeral 0. `_friendly_board_target` / `state_viewer_id` stay untouched for RPS and High Card. `handle_game_refresh` must branch by surface. |
| `services/telegram_ui.py` | New best-effort helpers: send panel, edit panel, delete panel — with the same “not modified is success / parse-mode degradation” conventions as `_run_edit_attempts` (`:242-311`), but **without** the multi-second flood ladder in front of the public-board edit. |
| `services/friendly.py` | No change to `friendly_audiences` / `viewer_audience` (other games still need them). Add a public-board audience helper: the panel needs the synthetic team audience, the public board needs none. |
| `handlers/friendly.py` | `render_friendly_board` uses the public renderer for Battleship on start/rematch/finish. |
| `services/game_sync.py` | Friendly branch renders the public board for panel-capable engines. |
| `handlers/games/common.py::handle_game_refresh` | Spectator refresh must not serve one player's perspective: public board only; the panel opens only via `bs_radar`. |
| `services/maintenance/service.py` | Only touches normal matchmaking boards (`group1_id`) — verify, no friendly path today. |
| `core/logging.py` | New events: panel sent / panel fallback level / panel delivery failure / shot via typed fallback. |
| `core/config.py` | Optional `BATTLESHIP_TIMEOUT` review (§3.7). |
| `services/config_store.py` (optional) | Feature flag `battleship_friendly_private_panel` if we want a staged rollout; otherwise write a `ui_version` marker into the state at match creation so a match never mixes board shapes. |

### 2.8 Test plan (for the implementation phase)

* **Engine:** public render contains no `رادار` grid, no `ناوگان شما`, and does contain the ordered log; team panel render preserves today's grid; panel keyboard marks `bs_shot` only for the current player; public keyboard has no `bs_shot`; `shot_log` is appended with attribution and stays JSON-serializable; hidden hull cells still never appear (keep `test_enemy_ship_positions_not_revealed_in_keyboard` green).
* **Handler:** panel send carries `receiver_user_id` + `callback_query_id` + `replace_callback_query_message`; shot edits the panel via `edit_ephemeral_message_text` and the group board via `edit_message_text`, in that order; the group text never contains a grid; **`group1_message_id` is never overwritten with 0 or with an ephemeral id**; a stale panel tap is rejected and resets the panel; non-participants get no panel; teammates get a read-only panel; bot turns touch only the public board.
* **Fallback:** levels 1, 2, 3, 4 each have a test with the corresponding exception injected (mirroring `tests/games/test_g2_friendly_reveal.py:251-280`).
* **Regressions:** `tests/test_battleship.py`, `tests/test_battleship_board_ux.py`, `tests/test_battleship_reliability.py`, `tests/games/test_g2_friendly_reveal.py`, plus a new `tests/games/test_bs1_friendly_privacy.py`.

### 2.9 Rollout

Gate the new rendering per match (state marker) rather than per deployment, so an in-flight match never changes keyboard shape mid-game. Watch for: panel send failure rate, fallback-level distribution, forfeits that follow a panel failure, and “wrong turn” alerts.

---

## 3. Technical challenges

1. **The 15-second ephemeral window versus the existing retry ladder.** The panel must be sent *before* the group-board edit, and the callback must be answered first. `safe_edit_screen` can spend up to ~30 s in flood waits (`services/telegram_ui.py:242-311`); sequencing the panel behind it guarantees expiry. This inverts today's presentation order and must be enforced by tests, not convention.
2. **`message_id == 0` for ephemeral messages.** Existing helpers assume a real id: `_edit_game_screen` (`common.py:49-69`), `_edit_game_callback` (`:71-85`), `_persist_board_message_id` (`:87-110`), `safe_edit_callback_message` (`services/telegram_ui.py:764+`), and the refresh path's `before_id/after_id` bookkeeping (`common.py:868-884`). Used on a panel they will either fail with “message not found” or **overwrite the group board pointer with 0**, permanently orphaning the match message. An explicit `callback.message.ephemeral_message_id is not None` branch is mandatory.
3. **No persistence of ephemeral identity — and ids are volatile.** The panel identity (`chat_id`, `receiver_user_id`, `ephemeral_message_id`) is only available in the update that carries the tap, which is convenient (zero schema work) but means edits are impossible from bot turns, timeouts or maintenance. Ids can also be reused after expiry, so an edit can legitimately 400 — the handler must fall back to sending a fresh panel while still inside the 15 s window.
4. **Delivery is not guaranteed, and only the triggering client receives it.** A player with phone + desktop who taps on the phone sees the panel only there; an offline player never gets a whisper. Nothing may depend on a panel arriving, which is why the public board must carry every verifiable fact.
5. **aiogram/API shape.** The flat `receiver_user_id` High Card uses is deprecated in 3.31 (`aiogram/methods/send_message.py:61-68`); `replace_callback_query_message` is only on `EphemeralMessageParameters`; the edit methods take flat `receiver_user_id` + `ephemeral_message_id` (`aiogram/methods/edit_ephemeral_message_text.py`, `delete_ephemeral_message.py`). The global default parse mode is Markdown (`bot.py:26-29`), so panel text must stay parse-safe exactly like the current board (names are already escaped into the state — `test_player_names_are_markdown_escaped_in_state`).
6. **The public shot log does not exist yet.** `teams[*].shots_taken` is a per-team dict of `"r,c" → hit|miss` (`engine.py:114-124`, `:185`/`:195`); it has no ordering across teams and **no author attribution**. `last_shot`/`last_event` keep only the most recent event (`:193-199`). A real history needs a new state field appended in `apply_action`, plus a defined rendering for matches that started before the deploy (best-effort history from `shots_taken`, or “history from the next shot on”).
7. **Timeout forfeit versus panel failure.** Battleship forfeits after 90 s (`core/config.py:77`, `services/timeout/service.py:76-105`). If the panel cannot be opened and the player has no fallback, the privacy fix *causes* losses. Level 3 (typed coordinate) is the mitigation; the timeout policy (bump to 120 s, or skip the forfeit when the player's panel delivery failed in this turn) is a product decision.
8. **Two surfaces, two failure modes.** The public board must remain correct if every panel dies, and the panel must remain useful if the board edit fails. The existing “DB authoritative, presentation best-effort” contract (`handlers/games/common.py:1-13`) already gives the right frame; the new risk is *ordering*, not authority.
9. **Explicit authorisation for the panel.** Today the only check is turn ownership (`engine.py:141`); `bs_radar` needs a participant check and a team/read-only decision, neither of which exists as an engine API (`state["players"][id]["team"|"team_code"]` is available — `engine.py:80-96`). Spectators pressing 🔄 today already get the active player's view (`common.py:838-846`, `:857-866`); that hole must close in the same change.
10. **Inline keyboards on ephemeral messages are a young surface.** They are documented (ephemeral pickers exist in the ecosystem) but this codebase has never used them. Staging must verify that a tap on an ephemeral message arrives with `message.ephemeral_message_id`, that `replace_callback_query_message` and `reply_markup` coexist, and that edits keep the keyboard. If they do not, the panel degrades to a DM (level 2) and ephemeral becomes decoration.
11. **Forum topics and `message_thread_id`.** Nothing in the codebase threads boards today; if a friendly match is played inside a topic, panel and board may diverge. Out of scope, but the panel send should mirror whatever thread id the board uses once threading exists.
12. **Message length and edit churn.** A full log can overflow 4096 characters; cap it. Every turn is now one ephemeral call plus one public edit (plus one panel open), which is still well inside flood limits, but retries must be idempotent — a duplicate tap must not produce a second panel or a second log line.
13. **`friendly_audiences` must survive the refactor.** RPS (`games/rock_paper_scissors`) and High Card depend on `viewer_audience` / `state_viewer_id`; the Battleship split must not change those helpers' behaviour, only stop using them for the public board.
14. **Duplicate/stale handling for Battleship is currently missing.** The silent-duplicate whitelist covers only RPS and High Card (`common.py:352-372`); once the grid lives in a panel, stale taps become common, so Battleship needs the same treatment plus the “panel reset” behaviour.

---

## 4. Open questions / decisions needed

1. **Panel placement:** replace the group board in the player's client (`replace_callback_query_message=True`, recommended) or send a separate whisper (level 1)? Replacement is cleaner but hides the history from the shooter while they aim — mitigate by echoing the last three log lines in the panel footer.
2. **One tap per turn is unavoidable** for a non-admin bot. Do we accept it, or also offer an admin-only “your turn” whisper as a convenience (delivery not guaranteed)?
3. **Level 3 syntax:** `شلیک C4` as a plain message, or as a reply to the board message (cheaper to authorise)? Persian vs Latin digits must be normalised.
4. **Timeout policy:** keep 90 s, raise it, or suspend the forfeit when this turn's panel delivery failed?
5. **Legacy keyboards:** keep the `bs_action` alias for one release or fail fast with a “re-open the board” hint?
6. **Spectators:** should a non-participant ever get a panel (e.g. a post-game replay)? Proposal: no.
7. **`ui_version` marker in state** versus a global feature flag — recommended: per-match marker so a rollout never produces half-old, half-new boards.

---

## 5. Appendix — citation index

| Claim | Evidence |
|---|---|
| Fleet is generated randomly at match start, no placement UI | `games/battleship/engine.py:33-69`, `:103-104`, `:114-124` |
| Turn/validation rules, public `last_shot`/`last_event` | `engine.py:132-157`, `:159-212`, `:193-199` |
| Radar markers only for already-shot cells (no hull disclosure) | `engine.py:246-269`, `:255-262` |
| Text renderer with per-viewer lines | `engine.py:271-363` (`:300-308`, `:344-351`, `:356-359`) |
| Keyboard with live cells only for the current player | `engine.py:365-404` (`:383-389`, `:397-402`) |
| Battleship callback handler | `handlers/games/battleship.py:10-18` |
| Friendly = one group, one message | `services/friendly.py:291-297`, `handlers/friendly.py:109-126`, `:274-310` |
| Synthetic team audiences | `services/friendly.py:86-111`, call at `:347` |
| Viewer election = player on turn | `services/friendly.py:131-166` |
| Friendly board renders use that viewer | `handlers/games/common.py:155-172`, `:735-798`, `:857-866`; `handlers/friendly.py:63-106`; `services/game_sync.py:78-113` |
| Any group member may refresh a friendly board | `handlers/games/common.py:838-846` |
| “نوبت شماست” can appear on a shared board | `games/base.py:19-42` (`:40-41`) |
| Bot turns re-render via `sync_match_messages` | `sib/bots/runner.py:381-387` |
| 90 s turn timeout → forfeit | `core/config.py:77`, `services/timeout/service.py:76-105` |
| High Card ephemeral send + alert fallback | `handlers/games/high_card.py:20-29`, `handlers/games/common.py:375-390`, `:530-556` |
| Ephemeral contract tests | `tests/games/test_g2_friendly_reveal.py:164-201`, `:203-219`, `:251-280` |
| Edit retry ladder can sleep ~30 s | `services/telegram_ui.py:242-311` |
| Refresh button helper | `services/telegram_ui.py:47`, `:207-236` |
| aiogram 3.31 field shapes | `aiogram/methods/send_message.py:61-68`; `aiogram/types/ephemeral_message_parameters.py`; `aiogram/types/message.py:165`, `:394-397`, `:895-916`; `aiogram/methods/edit_ephemeral_message_text.py`; `aiogram/methods/delete_ephemeral_message.py`; `aiogram/client/bot.py:6558+` |
| Bot API ephemeral rules (15 s window, admin exemption, replacement, `message_id = 0`) | Telegram Bot API 10.2 (2026-07-14) and 10.3 (2026-08-24) changelog, “Ephemeral Messages and Commands”, `Message`/`EphemeralMessageParameters`/`ReplyParameters` fields |
| Existing Battleship test suite to keep green | `tests/test_battleship.py` (148), `tests/test_battleship_board_ux.py` (457), `tests/test_battleship_reliability.py` (287) |
