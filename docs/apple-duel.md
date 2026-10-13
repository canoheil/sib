# Apple Duel — audit and decisions (2026-10-13)

## Phase 0: read-only audit
Initial `git status --short` was empty; branch arena/650356e1-sib.

* Commands: `handlers/apple_battle.py`, `cmd_battle`, `ab_pick` etc. Shared matchmaking uses Party/Match/MatchPlayer. Duel must not call it.
* Engine: `services/apple_battle.py`, `AppleBattleService._card_stats`, `_effective_score`, `compute_and_persist_result`. Current shared build_match_state uses the SAME logistic weighted random formula as standalone precompute. Hidden character modifiers are applied; there is NO mechanical event log, matchup/passive activation simulation or draw output. Do not introduce any of those as invented mechanics. Equal scores are NOT a draw (probability .5).
* Existing result: `database/models/apple_battle.py`, AppleBattle has cards, participants, seed, balance version, result, reward flag. It lacks inviter/chat, invitation status, snapshot and presentation cursor. Use it for results, add minimal invitation envelope, not a parallel engine.
* Main Card: `services/player_cards.py`, `get_main_card` / `set_favorite`, PlayerCard.is_favorite. Selection uses User mutex then ascending card locks; UI is `handlers/cards.py`, card_panel and card_main_set callbacks, accessed from collection in handlers/start.py. Queue loadout is a DIFFERENT selection; do not change it.
* Trading locks cards ascending and rejects pending overlapping trades. Upgrade locks User then cards. Accept must lock cards and recheck ownership; freeze effective stats and character balance metadata before presentation.
* Economy: SeedlingInventory / SeedlingAward with unique (source, source_ref_id, user_id). AppleBattleService._award_seedling already awards exactly one per battle; complete also grants five apples and must NOT be used for duel. SeedlingService inventory and ledger remain authoritative.
* Telegram: aiogram 3, legacy Markdown default, safe_* helpers in services/telegram_ui.py; PanelOwnershipService and PanelGuardMiddleware protect personal card panels. Public invitation callbacks need their own recipient/chat/message checks.
* Lifecycle: MaintenanceService.run_once/run_forever (5 sec) and RecoveryService. DB-driven deadlines/cursor, not sleep-only tasks. Existing narration is at-most-once with cursor committed before Telegram send; use this same delivery contract.
* Schema: SQLAlchemy 2, Alembic revisions through 034_card_affiliations; init_db also calls create_all. Additive migration must tolerate create_all on fresh installation; no destructive downgrade.
* Tests: pytest/pytest-asyncio, tests/conftest.py refuses any URL without sibcom_sib_test; SQLite ignores FOR UPDATE. No lint/type configuration found. Dependencies absent in sandbox initially. Use isolated local SQLite; real locking tests require MariaDB and must not be represented as SQLite-proven.
* Rendering: services/character_art.py resolves registry/bundled framed art. scripts/frame_card.py already uses Pillow; no runtime composition service exists. Add small dynamic presentation renderer using existing art resolver and Pillow dependency, no per-pair static images.

## Risks / planned changes
Invitation transitions must CAS and lock User rows in stable order; block incompatible shared sessions bidirectionally through SessionService. Do not consume normal loadouts. Pending overlapping trades make a selected card unavailable. Lock result and reward in the same transaction; failed payment must roll back acceptance too. Telegram is not transactional: skipped intermediate scenes are acceptable after crash; final persisted report remains retrievable and retried. Cursor and next due time persist independently of outcome/reward. Terminal message edits must be best effort.

Likely paths: new models/apple_duel, services/apple_duel, handlers/apple_duel, presentation renderer; models and handlers registration; maintenance, session guard, player_cards Main Card guard; config; Alembic 035; tests and docs. Engine formula and regular completion remain untouched.

## Product/technical decisions
* Pending invitations reserve both players globally; accepted invitations reserve them during the presentation, then release. No extra level constraint or daily quota.
* Default expiry 60 seconds. Six scenes with five 4-second gaps (20 seconds), configurable. Deadlines use UTC server time.
* Engine currently never draws. Adapter supports an explicit future winner=None as a draw, resolves via secrets.choice, and stores reason; never treat equal scores as draw.
* No fabricated damage/passive/counterattack claims. Cinematic encounter captions describe atmosphere and the stored final result, not unrecorded mechanics.
* Selection opens a separate existing collection/card panel. Original invitation stays untouched and is the durable return context. Players return to its accept/reject buttons; no new selection callbacks.
* Result and seedling are committed together before any scene. No apples, XP, ordinary match stats or matchmaking records are created.
* Telegram intermediate delivery at-most-once; final board retry is at-least-once. A transport timeout can produce duplicate final messages, never duplicate results/rewards. No exactly-once Telegram promise.

## Minimal shared compatibility fix (documented before final regression)
`services/character_art.py::resolve_character_photo` called nonexistent `AssetService.get_file_id` for registered artwork. The real API is `AssetService.file_id`. One-line correction is required so dynamic duel art can honor the existing registry. Test `test_renderer_uses_registered_and_bundled_art` checks registered-file precedence, bundled fallback and actual composition. It changes no battle formula, balance or economy. requirements now requests SQLAlchemy[asyncio] because the fresh sandbox import failed without greenlet, and adds Pillow (already used by frame_card, now required for runtime composition).

The legacy AppleBattle history/leaderboard reads duel rows once, as ordinary standalone battle results; there is no additional MatchPlayer, PlayerStats or XP grant. No duplicated win counters are written.

MySQL compatibility: `SessionService.live_party_for_user` and `live_match_for_user` accept optional `lock=True` (default behavior unchanged). Duel validation uses those existing queries as locking current-reads after the User mutex, avoiding stale REPEATABLE READ snapshots opened during user registration. The new duel predicate also uses a current-read on AppleDuel, with no slot→invitation lock inversion. Shared session formula/status constants are unchanged. SQLite cannot prove this lock behavior; MariaDB race tests remain required before deployment.

Pending-trade eligibility is a locking current-read too. Existing trading takes its own trade/card locks, so InnoDB may choose a transaction as a deadlock victim under contention. The callback rolls back its entire transaction and gives a Persian retry message; it never proceeds using stale trade eligibility. Main Card candidates are validated again under current card locks; a concurrent switch can safely refuse the first acceptance attempt without consuming the invitation.

## Operational configuration and timing
Install `requirements.txt`; local tests additionally need `aiosqlite` (and `ruff` for lint). Run Alembic using a reviewed non-production URL first. Revision 035 follows 034 and tolerates already-created fresh-install tables. Never use `init_db` to attempt an ALTER of existing production tables: it only creates missing tables.

* `APPLE_DUEL_EXPIRY_SECONDS=60` (validated range 1..3600).
* `APPLE_DUEL_SCENE_SECONDS=4` (validated range .1..60).
* Six scenes, five nominal 4-second gaps; existing maintenance polls every 5 seconds. Healthy delivery is roughly 20–25 seconds, plus polling/Telegram latency. Under load or network failure this can take longer; there is no claimed hard wall-clock SLA. No `time.sleep`, no dedicated in-memory timer and no bot detached task required.
* Cursor is committed before each send (at-most-once intermediate scene). A crash between claim and send can skip that scene. Final text on original invitation is separately retried after restart. Fallback sends can duplicate final text if Telegram accepts a timed-out request; database result/reward still never duplicates.
* Four different JPEG facing-card compositions (opening, encounter, climax, result); use registered art or bundled card art. Missing art uses a neutral card-shaped placeholder. Rendering is offloaded with asyncio.to_thread, not on the event loop. Image resolution/composition/send failures fall back to text. No remote live Telegram test was performed.

## Validation record
All DB tests used local URLs containing `sibcom_sib_test`, guarded by existing fixtures. No production DB/server changes, no deployment, commit or push.

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt aiosqlite Pillow ruff
# Initial import needed greenlet; requirements was corrected to SQLAlchemy[asyncio].
PYTHONPATH=/home/user sib_TEST_DATABASE_URL=sqlite+aiosqlite:////home/user/sib/tmp/sibcom_sib_test_duel.db \
  .venv/bin/python -m pytest tests/test_apple_duel.py tests/test_apple_duel_migration.py -q
# Final focused feature run: 55 passed, 3 skipped (real row-lock races).
PYTHONPATH=/home/user sib_TEST_DATABASE_URL=sqlite+aiosqlite:////home/user/sib/tmp/sibcom_sib_test_duel.db \
  .venv/bin/python -m pytest tests/test_apple_duel.py tests/test_apple_duel_migration.py \
  tests/test_apple_battle.py tests/test_player_cards.py tests/test_seedlings.py tests/test_wallet.py \
  tests/test_trading.py tests/test_card_upgrade.py tests/test_parties.py tests/test_recovery_hardening.py -q
# Focused regression: 148 passed, 8 skipped.
PYTHONPATH=/home/user sib_TEST_DATABASE_URL=sqlite+aiosqlite:////home/user/sib/tmp/sibcom_sib_test_final.db \
  .venv/bin/python -m pytest -q
# Full suite: 2083 passed, 6 failed, 34 skipped; 295.25 seconds.
PYTHONPATH=/home/user .venv/bin/alembic upgrade 034_card_affiliations:035_apple_duel --sql
# MySQL offline DDL generated successfully; no online MySQL server available.
PYTHONPATH=/home/user .venv/bin/python -m compileall -q core database handlers services
.venv/bin/ruff check --isolated --select E4,E7,E9,F,I services/apple_duel.py \
  services/apple_duel_presentation.py handlers/apple_duel.py database/models/apple_duel.py \
  database/migrations/versions/035_apple_duel.py tests/test_apple_duel.py tests/test_apple_duel_migration.py
# New modules clean; format --check: seven files formatted; router/import/config smoke successful.
git diff --check
```

Full-suite failures also reproduced in an isolated archive of untouched commit f08e39e:
1. `test_character_art.py::test_profile_shows_favorite_card_art`: fixture supplies MagicMock as chat id; SQLite cannot bind it.
2. `test_character_art.py::test_all_character_cards_exist_with_same_dimensions`: existing assets are not 896x1200 (e.g. 1121x1403).
3. `test_character_art.py::test_character_cards_share_one_frame_no_double_frame`: same existing dimension mismatch.
4. `test_character_art.py::test_character_cards_fill_the_opening_without_margins`: existing golden-card opening coverage below .95.
5. `test_navigation.py::test_shop_and_battle_and_garden_and_trade_have_back_to_main`: test lacks DB fixture and attempts unconfigured default local MySQL on 127.0.0.1:3306 (refused).
6. `test_transaction_regressions.py::test_E_daily_chest_retry_keeps_the_callers_user_row`: pre-existing chest retry rolls back caller's newly flushed User.

Initial full suite: 2075 passed, 7 failed, 31 skipped. The seventh failure, old `test_restart_during_narration_reuses_the_same_plates`, is intermittent: untouched baseline repeated five times returned 1 failure / 4 successes. It compares standalone plate selection with the full-plan anti-repeat selection. It passed in the final full run; no scene director/formula rewrite was made.

Expanded lint on touched legacy modules reports eight pre-existing E712/F401 findings. The same eight were reproduced in the baseline. No repository type-check configuration was found. No claim of a fully green original repository.

Earlier focused runs caught and corrected fixture issues (fixed-id users must be reused because existing fixture retains User rows; valid invitation message ids must be flushed; PanelOwner uses owner_user_id), and a mistyped nonexistent test path caused a no-tests collection run before the corrected regression command. Final counts above refer to executed successful/failed test runs, not those attempts. Full-suite run preceded the final facing-layout refinement and trade-current-read refinement; the relevant focused suite was rerun afterwards.

## Review file inventory
Added: `database/models/apple_duel.py` (invitation + unique slots); `database/migrations/versions/035_apple_duel.py` (frozen additive schema); `handlers/apple_duel.py` (shared aliases, validation, callbacks, existing selection adapter); `services/apple_duel.py` (atomic lifecycle, real engine + existing seedling award); `services/apple_duel_presentation.py` (six Persian scenes, dynamic art, cursor/recovery); `tests/test_apple_duel.py` (feature/transport/concurrency scenarios); `tests/test_apple_duel_migration.py` (safe online SQLite/offline MySQL upgrade); this audit/operations document.

Modified: `.env.example` and `core/config.py` (validated settings); `database/models/__init__.py` (model registration); `handlers/__init__.py` (router registration); `requirements.txt` (async SQLAlchemy and runtime Pillow); `services/character_art.py` (real asset API name); `services/maintenance/service.py` (isolated duel sweep AFTER bot turns are scheduled); `services/player_cards.py` (accepted duel Main Card guard under User mutex); `services/session.py` (duel active predicate and optional current-read of existing sessions); `tests/conftest.py` (clean new tables in isolated tests, in FK order).

## Remaining release gates
Real MySQL/MariaDB concurrent invite/accept/expiry races (three new tests intentionally skipped on SQLite); online 035 migration against staging MySQL; real Telegram permissions, media download, transport rate limits and wall-clock cadence. Six existing suite failures and existing intermittent scene-library test are not fixed by this feature. Code is ready for review, not represented as a fully production-certified deployment.

## Final verification addendum (review round 2)

Command verification against real code: the test URL variable is exactly `sib_TEST_DATABASE_URL` (read in `tests/conftest.py`), it must contain the substring `sibcom_sib_test`, and refusing otherwise is enforced by the fixture. `PYTHONPATH=/home/user` is mandatory because the repository root is the `sib` package (without it pytest fails with `ModuleNotFoundError: No module named 'sib.core'`). `.venv/bin/python` is the interpreter used; `pytest.ini` sets `asyncio_mode = auto`. Alembic offline SQL used the default settings URL with `--sql`, which never connects.

MySQL/MariaDB status: NOT available in this sandbox. No `mysql`/`mysqld`/`mariadbd`/`docker` binaries, TCP 127.0.0.1:3306 refuses connections, no distribution packages, and server binaries are not distributable from the allowed package hosts. The three `requires_row_locks` race tests therefore remain SKIPPED; their outcome is not fabricated. Compensation added: `test_concurrent_transitions_cas_layer` (duplicate accept / accept-vs-expire / overlapping invites) runs on SQLite and proves the application-level status CAS, the slot primary key and exactly-once settlement under concurrent independent-session workers. It passes. The InnoDB `FOR UPDATE` layer still must be exercised on staging before deployment.

Scene retry fix (no architecture change): previously, when every Telegram fallback for a claimed scene failed, the scene was permanently skipped because the cursor claim had already committed. `AppleDuelService.release_scene` now rewinds exactly one claim (CAS: status ACCEPTED and cursor == index+1) after total delivery failure, so the next maintenance sweep retries the same scene; total transport outage no longer loses scenes. Duplicate risk exists only for lost-response timeouts (cosmetic). Residual accepted limitation: a process crash between the claim commit and the send attempt still skips one intermediate scene; closing that window would need a delivered-flag/outbox schema change. The final board retries independently via `terminal_presented`, and a failed scene-5 photo never blocks it because release never rewinds a COMPLETED duel.

Tests added/updated this round: `test_scene_delivery_failure_retries_same_scene`, `test_release_scene_never_rewinds_a_newer_claim`, `test_terminal_edit_failure_fallback` (two params), `test_concurrent_transitions_cas_layer` (three params); `test_total_transport_failure_then_recover_final` rewritten for retry semantics (no progress while offline, full ordered delivery plus terminal board after recovery, seedlings still exactly one).

Re-run results: focused duel suite 62 passed, 3 skipped; regression set (duel, migration, apple battle, player cards, seedlings, wallet, trading, card upgrade, parties, recovery hardening) 155 passed, 8 skipped; invasion/marriage maintenance recovery suites 36 passed, 2 skipped. Scoped Ruff check/format, compileall and `git diff --check` clean.

No-recompute re-verification: `compute_and_persist_result` is called only from the accept CAS branch; `_award_seedling` is idempotent per (battle, user); maintenance paths call only `expire`, `claim_scene`, `release_scene` and `publish_terminal`, none of which touch the engine or the seedling ledger; `test_result_reward_atomic_and_idempotent` and `test_payment_failure_rolls_back_acceptance` pass.

Deployment gate unchanged: real MySQL/MariaDB race runs plus online 035 upgrade on staging, and live Telegram transport checks. Six pre-existing suite failures remain untouched. Conclusion: review-ready; deployment remains gated on staging verification.

## Cinematic plate library (assets/apple_duel)

Six pre-generated, cast-free arena plates — one per narrative scene — follow the
existing `assets/apple_battle` convention (manifest.json plus an art-direction doc at
`.agents/tasks/apple-duel-cinematic-presentation/ART_DIRECTION.md`). Style is the shared
Siib visual system: chibi apple-warrior world, circular stone arena, blossom orchard,
castle and waterfall, golden-hour god rays, crimson/green/gold/moon-blue palette; generated
with `assets/apple_battle/opening_01_crimson_green.jpg` as style reference. No text, UI,
borders or watermarks; the left/right thirds stay soft-focus and character-free because the
two REAL duelist cards are composited there at runtime. Duelists appear only as tiny distant
backlit silhouettes (scene 5 has none), so plates never contradict the actual pair.

| Scene | Plate | Beat |
|---|---|---|
| 0 | `assets/apple_duel/scene_0_entry_castfree.jpg` | ورود دو مبارز؛ سکوت سنگین |
| 1 | `assets/apple_duel/scene_1_sizeup_castfree.jpg` | سنجش حریف؛ تنش آرام |
| 2 | `assets/apple_duel/scene_2_clash_castfree.jpg` | برخورد نخست؛ موج ضربه |
| 3 | `assets/apple_duel/scene_3_turning_castfree.jpg` | تغییر جریان؛ نور تازه |
| 4 | `assets/apple_duel/scene_4_climax_castfree.jpg` | لحظه‌ی تعیین‌کننده |
| 5 | `assets/apple_duel/scene_5_aftermath_castfree.jpg` | پایان و پاداش؛ جوانه‌ی نهال |

Runtime wiring: `scene_plate_bytes(index)` reads the manifest-cached plate (never raises);
`compose_frame(arts, stage, background)` cover-crops the plate to 1000x650 and composites the
two framed cards; a missing/unreadable plate or manifest falls back to the original procedural
backdrop, so every scene stays sendable. Scenes 1 and 3 now carry photo messages instead of
text-only ones; total messages are unchanged (one per scene plus the terminal edit of the
invitation). Plates are progressive JPEG q85, 76-132 KB each (contract <=400 KB), versioned in
the repository like all other art. Artwork is background only: captions, winner, loser and the
seedling award remain derived exclusively from the persisted duel result.

Tests: `test_duel_plate_library_manifest_and_files`, `test_every_scene_photo_uses_its_plate`,
`test_missing_plate_falls_back_to_procedural`, `test_images_six_distinct_and_both_art_present`;
all transport-failure/retry/restart tests unchanged and passing. Focused duel suite:
65 passed, 3 skipped (MySQL row locks); related regression: 129 passed, 7 skipped.
