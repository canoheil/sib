# Apple Duel D1 — self-driving scene presentation

## Reconnaissance

- `AppleDuelPresentation.tick` is at `services/apple_duel_presentation.py:166`. It makes one `claim_scene` call and sends at most one indexed scene per invocation.
- `AppleDuelPresentation.recover_and_publish` is at `services/apple_duel_presentation.py:256`; it is invoked by maintenance at `services/maintenance/service.py:445`.
- `MaintenanceService.INTERVAL_SECONDS` is 30 seconds (`services/maintenance/service.py:39`). Before D1, each recovery pass advanced only one scene, so subsequent scenes could wait for the next 30-second sweep.
- Apple Battle’s background narration pattern is `AppleBattleService._narration_loop` at `services/apple_battle.py:1015` (background scheduling at lines 907–917; beat sleep at line 1033). Apple Battle is not changed by D1.
- Acceptance sets `AppleDuel.next_scene_at = now` in `services/apple_duel.py:325`. `claim_scene` sets it to `now + settings.APPLE_DUEL_SCENE_SECONDS` at lines 330–360; the default is 4 seconds (`core/config.py:81`). The new player will make the next scene immediately eligible after successful delivery, then use its own 1.5-second pacing delay.
- The presentation defines six scene entries with indices 0–5 in `scene_text` (`services/apple_duel_presentation.py:48–70`); `_photo` uses the same index (`:140–163`).

## Before and after flow

**Before:** acceptance awaited one `tick`; maintenance also awaited one `tick` per recovery scan. Because the maintenance interval is 30 seconds, scene progression depended on repeated maintenance passes (and the 4-second durable scene deadline also applied).

**After:** acceptance and recovery schedule a retained background task. `play` serializes per duel, calls the unchanged single-scene `tick`, checks persisted state, and on successful progress makes the next scene eligible before sleeping for 1.5 seconds. It stops on terminal/no-progress conditions; DB cursor/deadline state remains the restart recovery mechanism.

## Implementation and verification

Implementation, task-retention details, test results, measured timings, and limitations will be filled in after the code and tests are complete.
