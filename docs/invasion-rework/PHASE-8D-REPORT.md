# Phase 8D — Live HP on Announcement + Damage in Join Message

Status: complete. Feature remains guarded behind `system_config.invasion_enabled`
(seed value `false` — **not touched, still false**).

## 1. Where the captions are built

All presentation-layer work lives in `services/invasion_v2/presentation.py`.
`narrative.py` and the narrative pool are **unchanged**; the HP/damage lines are
built at the presentation layer only.

| What | Location |
|---|---|
| Word-boundary truncation helper | `presentation.py:65` `_fit_caption` |
| Announcement HP line (Persian digits) | `presentation.py:75` `announcement_hp_line` |
| **Announcement caption builder** | `presentation.py:80` `build_announcement_caption` |
| **Join caption builder** | `presentation.py:85` `build_join_caption` |
| Announcement caption composed in `announce()` | `presentation.py:217-220` |
| Announced narrative persisted (`inv.announcement_narrative`) | `presentation.py:234` |
| Join caption composed in `on_join()` | `presentation.py:272-275` |
| Metered caption edit (`_edit_announcement_caption`) | `presentation.py:95` |
| State read + caption rebuild for one edit (`_apply_hp_edit`) | `presentation.py:119` |
| Debounce table + delay constant | `presentation.py:161-162` (`_pending_edits`, `HP_EDIT_DEBOUNCE_SECONDS = 1.5`) |
| `schedule_hp_edit` / `_debounced_hp_edit` / `cancel_pending_hp_edits` | `presentation.py:165` / `:173` / `:185` |
| Kill-path synchronous edit call | `presentation.py:285` |
| Non-kill debounce scheduling call | `presentation.py:287` |
| Shutdown cancellation hook | `bot.py:101,105` |

Persian digits reuse the existing `fa_digits` helper from `sib.services.sib_claim`
(`handlers/daily_chest.py` and `handlers/marriage.py` already import it the same way).

## 2. Exact caption templates

Announcement (sent once, edited on every join). `{current_hp}` =
`inv2_invasions.enemy_hp_remaining`, `{total_hp}` = `inv2_invasions.enemy_hp`,
both rendered with `fa_digits`:

```
{narrative}

⚔️ سلامتی شیاسیب: {current_hp} / {total_hp}
```

The announcement narrative itself is rendered exactly as before by
`narrative.render_announcement(...)` (same fixed arguments and `random.Random(inv.id)`
seed); only the HP block is appended.

Join messages (one per participant, photo reply to the announcement):

```
normal:  {narrative}\n\n🔥 آسیب وارده به دشمن: {damage}
crit:    {narrative}\n\n💥 آسیب وارده به دشمن: {damage}
kill:    {narrative}\n\n🏆 آسیب وارده به دشمن: {damage}
```

- `{damage}` is this attack's committed `inv2_attacks.damage`, Persian digits.
- Kill marker: **🏆** (chosen per task's "any consistent marker"). Precedence
  matches the narrative pool's: a critical finishing blow is a kill → 🏆 wins
  over 💥.
- The damage line is on its own line after the narrative, separated by one
  blank line (a single `\n\n`), i.e. `…`-truncated narrative (when needed)
  ends the narrative paragraph, then a blank line, then the damage line.
- The old trailing `💥 ضربه کاری!` line on crit joins was removed: the 💥
  damage line is now the crit indicator (spec: use 💥 instead of 🔥 to
  visually indicate a crit).

Caption length: `_fit_caption` guarantees ≤ 1024 chars for both announcement
and join captions. If `narrative + suffix` exceeds 1024, the narrative is cut
at the last word boundary inside the remaining budget and `…` is appended
before the blank line; the status line is never truncated.

## 3. Throttling (per-invasion 1.5 s debounce)

Module-level in `presentation.py`:

```python
_pending_edits: dict[int, asyncio.Task] = {}   # invasion_id -> task
HP_EDIT_DEBOUNCE_SECONDS = 1.5

def schedule_hp_edit(bot, invasion_id):        # :165
    existing = _pending_edits.get(invasion_id)
    if existing is not None and not existing.done():
        return                                  # coalesce into the pending task
    _pending_edits[invasion_id] = asyncio.create_task(_debounced_hp_edit(bot, invasion_id))

async def _debounced_hp_edit(bot, invasion_id):  # :173
    try:
        await asyncio.sleep(HP_EDIT_DEBOUNCE_SECONDS)
        await _apply_hp_edit(bot, invasion_id)  # re-reads HP from DB, edits once
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(...)
    finally:
        _pending_edits.pop(invasion_id, None)
```

- Multiple joins inside one window coalesce: only the first `schedule_hp_edit`
  creates a task; later calls are no-ops. The single edit reads the **latest**
  committed HP, so the caption always lands on the newest value.
- On the **killing blow** the edit is *not* debounced: `on_join` awaits
  `_apply_hp_edit(bot, inv.id, allow_victory=True)` synchronously
  (`presentation.py:285`) so the group sees `0 / {total_hp}` **before** the
  victory flow rewrites the caption (task 4: do not skip the announcement
  edit in the kill path). No debounce task is created for kills.
- `_apply_hp_edit` refuses to edit non-live battles (PENDING → no announcement
  to edit; DEFEAT/VICTORY → the finalize narrative owns the caption), with the
  `allow_victory=True` exception for the synchronous kill edit. This is what
  makes "no edit after finalize" hold even if a debounced task from an earlier
  join is still sleeping when the battle ends.
- Shutdown: `bot.py` finally-block calls `await cancel_pending_hp_edits()`
  (`bot.py:105`) alongside the existing bot-turn/narration cancellations, so
  shutdown never leaves a half-run edit task (task 3 requirement).
- The join callback flow is otherwise unchanged: commit → ack → detached
  delivery task (photo, then kill-edit/finalize or debounced edit). The edit
  never blocks the join: failures are contained in the metered wrapper.

## 4. Circuit breaker integration

The edit has its own metered path (`_edit_announcement_caption`,
`presentation.py:95`) instead of reusing `_telegram` (which records *every*
failure):

| Outcome | Logging | Breaker |
|---|---|---|
| success | – | `record_send_success(group_id)` (resets the failure streak, same as all other sends) |
| `TelegramBadRequest` "Message is not modified" | `logger.debug` (swallowed) | **none** (expected back-to-back join no-op) |
| any other `TelegramBadRequest` / `TelegramForbiddenError` / exception | `logger.warning` | exactly **one** `record_send_failure(group_id)` |

Recording helpers `_breaker_failure` / `_breaker_success` are shared with the
existing `_telegram` wrapper (which was refactored onto them, behavior
identical). The edit never raises into the join flow and never re-blocks it;
the breaker threshold/mute logic itself is untouched.

## 5. Stored announcement message ids / narrative

- `message_id` / `message_chat_id` were **already** stored on the invasion row
  by `announce()` — no change needed (task 1 "if not already stored").
- New: `inv2_invasions.announcement_narrative` (TEXT, nullable) —
  migration `database/migrations/versions/041_inv2_announcement_narrative.py`
  (new head, down_revision `040_inv2_difficulty_default`). The rendered
  narrative is stored when the announcement is sent. This is required because
  the narrative pool keeps process-local per-key history, so re-calling
  `render_announcement` at edit time would (almost) always pick a *different*
  template and visibly rewrite the group message. With the stored narrative,
  edits change **only** the HP line.
- Legacy rows (NULL narrative, only possible for invasions announced before
  this deploy — short-lived 5-minute windows make this near-impossible) fall
  back to a best-effort re-render in `_apply_hp_edit` (`presentation.py:144-147`).

## 6. Task 5 — participant count: skipped

The `inv2_attacks` count is not stored on the invasion row, so showing
`👥 مدافعان: {n}` on the debounced edit would require a second
`COUNT(*)` query per edit — exactly what the task said to avoid. Skipped per
spec; no participant line is added to the announcement caption.

## 7. Sample captions

Before (announcement, fresh invasion, HP 200):

```
پیش از آنکه زنگ آخر در درّه ماه بنوازد، خود را به بارو برسانید. شیاسیب پیاده راه کاروان‌ها را بسته است؛ پشت این دیوار، کودکانی چشم به راه بازگشت خانواده‌هایشان دارند. مدافعان، نگذارید آن چشم‌انتظاری به ترس بدل شود.

⚔️ سلامتی شیاسیب: ۲۰۰ / ۲۰۰
```

After one join (debounced edit ~1.5 s later; narrative unchanged, HP only):

```
⚔️ سلامتی شیاسیب: ۱۳۳ / ۲۰۰        ← only this line changes in the message
```

Join photo captions:

```
normal:  …هنوز ۱۳۳ از توانش باقی بود.

🔥 آسیب وارده به دشمن: ۶۷

crit:    …ضربه با نیروی ۶۷ به سپر پیشرو خورد…

💥 آسیب وارده به دشمن: ۶۷

kill:    …مردم صدای گشوده شدن درِ پناهگاه را شنیدند.

🏆 آسیب وارده به دشمن: ۶۷
```

After the kill, the announcement shows `⚔️ سلامتی شیاسیب: ۰ / ۲۰۰`, then the
victory flow proceeds as before (finalize rewrites the caption with the
victory narrative and sends the result photo — unchanged).

## 8. Tests

`tests/invasion_v2/test_announcement_hp.py` (new, SQLite-only DB + mocked
Telegram, same harness as the phase 5/8C suites):

- `test_announcement_caption_contains_hp`
- `test_join_edits_announcement_to_new_hp`
- `test_kill_edits_announcement_to_zero`
- `test_edit_failure_does_not_break_join`
- `test_not_modified_is_swallowed`
- `test_throttle_coalesces_edits`
- `test_no_edit_after_finalize`
- `test_join_caption_contains_damage_line`
- `test_crit_uses_bomb_emoji`
- `test_kill_uses_win_emoji`
- `test_join_caption_stays_under_1024_chars` (×4 crit/kill params)
- `test_join_caption_truncates_narrative_if_needed`

Updated join tests: `tests/invasion_v2/test_presentation_phase5.py`
(caption assertions now expect the damage line; the shared `db` fixture now
cancels pending HP-edit tasks on teardown), `tests/invasion_v2/test_phase8c.py`
(migration head advanced to 041).

Results (Python 3.11, `pytest tests/invasion_v2/`):

```
422 passed in ~17 s   (407 pre-existing + 15 new; 0 failed, 0 errors)
```

The wider `tests/` tree has pre-existing environment failures in this sandbox
(no MariaDB for row-lock/DB fixtures; e.g. test_wiki, test_wallet_ledger_keys,
sib_kiss/hug concurrency). Verified identical on the pristine baseline commit
`3bc76c9` via a separate worktree run of the same files — none are caused by
this phase.

## 9. Limitations / edge cases

- **Caption length**: hard-capped at 1024 with word-boundary truncation + `…`
  on the narrative; the HP/damage line itself is short (~40 chars) and is
  never cut. Telegram's actual limit is 1024; we use it exactly (no extra
  headroom) since the status line must survive.
- **`reply_markup` on edit**: `edit_message_caption` is called without
  `reply_markup`, which for photo messages keeps the existing inline keyboard
  (the "⚔️ پیوستن به نبرد" button stays alive throughout the battle).
- **Back-to-back joins**: coalesced by the debounce; the residual
  "Message is not modified" race (caption identical to the live one) is
  swallowed at DEBUG, no breaker penalty.
- **Kill edit timing**: synchronous inside `on_join`, i.e. after the join
  photo send and before `finalize`; a failed kill edit (e.g. network blip) is
  logged + breaker-counted but the victory flow still proceeds (the victory
  caption will show the final state anyway).
- **Debounce vs. expiration**: if the join window expires while a debounced
  edit is still sleeping, the edit fires against a DEFEAT row and is skipped —
  the defeat narrative owns the caption.
- **No participant count line** (task 5 skipped, see §6).
- **Restart during a debounce window**: pending in-memory tasks are lost on
  process restart; the announcement simply keeps its last-edited HP until the
  next join or finalize. (Invasions are short-lived; acceptable.)
- `invasion_enabled` remains `false`; nothing in this phase enables, reads
  or seeds the flag.
