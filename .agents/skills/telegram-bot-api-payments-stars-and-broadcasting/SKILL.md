---
name: telegram-bot-api-payments-stars-and-broadcasting
description: Covers invoices, shipping, pre-checkout, Telegram Stars, subscriptions, paid media, and paid broadcasts. Use when implementing monetization, paid bot features, digital goods, or high-volume message delivery.
---

# Telegram Bot API: Payments, Stars, and Broadcasting

## Purpose

Use this skill when the bot charges users, delivers paid content, or broadcasts at scale.

## Core API surface

- `sendInvoice`, `createInvoiceLink`
- shipping and pre-checkout query handling
- successful and refunded payment events
- Stars balance and transaction methods
- subscriptions
- paid media
- paid broadcasts via `allow_paid_broadcast`

## Commerce design guidance

1. Treat billing, entitlement, and delivery as separate steps.
2. Persist invoice payloads and payment charge identifiers.
3. Make fulfillment idempotent.
4. Support failure, retry, and refund flows explicitly.

## Telegram Stars specifics

- Digital goods and services should use `XTR` where required.
- Track Stars transactions and subscription state separately from one-time purchases.
- Paid media and Stars subscriptions are product surfaces, not just transport flags.

## Broadcasting guidance

- Respect normal rate limits by default.
- Use paid broadcast only when high throughput is intentional and budgeted.
- Queue fanout and track per-recipient delivery failures.
- Never assume unlimited free bulk delivery.

## Common mistakes

- Shipping goods before pre-checkout validation succeeds.
- Coupling successful send with successful payment fulfillment.
- Ignoring `retry_after` and broadcast throttling.
- Mixing Stars and fiat-style flows without explicit product rules.

## Read next

- `04-media-and-files`
- `12-gifts-collectibles-and-star-economy`
- `20-localization-testing-and-local-bot-api`
