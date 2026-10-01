# KNOWLEDGE — gotchas & dos/donts (Friendly Party)

- **Env**: repo root IS the `sib` package; `sib.bots` lives in `sib/sib/bots`, so run everything with
  `PYTHONPATH=/home/user:/home/user/sib`. Use `.venv/bin/python` (system python has no aiogram).
- **Tests**: `sib_TEST_DATABASE_URL` must contain `sibcom_sib_test`; delete `/tmp/sibcom_sib_test.db`
  before a full run. MySQL/MariaDB is unavailable → `requires_row_locks` tests skip; never report
  them as passed. Run focused tests first, then the full suite.
- **Alembic**: config is `alembic.ini` at the repo root, versions live in `database/migrations/versions`.
  The full chain cannot be `upgrade`d on SQLite (migration 002 alters constraints); use
  `alembic heads` / `alembic stamp head` and offline `--sql` for DDL checks.
- **Async ORM**: after `session.rollback()`/`commit()` all objects are expired — accessing an
  attribute (even `.id`) triggers a lazy load and raises `MissingGreenlet`. Capture plain ints
  before rollbacks; use `selectinload` for every relationship a render path touches.
- **Handlers in tests**: patch `db_connection._async_engine` via the `db_session` fixture; handlers
  open their own sessions and commit, so re-query in the test after `rollback()`.
- **Engines**: resolve "my side" from `state["players"][…]["group_id"]`; friendly matches rewrite
  those to per-team audience ids (`abs(group)*1000+2` for team 2) — never change DB `group_id`s.
- **Keyboards**: `get_party_action_keyboard` delegates to the friendly keyboard when
  `party.is_friendly`; solo normal cards expose `make_friendly:{id}`. Back buttons must never cancel.
- **Rewards**: friendly payouts go through `AppleWalletService.earn(..., idempotency_key=...)`;
  seeds/group stats must never be touched for `match_type == friendly`.
