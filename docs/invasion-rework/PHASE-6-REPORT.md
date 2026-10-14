# Phase 6 — Admin controls for Invasion v2

## Implementation and migration

- `handlers/inv2_controls.py` handles private admin commands; `handlers/inv2_dashboard.py` handles the owner-bound inline dashboard. Both use the project's `IsAdminFilter` **and** a direct `sib.admin.auth.is_admin` check. No admin command runs from a group. `handlers/__init__.py` registers both routers.
- No `admin_logs` table or DB-writing admin-log helper existed in this checkout; previous admin actions called `core.logging.log_event` (structured process logs only). Reversible migration **`038_inv2_admin_logs`** adds a dedicated `admin_logs` table (`id`, `admin_id`, `action`, `details`, `created_at`, indexes). Model: `database/models/admin_log.py`. Apply migration before using commands. Each accepted Phase 6 command, including malformed inputs and read-only commands, records a row in the same transaction as its operation. Unauthorized invocations never mutate DB state. If the database itself is unavailable, even an audit row cannot be guaranteed; the command returns a short Persian error.
- `services/invasion_v2/admin_controls.py` implements the database operations without modifying scheduler, resolver, or presentation. Manual spawn calls the Phase 4 circuit-breaker availability check, locks the Group and breaker state, respects active status and the configured concurrency cap, uses the registered authoritative power resolver and pure enemy/HP rules, and bypasses **only** the interval. With the flag off, force-spawn refuses. Manual force-end settles through the existing idempotent reward/region service; it does not post a Telegram message.
- `services/maintenance/service.py::invalidate_invasion_config_cache()` clears the 60-second flag cache after committed toggle or valid `inv2_*` writes; a generation counter prevents an overlapping stale read from restoring the old TTL. Maintenance also records the last successful inv2 tick or exception in `services/invasion_v2/admin_runtime.py` for process-local status/health displays. No scheduler, resolver, or presentation logic changed.

## Commands (admin, private chat only)

| Command | Arguments | Illustrative short Persian reply |
|---|---|---|
| `/inv2_status` | none | `⚔️ وضعیت تهاجم · فعال: خیر · نبرد فعال: ۰ · آخرین تیک موفق: —` |
| `/inv2_toggle` | `on\|off` | `⚔️ تهاجم غیرفعال شد.` (the `on` action exists but was **not run on a live DB**) |
| `/inv2_force_spawn` | `<group_id> [region_code] [enemy_type]` | `⚔️ نبرد شمارهٔ ۴۲ برای گروه -۱۰۰ در درّه ماه آماده شد.` or a specific refusal |
| `/inv2_force_end` | `<invasion_id> <victory\|defeat>` | `⚔️ نبرد شمارهٔ ۴۲ با پیروزی پایان یافت.` |
| `/inv2_regions` | none | `🗺️ نواحی اواریا · moonfall_valley \| درّه ماه \| فرزندان ماه \| آزاد \| آخرین: —` |
| `/inv2_reset_region` | `<region_code>` | `🗺️ ناحیهٔ درّه ماه آزاد شد.` |
| `/inv2_mute` | `<group_id> <hours>` (1–8760) | `🔕 گروه -۱۰۰ برای ۲ ساعت خاموش شد.` |
| `/inv2_unmute` | `<group_id>` | `🔔 گروه -۱۰۰ از خاموشی بیرون آمد.` |
| `/inv2_group_info` | `<group_id>` | `⚔️ گروه -۱۰۰ \| فعال · پیروزی: ۱ \| شکست: ۰ · نبرد جاری: —` |
| `/inv2_invasions` | `[status]` (PENDING/ACTIVE/VICTORY/DEFEAT/EXPIRED) | `⚔️ نبردهای اخیر · #۴۲ گروه -۱۰۰ \| moon \| soldier \| ACTIVE \| ۲۵/۱۰۰` (at most 20) |
| `/inv2_config` | none | `⚙️ تنظیمات تهاجم · invasion_enabled: false · inv2_member_active_window_days: 30` (includes `invasion_*` and `inv2_*` rows) |
| `/inv2_set` | `<key> <value>` | `⚙️ inv2_member_active_window_days برابر ۵ ثبت شد.`; only implemented/whitelisted `inv2_*` keys accepted |
| `/inv2_health` | none | `🩺 سلامت تهاجم \| غیرفعال · نبرد: ۰ \| گروه خاموش: ۰ \| ناحیهٔ ازدست‌رفته: ۰ · آخرین تیک موفق: — · آخرین خطای این فرایند: —` |
| `/inv2_dashboard` | none | Sends an inline status screen with Toggle On/Off/Refresh; Regions/Muted Groups/Active Invasions; Config/Health/Simulate |
| `/inv2_simulate` | `<group_id> [enemy_type]` | `⚔️ شبیه‌سازی گروه -۱۰۰ \| شیاسیب پیاده · جان دشمن: ۱۲۰ · ۳ نگهبان: آسیب موردانتظار ۹۹ \| بخت پیروزی ۱۵٫۰٪` (illustrative only) |

The dashboard's `inv2adm:<owner_id>:<action>` callback rejects another admin's press, audits valid actions and edits its private message in place. Because the Simulate button has no group ID, it displays the command syntax rather than inventing a group. The `/inv2_simulate` command also accepts Phase 5's former third `user_count` argument for compatibility, but now always estimates **N = 1, 3, 5, 10**. It uses the current `DbGroupPowerResolver` for authoritative HP, reads active members' own Main Card powers for the distribution, and uses 2,000 deterministic damage trials per N (sampling with replacement). No invasion, reward, item, or config rows are written by simulation. Its one `admin_logs` row is **required audit**, resolving the conflict between “every command is audited” and “simulation does not write to DB” in favor of game-state read-only + audit-only write.

## Verification, safety and limits

- `PYTHONPATH=/home/user:/home/user/sib .venv/bin/pytest -q tests/invasion_v2 tests/test_invasion_feature_flag.py` → **140 passed**, 0 failed (14 new Phase 6 tests). All DB checks used isolated SQLite; no bot started, production DB opened, or Telegram group contacted. Tests toggle `on` and back `off` only in the disposable database; the final isolated flag is `false`.
- Focused line coverage: `handlers/inv2_controls.py` **91%**, `handlers/inv2_dashboard.py` **100%**, `services/invasion_v2/admin_controls.py` **86%**, `services/invasion_v2/simulation.py` **88%**; combined **90%** (367 statements). `git diff --check` and `compileall` passed.
- **Live `invasion_enabled` value cannot be confirmed** without DB access. Phase 6 made no production DB connection and did not issue a live `/inv2_toggle on`; source defaults/older migration seed false, but that is not proof of a deployed value.
- Admin status tick/error timestamps are **process-local**, lost on restart and not shared across workers; `last_error` covers errors caught by the inv2 maintenance block, not every unrelated application log. `/inv2_set` deliberately excludes `invasion_*` keys (including join window, interval and difficulty) because the requested whitelist is **only known `inv2_*` keys**; these are visible in `/inv2_config` but cannot be changed with this command. Manual force-end updates DB/rewards/state but does not send a final result to the group. The inherited Phase 5 `/inv2_preview` remains read-only and is outside the new Phase 6 audit command set.
- Configuration invalidation affects this process's cache; an already-running tick cannot be undone. In a multi-worker deployment, other workers' in-memory flag caches may remain valid for up to their original TTL. Simulation probabilities are estimates, not exact odds; its sample is observed members, not the Telegram roster, and N may exceed observed members (replacement sampling). Manual spawn respects the breaker and refuses while the feature flag is off.
