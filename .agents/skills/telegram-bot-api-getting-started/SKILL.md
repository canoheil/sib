---
name: telegram-bot-api-getting-started
description: Introduces Telegram bot architecture, BotFather setup, token handling, bot metadata, and API capability planning. Use when starting a new Telegram bot project, defining the bot's baseline architecture, or mapping product requirements to Bot API features.
---

# Telegram Bot API: Getting Started

## Purpose

Use this skill when designing a new Telegram bot before writing handlers.

## Cover here

- Registering bots with `@BotFather`
- Bot identity, public metadata, commands, menu button, and profile assets
- Token handling and environment configuration
- Choosing major product surfaces: direct chat, groups, channels, inline mode, Mini App, Business, payments, games, gifts
- Mapping user journeys to Bot API primitives

## Builder workflow

1. Create the bot in `@BotFather` and store the token as a secret.
2. Decide which chat contexts the bot must support:
   - private chats
   - groups and supergroups
   - channels
   - inline mode
   - Business connections
   - Mini App entry points
3. Decide how the bot will receive updates:
   - long polling for simple deployments
   - webhooks for production event delivery
4. Define command surface early:
   - `/start`
   - `/help`
   - `/settings` when relevant
5. Set public-facing metadata:
   - name
   - description
   - about text
   - profile photo or video
   - command list
6. Design storage for:
   - users
   - chats
   - bot state
   - idempotency/update offsets
   - Telegram `file_id` reuse

## Architectural guidance

- Treat Telegram as the transport and interface layer; keep domain logic separate from Bot API adapters.
- Build handlers around update types, not around raw JSON blobs.
- Keep one clear policy for parse mode and formatting generation.
- Assume optional fields are often absent.
- Keep feature flags for optional surfaces like inline mode, payments, stories, and Business features.

## Important Bot API facts

- Bots cannot start a conversation with a user until the user interacts first.
- Bots do not receive messages from other bots.
- `file_id` values are reusable for the same bot, but are not portable across bots.
- Most bots should be fine on Telegram-hosted Bot API; use the local Bot API only when you need its extra capabilities.

## Common mistakes

- Hardcoding the token into source control.
- Mixing webhook and `getUpdates` flows at the same time.
- Designing command behavior without considering privacy mode in groups.
- Treating every chat as if it were a private 1:1 conversation.

## Read next

- `02-getting-updates`
- `05-commands-keyboards-and-input`
- `19-security-privacy-and-identity`
