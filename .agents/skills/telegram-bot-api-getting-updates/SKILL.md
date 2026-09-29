---
name: telegram-bot-api-getting-updates
description: Explains how Telegram bots receive updates through long polling or webhooks, how to route each update type safely, and how to recover from duplicates or downtime. Use when implementing event ingestion, webhook endpoints, or polling loops.
---

# Telegram Bot API: Getting Updates

## Purpose

Use this skill when building the event-ingestion layer for a Telegram bot.

## Core API surface

- `getUpdates`
- `setWebhook`
- `deleteWebhook`
- `getWebhookInfo`
- `allowed_updates`
- the `Update` object and all of its branches

## Update ingestion rules

1. Choose exactly one delivery mode:
   - long polling with `getUpdates`
   - outgoing webhooks with `setWebhook`
2. Persist progress:
   - store the last handled `update_id`
   - call `getUpdates` with `offset = last_update_id + 1`
3. Make handlers idempotent:
   - duplicate deliveries happen
   - retries and redeploys happen
4. Route by update family first:
   - `message`
   - `edited_message`
   - `channel_post`
   - `callback_query`
   - `inline_query`
   - `poll_answer`
   - `my_chat_member`
   - `chat_member`
   - `chat_join_request`
   - `business_*`
   - `message_reaction*`
   - `purchased_paid_media`

## Long polling guidance

- Use long polling in development and small deployments.
- Use positive `timeout` values, not tight short-poll loops.
- Recompute offset after every successful batch.
- Use `allowed_updates` to reduce load and simplify handler logic.

## Webhook guidance

- Use HTTPS and a public endpoint.
- Set a `secret_token` and validate the `X-Telegram-Bot-Api-Secret-Token` header.
- Inspect `getWebhookInfo` during incidents.
- Consider `drop_pending_updates` when intentionally resetting a deployment.

## Handler design

- Normalize raw updates into internal event types.
- Separate transport errors from business-logic errors.
- Acknowledge or return quickly, then do slow work asynchronously when possible.
- Keep callback-query responses fast to avoid stuck client spinners.

## Recovery patterns

- After downtime, process pending updates in order.
- When switching from webhook to polling, delete the webhook first.
- When switching to a local Bot API server, follow Telegram's `logOut`/`close` guidance.

## Common mistakes

- Running polling while a webhook is still configured.
- Forgetting to persist offsets.
- Treating missing optional branches as errors.
- Ignoring new update families such as Business, paid media, or reactions.

## Read next

- `03-messages-and-formatting`
- `06-inline-mode-and-callbacks`
- `20-localization-testing-and-local-bot-api`
