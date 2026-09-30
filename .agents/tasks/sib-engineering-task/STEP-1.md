# STEP-1 — Install persian-writing skill + task docs + environment setup

Status: COMPLETED

## Sub tasks

1. [x] inspect branch/HEAD/history (`arena/01a0f270-sib` @ 4138652, clean tree)
2. [x] inspect skills dir (`.agents/skills/` — tasks, telegram*, flux-image-best-practices)
3. [x] activate `tasks` skill (this folder)
4. [x] install `persian-writing` skill from github.com/ali2000hos/persian-writing →
   `.agents/skills/persian-writing/` (SKILL.md v1.3.6 + references + scripts + word list),
   registered in `skills-lock.json`
5. [x] image/design skill identified: `flux-image-best-practices` (prompting) + repo art direction
   in `services/banner.py` / `assets/items/` 1024×1024 convention
6. [x] architecture mapped (see CURRENT_DESIGN.md)
7. [x] test environment: `.venv` with requirements + greenlet + aiosqlite + Pillow;
   `PYTHONPATH=/home/user`; `sib_TEST_DATABASE_URL=sqlite+aiosqlite:////tmp/sibcom_sib_test.db`;
   baseline full suite: **459 passed, 6 skipped** (skips = `requires_row_locks`, needs MySQL)

## NOTES

- Repo root is package `sib` (imports `sib.core...`), resolved by putting the repo's PARENT on
  PYTHONPATH — exactly what `deploy/*.sh` do.
- 2 suite tests use no fixture → need `DATABASE_URL` also pointed at the test DB.
- MySQL/MariaDB is NOT available in this environment; `requires_row_locks` tests must be reported
  as skipped, not passed.
