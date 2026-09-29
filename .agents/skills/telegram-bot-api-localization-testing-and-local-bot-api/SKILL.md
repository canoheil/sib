---
name: telegram-bot-api-localization-testing-and-local-bot-api
description: Covers localization, command scopes by language, testing strategies, webhook troubleshooting, and the local Bot API server. Use when validating bot behavior across languages, environments, or production deployment setups.
---

# Telegram Bot API: Localization, Testing, and Local Bot API

## Purpose

Use this skill when hardening a bot for real deployment.

## Language support guidance

- Use `language_code` from updates when available.
- Provide fallback behavior when it is absent.
- Localize:
  - text responses
  - command descriptions
  - bot name/description/about text where needed
  - inline results and Mini App UI

## Testing guidance

- Keep a separate test bot token for safe experimentation.
- Replay real update payloads as fixtures.
- Add contract tests for:
  - formatting
  - callback data parsing
  - topic-aware routing
  - permission-dependent features
  - payment and business flows

## Webhook troubleshooting

- Check `getWebhookInfo`.
- Watch `pending_update_count`.
- Inspect `last_error_message`.
- Verify HTTPS, supported ports, certificate validity, and secret token checks.

## Local Bot API server

- Use it when you need:
  - larger file handling
  - local-path upload support
  - local webhook/network flexibility
- Follow Telegram's guidance for `logOut` and `close` when moving between cloud and local servers.

## Operations guidance

- Handle `retry_after`.
- Instrument update latency and handler failures.
- Use queueing/backoff for broadcasts and slow tasks.
- Watch for BotFather status alerts on degraded responsiveness.

## Common mistakes

- Testing production flows with the live bot token.
- Assuming every user has `language_code`.
- Switching to the local Bot API without following migration steps.
- Ignoring webhook diagnostic fields during incidents.

## Read next

- `02-getting-updates`
- `11-payments-stars-and-broadcasting`
- `21-schema-and-type-modeling`
