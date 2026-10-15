# Shop Chests

Shop chests are apple-priced chests sold in the shop's **Chests** section. The
shop root has exactly four sections: Items, Cards, Sell Items, Chests.

Daily chests (`/chest`, `services/daily_chest.py`) are a separate system. Shop
chests do not change their limits, claim rules, probabilities, claim records or
operation-key namespace.

## Configuration (data, not code)

Chest prices, weights, rewards and duplicate compensation are **data**. The
repository ships no game-balance defaults. Configure them with:

```
python -m sib.scripts.shop_chests list
python -m sib.scripts.shop_chests show <code>
python -m sib.scripts.shop_chests apply <file.json> [--dry-run] [--confirm-database NAME]
python -m sib.scripts.shop_chests set-active <code> on|off [--expected-version N] [--confirm-database NAME]
```

- **Target.** Every command prints the target database (driver, host, port,
  database name). The password is never printed.
- **Confirmation.** `apply` (without `--dry-run`) and `set-active` write, so they
  need confirmation before anything is written: `--confirm-database NAME` must
  equal the target database name, or in an interactive terminal the name must be
  typed at the prompt. Otherwise the command is refused with nothing written.
  `apply --dry-run` needs no confirmation and rolls back.
- **Optimistic concurrency.** Every chest in an `apply` file carries
  `expected_version`: the `version` you read with `list` or `show`, or `0` for a
  chest that does not exist yet. A chest whose stored version differs (or a
  `0` for an existing code) refuses the **whole file**. `set-active` takes an
  optional `--expected-version` with the same meaning. A versioned write reads
  the chest row with `SELECT ... FOR UPDATE` and re-reads it into the session
  (`populate_existing`), so a copy loaded earlier in the same session is not
  trusted. The lock is what stops two writers passing the check together; that
  is verified only on MariaDB, which has not been run here.
- **Migrations are not run here.** If the schema is missing, the command fails
  with a hint to run `alembic upgrade head` as a separate, reviewed step.
- `apply` writes every chest in the file in one transaction. One invalid or stale
  chest rejects the whole file. Malformed JSON is reported and nothing is written.

Example `apply` file entry:

```json
{
  "code": "starter_chest",
  "name": "Starter Chest",
  "price_apples": 50,
  "duplicate_apples": 10,
  "is_active": false,
  "expected_version": 0,
  "rewards": [{"kind": "apples", "weight": 1, "apples": 5}]
}
```

The values above are an example of the format only. They are not game-balance
defaults.

### Chest fields

| Field | Rule |
| --- | --- |
| `code` | unique; starts with a lowercase letter; `[a-z0-9_]`; 3–40 characters |
| `name` | 1–80 characters |
| `image_key` | optional bare asset key; the file is `assets/items/<key>.jpg`. Keys containing `/` or `..` are rejected |
| `price_apples` | integer ≥ 1 |
| `duplicate_apples` | integer ≥ 0; compensation when a card is already owned |
| `is_active` | must be `true` only for a fully valid chest |
| `sort_order` | display order (ascending) |
| `version` | incremented on each change; used for optimistic checks |

### Reward rows (each chest has its own independent table)

| `kind` | Fields | Notes |
| --- | --- | --- |
| `apples` | `apples` ≥ 1 | fixed apple amount |
| `item` | `item_key` | must exist in `ITEM_CATALOG` |
| `card` | `character_code` (preferred) or `character_id` | character must be released, shop-enabled and active. A `character_code` is resolved in the target database; an unknown code refuses the file. Give one of the two, not both |

Every row has `weight` ≥ 1. The total weight is capped. Probability of a row
is exactly `weight / total_weight`, shown to the player as a percentage with
two decimals (`< 0.01%` for tiny values).

## Green Apple Chest (the first shop chest)

Shipped definition: `data/shop_chests/green_apple_chest.json`. It is data read
by the CLI on request. **Nothing seeds it at startup and nothing in the bot
loads it automatically.** It must be registered explicitly, as described below.

| Field | Value |
| --- | --- |
| Display name | `صندوق سیبی` |
| Code | `green_apple_chest` |
| Price | 150 apples |
| Duplicate compensation | 50 apples |
| Image | `assets/items/green_apple_chest.jpg` (1024×1024, square, centred, no text) |
| Active | yes; it is the only chest in the initial release |

Reward table (weights out of 100, one independent row per reward):

| Reward | Identifier | Weight | Probability |
| --- | --- | --- | --- |
| Card: سیب سرخ | `sib_sorkh` | 15 | 15% |
| Card: سیب سبز | `sib_sabz` | 15 | 15% |
| Card: سیب طلایی | `sib_talayi` | 15 | 15% |
| Card: سیب ماهی | `sib_mahi` | 15 | 15% |
| Item: حلقه ازدواج | `wedding_ring` (`ITEM_CATALOG`) | 1 | 1% |
| 100 apples | fixed `apples: 100` | 39 | 39% |
| **Total** | | **100** | **100%** |

A test checks the total is exactly 100 and that each row has its weight.

- Each purchase charges 150 apples once and grants exactly one outcome. Repeat
  purchases are allowed while the chest is active and the balance suffices.
- A card the player already owns converts to 50 apples, credited once, with no
  collection XP. A new card is granted with the normal collection XP.
- The ring is granted through `ItemService.grant`. The 100-apple outcome
  credits exactly 100.
- The card identifiers are the audited codes from `data/characters_seed.json`.
  Their database ids differ per database and are resolved at apply time.

## Four-chest expansion

Approved definitions: `data/shop_chests/additional_chests.json`. This file
contains **only the four new chests**, so it can be applied without editing or
reapplying the existing Green Apple Chest. No service, handler, schema or daily
chest changes are needed. Like the starter definition, this file is not loaded
at startup; deployment alone does **not** register these chests.

| Code / image key | Display name | Price | Duplicate compensation | Order |
| --- | --- | --- | --- | --- |
| `enchanted_garden_chest` | صندوق باغ افسون | 80 | 0 (no cards) | 2 |
| `empire_chest` | صندوق امپراتوری | 200 | 60 | 3 |
| `sages_chest` | صندوق فرزانگان | 200 | 60 | 4 |
| `moonlight_chest` | صندوق مهتاب | 200 | 60 | 5 |

Each reward weight is a percentage (every table totals exactly 100). Each
opening still grants **one** outcome, not all the listed rewards. Each item
outcome grants one inventory item.

- **Enchanted Garden:** Magic Powder 40%, Golden Apple item 30%, Diamond Apple
  item 8%, Obsidian Apple item 2%, 40 normal apples 20%.
- **Empire:** `sib_sorkh`, `filo`, `goje`, `igris`, `zarush`, each 14%
  (70% cards total); Flower 19%, Wedding Ring 1%, 120 normal apples 10%.
- **Sages:** `sib_sabz`, `sib_talayi`, `green_knight`, `sage_king`, `treyni`,
  each 14% (70% cards total); Magic Powder 15%, Golden Apple item 5%,
  120 normal apples 10%.
- **Moonlight:** `sib_mahi`, `mmung`, `moon_knight`, `moon_lily`, `wizard`,
  each 14% (70% cards total); Diamond Apple item 15%, Obsidian Apple item 5%,
  120 normal apples 10%.

The Golden Apple **inventory item** (`apple_gold`) is distinct from the Golden
Apple **character card** (`sib_talayi`). Card codes are resolved in the target
database and all cards must be active, released and shop-enabled.

### Artwork

Four AI-generated illustrations are bundled in `assets/items/<image_key>.jpg`:
RGB, baseline JPEG, 1024×1024, centered closed chests on light parchment with
no text. The original Green Apple Chest was used as a visual style reference
and was not modified.

- Garden: living wood, intertwined roots, leaf lid and green apple lock.
- Empire: dark armored metal, crimson panels and a gold crown lock.
- Sages: blue/violet coffer, silver filigree and amethyst crystal lock.
- Moonlight: midnight navy, silver framing and a crescent moon lock.

The list remains text/buttons; each detail page resolves its own artwork via
`AssetService`. A runtime `/setasset <image_key>` registration overrides the
bundled image, so check for old overrides if a deployed image looks different.

### Expansion rollout (operator step; not run against the live game)

Use the existing schema and tool. First deploy the bundled images and JSON,
back up the selected database, confirm the character roster is seeded, then
validate on **staging**. From the repository parent:

```bash
DATABASE_URL='<staging async URL>' python -m sib.scripts.shop_chests apply sib/data/shop_chests/additional_chests.json --dry-run
DATABASE_URL='<staging async URL>' python -m sib.scripts.shop_chests apply sib/data/shop_chests/additional_chests.json --confirm-database <staging DB name>
DATABASE_URL='<staging async URL>' python -m sib.scripts.shop_chests list
```

The file has `is_active: true`; the confirmed apply makes all four buyable in
one transaction. On staging, check all five list entries (including the
previously registered starter), each picture, name, price, reward probability
and opening result. On production, repeat only after staging verification and
explicit operator approval, using the production URL and exact database name.
No production registration is performed by this repository change.

`expected_version: 0` means **new chests only**. Do not reapply unchanged after
registration: an existing code rejects the whole file. For later edits, obtain
each current version with `show` and update the input's `expected_version`.
To hide an individual chest, use `set-active <code> off --expected-version N`
with the correct database confirmation; keep the opening history.

With repository-default item sell prices, expected cash return (owned cards
converted to compensation, sellable items sold, unsellable powder valued at
zero) is 35, 61.85, 56.5 and 79 apples respectively, below each purchase price.
This is not a guarantee for runtime overrides or a valuation of new cards.
Check the target environment's `item_sell_prices` before activation. A rare
high-value reward is intentional; not every individual opening must lose money.

`tests/test_additional_shop_chests.py` pins the approved definitions and
faction pools, distinct artwork, registration alongside the starter, all reward
outcomes, duplicate compensation, replay safety, insufficient funds, daily
chest independence, detail-page images and CLI dry-run rollback. Existing
row-lock concurrency tests still need MariaDB; SQLite skips are not passes.

### Registering the Green Apple Chest (dev or test database only)

Never run these against production. Name the target database explicitly. The
database must already have migration `032_shop_chests` applied, which is a
separate, reviewed step (`alembic upgrade head` on that database only). The
card rows must already exist there (the characters seed).

```
# from the directory that contains the sib package (the repository parent)
DATABASE_URL='<dev-or-test DB URL>' python -m sib.scripts.shop_chests apply sib/data/shop_chests/green_apple_chest.json --dry-run
DATABASE_URL='<dev-or-test DB URL>' python -m sib.scripts.shop_chests apply sib/data/shop_chests/green_apple_chest.json --confirm-database <dev-or-test DB name>
DATABASE_URL='<dev-or-test DB URL>' python -m sib.scripts.shop_chests show green_apple_chest
```

`--dry-run` validates the file and rolls back. The real `apply` must name the
database in `--confirm-database`. On MariaDB that is the database name. On SQLite
it is the file path shown on the `target:` line. Unknown card codes, ineligible cards, and any
invalid row refuse the whole file with nothing written.

To change the chest later, read its `version` with `show`, put that number in
`expected_version`, and apply again. A stale or `0` value on an existing chest
is refused. Use `set-active green_apple_chest off --expected-version N` to hide
it.

## Opening

Read-only decisions come first, and nothing is written until step 6:

1. The user row is locked first (see `docs/lock-order.md`).
2. If an opening with the same `(user_id, operation_key)` exists, its stored
   outcome is returned. No draw, no charge, no reward.
3. The chest must be active and fully valid (validated **on use** as well).
4. One weighted draw from an injectable RNG (`rng.randrange(total_weight)`).
5. Duplicate check: a card the user already owns converts to `duplicate_apples`
   with **no collection XP**; a new card grants the normal collection XP. Funds
   are checked here too. A refusal at any point in steps 1–5 writes nothing.

From step 6 the writes run in **one atomic block** (a savepoint on MariaDB):

6. The item or card reward is written (before the wallet, per the lock order).
7. The opening row is inserted as the idempotency gate. A concurrent request
   with the same key that wins this insert makes this one replay the stored
   outcome.
8. One `debit_once` charges `price_apples` under the key
   `shop_chest:{user_id}:{operation_key}:charge`.
9. Apple rewards and duplicate compensation are credited.
10. Collection XP for a new card (progression, always last). The balance after
    the opening is snapshotted into the opening row.
11. The caller commits.

A refusal inside the block (step 6–10) rolls the whole block back, so the
reward, the gate row and the charge go together. On MariaDB this holds even if
the caller catches the refusal and commits; that is what the savepoint is for.
The MariaDB savepoint branch is logic-checked on SQLite only. It is not verified
on MariaDB by this suite. On SQLite (test engine, no savepoints) the caller's
rollback is the only undo. Refusals in steps 1–5 write nothing on either engine.

Purchases are unlimited while funds suffice and the chest is active.

### Keys and replays

- Each rendered chest-detail page carries a fresh nonce. A double tap reuses it
  and is charged once. A new page is a new opening.
- A consumed key never authorizes a new purchase: the replay returns the
  recorded outcome, and a key reused for a different chest is refused
  (`ShopChestConflictError`).
- Chest keys are user-scoped and live in the `shop_chest:` namespace, separate
  from daily chest keys.

### Ambiguous commits

If COMMIT's outcome is unknown (connection lost during commit),
`open_chest_reliably` looks the opening up in a **fresh session** by the SAME
key:

- found: the purchase happened; its stored outcome is returned with
  `replayed=True`;
- not found: nothing committed; the open is retried **once** with the same key.

A new key is never generated to retry an unknown-status operation.

## Callbacks

| Callback | Purpose | Length |
| --- | --- | --- |
| `shop_chests` | list of valid active chests | 11 |
| `shop_chest:<id>` | detail with probabilities and open button | ≤ 30 |
| `shop_chest_open:<id>:<version>:<nonce>` | open (charges once per key) | ≤ 52 in practice (≤ 64 limit) |

The open button carries the chest **version** the page was rendered from.
Opening refuses, before any write, when the stored version differs
(`STALE_OPEN_TEXT`). So a page rendered before a price or reward-table change
cannot buy the new terms unseen. A recorded opening is still replayed whatever
the version is now, so a double tap or retry is never charged again. A callback
in any other shape, including the earlier unversioned `shop_chest_open:<id>:<nonce>`
form, is refused the same way. The "open again" button on the result screen is
bound to the version that opening was priced at.

Length bounds: ids below 10¹², versions up to the INT maximum (10 digits), and
the 12-character nonce from `secrets.token_urlsafe(9)`. The worst case is 52 bytes.

An unexpected failure while opening answers with a neutral alert ("the outcome
is unknown; check your balance and try again"). It never says the chest opened.
A refusal (funds, stale page, unavailable chest) shows its own alert.

`shop_chest:` and `shop_chest_open:` are owner-scoped (`services/panel.py`).
They do not collide with `shop_buy`, `shop_good_buy`, `shop_sell*` or the daily
chest callbacks (`apple_chest`, `group_chest`).

## Tables

- `shop_chests`: definitions (unique `code`).
- `shop_chest_rewards`: reward tables; `RESTRICT` foreign keys to `shop_chests`
  and `characters`; per-kind shape check.
- `shop_chest_openings`: one row per opening; unique `(user_id, operation_key)`;
  snapshot of price, reward and balance.

Migration `032_shop_chests` (down-revision `031_invasion_four_wave`) creates the
three tables and nothing else. No rows are seeded.

## Verification

- SQLite tests run the opening logic, refusals, replays and the CLI on the test
  database. The Green Apple Chest tests are in `tests/test_green_apple_chest.py`.
  Commit-after-catch refusal tests are in `tests/test_shop_chest_refusals.py`.
- Stale-page and stale-version tests (`test_green_apple_chest.py`) run on SQLite:
  a page rendered before a price change, a legacy callback, a replay after a
  change, and a `set-active` or `save_chest` whose version is stale. The
  `set-active` case reproduces on SQLite. The row-lock part of these checks does
  not, because SQLite has no row locks.
- Row locks, concurrent openings and the savepoint itself need MariaDB. Those
  tests are marked `requires_row_locks` and are reported as SKIPPED on SQLite.
  A skip is not a pass. Nothing here has been run against MariaDB.
- The migration's DDL was rendered offline for the MySQL/MariaDB dialect
  (`alembic upgrade 031_invasion_four_wave:032_shop_chests --sql`): three tables,
  their indexes and constraints, and the version bump. Nothing was executed
  against a database.

## Rollout (commands; not executed by this change)

The bot needs the migration before it runs the chest code, and the chest needs
its configuration before any player can see it. Run each step only on the
environment named, after a backup of that environment's database.

1. **Staging first.** Deploy the code (a restart is the operator's step). Then,
   on the **staging** database only:
   Alembic runs from the repository root (the directory that contains
   `alembic.ini`). The CLI runs from the directory **above** the repository,
   because the package is imported as `sib`.
   ```
   (cd <repository root> && DATABASE_SYNC_URL='<staging sync URL>' alembic upgrade head)
   cd <parent of the repository>
   DATABASE_URL='<staging async URL>' python -m sib.scripts.shop_chests apply sib/data/shop_chests/green_apple_chest.json --dry-run
   DATABASE_URL='<staging async URL>' python -m sib.scripts.shop_chests apply sib/data/shop_chests/green_apple_chest.json --confirm-database <staging DB name>
   DATABASE_URL='<staging async URL>' python -m sib.scripts.shop_chests show green_apple_chest
   ```
   The shipped file is `is_active: true`, so the apply makes the chest buyable
   straight away. For a staged release, copy it with `"is_active": false`, apply,
   check the page, then turn it on:
   ```
   DATABASE_URL='<staging async URL>' python -m sib.scripts.shop_chests set-active green_apple_chest on --expected-version <version from show> --confirm-database <staging DB name>
   ```
2. **Production.** The same steps, against the production database, only after
   staging has been checked and the operator has approved the production window.
   Each command names the production database in `--confirm-database`.
3. **Disable without a rollback.** `set-active green_apple_chest off --expected-version N`
   hides the chest and refuses new openings. Existing openings are kept.
4. **Rolling back the migration** (`alembic downgrade 031_invasion_four_wave`)
   drops the three tables and **deletes every opening record**. Do not use it as
   a way to hide a chest. Use step 3.

On MariaDB, `alembic upgrade head` is the only migration step. Confirm the
database first with the `target:` line each CLI command prints.

## Known limits

- The chest image is optional; without one the detail page is text-only.
- Repeat-purchase limits, the card pool source and whether a traded-away card
  may be re-bought at full price are product decisions not yet made. The
  current implementation allows unlimited purchases and a full-price re-buy of
  an unowned card.
