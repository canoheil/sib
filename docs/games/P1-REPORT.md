# P1 — Party zombie cleanup and lifecycle fix

## Reconnaissance

Party memberships are inserted by `PartyService.create_party()` and `join_party()`; friendly bot filling and `services/friendly.py` also add `PartyMember` rows. `leave_party()` removes only the member explicitly leaving. When the party becomes empty it marks the party `CANCELLED`, but does not clear remaining rows. `cancel_party()` similarly changes status without deleting memberships. Matchmaking and match completion do not provide a general sweep for these membership rows. Consequently, abandoned/cancelled parties can retain memberships indefinitely. There is a uniqueness constraint only for `(party_id, user_id)`, not for a user's active membership across parties.

## Fix

Party creation and joining use the existing per-user row lock, then remove memberships in other non-live parties before inserting the new membership. Old queue entries are deactivated, old party counts/statuses are updated, and memberships belonging to a currently active match are preserved. The user lock serializes competing create/join operations for the same user.

`MaintenanceService.cleanup_zombie_parties()` removes memberships for parties older than two hours or parties without an active queue or active match. Active-match memberships are protected. The maintenance worker invokes this isolated sweep every existing 30-second cycle and logs the number of rows removed at INFO.

## Verification

Automated tests could not be run in this environment: `pytest` is not installed, and the repository's async test fixture also requires `sib_TEST_DATABASE_URL` pointing to the isolated test database. Run the suite in the configured project environment before deployment.
