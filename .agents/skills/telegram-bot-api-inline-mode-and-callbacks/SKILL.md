---
name: telegram-bot-api-inline-mode-and-callbacks
description: Covers inline mode, inline query results, callback queries, and message-edit driven interaction loops. Use when implementing searchable inline bots, callback-button UX, or cross-chat result insertion.
---

# Telegram Bot API: Inline Mode and Callbacks

## Purpose

Use this skill when the bot must work outside its own chat or when buttons drive stateful interactions.

## Core API surface

- `inline_query`
- `chosen_inline_result`
- `answerInlineQuery`
- `callback_query`
- `answerCallbackQuery`
- inline result types
- inline keyboards and `callback_data`

## Inline mode guidance

- Enable inline mode in `@BotFather`.
- Build stateless or lightly stateful search flows.
- Use compact result identifiers and stable pagination offsets.
- Treat every URL in inline results as public.
- Personalize only when justified and set caching flags accordingly.

## Callback-query guidance

- Always call `answerCallbackQuery`, even for silent acknowledgements.
- Keep `callback_data` compact and versioned.
- Design callbacks so they can be replayed safely.
- Prefer editing the original message over sending a new one.

## Design patterns

- Search-select-send for inline content pickers
- Inline search plus deep link back to private chat for auth/setup
- Callback-driven settings menu with message edits
- Pagination using encoded cursor state

## Common mistakes

- Forgetting to acknowledge callback queries.
- Storing too much mutable state in `callback_data`.
- Returning overly large or slow inline result sets.
- Assuming inline messages can always upload new files during edits.

## Read next

- `03-messages-and-formatting`
- `14-mini-apps-and-attachment-menu`
- `15-web-login-and-deep-linking`
