---
name: telegram-bot-api-mini-apps-and-attachment-menu
description: Covers Telegram Mini Apps, Web Apps launched from bots, attachment-menu integration, and web_app data exchange. Use when building rich in-app interfaces that extend beyond text-and-button chat UX.
---

# Telegram Bot API: Mini Apps and Attachment Menu

## Purpose

Use this skill when chat UI is not enough and the bot needs a full web interface inside Telegram.

## Core API surface

- `WebAppInfo`
- `web_app` buttons
- menu-button Web App launch
- `web_app_data`
- `answerWebAppQuery`
- attachment-menu bot exposure

## Developer guidance

1. Treat the Mini App as a separate frontend application with a secure backend.
2. Validate Telegram init data server-side.
3. Keep Bot API message delivery and Mini App state synchronized explicitly.
4. Use the bot chat for durable notifications and approvals; use the Mini App for rich interaction.

## Launch surfaces

- private-chat keyboard button
- inline keyboard button
- menu button
- direct t.me or deep-link based entry
- attachment menu where available

## Product-fit guidance

Mini Apps are best for:

- checkout flows
- dashboards
- games with rich controls
- multi-step forms
- map or catalog experiences
- AI tools that need more than plain chat

## Common mistakes

- Trusting client-provided Web App data without signature validation.
- Mixing chat handler state with browser session state implicitly.
- Building web flows that fail when Telegram theme or viewport changes.
- Treating attachment-menu availability as universal.

## Read next

- `15-web-login-and-deep-linking`
- `11-payments-stars-and-broadcasting`
- `19-security-privacy-and-identity`
