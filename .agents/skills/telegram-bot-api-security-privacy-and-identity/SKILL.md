---
name: telegram-bot-api-security-privacy-and-identity
description: Covers bot token handling, webhook trust, privacy mode, Telegram Login verification, Mini App init-data validation, and identity-sensitive flows like Passport. Use when implementing security boundaries or handling personal data.
---

# Telegram Bot API: Security, Privacy, and Identity

## Purpose

Use this skill whenever the bot handles secrets, personal data, or trust decisions.

## Core concerns

- bot token secrecy
- webhook authenticity
- privacy mode behavior
- Telegram login signature validation
- Mini App init-data validation
- Telegram Passport handling
- data minimization and retention

## Developer guidance

1. Treat the bot token as root access.
2. Validate webhook requests with a `secret_token` header and HTTPS.
3. Understand privacy mode before designing group behavior.
4. Verify all Telegram-provided auth payloads server-side.
5. Limit storage of PII and document why you keep it.

## Privacy mode implications

- Privacy mode changes which group messages reach the bot.
- It improves privacy and reduces processing load.
- Step-by-step flows in groups should rely on explicit commands, replies, or force-reply patterns.

## Identity-sensitive flows

- Web login and Mini App auth are different validation problems.
- Passport data is sensitive and should be isolated operationally.
- Mentions by numeric user ID have context-specific limits.

## Common mistakes

- Logging tokens or raw auth payloads.
- Trusting Web App or login data without signature checks.
- Building group features that require messages privacy mode blocks.
- Treating Telegram IDs, usernames, and business chat identifiers as interchangeable.

## Read next

- `02-getting-updates`
- `14-mini-apps-and-attachment-menu`
- `15-web-login-and-deep-linking`
