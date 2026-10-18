# Economy Lock-Order Policy

The authoritative table is `LOCK_RANKS` in `services/lock_order.py`.
`tests/test_lock_ordering.py` imports it and fails if the relationship-domain
ranks drift from the suite's own table.

| Rank | Resource | Notes |
| --- | --- | --- |
| 0 | `users` | per-user economy mutex; locked first |
| 1 | `marriage` | relationship domain |
| 2 | `marriage_proposal` | |
| 3 | `flower_gift` | |
| 4 | `player_cards` | ascending `id` when several |
| 5 | `user_items` | inventory rows, ascending `item_key` when several |
| 6 | `apple_wallets` | the apple balance |
| 7 | `player_progressions` | XP / level rows; always last |

## Rules

1. **User row first where the path uses it.** Paths that call
   `lock_user_row` take rank 0 before any read that decides a rule. Today these
   are: shop purchase, shop chest opening, item buy and sell, card upgrade and
   garden unlock. The daily-chest claim takes the same rank-0 row with its own
   `SELECT ... FOR UPDATE` (it does not call the helper). Other economy paths
   do not take the user row first. Apple battle settlement locks battle rows,
   invasion rewards and panel claims take no user-row lock, and the sib claim
   takes the user row through its own `SELECT ... FOR UPDATE`. Same-user work
   on these paths serializes only where the paths share a resource. This rule
   therefore does NOT say that every economy operation serializes on rank 0.
2. **Ascending order inside a transaction.** A transaction must not wait for a
   lock ranked earlier while it holds a later one.
3. **Multi-user work** locks user rows in ascending `id` order
   (`lock_user_rows_in_order`).
4. **Apple battle settlement** pays the winner's apples (rank 6) before the
   match progression award (rank 7). Before this fix it did the reverse, which
   could deadlock against a shop purchase of the same user (wallet → progression).

## Current paths

"Lock order" is the order in which the path takes its locks. "Write order" is the
order of its first writes. Where the two differ, the note says why.

| Path | Lock order | Write order |
| --- | --- | --- |
| Item buy | user → user_items → wallet (→ grant) | as locked |
| Item sell | user → user_items → wallet | as locked |
| Shop card purchase | user → player_cards → wallet → progression | card acquire → wallet debit → XP (all in one savepoint on MariaDB) |
| Card upgrade | user → player_cards → user_items → wallet | card CAS → **items consumed → wallet debited** → level and XP |
| Chest opening | user → player_cards (duplicate check, new-card insert) → user_items (item reward) → wallet (debit, apple reward or compensation) → player_progressions (collection XP) | item or card reward → opening row (idempotency gate) → wallet debit → credits → XP. The whole write section is one savepoint on MariaDB. |
| Garden unlock | user → garden → wallet | as locked |
| Apple battle settlement | battle row → wallet (winner) → progression | wallet → progression |

### Card upgrade write order (fixed)

`services/card_upgrade.py` `upgrade` used to debit the wallet before it consumed
the items. The locks were already taken in the documented order by the reads in
step 3, so this was **a write-order fix, not a proven deadlock**. The writes now
follow the same order as the locks (`player_cards` → `user_items` →
`apple_wallets`). The regression test
`tests/test_lock_ordering.py::test_card_upgrade_locks_and_writes_items_before_the_wallet`
pins the order (`user, item, wallet, item, wallet`). It is a deterministic order
test; it does not prove that a deadlock is impossible on MariaDB.

## Accepted deviations (serialized by the user row)

- `services/daily_chest.py` pays apples and grants cards after the claim insert
  (wallet before cards). The daily chest is unchanged by request.
- `services/items.py` `grant` inserts the first inventory row for an item after
  the wallet debit. The row is user-scoped, so only same-user work contends for
  it.

## Savepoints: what they do and do not protect

- A **savepoint** (`begin_nested`) undoes only the statements issued inside it
  when a statement-level error or a Python exception unwinds it. It is what makes
  a refusal inside the purchase or chest-open write section leave no rows, even
  when the caller catches the refusal and commits.
- A **savepoint does not protect against a deadlock.** On InnoDB, a deadlock
  victim (error 1213) is rolled back as a whole transaction, not only the
  statement. A savepoint cannot restore work from before it.
- Savepoints are used on MariaDB/MySQL only (`savepoints_supported` in
  `services/apple_wallet.py`). SQLite runs the same code with no savepoint, so
  the caller's rollback is the only undo there.

## Cross-user risk (not fully addressed)

Trades lock two users' card rows (`services/trading.py`). Those locks must be
taken in ascending card id order to be deadlock-free. This is a hypothesis for
MariaDB and is not covered by the SQLite suite (`requires_row_locks` tests skip
there).

## Verification

- Deterministic order tests: `tests/test_lock_ordering.py`.
- Real races and row locks need MariaDB. Those tests are marked
  `requires_row_locks` and are reported as SKIPPED on SQLite. A SKIPPED test is
  not a pass, and SQLite results do not show MariaDB locking or concurrency
  safety.

## Apple Castle (C1)

Castle mutations lock **User → group_castles → donation/claim gate → wallet**.
Donation cap reads use current locking reads under the User mutex, globally
across groups; a wallet lock may be taken before inserting a new donation ID.
Claim inserts and wallet credit share the caller's transaction. No XP/items or
other users are acquired by castle operations.

Castle creation uses a dialect upsert on the unique castle group ID. Do not
explicitly lock the parent Group: invasion spawning locks Group then invasion,
while invasion joins can lock invasion then User. Adding User → Group here
would introduce a cross-feature cycle. The Group active flag is read without a
lock. Parent foreign-key checks on first creation may still contend with group
administration; deadlocks/errors must roll back the entire caller transaction.
A racing group deactivation may let an already-started transaction finish.
Menu-only transactions take the castle lock but never subsequently acquire a
User. Invasion reads the castle bonus without a castle row lock.
