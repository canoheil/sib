# Phase 0 — invasion rework guard report

## Checklist

- [x] Add `MaintenanceService._invasion_enabled()` using `ConfigService.get_raw` and a module-level, 60-second monotonic cache; missing/invalid values and DB errors fail closed.
- [x] Guard maintenance cycle, Telegram publishing, and the scheduler call. Also guard the independently invoked startup-recovery cycle.
- [x] Guard the only registered invasion user entry point (`invasion:` callbacks for join, Brace, status) with a Persian disabled notice. No separate invasion command handler exists.
- [x] Log the flag value once when the maintenance worker starts.
- [x] Inventory invasion files, Persian text and code paths in [PHASE-0-STATE.md](PHASE-0-STATE.md).
- [x] Commit in four small steps; add DB-free regression tests (14 passed).
- [ ] **Skipped by instruction:** DB connection/verification, schema changes, migrations, service restart, production log review and CPU sampling. The operator states the DB flag is `false`; this cannot be verified here.

## Files changed (line numbers in this branch)

- `services/maintenance/service.py:3-23,44-78` — flag cache/helper; `:948-959` — scheduling call guard; `:1162-1172` — maintenance DB guard; `:1192-1205` — presentation guard; `:1264-1268` — startup log.
- `services/recovery/service.py:172-189` — guarded invasion recovery call. This entry point runs before the maintenance worker.
- `handlers/invasion.py:50,61,797-805` — reuse flag and answer disabled callbacks before accessing invasion state.
- `tests/test_invasion_feature_flag.py:1-97` — mocked, database-free value/cache/fail-closed, callback, and scheduler guard tests.
- `docs/invasion-rework/PHASE-0-STATE.md` — inventory, Persian text snapshot, entry point map and architecture.
- `docs/invasion-rework/PHASE-0-REPORT.md` — this checklist and change report.

## Verification and issues

- `PYTHONPATH=/home/user .venv/bin/python -m pytest -q tests/test_invasion_feature_flag.py` → **14 passed**; tests mocked the config read/session, never connecting to a DB.
- Python syntax compilation and `git diff --check` passed. Existing integration tests requiring `sib_TEST_DATABASE_URL` were not run (DB access expressly prohibited).
- Initial system-wide `pip install` was rejected as an externally managed environment; dependencies were installed into an ignored local `.venv` for the DB-free tests. No runtime service was started.
- The requested basic Persian grep range `[آ-ی]` failed under the environment locale (`Invalid collation character`), so the state snapshot uses a PCRE Unicode range instead. The requested inventory command emits expected warnings on matching directories and includes locally generated, ignored `__pycache__` files.
- Caching means a change to the DB flag may take up to 60 seconds to take effect. Failures are cached as disabled to avoid repeated reads. Already-running in-flight work cannot be undone by this guard. Direct/manual domain calls (outside the registered production call sites) are still possible; future callers must check the flag.
- Source path differs from the original phase request: the maintenance module is `services/maintenance/service.py`, **not** `sib/services/maintenance/service.py`. No other described path differed. The user asked to remain on the current branch; branch is `arena/f5af6c7e-sib`.

## Final commit hash

`git rev-parse HEAD` on `arena/f5af6c7e-sib` after this documentation commit is the final hash; the exact hash is included in the delivery response. A commit cannot contain its own cryptographic hash without changing that hash.
