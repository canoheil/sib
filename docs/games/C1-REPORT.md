# C1-IMPL — Apple Castle implementation report

Date: 2026-10-18. Branch: `arena/f9e34b29-sib`.

## 1. Image generation status

**10/10 images generated; none missing.** Two batches of five were completed without generation failures or limits. The first banner established the composition; every later banner was generated as an edit using that same level-1 reference, with instructions to preserve camera, warm lighting, misty mountains and surrounding landscape while evolving the castle. Milestone banners 1, 5 and 10 were visually inspected. This is reference-guided visual consistency, not a claim of pixel-identical AI output.

The generator returned 1376×768 JPEGs; each was resampled to the required **1280×720** with Lanczos. Automated checks verify all ten final files exist, are JPEGs and have exactly the required dimensions. Total deployed artwork is approximately 2.3 MiB. No alternate options, thumbnails or runtime generation are included.

| Level | File | Evolution |
|---:|---|---|
| 1 | `assets/castle/level_1.jpg` | Ruined stones, broken towers/banner |
| 2 | `assets/castle/level_2.jpg` | Gate repairs and timber supports |
| 3 | `assets/castle/level_3.jpg` | Rebuilt lower towers and orchard refuge |
| 4 | `assets/castle/level_4.jpg` | Complete ramparts and modest keep |
| 5 | `assets/castle/level_5.jpg` | Solid stone castle and spring fountain |
| 6 | `assets/castle/level_6.jpg` | Expanded keep, blossom vines and second walls |
| 7 | `assets/castle/level_7.jpg` | Emerald fortress and raised orchard terraces |
| 8 | `assets/castle/level_8.jpg` | Enchanted skybound citadel |
| 9 | `assets/castle/level_9.jpg` | Gold-green towers and suspended gardens |
| 10 | `assets/castle/level_10.jpg` | Celestial palace, emerald spires and golden aura |

`services/castle_art.py::castle_image` searches from the requested level down to 1, selecting the closest existing lower banner. If no image exists, the handler renders the same caption and keyboard as text. Photo-delivery failures also fall back to text. Fallback is tested independently with missing-file fixtures.

## 2. Implementation and recon notes

### Database

Migration **`049_apple_castles`**, following `048_apple_friendships`, creates:

- `group_castles`: requested signed BIGINT group PK/FK, INTEGER level default 1 and INTEGER cumulative donations default 0, with nonnegative-total and level-range constraints.
- `castle_donations`: requested ID, user/group FKs, positive INTEGER amount and DATETIME donation timestamp. Adds a unique `request_key` for replay safety and indexes for daily-cap queries and group donor ranking.
- `castle_income_claims`: additional durable claim gate, snapshotted amount and paying group. Unique `(user_id, claim_day)` prevents collecting from many castles on the same UTC day.

Models are registered in `database/models/__init__.py`. Group IDs retain Telegram's signed representation; autoincrement IDs use SQLite-compatible test variants. Group deactivation preserves castle state and audit history. Deleting referenced groups/users is restricted rather than silently discarding contribution records.

### Domain: `services/castle_service.py`

`CastleService` implements all four requested entry points:

- `get_or_create_castle(session, group_id)` validates a registered active Group, upserts the unique castle and reads it under a row lock. New castles begin at free level 1.
- `donate(session, user_id, group_id, amount)` takes the existing User economy mutex, locks castle/cap data and wallet, rejects invalid amounts or insufficient capacity/funds, writes the donation audit row, debits through `AppleWalletService.debit_once`, updates cumulative donations and recalculates level. `amount=None` means Max. Fixed amounts are exact; Max is bounded by balance, remaining global daily allowance and remaining lifetime target.
- `get_invasion_buff(session, group_id)` returns the **bonus fraction** `level × 0.02`; an absent castle or inactive group returns zero. It does not create castles or buff enemy HP.
- `claim_daily_income(session, user_id, group_id)` gives `level × 2` normal apples once per UTC day across all castles, using a locked User, unique claim row and canonical keyed wallet credit.

The caller owns commit/rollback. Ledger debit/credit and all domain updates are atomic. A failed transaction cannot leave a consumed claim or contribution without its wallet movement. Financial operations finish before Telegram acknowledgements or image delivery.

Donation callbacks pass `castle:cb:<Telegram callback ID>` as the request key. Redelivery of the same callback returns the recorded contribution without another debit/progress increment, including Max requests. A new deliberate press is a new operation, still subject to the daily cap. Non-Telegram service callers may supply a stable key for retries; the default generates a fresh operation key.

Daily caps count **all castles together**, not 500 per group. Current locking donation reads avoid stale MariaDB REPEATABLE READ sums after acquiring the User mutex. Reset is 00:00 UTC, not local midnight or a rolling cooldown. No negative balances, special apple-item spending, refunds, withdrawals or donations after level 10.

### Lock-order investigation

Do not explicitly lock the parent Group during a castle mutation. Existing invasion spawn code locks Group then invasion; join/settlement can lock invasion then User. Adding User → Group would introduce a cross-feature cycle. Castle operations instead use User → castle → operation gate/wallet; creation is serialized by a dialect-specific upsert on the unique castle key. `docs/lock-order.md` documents this branch without changing existing resource ranks.

The Group active flag is a nonlocking check: a transaction already in progress can complete concurrently with deactivation. First-creation foreign-key checks and existing domain locks can still contend. Database errors/deadlocks roll back the transaction; this implementation does not automatically retry failed donations under a new key. Actual MariaDB concurrency validation remains a deployment gate.

### Progression retained from C1-RECON

| Level | Cumulative normal apples | Daily income per user | Castle damage bonus |
|---:|---:|---:|---:|
| 1 | 0 | 2 | 2% |
| 2 | 5,000 | 4 | 4% |
| 3 | 20,000 | 6 | 6% |
| 4 | 50,000 | 8 | 8% |
| 5 | 110,000 | 10 | 10% |
| 6 | 210,000 | 12 | 12% |
| 7 | 360,000 | 14 | 14% |
| 8 | 580,000 | 16 | 16% |
| 9 | 880,000 | 18 | 18% |
| 10 | 1,300,000 | 20 | 20% |

The personal daily cap is 500. At 20 donors using the full allowance, the ideal time to level 10 is 130 donation days; at 10 it is 260. Unlike the recon recommendation, **there is no additional 10,000/group/day cap in the implementation request**: a sufficiently large wealthy group can finish sooner. This implementation follows the updated task, not that unapproved recon addition.

### UI: `handlers/castle.py`

Registered before the broad start router in `handlers/__init__.py`.

- Exact `قلعه سیبی` entry in groups/supergroups only; no substring/private trigger.
- Current banner, Persian level/donated/next-threshold caption, damage bonus, daily income and UTC rules. Level 10 displays a completion label instead of a nonexistent next threshold.
- Donate submenu: 100, 500, Max, Back. Selecting an amount is the donation action; the caption warns it is irreversible. No separate recon-style quote/confirmation workflow is added.
- Daily-income button credits through the service and privately acknowledges success/already-claimed refusal.
- Top donors: five accepted lifetime totals, descending amount with deterministic User-ID tie break, escaped/truncated names, no wallet exposure.
- Public menus use the pressing callback user's identity. They never accept a spending user ID from callback data. Current Telegram membership, originating group, callback action shape, bot actor and banned-player checks protect mutations. Cross-group/invalid callbacks fail before a database write.
- Safe callback acknowledgements prevent Telegram callback delivery errors from undoing committed operations. No mandatory private chat is needed.

The shared submenu deliberately remains public: another member pressing its buttons spends only their own wallet. Telegram membership verification fails closed if unavailable. Sending repeated menus is governed by existing middleware/Telegram limits; a new dedicated castle post cooldown or edit-coalescing system is not included in this phase.

### Invasion integration

The existing join handler computes base damage from the Main Card with its unchanged random factor and critical roll. Immediately afterwards:

1. Read current castle bonus through `CastleService.get_invasion_buff`.
2. Apply **`int(base_damage × (1 + castle_buff))`**, exactly as requested.
3. If “Join with Buff” is used, double that resulting integer damage.
4. Use the final value for narrative, guarded HP update and persisted attack damage.

Example: base 101, L10 castle → `int(101 × 1.20) = 121`; with Sib-Buff → **242**. Doubling before truncation would incorrectly produce 243, so the order has an end-to-end regression test. Consumable consumption, duplicate attack constraints, victory settlement and HP CAS remain in their existing transaction.

`services/invasion_v2/simulation.py` now uses the same castle calculation and labels the current castle bonus, excluding the optional consumable. Enemy HP/group card power are unchanged.

**Updated task supersedes recon snapshotting:** this is a live bonus read at attack time, not a stored per-invasion snapshot. Upgrading during an invasion can benefit later attackers. There is no timed cannon. Likewise income is a requested user-triggered daily claim, not the automatic bulk distribution/extra planner tables proposed in recon.

## 3. Validation results

### Focused feature coverage

**31 tests passed** across `tests/test_castle.py` and `tests/invasion_v2/test_castle_integration.py`, including parametrized cases:

- `test_castle_donation_cap`: multiple contributions, global cross-group cap, next-day reset.
- `test_castle_level_up_calculation`: every threshold and immediately preceding total.
- `test_castle_daily_income_claim`: daily reward, global duplicate refusal, UTC rollover.
- `test_invasion_buff_applied_correctly`: no castle, level 10, zero hits and item ordering.
- `test_castle_image_fallback_if_missing`: exact image, nearest lower image and no-image cases.
- Donation upgrades, callback replay/conflicting key, Max/lifetime target, insufficient funds, invalid amounts, and rollback after debit/credit failure.
- Real join handler with/without Sib-Buff, membership/cross-group callback refusals.
- All ten image formats/dimensions and migration upgrade/downgrade.

### Expanded regression run

```sh
PYTHONPATH=/home/user \
sib_TEST_DATABASE_URL=sqlite+aiosqlite:////home/user/.cache/sibcom_sib_test_c1.sqlite \
.venv/bin/python -m pytest \
  tests/test_castle.py tests/invasion_v2 \
  tests/test_wallet.py tests/test_wallet_ledger_keys.py \
  tests/test_daily_chest.py tests/test_daily_chest_group.py \
  tests/test_lock_ordering.py \
  --ignore=tests/invasion_v2/test_imaging.py -q
```

**639 passed, 2 skipped**. The skipped tests require real database row locks; SQLite cannot establish MariaDB concurrency correctness. Existing inv2 fixtures with explicitly enumerated tables were extended with `GroupCastle`; the migration-chain expectation now includes 049. Shared test cleanup deletes the new child rows before parent groups.

### Existing invasion imaging failures

Running the invasion suite including `test_imaging.py` exposed **12 existing failures**: manifest/dimensions for older invasion assets. All twelve were reproduced by exporting **pre-implementation commit `8626c1e`** to an ignored external scratch directory and running the unchanged imaging test there: **12 failed, 39 passed**. No old invasion images or imaging expectations were changed to hide these failures. They are unrelated to the ten castle banners.

### Other checks

- Migration 049 upgrade/downgrade executed successfully against an isolated SQLite database.
- `alembic upgrade 048_apple_friendships:049_apple_castles --sql` successfully generated MySQL-dialect DDL, without connecting to production.
- Changed modules compile; `git diff --check` is clean.
- New modules/tests formatted with Black.
- No production DB, live Telegram calls or live bot process were used. No real MariaDB migration/concurrency test was available in this workspace. This is a targeted regression run, not a claim that the entire repository suite passed.

## 4. Deployment / operational notes

1. Back up the database and apply `049_apple_castles` through the normal Alembic deployment process **before** starting the updated bot. Invasion joins now read `group_castles`, even in groups without a castle row; deploying code without the table will break those joins.
2. Deploy all ten `assets/castle/level_N.jpg` files. Missing intermediate banners degrade gracefully, but complete deployment is expected.
3. Verify real MariaDB concurrent donations/cap enforcement and duplicate daily claims using parallel sessions. Verify membership API rights and a real group callback flow in staging.
4. Smoke-test exact entry, 100/500/Max, top-five ranking, income replay and invasion with/without Sib-Buff. Check ledger reasons `castle_donation` and `castle_spring` against donation/claim rows.
5. On a post-commit display failure, inspect saved state rather than manually awarding/refunding apples. A fresh button press has a fresh callback ID and is a new donation; only redelivery of the same callback ID is a replay.
6. Do not downgrade while this code is running. Downgrade drops castle history/claims; restoring a database backup is the safe way to undo financial history. Group-ID migration and administrative deletion policies need explicit follow-up if introduced.

Requested commit message: `[c1] implement apple castle, donation, and invasion buff`. The final response includes `git log --oneline -5`; this sandbox checkout has shallow history, so fewer than five entries may exist.
