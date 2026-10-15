# Phase A2 — Auto-register groups on bot membership changes

## Recon findings

- **Group model:** `database/models/group.py:9-22` defines `Group`. Its final columns are `id` (`:14`), `title` (`:15`), `username` (`:16`), `is_active` (`:17`), and the new nullable `welcomed_at` (`:18`). `TimestampMixin` adds `created_at` and `updated_at` at `database/base.py:13-25`. At reconnaissance time, before A2, the model had the first four columns only; the timestamp columns come from the mixin.
- **Group-membership middleware:** `middleware/group_membership.py:48-113` defines `GroupMembershipMiddleware`. It observes group/supergroup messages and callbacks (`:56-75`), schedules bounded writes without blocking incoming updates (`:77-112`), and relies on registered foreign-key rows as documented at `:1-4`. This middleware was not modified.
- **Admin-log writer:** there is no shared, standalone admin-log writer helper. The existing persistence pattern is `handlers/inv2_controls.py:40-54` (`_execute`), which gets a session and adds an `AdminLog`; the table model is `database/models/admin_log.py:10-17`. `services/broadcast.py:67-72` is another direct writer. The lifecycle handler reuses `get_session` and `AdminLog`, storing the group ID/title in `details` because `AdminLog` has no `group_id` column.
- **Outer middleware reference:** `middleware/user_ban.py:10-26` implements `UserBanMiddleware.__call__`; it reads through `get_session`, catches database errors, logs them, and prevents the exception from escaping update handling.
- **Dispatcher setup:** `bot.py:30-45` installs `UserBanMiddleware` and `GroupMembershipMiddleware` as message/callback outer middleware, then registers all routers at `:43-44`. `handlers/__init__.py:39-80` aggregates routers; the new group lifecycle router is included there.

## Status transitions handled

The router listens to aiogram's `my_chat_member` observer (`ChatMemberUpdated` for the bot). It skips private chats, non-group/supergroup chats, non-bot users, mismatched old/new member identities, and unchanged statuses.

| Case | Old status | New status | Result |
|---|---|---|---|
| Register/reactivate | `LEFT`, `KICKED`, or `RESTRICTED` | `MEMBER` or `ADMINISTRATOR` | Upsert `groups` by `id`; insert `id`, `title`, `username`, `is_active`; on duplicate update only `title` and `is_active = 1`. Add `group_auto_registered` audit log and INFO log. Atomically claim and send the welcome only when `welcomed_at IS NULL`. |
| Deactivate | Any different status | `LEFT` or `KICKED` | Set an existing group's `is_active = 0`, add `group_deactivated` audit log, and write an INFO log. |
| Other status changes | Any other changed pair | — | No database write, audit log, or message. |

The registration upsert intentionally does not update `welcomed_at` on duplicate rows. The specified registration matrix is applied literally; for example, `MEMBER -> ADMINISTRATOR` is not in the listed registration transitions and is therefore a no-op.

The audit actor is `ChatMemberUpdated.from_user.id`; `AdminLog.details` contains `group_id` and, for registration, `title`. The log table has no dedicated group-ID column.

## Migration

- **ID:** `045_group_welcomed_at`
- **Parent:** `044_user_is_banned`
- Adds one nullable `DATETIME` column, `groups.welcomed_at`; downgrade drops that column.
- Upgrade/downgrade were exercised against SQLite in the lifecycle tests. The migration has not been applied to a production MariaDB database.

## Test results

- `tests/groups/test_group_lifecycle.py`: **13 passed, 0 failed**. Covers the nine requested lifecycle behaviors, ignored status transitions, dispatcher router registration, reversible migration behavior, and failed welcome delivery.
- Related regression checks (`tests/invasion_v2/test_group_power_membership.py`, `tests/invasion_v2/test_dispatcher_imports.py`, `tests/invasion_v2/test_admin_phase6.py`): **25 passed, 0 failed**.
- Schema checks (`tests/test_models.py`, `tests/test_migrations.py`): **8 passed, 0 failed** (3 model tests and 5 migration tests).
- Total across those targeted commands: **46 passed, 0 failed**. The full repository suite was not run. `git diff --check` and Python compilation checks passed.

## Sample logs

```text
2026-10-15 10:21:11 [INFO] [sib] Group auto-registered: -100123 گروه آزمایشی
2026-10-15 10:21:11 [INFO] [sib] Group auto-deactivated: -100123
```

## Limitations and scope

- The database registration, audit record, and `welcomed_at` claim commit before the Telegram send. If the bot lacks permission to post, registration still succeeds and the send failure is logged; `welcomed_at` remains set, so the welcome is not retried automatically.
- Apply migration `045_group_welcomed_at` before deploying this handler. No live Telegram permission test or production MariaDB migration was performed.
- No invasion-v2 code or `invasion_enabled` setting was changed. No `_archive_inv1_*` table was touched. The live database value was not queried, so the guarantee here is that A2 does not modify it.
