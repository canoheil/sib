---
name: telegram-bot-api-web-login-and-deep-linking
description: Covers Telegram deep links, start parameters, startgroup flows, login URLs, and website authorization using Telegram identity data. Use when connecting bot interactions with external websites, apps, onboarding links, or account linking flows.
---

# Telegram Bot API: Web Login and Deep Linking

## Purpose

Use this skill when the bot must hand users off to a website or recover structured context from a Telegram entry link.

## Core API surface

- `/start` with parameters
- `start` and `startgroup` links
- login URL buttons
- Telegram Login Widget
- domain binding through `@BotFather`

## Deep-linking guidance

- Use start parameters to carry compact context such as:
  - referral code
  - onboarding path
  - account-link token reference
  - campaign identifier
- Keep payloads short and opaque.
- Resolve payloads server-side instead of embedding secrets directly.

## Website login guidance

- Always verify the signed Telegram auth payload on the server.
- Match the bot and linked domain configuration correctly.
- Distinguish:
  - Telegram website login
  - Mini App init-data validation
  - general bot deep-link flows

## Design patterns

- account linking from `/start <token>`
- referral and attribution links
- start-in-private, continue-on-web
- web login from inline or callback buttons

## Common mistakes

- Using deep links as if they were secure long-lived secrets.
- Skipping hash validation for Telegram login data.
- Mixing login URL semantics with Web App semantics.
- Designing payload formats that exceed Telegram's parameter constraints.

## Read next

- `01-getting-started`
- `06-inline-mode-and-callbacks`
- `14-mini-apps-and-attachment-menu`
