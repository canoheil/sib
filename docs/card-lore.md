# Official card content

The shared runtime source for wiki, shop and owned-card detail is the
`characters` table. `data/characters_seed.json` ships the definitions and
is also the only shipped source of official text; there is no separate
wiki or shop copy.

## Exact content and scoped sync

An entry containing `affiliations_fa` is explicitly marked as officially
supplied. Its `name_fa`, `affiliations_fa`, `power`, `speed`, `intelligence`
and `lore_fa` are authoritative. Store these strings verbatim: no trimming,
normalization, spellchecking, rewriting or Markdown escapes in storage.
Do not mark or change a card before its official content has been supplied.

On startup, `seed_characters` creates missing definitions and updates only
these six fields on existing officially marked cards. It sends only changed
fields to `CharacterService.update`, so reruns are no-ops and actual stat
changes retain normal balance versioning. Existing unmarked cards retain
the append-only seed semantics. Availability, artwork, legacy roles,
passives, ownership, progression and economy are not rewritten.

For a card whose supplied affiliations establish a primary faction,
`faction` maps that membership to an existing faction code for wiki grouping.
Setting `sync_faction: true` explicitly opts **only that entry** into updating
its `faction_id` as well, including when an old installation holds a different
link. An unknown faction or incomplete/non-boolean opt-in fails the sync;
nothing infers a role, ability or affiliation string from the story. Existing
cards without this opt-in retain their previous faction-link behavior.
This uses the existing column and requires no further schema migration.

All writes belong to the caller's transaction; a failed sync must roll back.

`affiliations_fa` is free text, not a role or a new faction classification.
The legacy faction link remains useful for wiki grouping. Its prose and
legacy strengths/weaknesses are not appended to officially supplied card
bodies. Owned-card detail reads the same official affiliations but continues
to derive attributes from level and does not display the story.

Names and the labels «وابستگی‌ها» and «داستان» use Telegram's legacy
Markdown **single-asterisk** bold syntax. Escaping is presentation-only.
Wiki bodies omit price. Shop bodies use the identical renderer with the
separate final line `💰 قیمت: 300 سیب`; purchases still use the existing
unified `ShopService.card_price()` and do not read historical price columns.

## Deployment and migration safety

Migration `034_card_affiliations` adds only a nullable TEXT column. It does
not update any existing row. Apply it to the intended database **before**
starting the updated bot; `Base.metadata.create_all` cannot add a column to
an existing table. After normal database backup and target verification:

```sh
alembic upgrade head
```

This document is not evidence that a live migration has run. The repository
sandbox has no configured live database or Telegram connection. Tests use a
separate SQLite database. MariaDB SQL can be inspected without a connection:

```sh
alembic upgrade 033_shop_card_price:034_card_affiliations --sql
```

Downgrade deliberately retains the nullable column and its text, so an
application rollback cannot delete official content. Re-upgrade detects the
retained column. Older app code is compatible with the extra column.

## Verification

Use an isolated database whose URL contains `sibcom_sib_test`, as required
by `tests/conftest.py`. That fixture deletes **test** data between tests;
never point it at a live database. Example from the repository root:

```sh
PYTHONPATH=/home/user \
sib_TEST_DATABASE_URL=sqlite+aiosqlite:////home/user/sib/tmp/sibcom_sib_test_lore.db \
.venv/bin/python -m pytest -q tests/test_card_lore.py tests/test_card_lore_migration_034.py
```

The local test environment additionally needs `aiosqlite`, `greenlet` and
`Pillow`. SQLite cannot prove MariaDB row locking or live Telegram rendering.
Keep live verification distinct from unit/integration results.
