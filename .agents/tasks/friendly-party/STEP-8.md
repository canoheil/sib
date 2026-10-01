# STEP-8 — Focused tests (friendly flows)

Status: COMPLETED

## Sub tasks

1. [x] `tests/test_friendly_party.py` — 35 tests: creation/join/team caps, 2v2 + 4v4 + 8-player party,
   solo conversion, bot filling (2v2/4v4, host-only, idempotent), start + no double start,
   never-ranked guarantee, apples + daily caps + duplicate prevention + no-rank/no-seedling,
   rematch (+ guards, + reward limits), audience helpers, final-text shaping,
   party/match-type keyboards, handler flow create → fill bots → start, one-board game action,
   neutral final board with Rematch
2. [x] concurrency: row-lock races are covered by the existing `requires_row_locks` suite on MySQL;
   SQLite runs skip them (never reported as passed). Service-level guards are asserted directly.

## NOTES

- Result: `35 passed`. Full suite after all steps: **595 passed, 8 skipped** (SQLite; baseline 560+8).
