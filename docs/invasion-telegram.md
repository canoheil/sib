# Invasion Telegram delivery

The Invasion battle screen is a Group message with the Invasion artwork
(`assets/invasion/moon_descendant_invasion.jpg`), falling back to plain text if
the photo cannot be sent, and inline **Join Defense**, **Brace**, and **Status**
buttons. It is rendered from persisted event, wave, participant, contribution,
faction, and outcome data.

For four-wave battles the message lifecycle is: the battle screen at activation
(pinned quietly), one silent notice per wave start, and one result notice when
the battle ends. Each delivered stage is recorded, so no notice is repeated. See
[`invasion-four-wave-battles.md`](invasion-four-wave-battles.md) for the full
timeline, the balance formula, and the reward rules. The guarantees below apply
to all Invasion messages.

Result notice variants for four-wave battles (one notice in every case):

- **Resolved**: outcome, defense total against enemy power, participant count,
  and the reward line. The reward names 🌕 سیب طلایی (Golden Apple inventory,
  not wallet Apples), is paid to defenders only, and reads "no eligible defender"
  when nobody defended.
- **Interrupted** (downtime exceeded the recovery budget): says the battle closed
  without a result because of a long bot outage. It records no victory or
  defeat and states that no reward is given.
- **Cancelled without a start** (legacy voided occurrence): says the battle was
  cancelled without a defense and no reward is given.

`MaintenanceService.run_once()` runs the DB-only Invasion lifecycle step first,
then invokes `InvasionTelegramService` after the transaction has committed and
after pending game bot turns have been scheduled. Sending/editing a message and
persisting `Invasion.announcement_message_id` are separate operations. Message
id writes use an expected-id compare-and-set scoped to the persisted Group.
`safe_edit_screen()` replacement ids are saved and become the target of later
updates.

## Delivery guarantees and limits

- New announcements remain discoverable while `announcement_message_id` is
  null and are retried by the existing maintenance sweep, with a 60-second
  in-process retry throttle and a limit of ten new sends per pass. A process
  restart resets only that throttle; the event and pending-send condition stay
  in the database.
- Lifecycle updates edit the existing screen only when the persisted event
  transitions, not on every five-second maintenance poll. The Status button
  also refreshes the screen from current DB state.
- Telegram and the database do not share a transaction. If Telegram accepts a
  send but the response is lost, or the database fails before the returned
  message id is persisted, a later retry can create a duplicate Group message.
  Exactly-once delivery is not promised; no outbox is introduced.
- If an edit and its fresh-message fallback both fail, the Invasion continues
  unchanged. The stored id is retained; a later lifecycle transition retries
  the edit, and users can request a status refresh. A failed progress edit is
  not retried every maintenance pass, to avoid a high-frequency edit loop.
- If the bot cannot post to a Group, the persisted Invasion still starts,
  advances, and resolves. A still-unannounced event can be retried later, but
  delivery ultimately requires Telegram access to that Group.
