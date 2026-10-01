# Finish repository audit and lock the Friendly/privacy design

Status: COMPLETED

## Sub tasks

1. [x] Inspect skills, party/matchmaking/match models, result/timeout lifecycle, existing bot infrastructure, supported game modes, Apple Battle, trade, panel guard, migration/test setup.
2. [x] Record chosen Friendly board, team, reward, Apple Battle, rematch, bot-fill, and privacy architecture in `CURRENT_DESIGN.md`.
3. [x] Verify remaining public/personal callback ownership paths and current focused test baseline before implementation.
4. [x] Update `KNOWLEDGE.md` with migration/test/runtime gotchas.

## NOTES

- No application files or tests were changed in STEP-1; only task documents were added/updated.
- Callback audit confirms several private command/callback entry points are not consistently claimed (`/shop`, `/trade`, `/battle`, Apple Battle entry), while intentional public group callbacks must remain unblocked and continue using service-level actor checks.
- The pre-implementation focused baseline is **41 passed** with SQLite. One initial attempt used the repository's default MySQL URL and failed because no MySQL service is running; rerunning with explicit test SQLite URLs passed.
- `.venv` contains local test-only dependencies and is ignored/excluded; no requirements or lock file changed.
- Friendly architecture is locked in `CURRENT_DESIGN.md`; continue to STEP-2 unless a code constraint contradicts it.
