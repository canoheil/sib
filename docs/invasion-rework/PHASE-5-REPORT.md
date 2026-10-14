# Phase 5 — Telegram presentation and local Invasion v2 art

## Asset discovery before imaging

`services/character_art.py::artwork_path(character)` maps `Character.code` to
`assets/characters/{code}.jpg`; e.g. `assets/characters/sib_sabz.jpg` is the
existing framed Main Card used by the sample join. `Character.artwork` is an
asset registry key, which may resolve through `AssetService.file_id` to a
Telegram file ID (not a local compositable image). Existing bot screens do
not use a local Persian font for their Telegram text; the repository does
track `.agents/skills/persian-writing/assets/fonts/Vazirmatn-Bold.ttf`, used
here with Pillow libraqm or local reshaper/bidi fallback. No network calls
are made by imaging. Missing font or local required card art is an explicit
error, not a guessed replacement.

## Implementation / output

- `services/invasion_v2/imaging/{__init__,templates,backgrounds,compose,cache}.py`:
  five gradients/apple silhouettes, 1280×720 announcement/join/victory/defeat
  composition, Persian font checks, atomic SHA-256 JPEG cache at
  `~/.cache/inv2/` and 30-day prune at bot startup (`bot.py`). Join keys are
  `(invasion_id,user_id,damage,is_crit,enemy_type)`; result keys are
  `(invasion_id,status)`. Font shaping dependencies added to `requirements.txt`.
- Sample images (committed, not production messages):
  - `docs/invasion-rework/samples/soldier-announcement.jpg`
  - `docs/invasion-rework/samples/shadow-join.jpg`
  - `docs/invasion-rework/samples/king-victory.jpg`
  - `docs/invasion-rework/samples/brute-defeat.jpg`
- `services/invasion_v2/presentation.py` announces PENDING rows with an image,
  narrative and one join button; on successful send saves message IDs and
  transitions to ACTIVE with a 300-second default window. A join posts a new
  reply photo; terminal results edit the announcement caption/remove buttons
  and send a separate result photo with the recorded victory reward list.
  All group Telegram sends/edits and callback answers pass through `_telegram`
  and Phase 4's circuit-breaker success/failure methods. Admin-only private
  previews use the same exception-safe wrapper **without a group circuit
  state** so they cannot write DB state; this is the necessary exception to
  group-metering for private-chat, read-only preview sends.
- `handlers/inv2_join.py` uses the existing Main Card/level-derived power,
  computes a single domain attack, guards membership, status, announcement
  origin and window, then CAS-updates HP and records a unique attack in one
  transaction. It acknowledges before detached image work. On victory it
  settles rewards/state, then posts join and final result best-effort.
- `services/invasion_v2/rewards_service.py` grants through `ItemService.grant`
  with real keys `apple_gold`, `apple_diamond`, `apple_obsidian`; Phase 3's
  independent 10% and 2% rolls are recorded in `inv2_rewards`. The invasion
  row lock + prior-reward guard makes replay safe, and item grants/reward rows
  belong to the caller's single transaction. `settle_outcome` sets the region
  loss/last-invasion fields and **assigns** group victory/defeat counters from
  durable terminal invasion counts, making repeated settlement idempotent.
- `handlers/inv2_admin.py` registers admin-filtered `/inv2_preview <enemy_type>`
  (four photos in a private album, placeholder only if the admin has no Main
  Card) and `/inv2_simulate <group_id> <enemy_type> <user_count>` (read-only,
  fails closed without an authoritative group average). Neither posts to a
  group, creates a row, nor enables the feature. Commands issued from a group
  are ignored without replying there (the preview must remain private).
- No new migration; existing inv2 tables suffice. No changes to
  `invasion_enabled` were made. Startup recovery still does **no** rewards or
  Telegram sends; maintenance invokes presentation separately only when the
  flag is enabled and a Bot instance exists. The Phase 4 DB-only scheduler
  still makes no Telegram calls or grants itself.

## Verification / caveats

- Isolated SQLite + mocks: `PYTHONPATH=/home/user:/home/user/sib .venv/bin/pytest
  -q tests/invasion_v2 tests/test_invasion_feature_flag.py` → **126 passed**
  (15 new Phase 5 tests, zero live Telegram or production DB calls).
- Focused line coverage: `presentation.py` **87%**, `inv2_join.py` **92%**;
  combined **89%**, measured under the locally installed SQLAlchemy 2.0.44.
- Caveats: a successful Telegram photo send followed by a failed DB commit
  may be resent on retry; Telegram has no shared transaction with the DB.
  Terminal photo delivery has no durable sent marker, so a crash after a
  terminal transition can omit its final photo; retrying `finalize` manually
  may post an extra result photo. Phase 5 introduces no schema/migration for
  a durable Telegram outbox. The existing framed JPEG may include English
  lettering baked into the art. Source art for a card that exists only as a
  Telegram file ID is unavailable offline. Admin private messages have no
  group to circuit-break. Region mapping and post-Phase-5 cleanup remain as
  described in the earlier design documents.
- The actual **live DB** value of `invasion_enabled` is **not verified**:
  this work did not open a production DB, start the bot, enable the flag, or
  send to a real group. Migration 036 originally seeds `false` if missing;
  no Phase 5 code writes that config key. The feature remains gated and should
  not be enabled until operational review of the outbox/retry limitations.
