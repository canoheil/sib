# G2-RECON — Apple Garden Rework: Invasion Corruption (پژمردگی) & Divine Fertilizer (کود الهی)

**Phase:** design and reconnaissance only — **no code written in this phase** · **Date:** 2026-10-19
**Inspected baseline:** `8def898` (branch `arena/eb8c90e9-sib`, branched from `main`)
**Scope of this change:** this report only. No application code, migrations, handlers, configuration, assets, or database data are changed. Everything described as “proposed” is future work for the implementation phase, not existing behavior.

---

## 0. Proposed design at a glance

1. **36-hour bug fix is config-only.** `garden_growth_seconds` drops from `86400` (24 h) to `43200` (12 h); `garden_fruit_seconds` stays `43200` (12 h). First fruit is then ready at exactly **24 h** after planting (12 h growing 🌱 + 12 h mature 🌳 → 🍎), and every later harvest cycle stays at 12 h.
2. **Withered plots.** `garden_plots` gains a nullable-free boolean `is_withered` (default `0`). A new `PlotState.WITHERED` is derived *before* the timestamp logic; withered plots refuse planting and harvesting and are rendered 🥀.
3. **Corruption trigger.** When an Invasion ends in **DEFEAT** (or an *announced* **EXPIRED** — see §2.3 for the never-announced exception), every **active group member who has zero `inv2_attacks` rows** in that invasion is a candidate. One candidate loses exactly **one random non-withered plot**.
4. **Fairness is database-enforced.** A new `garden_wither_events` ledger carries two unique constraints: `(user_id, invasion_id)` makes settlement retries idempotent, `(user_id, wither_day)` makes the **once-per-UTC-day** cap a hard DB arbiter — the same convention as `daily_chest_claims`.
5. **Divine Fertilizer.** New inventory item `divine_fertilizer`, buy price **80 apples**, not sellable, art at `assets/items/divine_fertilizer.jpg` (1024×1024, same watercolor family as `healing_potion.jpg`). Using one on a withered plot clears the flag; the tree's timestamps, magic level and cycle count are preserved.
6. **Zero shop-handler code.** The shop's Items section renders straight from `ITEM_CATALOG` + `item_buy_prices`, and `AssetService` auto-resolves `assets/items/<key>.jpg`, so the item appears from catalog + config + artwork alone.

---

## 1. TASK 1 — Reconnaissance

Paths are relative to repo root. Python imports use the `sib.*` namespace.

### 1.1 How garden plots are stored today

| Source | Finding |
|---|---|
| `database/models/garden.py:27-39` | `Garden` (`gardens`): PK `user_id` (FK→`users`, `ON DELETE CASCADE`), `capacity_plots` (default 5). One garden per player. |
| `database/models/garden.py:42-79` | `GardenPlot` (`garden_plots`): surrogate `id`, `garden_id` FK→`gardens.user_id` CASCADE, `plot_index`, `UniqueConstraint("garden_id", "plot_index", name="uq_garden_plot_index")`, nullable timestamps `planted_at`, `mature_at`, `fruit_ready_at`, plus `cycle_count` and per-tree `magic_level`. `TimestampMixin` adds `created_at`/`updated_at`. |
| `database/models/garden.py:20-25` | `PlotState` constants: `EMPTY`, `GROWING`, `MATURE`, `FRUIT_READY`. **There is no status column and no persisted state.** |
| `services/garden.py:143-159` | `GardenService._derive_state(plot, now)` derives state purely from server timestamps: no `planted_at` → `EMPTY`; `now >= fruit_ready_at` → `FRUIT_READY`; `now >= mature_at` → `MATURE`; otherwise `GROWING`. Client clock can never exploit growth. |
| `services/garden.py:103-138` | `get_or_create` lazily creates the garden and `capacity` contiguous `GardenPlot` rows. Read paths skip `FOR UPDATE`; mutating paths pass `for_update=True`. |
| `services/garden.py:162-199` | `view()` maps plots to `PlotView` dataclasses (state, timestamps, `seconds_to_next_stage`, `magic_level`). |
| `database/migrations/versions/011_garden_wallet.py:41-55` | Original `garden_plots` DDL. The newest revision is `053_apple_pickaxes`; the next migration must be `054_*` with `down_revision = "053_apple_pickaxes"`. |

**Answer — how to add `is_withered`:** follow the existing boolean convention used by `Inv2Region.is_lost_to_enemy` (`database/models/invasion_v2.py:34-37`, `Boolean, default=False, server_default="0", nullable=False`) and `User.is_banned` (`database/models/user.py`, `server_default=false()`). An `op.add_column(..., server_default="0")` backfills every existing plot to “not withered” in one DDL step — no data migration. `_derive_state` must check the flag **first** and return a new `PlotState.WITHERED` before any timestamp comparison, so withering overrides every lifecycle state.

### 1.2 The 36-hour bug, exactly

| Source | Finding |
|---|---|
| `services/config_store.py:39-40` | `"garden_growth_seconds": 24*60*60` (86 400 s = 24 h) and `"garden_fruit_seconds": 12*60*60` (43 200 s = 12 h) live in `CONFIG_DEFAULTS`. `ConfigService.get` falls back to these defaults when `system_config` has no row (`services/config_store.py:435-443`), so no seeding is required. |
| `services/garden.py:240-244` | On plant: `mature_at = now + growth`; **`fruit_ready_at = mature_at + fruit`** → 24 h + 12 h = **36 h** to the first apple. |
| `services/garden.py:301-303, 326` | On harvest the mature tree restarts its cycle: `fruit_ready_at = now + fruit` (12 h). So only the *first* fruit is late; repeat cycles are already 12 h. |
| `handlers/admin_meta.py:155-167` | The admin economy panel already displays both values and states they are changeable via `system_config` — no admin change is required for the fix. |
| `tests/test_garden.py:92,118,153,176`, `tests/test_garden_state.py` | Tests set `mature_at`/`fruit_ready_at` manually with explicit `timedelta`s; they do not hardcode the 24 h default, so the config change does not break existing growth tests. |

### 1.3 Where an Invasion ends — the settlement map

| Source | Finding |
|---|---|
| `services/invasion_v2/enums.py:15-20` | Terminal statuses: `VICTORY`, `DEFEAT`, `EXPIRED` (plus live `PENDING`, `ACTIVE`). |
| `services/invasion_v2/scheduler.py:62-88` | `InvasionScheduler._finalize_due` (DB-only, runs each maintenance tick): a due **`PENDING` row becomes `EXPIRED`**; an `ACTIVE` row with HP ≤ 0 becomes `VICTORY`; an `ACTIVE` row with HP > 0 becomes `DEFEAT`. **Only `VICTORY`/`DEFEAT` are appended to the returned `transitions` list** (`scheduler.py:86-87`). `EXPIRED` is finalized in the DB but never leaves the scheduler. |
| `services/maintenance/service.py:553-570` | The maintenance cycle calls `tick()`, then for each `(inv_id, status)` in `terminal` opens a session, calls `settle_outcome(session, inv_id, status)`, commits, then runs `presenter.finalize(...)`. Failures are logged per invasion and isolated. This is the natural corruption driver — **no loop change is needed once `EXPIRED` enters `transitions`.** |
| `services/invasion_v2/rewards_service.py:50-76` | `settle_outcome` **early-returns unless status is `VICTORY`/`DEFEAT`** — today it refuses `EXPIRED`. It locks the invasion row `FOR UPDATE`, re-checks status (idempotent on retry), grants victory rewards, updates the region's `is_lost_to_enemy` flag and recomputes `Inv2GroupState` win/loss counters from terminal rows. |
| `handlers/inv2_join.py:128-129` | A killing blow settles inline (`VICTORY` only), inside the same transaction as the attack. |
| `services/invasion_v2/admin_controls.py:136-146` | `force_end` sets `VICTORY`/`DEFEAT` and calls `settle_outcome`; the “already terminal” branch retries settlement — any new side effect **must** be idempotent under replay. |
| `services/invasion_v2/recovery.py:33-45` | Startup recovery converts overdue `ACTIVE` rows to `EXPIRED` and explicitly grants **no rewards and changes no loss counters** (“No rewards or result counters are granted/changed here”). This deliberate downtime amnesty must be reconciled with the new penalty (§2.3). |
| `services/invasion_v2/presentation.py:340-344` | `finalize()` renders only `VICTORY`/`DEFEAT`. A `narrative.render_expired(...)` function and an `EXPIRED` narrative pool already exist (`services/invasion_v2/narrative.py:130`, `narrative_pool.py:132`) but are currently unused. |

### 1.4 How per-user damage is tracked

| Source | Finding |
|---|---|
| `database/models/invasion_v2.py:88-117` | `Inv2Attack` (`inv2_attacks`): one row per join, with `UniqueConstraint("invasion_id", "user_id", name="uq_inv2_attacks_invasion_user")`, an integer `damage` column, `is_crit`, `card_power`, `joined_at`. |
| `handlers/inv2_join.py:64-67` | A duplicate join is rejected before insert (“قبلاً در این یورش شرکت کرده‌اید.”), so participation is exactly “has an `inv2_attacks` row”. |
| `services/invasion_v2/damage.py:13-15` | Damage can mathematically roll `0` for a zero-power card — but such a player **still has an attack row**, i.e. they participated. |

**Decision for “dealt ZERO damage”:** treat it as *no `Inv2Attack` row for this invasion* (never joined). A joined player with a rolled-zero hit is a participant and must be exempt. This matches the existing invasion philosophy (“Joining alone never qualifies”, `config_store.py` invasion defense comment) and is a simple `NOT EXISTS` / set-difference query.

### 1.5 Who counts as “a player in that group”

| Source | Finding |
|---|---|
| `database/models/invasion_v2_group_member.py:17-29` | `Inv2GroupMember` (`inv2_group_members`): `UniqueConstraint("user_id", "group_id")`, `last_seen_at`, indexes on `(group_id, last_seen_at)`. Maintained non-blockingly by `middleware/group_membership.py` (throttled upsert on every group message/callback). FKs guarantee the `User` and `Group` rows exist. |
| `services/config_store.py:29` | `inv2_member_active_window_days = 30` — the established “active member” window. |
| `services/invasion_v2/simulation.py:44-48` | Canonical active-member query: `group_id == … AND last_seen_at >= now - timedelta(days=window)`, ordered by `user_id`. The corruption candidate query should reuse this exact definition. |

Candidate set for one lost invasion:

```
active_members = SELECT user_id FROM inv2_group_members
                 WHERE group_id = :gid
                   AND last_seen_at >= :now - INTERVAL :window DAY
attackers      = SELECT DISTINCT user_id FROM inv2_attacks
                 WHERE invasion_id = :iid
candidates     = active_members EXCEPT attackers      -- zero-damage players
```

### 1.6 The DEFEAT vs EXPIRED distinction (critical fairness finding)

There are three mechanically different “loss” paths, and they must not be treated identically:

1. **`ACTIVE` → `DEFEAT`** (normal maintenance, `scheduler.py:82-85`): the invasion *was announced* (`message_id`/`started_at` set), its 30-minute join window (`DEFAULT_INVASION_JOIN_WINDOW_SECONDS = 1800`) opened and closed, and the group failed to kill the boss. Full corruption applies.
2. **`PENDING` → `EXPIRED`** (`scheduler.py:75-78`): the announcement was **never sent** — the group was muted/circuit-broken or the bot could not deliver. No player ever had a join button. Penalizing this would be precisely the “3 AM invasion nobody saw” injustice. **Proposed: exempt.** The row is distinguishable by `message_id IS NULL` / `started_at IS NULL`.
3. **`ACTIVE` → `EXPIRED` via startup recovery** (`recovery.py:38-44`): the bot was down past the deadline, so nobody could finish a battle that was announced. Proposed policy in §2.3: settle these like DEFEAT *only when announced*, because the daily cap and participation exemption already bound the damage; the alternative (downtime amnesty) is documented as an explicit product decision.

### 1.7 Existing “once per day” precedent

`database/models/daily_chest.py:36-56` — `DailyChestClaim` stores `claim_day` as the UTC calendar date string `"YYYY-MM-DD"` with `UniqueConstraint("user_id", "claim_day", name="uq_daily_chest_claim_user_day")`. The model docstring names this “the project's established daily-boundary convention used by trades, Flowers and XP daily caps”. The wither limit should copy it verbatim: a UTC `wither_day` string plus a unique constraint, so concurrent maintenance workers, duplicate updates and retries all collide at the database instead of relying on application checks.

### 1.8 Item, shop and asset pipeline (what a new item actually requires)

| Source | Finding |
|---|---|
| `services/items.py:35-78` | `ITEM_CATALOG` maps key → `emoji`, `label`, `description`. `ItemService.grant/consume/get_total` operate on `user_items` rows; unknown keys are rejected. |
| `services/config_store.py:143-149` | `item_buy_prices` is the single buy-price source (magic_powder 40, sib_buff 50, wedding_ring 1000, flower 30, healing_potion 150). `item_sell_prices:153-160` omits `magic_powder` deliberately — consumable progression is buy-only. |
| `handlers/shop.py:299-340` | `_goods_view` builds the Items section from `ITEM_CATALOG` ∪ `item_buy_prices`; `_good_view` shows art, `item_description(key)`, price and buy buttons. **A catalog entry + a buy price makes the item appear with zero shop-handler changes.** |
| `services/assets.py:24-35, 47-58` | `AssetService.resolve_photo` resolves a key to a `bot_assets` override or the bundled `assets/items/<key>.jpg`/`.png`. Dropping `assets/items/divine_fertilizer.jpg` into the repo is enough; admins can later replace it via `/setasset`. |
| `services/war_service.py:583-621` | `WarService.use_healing_potion` is the reference template for “use a consumable”: lock user row → check inventory (`for_update=True`) → validate game precondition → `ItemService.consume(..., 1)` → apply effect, all in one caller-committed transaction. The fertilizer use-case mirrors this. |
| `core/logging.py:34-38` | `Events.GARDEN_EVENT`, `ITEM_CONSUMED`, `ITEM_PURCHASED` already exist; wither/restore can log through `GARDEN_EVENT` with new `action` values (`withered`, `restored`). |

### 1.9 How the bot can tell a player about a background penalty

There is no user interaction during maintenance, so no callback chat is available. Two precedents:

- `services/player_xp.py:577-670` — `deliver_pending_notifications` sends a post-commit private `bot.send_message(chat_id=user_id, …)`, catches delivery failures (`TelegramForbiddenError` when the user never started the bot), retries undelivered rows on later maintenance runs, and never lets Telegram I/O roll back game state.
- `services/invasion_v2/presentation.py:33-47` — `_telegram` wraps every send in a breaker-metered try/except that swallows `TelegramBadRequest`/`TelegramForbiddenError`.

Proposed: corruption DB writes commit inside `settle_outcome`; the wither DM is sent **after commit**, best-effort, with forbidden/blocked users logged and skipped. The garden panel itself always shows the 🥀 state, so a missed DM never hides the penalty.

### 1.10 Lock ordering constraints

`services/lock_order.py` documents the economy lock ranks (users = 0, user_items = 5, apple_wallets = 6, …) and the rule that multi-user work locks user rows in ascending id order (`lock_user_rows_in_order`, already used by `grant_victory_rewards` at `rewards_service.py:30-31`). Corruption settlement touches many users in one transaction, so it must: lock candidate user rows ascending; then garden/plot rows `FOR UPDATE`; insert ledger rows last. Fertilizer use is single-user and follows the normal `lock_user_row` → inventory row → plot row sequence. No new lock rank is needed.

---

## 2. TASK 2 — Design Proposal

### 2.1 Database changes

#### 2.1.1 `garden_plots.is_withered` (model + migration `054`)

Proposed model addition in `database/models/garden.py` (illustrative — not applied in this phase):

```python
is_withered: Mapped[bool] = mapped_column(
    Boolean, default=False, server_default="0", nullable=False
)
```

Proposed migration (new file `database/migrations/versions/054_garden_corruption.py`,
`revision = "054_garden_corruption"`, `down_revision = "053_apple_pickaxes"`):

```python
def upgrade() -> None:
    op.add_column(
        "garden_plots",
        sa.Column("is_withered", sa.Boolean(), nullable=False, server_default="0"),
    )
    op.create_table(
        "garden_wither_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("invasion_id", sa.BigInteger(), nullable=False),
        sa.Column("group_id", sa.BigInteger(), nullable=False),
        sa.Column("plot_id", sa.BigInteger(), nullable=False),
        sa.Column("wither_day", sa.String(length=10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["invasion_id"], ["inv2_invasions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["group_id"], ["groups.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["plot_id"], ["garden_plots.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "invasion_id",
                            name="uq_garden_wither_user_invasion"),
        sa.UniqueConstraint("user_id", "wither_day",
                            name="uq_garden_wither_user_day"),
    )
    op.create_index("ix_garden_wither_user_id", "garden_wither_events", ["user_id"])
    op.create_index("ix_garden_wither_invasion_id", "garden_wither_events", ["invasion_id"])

def downgrade() -> None:
    op.drop_index("ix_garden_wither_invasion_id", table_name="garden_wither_events")
    op.drop_index("ix_garden_wither_user_id", table_name="garden_wither_events")
    op.drop_table("garden_wither_events")
    op.drop_column("garden_plots", "is_withered")
```

#### 2.1.2 `garden_wither_events` — the daily cap, idempotency and audit ledger

Proposed model (place in `database/models/garden.py` and export from `database/models/__init__.py`, following the `Inv2GroupMember` export pattern at `models/__init__.py:134-136`):

| Column | Purpose |
|---|---|
| `id` | surrogate PK (BigInt with SQLite Integer variant, as elsewhere) |
| `user_id` | FK→`users.id` CASCADE |
| `invasion_id` | FK→`inv2_invasions.id` CASCADE — which loss caused it |
| `group_id` | FK→`groups.id` — audit, supports multi-group players |
| `plot_id` | FK→`garden_plots.id` CASCADE — snapshot of the hit plot |
| `wither_day` | `String(10)`, UTC `"YYYY-MM-DD"` of `finalized_at` |
| `created_at`/`updated_at` | `TimestampMixin` |

The two unique constraints are the whole fairness mechanism:

- **`uq_garden_wither_user_day`** — at most one wither per player per UTC day. A second loss on the same day collides and is skipped.
- **`uq_garden_wither_user_invasion`** — re-running `settle_outcome` (admin retry, maintenance re-drive, duplicated task) can never wither twice for one invasion.

A lighter alternative considered: a single `last_withered_at` column on `gardens`. Rejected — it provides no per-invasion idempotency, no audit trail and no group/invasion attribution, and every daily-limit feature in this codebase uses the ledger pattern.

#### 2.1.3 New plot state

Add `WITHERED = "WITHERED"` to `PlotState` and make `_derive_state` return it first:

```python
if plot.is_withered:
    return PlotState.WITHERED
# ...existing timestamp derivation unchanged
```

`PlotView` gains an `is_withered: bool = False` field so handlers render without touching the ORM object directly.

### 2.2 The exact fairness algorithm (anti-3-AM specification)

New service entry point (proposed name `GardenService.apply_invasion_corruption(session, invasion) -> list[WitherResult]`), invoked from `settle_outcome`'s loss branch, inside the same transaction:

1. **Status gate.** Proceed only for `DEFEAT`, or `EXPIRED` where `inv.started_at IS NOT NULL` (announced). Never-announced `PENDING→EXPIRED` rows return immediately.
2. **Candidate query.** Active members of `inv.group_id` inside `inv2_member_active_window_days` (30 d) minus every user with an `inv2_attacks` row for this invasion. Anyone who landed even one hit — including a rolled-zero-damage hit — is exempt.
3. **Per candidate, in ascending `user_id` order** (multi-user lock order, `lock_order.py`):
   1. `lock_user_row`; skip if the user no longer exists.
   2. **No garden → skip.** Never lazily create a garden from a background job; a player who never opened the garden cannot lose a plot.
   3. Lock the `gardens` row and its `garden_plots` rows `FOR UPDATE`.
   4. `wither_day = inv.finalized_at.astimezone(UTC).date().isoformat()`. If a `garden_wither_events` row exists for `(user, wither_day)` → **skip (daily cap)**. If one exists for `(user, invasion_id)` → **skip (replay idempotency)**. The unique constraints remain the hard backstop against races.
   5. Candidate plots = plots with `is_withered = False`. If none (all plots already withered) → skip.
   6. Pick one plot uniformly at random (`rng.choice` over plot ids; accept an injectable `rng` like the rest of the codebase, default `random.Random()`). Empty plots are eligible — a withered empty plot blocks planting, which is a real consequence; restricting to treed plots is listed as an open product decision in §5.
   7. Set `plot.is_withered = True`; insert the `garden_wither_events` row; `log_event(Events.GARDEN_EVENT, {..., "action": "withered", "invasion_id": ..., "plot": ...})`.
4. Return one result per affected user `(user_id, plot_index)` so the post-commit DM sender has its work list. Telegram sends happen only after the caller commits; failures are swallowed like `presentation._telegram`.

Guarantees produced by this algorithm:

| Unfair scenario | Defense |
|---|---|
| Invasion at 3 AM that nobody saw | Never-announced `EXPIRED` is exempt; announced losses still require the player to be an *active* member |
| Player slept through one announced loss | Daily cap: at most one plot per UTC day no matter how many groups/losses |
| Player actually fought | Any `inv2_attacks` row exempts them, even at zero rolled damage |
| Bot downtime recovery storm | Idempotent `(user, invasion)` constraint + daily cap; recovery settlement cannot double-apply |
| Retried settlement / admin `force_end` | Same idempotency constraint; settlement is safe to replay |
| Player with no garden / all plots withered | Explicit no-ops |
| Telegram blocked the bot | DB effect stands; DM failure is logged; the 🥀 state is visible in the garden panel |

### 2.3 Settlement integration points (for the implementation phase)

1. `services/invasion_v2/rewards_service.py::settle_outcome` — widen the accepted statuses to include announced `EXPIRED`; in the loss branch call `GardenService.apply_invasion_corruption(...)` before the region/state counter update. Victory path is untouched.
2. `services/invasion_v2/scheduler.py::_finalize_due` — append `(invasion.id, EXPIRED)` to `transitions` **only when `started_at`/`message_id` is not null**; never-announced expirations stay invisible to settlement.
3. `services/invasion_v2/recovery.py::run_on_startup` — after the `ACTIVE→EXPIRED` commit, drive `settle_outcome` for the announced rows (recommended; the idempotent ledger makes it safe) or explicitly document downtime amnesty. This is the one genuine product decision flagged in §5.
4. `services/maintenance/service.py:559-567` — no structural change; the existing loop already settles every returned transition and isolates per-invasion failures. A new post-commit DM step can sit next to `presenter.finalize`.
5. `services/invasion_v2/admin_controls.py:139,146` — unchanged call sites; replay safety comes from the ledger constraints.

### 2.4 Config values — fixing the 36-hour bug

| Key | Today | Proposed | Effect |
|---|---|---|---|
| `garden_growth_seconds` | `24*60*60` (86 400) | **`12*60*60` (43 200)** | Seedling reaches mature tree in 12 h |
| `garden_fruit_seconds` | `12*60*60` (43 200) | `12*60*60` (43 200, unchanged) | Mature tree fruits 12 h later; post-harvest cycles stay 12 h |

Result: `fruit_ready_at = planted_at + 12 h + 12 h = planted_at + 24 h` — the **first fruit is ready in exactly 24 hours**. The two-stage lifecycle (🌱 growing → 🌳 mature → 🍎 ready) is preserved; the alternative `growth=86400, fruit=0` would make the `MATURE` state instantaneous and effectively delete a lifecycle stage, so it is not recommended.

- Change is confined to `CONFIG_DEFAULTS` in `services/config_store.py:39-40`; no code path changes.
- Existing plots keep their already-computed timestamps — no backfill; only new plantings use the new cadence.
- Optional kill-switch config (proposed): `"garden_corruption_enabled": True` next to the garden keys, so operations can disable withering without a deploy while leaving the item and restore flow intact.

### 2.5 Shop item — `divine_fertilizer` (کود الهی)

| Property | Value |
|---|---|
| Item key | `divine_fertilizer` |
| Emoji | 🌿 |
| Persian label | کود الهی |
| Catalog | New `ITEM_CATALOG` entry in `services/items.py` (description in §3.3) |
| Buy price | **80** in `item_buy_prices` (`config_store.py:143`) — between `sib_buff` (50) and `healing_potion` (150); roughly one day of casual garden income, deliberately cheap enough to recover but costly enough to make invasions matter |
| Sell price | **None** — omitted from `item_sell_prices`, same policy as `magic_powder` (consumable remedy, not currency) |
| Artwork | `assets/items/divine_fertilizer.jpg`, 1024×1024 (§3.1); auto-resolved by `AssetService` |
| Storage | Standard `user_items` row via `ItemService.grant/consume` |
| Effect | Clears `is_withered` on exactly one chosen plot; preserves `planted_at`/`mature_at`/`fruit_ready_at`, `cycle_count`, `magic_level`. An empty withered plot returns to `EMPTY`; a treed plot resumes its derived lifecycle |
| Use constraints | Refused when the plot is not withered; refused (nothing consumed) when inventory is zero — mirroring `WarService.use_healing_potion`'s check-before-consume rule |

Proposed service method `GardenService.restore_plot(session, user_id, plot_index) -> GardenPlot`: lock user row → lock/load plot → require `is_withered` (else Persian `ValueError`) → `ItemService.consume(session, user_id, "divine_fertilizer", 1)` → clear flag → flush → `GARDEN_EVENT` `action="restored"` → XP award hook optional via `PlayerXPService.award(source="garden_restore", …)`.

### 2.6 Garden service behavior changes summary

- `_derive_state` / `view`: wither-first branch + `PlotView.is_withered`.
- `plant` (`garden.py:213-270`): refuse withered plots before the empty-state check.
- `_harvest_plot` / `harvest_all` (`garden.py:273-365`): withered plots are never `FRUIT_READY` under the new derivation, so they are naturally excluded from harvest-all; add an explicit refusal with a clear Persian message on single-plot attempts.
- `apply_magic` (`garden.py:375-432`): withered trees cannot be enchanted.
- New `apply_invasion_corruption` and `restore_plot` as above.
- Timestamps are **not** rewritten on wither or restore (no drift bugs); a tree that ripens while withered is simply ready immediately when restored — the intended “back to normal” semantics.

---

## 3. TASK 3 — UI, Image & Keyboard Rules

### 3.1 Exact AI image prompt for `divine_fertilizer`

Reference family observed in `assets/items/healing_potion.jpg`: warm cream watercolor paper, an irregular soft sepia/brown watercolor wash blob behind a centered cork-stoppered glass bottle, delicate brown ink outlines, glowing contents with golden swirls, sparse golden four-point sparkles, a soft shadow under the object, gentle sepia vignette toward the edges, no text. The fertilizer asset must read as a sibling of that image. Per the `create-game-assets` skill, generate it as a family asset using `healing_potion.jpg` as a reference/edit input where the tool supports it, then normalize to exactly 1024×1024 JPG.

**Prompt (English, feed verbatim to the image model; square 1:1, 1024×1024):**

```text
ROLE/PURPOSE: production game item icon for a Persian fantasy Telegram bot (sib), sibling asset to an existing watercolor potion-bottle icon family.
SUBJECT: one magical glowing green fertilizer bottle — a round clear glass apothecary bottle sealed with a natural cork stopper and a thin twine tie around the neck, filled with luminous emerald-green liquid in which tiny golden sparkles and small green leaves swirl; a soft magical green glow radiates from inside the bottle.
VIEW: straight-on centered front view, full object visible with generous margin, orthographic icon framing.
ART DIRECTION: hand-painted storybook watercolor on warm cream textured paper; loose irregular sepia-brown watercolor wash blob behind the object with soft organic edges; warm sepia vignette darkening gently toward the corners; delicate soft brown ink outlines; translucent watercolor gradients; muted earthy palette with emerald green as the single accent; gentle hand-drawn shadow beneath the bottle; a few small golden four-point sparkles around the glass.
GAME-SCALE READ: strong centered silhouette of a single round bottle; the green glow and cork must stay recognizable at small chat thumbnail size.
TECHNICAL OUTPUT: 1024x1024 pixels, square 1:1, flat illustration (not a photo, not 3D), scene background painted on the same cream paper — no transparency, no mockup.
LOCKS: preserve the warm cream paper tone, sepia wash blob, brown ink line weight, soft watercolor texture, centered composition and bottom shadow of the reference item family.
EXCLUDE: text, letters, Persian or Latin writing, labels, numbers, frames, borders, watermarks, signatures, logos, multiple bottles, pouches, hands, characters, scenery, cropped edges, photorealism, dark or black background, neon, heavy saturation.
```

Negative prompt (if the tool has a separate field): `text, words, label, frame, border, watermark, signature, collage, grid, multiple objects, pouch, photo, 3d render, dark background, cropped`.

Fallback variant (only if product prefers a pouch over a bottle, same style block): replace SUBJECT with *“a small rustic leather fertilizer pouch tied with rope, opened slightly to reveal a glowing emerald-green magical powder with tiny leaves and golden sparkles floating out of it”* — but the bottle is recommended because it matches `healing_potion.jpg` most directly and keeps the item family coherent.

Acceptance gates after generation (from the asset skill): exact 1024×1024, visual contact-sheet comparison against `healing_potion.jpg` for paper tone/vignette/line weight, no baked text, legible at thumbnail size, saved as `assets/items/divine_fertilizer.jpg`.

### 3.2 Button color plan (semantic glass buttons only)

The project's color system is defined in `keyboards/common.py:199-232` with the docstring rule that every user-facing button carries one of three styles and unstyled buttons are reserved for the legacy-client fallback. **All new and edited keyboards in this feature MUST use these helpers — raw `InlineKeyboardButton(...)` without `style=` is prohibited for callbacks:**

- `success_button(...)` — **green**, positive/confirm/restore actions.
- `danger_button(...)` — **red**, back/cancel/navigation-away actions (the established `nav_back_row` convention).
- `primary_button(...)` — **blue**, navigation and informational cells.

| Screen | Button | Style | Proposed callback |
|---|---|---|---|
| Garden menu (`handlers/garden.py:79-130`) | Withered plot row (navigates to tree panel) | `primary_button` 🔵 | `garden_tree:<idx>` |
| Garden menu | Unlock plot / harvest-all | `success_button` 🟢 (unchanged) | existing |
| Garden menu | Refresh / back to main menu | `primary_button` / `danger_button` (unchanged) | existing |
| Tree panel, withered plot | «🌿 استفاده از کود الهی (موجودی: N)» | `success_button` 🟢 | `garden_fertilize:<idx>` |
| Tree panel, withered plot | Back to garden | `danger_button` 🔴 (unchanged) | `garden_refresh` |
| Shop Items list/detail (`handlers/shop.py:299-400`) | Buy 1 / 5 / 10 / max fertilizer | `success_button` 🟢 (generic existing buttons, no shop code change) | `shop:buy:divine_fertilize…` |
| Shop | Back buttons | `danger_button` 🔴 (unchanged) | existing |
| Any confirmation step (if added) | Confirm restore = green; cancel = red | `success_button` / `danger_button` | — |

URL-style buttons (none expected here) would use `style="primary"` explicitly, as `common.py` already does.

### 3.3 Exact Persian UI text drafts

All strings follow the existing bot tone (informal second-person, emoji-prefixed) and use `parse_mode=None` / Markdown bold exactly as the current garden screens do.

#### A. Withered plot — garden menu line (`_render_garden`, replaces the per-plot hint)

```text
🥀 قطعه ۱ — پژمرده؛ برای احیا به کود الهی 🌿 نیاز است
```

Button label for that row:

```text
🥀 قطعه ۱ — پژمرده
```

(Plot number rendered with Persian digits via the existing `fa_digits` convention; magic-level suffix is hidden while withered.)

#### B. Withered plot — tree panel (`_tree_view` withered branch)

```text
🥀 **قطعهٔ پژمرده** — قطعه ۱

در اثر شکست گروه در یورش، این قطعه خشک شده است.
تا وقتی پژمرده است، نه می‌توانی نهال بکاری و نه میوه برداشت کنی.

🌿 برای احیا، یک «کود الهی» مصرف کن.
📦 کود الهی موجود: `۲`
```

#### C. Player notification (private DM, sent post-commit, best-effort)

```text
🥀 باغ تو پژمرده شد!

در یورش اخیر گروه، دشمن پیروز شد و چون در آن نبرد ضربه‌ای نزدی، یکی از قطعه‌های باغت به‌صورت تصادفی پژمرده شد.

قطعهٔ پژمرده نه قابل کاشت است و نه برداشت.
برای احیایش، از فروشگاه «کود الهی» 🌿 بگیر و روی همان قطعه استفاده کن.

🛡 یادت باشد: اگر در یورش‌ها شرکت کنی و ضربه بزنی، باغت پژمرده نمی‌شود.
(هر روز فقط یک قطعهٔ باغت می‌تواند پژمرده شود.)
```

#### D. Shop item — `ITEM_CATALOG["divine_fertilizer"]`

```python
"divine_fertilizer": {
    "emoji": "🌿",
    "label": "کود الهی",
    "description": (
        "کودی آسمانی و جادویی که یک قطعهٔ پژمردهٔ باغ را دوباره سرسبز "
        "می‌کند و کاشت و برداشت در آن قطعه دوباره ممکن می‌شود."
    ),
}
```

The shop detail screen composes the price/stock lines generically; it will read:

```text
🌿 **کود الهی**

کودی آسمانی و جادویی که یک قطعهٔ پژمردهٔ باغ را دوباره سرسبز می‌کند و کاشت و برداشت در آن قطعه دوباره ممکن می‌شود.

💰 قیمت: `80` سیب 🍎
📦 موجودی شما: `۰`
💰 سیب‌های شما: `…`
```

#### E. Toasts and refusal messages

| Situation | Exact Persian text |
|---|---|
| Restore succeeded (callback answer) | `🌿 قطعه با کود الهی احیا شد؛ دوباره سرسبز شد!` |
| No fertilizer in inventory | `کود الهی نداری! می‌تونی از فروشگاه تهیه‌اش کنی. 🌿` |
| Plant/harvest/magic on a withered plot | `این قطعه پژمرده است؛ اول با کود الهی احیایش کن. 🥀` |
| Fertilizer used on a healthy plot | `این قطعه سالم است؛ کود الهی فقط برای قطعه‌های پژمرده کاربرد دارد.` |
| Fertilize button when no withered plot exists (defensive) | `هیچ قطعهٔ پژمرده‌ای در باغ تو نیست. 🌳` |

---

## 4. TASK 4 — Implementation touch list and test impact (for the next phase)

**Application files expected to change** (none changed in this phase):

1. `database/models/garden.py` — `is_withered`, `PlotState.WITHERED`, `GardenWitherEvent`, `PlotView` field.
2. `database/models/__init__.py` — export `GardenWitherEvent`.
3. `database/migrations/versions/054_garden_corruption.py` — new revision (§2.1.1).
4. `services/config_store.py` — timing defaults (§2.4), `item_buy_prices["divine_fertilizer"] = 80`, optional `garden_corruption_enabled`.
5. `services/items.py` — catalog entry (§3.3-D).
6. `services/garden.py` — wither derivation, action guards, `apply_invasion_corruption`, `restore_plot`.
7. `services/invasion_v2/rewards_service.py` — loss-branch corruption hook + announced-`EXPIRED` acceptance.
8. `services/invasion_v2/scheduler.py` — announced-`EXPIRED` transitions.
9. `services/invasion_v2/recovery.py` — post-recovery settlement decision (§2.3-3).
10. `handlers/garden.py` — 🥀 rendering, withered tree panel, `garden_fertilize:<idx>` callback and toasts.
11. `assets/items/divine_fertilizer.jpg` — new 1024×1024 artwork.
12. `handlers/shop.py` — **no change expected** (config/catalog-driven); verify only.

**Tests to add/update:**

- New `tests/test_garden_corruption.py`: wither derivation overrides all timestamp states; plant/harvest/magic refusals; random-plot selection; all-withered and no-garden no-ops; restore consumes exactly one fertilizer and preserves timestamps/magic level; zero-inventory refusal consumes nothing.
- New invasion settlement tests: attackers (including zero-damage rolls) exempt; inactive/unknown members exempt; `(user, day)` cap blocks a second loss; `(user, invasion)` replay is idempotent; never-announced `EXPIRED` exempts; announced `EXPIRED` withers.
- Update `tests/invasion_v2/test_scheduler.py:246` and `test_recovery.py:44` (currently assert bare `EXPIRED` status) for the new transition/settlement behavior; extend `test_presentation_phase5.py:202-210` DEFEAT idempotency cases.
- Shop/item tests: catalog entry present, buy price 80, absent from sell prices, item appears in `_goods_view`, art resolves to the bundled file.
- Migration test following `tests/test_invasion_migration_031.py` conventions (column default, both unique constraints, downgrade).
- Existing timing tests are safe (manual timestamps); add one config-level test asserting `garden_growth_seconds + garden_fruit_seconds == 24h`.

---

## 5. Open product decisions (to confirm before implementation)

1. **Startup-recovery `EXPIRED`** — settle announced-but-recovered invasions as losses (recommended; bounded by the daily cap and idempotency ledger) or keep the current downtime amnesty.
2. **Eligible plots for random withering** — any non-withered plot including empty ones (recommended; simplest and still punitive) or only plots containing a tree.
3. **UTC vs local daily boundary** — recommendation is UTC to match `daily_chest_claims`; a Tehran-local reset would be friendlier but would diverge from every existing daily cap.
4. **Kill-switch config** — whether to ship `garden_corruption_enabled` (default true) alongside the feature.
5. **XP for restoring** — whether `garden_restore` awards account XP like planting/harvesting do today.

---

## Appendix — key file reference

| Concern | File |
|---|---|
| Plot/garden model | `database/models/garden.py` |
| Invasion models (attack, invasion, group state) | `database/models/invasion_v2.py` |
| Active group members | `database/models/invasion_v2_group_member.py` |
| Daily-limit precedent | `database/models/daily_chest.py` |
| Item inventory & catalog | `services/items.py` |
| Garden lifecycle service | `services/garden.py` |
| Invasion finalization (DB) | `services/invasion_v2/scheduler.py` |
| Settlement & victory rewards | `services/invasion_v2/rewards_service.py` |
| Startup recovery | `services/invasion_v2/recovery.py` |
| Maintenance driver | `services/maintenance/service.py:553-570` |
| Join/damage write path | `handlers/inv2_join.py` |
| Garden UI | `handlers/garden.py` |
| Shop UI (generic item rendering) | `handlers/shop.py` |
| Button style helpers | `keyboards/common.py` |
| Config defaults | `services/config_store.py` |
| Asset resolution | `services/assets.py` |
| Consumable-use template | `services/war_service.py:583-621` |
| Item artwork family | `assets/items/healing_potion.jpg` |
| Latest migration revision | `database/migrations/versions/053_apple_pickaxes.py` |
