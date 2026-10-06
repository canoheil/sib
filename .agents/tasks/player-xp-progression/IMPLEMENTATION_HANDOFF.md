# Account XP & Level Progression — Implementation Handoff

**Status:** Implemented on `arena/a2a06526-sib` against [`FINAL_DESIGN.md`](FINAL_DESIGN.md).

**Scope:** Account XP/Level progression and the companion 30-image player-level art set. XP values and economy rules remain unchanged; the standalone artwork is under `assets/player_levels/` and follows [`ART_DIRECTION.md`](ART_DIRECTION.md).

## Implementation summary

- `services/player_xp.py` is the single authority for the Level 1–30 curve, profile progress, approved source registry, idempotent grant operation, UTC daily/source caps, MAX clamp, match classification, and level-up notice delivery.
- `player_progressions.total_xp` stores cumulative XP; Level and the current-level bar are derived, not separately persisted. `player_xp_grants` records each event, its actual post-cap amount, level transition, and notification state. A per-user progression-row lock plus `(user_id, source, event_key)` uniqueness protects retries/concurrent awards.
- Source changes and their XP award share the source transaction. Match settlement uses a savepoint: an XP-side failure preserves the finished result, leaves `Match.xp_awarded` retryable, and is retried by the existing maintenance service. Apple Battle uses the shared match settlement exactly once; its Apple/seedling rewards are unchanged.
- Level-up notifications are published only after commit. The XP ledger acts as the outbox; a claim lease prevents overlapping sends, and failed delivery remains retryable. Existing personal profile views show the badge, Level, progress, and a distinct Level 30 `MAX` display.

### Approved source policies implemented

| Source | XP | Limit / eligibility |
|---|---:|---|
| Normal human match | 100 each; +25 per winner | 8 eligible matches per user/UTC day; started, applied result |
| Bot/practice | 20 per human; no win bonus | 2 matches per user/UTC day; bot accounts receive zero |
| Friendly | 35 per real human; no win bonus | 5 matches per user/UTC day and at least 2 distinct humans |
| Garden harvest | 5 per harvested plot-cycle | First 5 harvests per user/UTC day |
| Plant / paid plot unlock / magic level increase | 10 / 15 / 10 | One-time per plot or target level; only successful actions |
| New character collection / paid card-level step | 25 / 10 | Once per user-character / target card level |
| Completed trade | 10 per trader | Only after the ownership swap; 1 per user/UTC day |
| Accepted Flower | 5 to sender and recipient | 10 flower XP per user/UTC day; acceptance only |
| First successful marriage | 50 per partner | One-time per account |
| All other economy/UI events | 0 | No XP for Apple/currency flow, claims, browsing, etc. |

All sources share the 1,000-XP UTC-day ceiling and the 87,000 lifetime ceiling. A capped or MAX event records a zero/partial grant so retries cannot re-award it.

## Migration and launch behavior

- New Alembic revision: `database/migrations/versions/025_player_xp_progression.py`, after `024_marriage_and_love`.
- Existing accounts receive Level 1 / 0 XP; `UserService` initializes new accounts the same way. No historical XP is converted.
- Migration marks pre-launch character ownership, planted plots, current magic levels, paid plot unlocks, and prior marriage participation with zero-XP idempotency records. Paid unlocks are identified by successful wallet-ledger idempotency keys, not guessed from plot index/config. The migration test also asserts the shipped five-free-plot default and distinguishes an unmarked index-4 plot from a paid index-5 unlock.
- `matches.xp_awarded` prevents replay of historical applied results and supports retry for new settlement failures.

## Main files

- **Core and persistence:** `services/player_xp.py`; `database/models/player_xp.py`; `database/models/match.py`; `database/models/__init__.py`; `services/user.py`; migration 025.
- **Settlement and delivery:** `services/match_result.py`; `services/maintenance/service.py`; `services/apple_battle.py`; `handlers/games/common.py`; `handlers/{apple_battle,start,cards,garden,marriage,shop,trading}.py`.
- **Approved activity hooks:** `services/{garden,shop,card_upgrade,trading,love,marriage}.py`.
- **Coverage:** `tests/test_player_xp.py`, migration tests, source/service integration tests, and Apple Battle result/narration/scene tests. Apple Battle narration assertions distinguish the new post-settlement Level-up notices from narrative beats.

## Verification

- Focused progression/migration/source/match/Apple Battle batch: **203 passed, 4 skipped** on SQLite.
- Full suite: **1176 passed, 10 skipped, 3 failed** on SQLite.
- `python3 -m compileall -q core database handlers services tests` and `git diff --check`: passed.
- Artwork audit: exactly 30 padded final filenames, each JPEG 1200×1500 sRGB; temporary retakes/contact sheet were removed.
- Migration 025 is covered by the SQLite migration test. SQLite cannot exercise `SELECT ... FOR UPDATE`; MySQL-specific concurrency cases are skipped. A live MySQL/InnoDB migration and concurrency run remains outstanding.

### Outstanding full-suite failures (outside this feature)

Three checks in `tests/test_character_art.py` fail on existing character-card art: `sib_sorkh` is 1121×1403 rather than the asserted 896×1200 (also causing the shared-frame dimension assertion); `sib_talayi` opening fill is 472158/505324, below the 95% threshold. These card-art files/tests are outside this Level-series change and were not modified.

No feature-specific regression surfaced in the focused batch. Before rollout, apply migration 025 in a MySQL staging environment and verify schema/backfill behavior plus the row-lock/concurrency tests against the deployed MySQL/InnoDB version.
