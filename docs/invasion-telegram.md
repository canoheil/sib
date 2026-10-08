# Invasion Telegram delivery

The Invasion announcement is a plain-text Group message with inline **Join
Defense** and **Status** buttons. It is rendered from persisted event, wave,
participant, contribution, faction, and outcome data; there is no Invasion art
asset, so the existing text-screen fallback is used.

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
