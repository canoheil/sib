---
name: telegram-bot-api-commands-keyboards-and-input
description: Covers commands, menus, reply keyboards, inline keyboard basics, force reply, and structured user/chat selection inputs. Use when designing conversational input flows or command-driven bot UX.
---

# Telegram Bot API: Commands, Keyboards, and Input

## Purpose

Use this skill when defining how users will tell the bot what to do.

## Core API surface

- Bot commands and command scopes
- `setMyCommands`, `getMyCommands`, `deleteMyCommands`
- `setChatMenuButton`, `getChatMenuButton`
- `ReplyKeyboardMarkup`
- `ReplyKeyboardRemove`
- `ForceReply`
- `KeyboardButton`
- `KeyboardButtonRequestUser`, `KeyboardButtonRequestChat`, contact/location/poll requests

## Developer guidance

1. Define concise command names and descriptions.
2. Always implement `/start` and `/help`.
3. Add `/settings` if the bot has mutable per-user preferences.
4. Keep command validation server-side; command scopes do not replace authorization.
5. Prefer explicit buttons when free text would increase user error.

## Input surface choices

- Commands for expert or repeat usage
- Reply keyboards for constrained answers
- Force reply for step-by-step collection in privacy mode
- Chat or user selection buttons when the bot needs identifiers without copy/paste
- Menu button for command discovery or Mini App entry

## Group-specific concerns

- Privacy mode changes which messages the bot receives.
- Commands in groups should remain explicit and auditable.
- Avoid fragile natural-language parsing in groups.

## Common mistakes

- Treating command scope as access control.
- Building flows that require messages the bot will never receive in privacy mode.
- Using reply keyboards when inline keyboards or editing would feel smoother.
- Forgetting localization for command descriptions and menu text.

## Read next

- `06-inline-mode-and-callbacks`
- `07-chat-types-membership-and-invites`
- `20-localization-testing-and-local-bot-api`
