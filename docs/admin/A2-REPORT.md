# Phase A2 — Group lifecycle reconnaissance

## Recon findings

- **Group model:** `database/models/group.py:9-20` defines `Group`; current columns are `id` (`:13`), `title` (`:14`), `username` (`:15`), and `is_active` (`:16`). `TimestampMixin` adds `created_at` and `updated_at` at `database/base.py:13-25`. No other group columns existed at reconnaissance time.
- **Group-membership observer:** `middleware/group_membership.py:48-113` defines `GroupMembershipMiddleware`. It observes only group/supergroup messages and callbacks (`:56-75`), schedules bounded writes without blocking updates (`:77-112`), and relies on registered foreign-key rows as documented at `:1-4`.
- **Admin-log writer:** there is no shared, standalone admin-log writer helper. The existing persistence pattern is `handlers/inv2_controls.py:40-54` (`_execute`), which obtains `get_session()` and adds `AdminLog`; the table model is `database/models/admin_log.py:10-17`. Other writers also add `AdminLog` to their caller's session, e.g. `services/broadcast.py:67-72`.
- **Outer middleware reference:** `middleware/user_ban.py:10-26` implements `UserBanMiddleware.__call__`; it reads through `get_session`, catches database errors, logs them, and returns without allowing moderation lookup errors to escape.
- **Dispatcher setup:** `bot.py:30-45` installs `UserBanMiddleware` and `GroupMembershipMiddleware` as message/callback outer middleware, then registers all routers at `:43-44`. `handlers/__init__.py:39-75` is the router aggregator.

## Implementation and validation

This report will be completed with the A2 transition behavior, migration, test results, deployment limitations, and the final invasion-flag verification after implementation.
