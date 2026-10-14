# Phase 8H — the invasion announcement is pinned so the group cannot miss it

Status: shipped. One production module touched for behaviour
(`services/invasion_v2/presentation.py`), one nullable column
(`inv2_invasions.pinned_at`, migration `042_inv2_pinned_announcement`), nine
new tests. No narrative, scheduler, resolver, reward or circuit-breaker logic
changed, and `invasion_enabled` was not read, written or seeded by this phase.

## Reported symptom

An invasion would start and the announcement — the only message carrying the
«⚔️ پیوستن به نبرد» button — scrolled away in a busy group, so members never
saw it and never joined.

## Behaviour now

1. When the announcement photo is delivered, the bot pins it in the group
   **with a notification** (`disable_notification=False`), which is what makes
   every member see it.
2. When the *next* invasion of that group is announced, the bot pins the new
   announcement first and then unpins the previous one. If the unpin is
   refused, the new message is still pinned (the explicit fallback requested).
3. Finishing a battle does **not** unpin: the announcement stays pinned until
   the next invasion replaces it.

## What tracks the pin

`inv2_invasions.pinned_at` (nullable `DATETIME`, migration 042, head after
`041_inv2_announcement_narrative`):

- stamped **only** after Telegram accepts the pin;
- cleared **only** after that message's unpin is accepted;
- `NOT NULL` therefore means "this message is the pin the bot placed", so the
  bot never pins twice, never unpins a message an admin pinned themselves, and
  never touches another group's pin (the lookup is scoped by `group_id`);
- pre-existing rows are `NULL`, i.e. invisible to the unpin step;
- a transient unpin failure keeps the row marked, so the next invasion retries.

Because `_spawn_if_due` allows at most one `PENDING`/`ACTIVE` invasion per
group, "the previous pin" is always unambiguous.

## Three rules the implementation had to respect

| Risk | Why it would break the system | What the code does |
|---|---|---|
| Circuit breaker | `_telegram()` records a breaker failure for every `TelegramBadRequest`/`TelegramForbiddenError`; three in a row mute the group for 24h (`invasion_circuit_breaker_threshold`). A bot without pin rights would silently kill invasions in that group — and a successful pin would wrongly reset a real failure streak. | Pin/unpin go through the new `_unmetered()` helper: logs, never raises, never touches the breaker. |
| Transaction safety | A pin exception raised inside the announcement transaction would roll back `message_id` + `ACTIVE` after Telegram already delivered the photo, so the next tick would announce the same invasion again. | `pin_announcement()` is awaited **after** the announcement transaction commits, and wraps everything in its own `try/except`. |
| Bots without the method | Preview/test doubles are `SimpleNamespace`s; `bot.pin_chat_message` would raise `AttributeError`. | `_unmetered()` resolves the method with `getattr(..., None)` and no-ops when it is missing. |

## Operational requirement

The bot must be an **admin with the "Pin Messages" right** in the group. If it
is not, nothing breaks: the pin is skipped and a single WARNING
(`inv2 pin_chat_message rejected group=…`) is logged per announcement, while
the invasion runs normally. Telegram also posts its usual "pinned a message"
service message in the chat.

Deployment: `alembic upgrade head` (adds `pinned_at`). The downgrade drops the
column; do it only after rolling back app code that reads it.

## Tests

`tests/invasion_v2/test_announcement_pin.py` (9 tests):

1. `test_announcement_is_pinned_with_notification` — pin called with the stored
   chat/message and `disable_notification=False`; `pinned_at` stamped.
2. `test_next_invasion_unpins_previous_and_pins_new` — second invasion pins
   456 and unpins 123; `pinned_at` moves from the old row to the new one.
3. `test_other_groups_pins_are_left_alone` — a different group's announcement
   never unpins ours.
4. `test_pin_rejection_keeps_the_invasion_live_and_the_breaker_clean` — a
   "not enough rights" rejection leaves the invasion `ACTIVE` with its
   `message_id`, leaves `pinned_at` NULL, unpins nothing, and does **not**
   call `record_send_failure`.
5. `test_bot_without_pin_support_still_announces` — a bot lacking the method
   announces normally.
6. `test_failed_unpin_leaves_the_previous_row_marked_for_retry`.
7. `test_duplicate_announce_does_not_pin_twice`.
8. `test_finalize_keeps_the_announcement_pinned` — pins the agreed
   unpin-only-on-next-invasion behaviour.
9. `test_migration_042_adds_and_drops_pinned_at` — upgrade/downgrade round trip
   on a real SQLite file keeps existing rows intact.

`tests/invasion_v2/test_phase8c.py::test_migration_041_is_new_head` became
`test_migration_chain_head`: it now pins head `042_inv2_pinned_announcement`
and asserts the 040 → 041 → 042 chain.

Run: `tests/invasion_v2` + `tests/test_invasion_feature_flag.py` — 461 passed
(452 before this phase).
