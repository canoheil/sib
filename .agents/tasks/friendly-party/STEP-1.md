# STEP-1 — Inspection, architecture audit, baseline

Status: COMPLETED

## Sub tasks

1. [x] read skills: `tasks` (this folder), `persian-writing`, telegram skills overview
2. [x] audit party / matchmaking / match / reward / bot / navigation architecture (see CURRENT_DESIGN.md)
3. [x] test environment: `.venv` with aiogram 3.31, SQLAlchemy 2.1, aiosqlite, Pillow, pytest-asyncio
4. [x] baseline full suite: **560 passed, 8 skipped** (skips = `requires_row_locks`, MySQL only)

## NOTES

- `sib.bots` lives under `sib/sib/bots` → PYTHONPATH must contain `/home/user` AND `/home/user/sib`.
- MySQL/MariaDB is NOT available here; row-lock tests are reported as skipped, never as passed.
- No existing Friendly/رفیقانه implementation anywhere in the codebase.
