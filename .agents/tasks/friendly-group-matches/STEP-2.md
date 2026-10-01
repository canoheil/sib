# Add schema, migration, configuration, and test-fixture support

Status: COMPLETED

## Sub tasks

1. [x] Add match-type constants and ORM fields/models for team snapshots, Friendly results/rematches/reward claims, and temporary private trade selections.
2. [x] Add Alembic revision 023 with safe defaults for existing rows and reversible DDL.
3. [x] Add tunable Friendly reward defaults (10 Apples, five claims per UTC day).
4. [x] Extend test cleanup and focused model/config/migration-chain regression coverage.
5. [ ] Run focused schema/config/migration tests; update design/knowledge and mark STEP-2 complete.

## NOTES

- Preserve Competitive defaults for every existing party/match/member row.
- Keep this step limited to schema/configuration/fixture scaffolding; service behavior lands in later steps.
- Test via `.venv/bin/python -m pytest` with explicit SQLite `DATABASE_URL` and `sib_TEST_DATABASE_URL`; SQLite does not validate MySQL row locks or MySQL-only migration DDL.
