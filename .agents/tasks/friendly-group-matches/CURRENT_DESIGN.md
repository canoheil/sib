# CURRENT_DESIGN — Friendly/group matches and panel authorization

## Repository and testing

- Repository root is `/home/user/sib`; application modules are importable as `sib.*` when `PYTHONPATH` includes the repository parent.
- Tests use `sib_TEST_DATABASE_URL` containing `sibcom_sib_test`; SQLite runs most coverage, while row-lock race tests are marked `requires_row_locks` and need MySQL/MariaDB.
- `tests/conftest.py` creates tables from model metadata and explicitly clears transient rows. A new reward/selection table must be cleared before its referenced matches/users.
- Current Alembic head is `022_card_upgrade_levels`; new DDL belongs in revision 023.
- Relevant skills are under `.agents/skills/` (not `.agent/skills/`): tasks, Telegram callbacks/keyboards, and Persian-writing. Telegram game UI uses colloquial written Persian.

## Existing party / matchmaking / match architecture

| Responsibility | Current owner | Notes |
|---|---|---|
| Party model | `database/models/party.py` | `Party` carries group/game/mode/capacity/status/creator. `PartyMember` has user and join time only. |
| Party service | `services/party/service.py` | `MODE_SLOTS` remains one side (1/2/4). Competitive parties preserve that capacity; Friendly parties use two sides (2/4/8), explicit team joins, same-group guards, and creator-only bot fill/cancel. Leaving is limited to OPEN/FULL and stale cancel cannot cancel queue/match states. |
| Party UI | `handlers/party/handlers.py`, `handlers/party/renderer.py`, `keyboards/party.py` | Flow is games → game → supported size/mode → create party. Active Games is based on `SessionService`. Back is navigation; explicit cancel is separate. Party cards currently show one undifferentiated team. |
| Competitive queue | `matchmaking/matcher.py` | Enqueues full parties and matches same game/mode/capacity across *different* Telegram groups. Creates `Match` in `WAITING_FOR_ACCEPTANCE`, with team 1/2 and a message per group. |
| Match acceptance | `matchmaking/acceptance.py`, `handlers/matchmaking/handlers.py` | One acceptance per side starts competitive matches. Matches/parties are locked, but acceptance currently does not check the callback chat against the actor's stored group. |
| Gameplay | `games/*/engine.py`, `handlers/games/common.py` | Engines receive team id and group id in player state. The common action path locks the match and calls the engine, but must explicitly check `MatchPlayer` membership and the actor's group before stale-board recovery or action validation. |
| Result / cleanup | `services/match_result.py`, `services/match_lifecycle.py`, `services/timeout/service.py` | `MatchResultService` updates player/group statistics, awards Seedlings for normal PvP wins, finalizes the match, and cancels its parties. Timeout/forfeit results are marked in `GameResult.details`. |
| Bot fallback | `services/bot_match.py`, `services/bot.py`, `sib/bots/service.py`, `sib/bots/runner.py` | Reusable persisted bot `User`/`MatchPlayer` identities and strategies exist. `MatchPlayer.bot_profile` is available. Bot fallback is currently a separate opponent team, not a party-slot filler. |
| Match UI sync | `services/game_sync.py`, `ser Central render/edit helpers maintain message IDs across text/photo replacement; `with_refresh_button` adds safe board refresh. |

## Friendly design locked for implementation

- Match type is `COMPETITIVE` or `FRIENDLY`; generic UX order is game → supported mode/size → match type → party. Do not add a top-level “Normal Game / Group Game” menu. Existing direct `create_party:<game>:<mode>` callbacks remain Competitive-compatible.
- Apple Battle remains hidden from the generic game grid and keeps its dedicated loadout/narrative UX. Its only supported size is solo 1v1, and it also gets a Competitive/Friendly choice; a Friendly Apple Battle party snapshots each member's selected card.
- Competitive party capacity remains one side (1/2/4). Friendly total capacity is two sides (2/4/8), with an explicit `PartyMember.team` (1 or 2), per-team capacity, and remaining slots shown in the party card.
- Friendly starts directly from one full same-group party. It reuses `Match`, `MatchPlayer`, the existing engine, bot runner, and result lifecycle. Representation: `group1_id=party.group_id`, `group2_id=NULL`, one party, and explicit `MatchPlayer.team`; group identity is never used as a side for Friendly.
- One shared board message is rendered in the party's group. Each player action is authorized by match membership and originating group. Team-based engines use team perspective, not `group_id`; Battleship displays both teams' radar histories on the shared board. Simultaneous solo engines expose a shared choice keyboard while validating/locking each actor's own choice server-side.
- Friendly rematches create one idempotent invitation from a finished match. All human participants must accept; bot participants auto-accept. Rejection/timeout cancels the invitation party and releases every participant. The resulting match can itself be rematched.
- Friendly result handling never updates Competitive `PlayerStats`, group leaderboard stats, Rank/ELO/MMR, or Seedlings. Apple Battle's Competitive +5-winner-Apple award and its Competitive leaderboard/profile totals are also bypassed for Friendly matches.
- Starting Apple rewards: 10 Apples per eligible human participant, at most five claims per user per UTC day. Both sides (including a draw) qualify after natural completion only when every participant is human. Forfeit/turn-timeout/simultaneous-timeout and any bot-filled match earn no Friendly rewards. A user earns at most once against any given human opponent per UTC day; this applies across rematches and team modes. Amount and cap live in `ConfigService` defaults. Claims and Apple ledger credits are in the result transaction, protected by per-user locking, unique `(match_id,user_id)` claims, and an idempotency key.
- Bot fill is a reusable party service operation, limited to the Friendly party creator while the party is open and in its owning group. It fills each team's empty slots, keeps the mode's team capacity, stores bot profiles, and uses Persian-theme names that explicitly identify themselves as bots (e.g. «باتِ سیبیِ بهار 🤖»). Bot-filled matches remain playable but are reward-ineligible.

## Apple Battle constraints

- Apple Battle uses `AppleBattleQueueEntry` for selected loadouts, `AppleBattleService.build_match_state` to precompute the outcome, and `after_start` to schedule narration.
- The shared match state must select Friendly cards from each `PartyMember` snapshot (not a consumed/global queue row); rematches reuse those snapshots after validating card ownership.
- Narration currently loops `(group1_id, group1_message_id)` and `(group2_id, group2_message_id)` and keys rendered text by group ID. The Friendly same-group case must deduplicate to one edit target.
- Apple Battle supports solo only. Friendly completion goes through normal Friendly reward rules and must not grant its Competitive +5 Apples or Seedling. Friendly battles must not appear in Competitive Apple Battle leaderboard/profile counts.

## Authorization / navigation audit map

| Surface | Current state / change required |
|---|---|
| Personal panel registry | `middleware/panel_guard.py`, `services/panel.py`, `database/models/panel.py`. Registry is keyed by `(chat_id,message_id)`; an unclaimed copied owner-scoped callback can currently claim a new message as its pressers' panel. Personal mutations/navigation need a claim from the actual panel, and copied/stale private actions on unclaimed messages must be rejected. Make first-owner claims race-safe. |
| Panel entry/rendering | `handlers/start.py`, `handlers/shop.py`, `handlers/garden.py`, `handlers/cards.py`, `handlers/trading.py`, `handlers/apple_battle.py`. Command-created screens and callback-created screens must claim their message, including replacement-message claim moves. |
| Trade offer | `handlers/trading.py`. `trade_join` is intentionally public in a group, but currently replaces that shared message with the joiner's card list. Move only the card-selection step to a private, owner-claimed message; leave the public offer intact, then publish a neutral shared trade card after selection. Keep shared confirm/cancel available to authorized trade participants. |
| Parties | `handlers/party/handlers.py`, `services/party/service.py`. Join/view/leave/cancel/bot-fill/start and queue callbacks must validate actor permission, party status, and that callback chat equals `Party.group_id`. Do not let Back cancel parties or active matches. |
| Acceptance | `matchmaking/acceptance.py`, `handlers/matchmaking/handlers.py`. Require the actor's persisted team/group context for Competitive acceptance; Friendly rematch acceptance is per human participant in the owning group. |
| Match actions and refresh | `handlers/games/common.py`. Require a live `MatchPlayer` membership and matching Telegram group *before* stale-state rendering, refresh, or game validation; preserve public group multiplayer buttons for legitimate participants. |
| Personal commands/panels | start/profile, cards, garden, shop, trade, and Apple Battle use sender/actor identity and/or `PanelOwner`; ensure every personal surface has an explicit claim and no copied unclaimed mutation is accepted. |
| Navigation | Existing main-menu, Back, Refresh, Cancel, and Active Games helpers remain; Back is navigation only. Start/cancel/refresh are safe under stale buttons and double taps. |

## Data changes planned (revision 023)

- `parties.match_type` with Competitive default.
- `party_members.team`, `party_members.bot_profile`, `party_members.selected_card_id` (Apple Battle loadout snapshot).
- `matches.match_type`, `matches.winner_team`, nullable unique `matches.rematch_of_match_id`.
- `friendly_match_rewards` claim ledger, unique per match/user.
- `trade_selections` short-lived private selection token carrying recipient/proposer/card/group and expiry/status.

STEP-1 reconnaissance baseline: 41 tests passed across panel ownership/locking, navigation regressions, party cancellation, and stale/duplicate callbacks (SQLite; row-lock races are not covered). STEP-2 implemented migration 023 and schema/config/fixture coverage. STEP-3 implemented Friendly party/team/bot/direct-start services, group-checked handlers and UI, and a Competitive queue guard; its focused party/navigation/matchmaking suite passed 53 tests with 2 SQLite row-lock skips. `KNOWLEDGE.md` records current test setup and STEP-4 onward gotchas.