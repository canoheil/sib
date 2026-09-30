# KNOWLEDGE — sib (project gotchas)

## Environment / running things (repo-wide)

- The repository root **is** the `sib` namespace package: `import sib.core...`
  resolves to `<repo>/core/...` **only when the repository's parent directory is
  on `sys.path`**. The nested `<repo>/sib/` directory holds the bot-opponent
  package (`sib.bots.*`). Both roots must be importable — exactly what
  `deploy/check_health.sh` does:
  `PYTHONPATH="<repo-parent>:<repo-parent>/sib:."`.
  - Local test command used here:
    `cd <repo> && PYTHONPATH=/home/user:/home/user/sib:. sib_TEST_DATABASE_URL="sqlite+aiosqlite:////tmp/sibcom_sib_test.db" .venv/bin/python -m pytest -q`
- `tests/conftest.py` refuses to run without `sib_TEST_DATABASE_URL` whose value
  contains `sibcom_sib_test`. SQLite works (BigInteger PK shim, `requires_row_locks`
  skips lock-only races). Venv extras needed beyond `requirements.txt`:
  `greenlet`, `aiosqlite`, `pillow`.
- No MariaDB is installable in this sandbox (apt cannot reach deb.debian.org);
  the two suite failures at baseline (`test_navigation.py::test_shop_and_battle_...`,
  `test_stale_and_duplicate_callbacks.py::test_non_existent_match_callback`) are
  MySQL-only paths that `asyncio.run()` outside the injected session — they are
  environment artefacts, not regressions.

## Feature gotchas

- **Trade**: `trade_ok:`/`trade_cancel:` were owner-scoped; a shared GROUP trade
  message needs both parties to press it, so the scope moved to the service
  (participants only). Anything that must be pressable by several members must
  NOT be claimed as a PERSONAL panel (`services/panel.py::PERSONAL_PANEL_TYPES`).
- **Special drops**: the harvest outcome's `message` is only a callback toast;
  the celebration image is sent by `AssetService.send(...)` — pass `caption`
  there, never edit `assets/**`.
- **Active game**: `SessionService.has_active_session` and
  `PartyService.get_active_parties_for_group` used different definitions
  (statuses + group scope). Keep both derived from `services/session.py`.
- **Rarity**: `characters.rarity` is a NOT NULL column with `server_default
  "COMMON"`; keep the column (deprecated) so no data is lost, but no code path
  may treat it as a classification.
- **Factions**: never branch on faction code in generic card logic — enumerate
  from the `factions` table / data layer.
