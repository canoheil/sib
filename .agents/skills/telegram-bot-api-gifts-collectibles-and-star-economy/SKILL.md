---
name: telegram-bot-api-gifts-collectibles-and-star-economy
description: Covers Telegram gifts, premium gift subscriptions, unique gifts, collectible flows, gift upgrades, transfers, and star-linked economic objects. Use when implementing gifting, collectible ownership features, or gift inventory/business-account gift management.
---

# Telegram Bot API: Gifts, Collectibles, and the Star Economy

## Purpose

Use this skill when the bot sends, receives, upgrades, converts, or transfers gifts and related assets.

## Core API surface

- `getAvailableGifts`
- `sendGift`
- `giftPremiumSubscription`
- gift service-message objects
- user/chat/business gift retrieval methods
- gift conversion, upgrade, and transfer flows

## Developer guidance

- Model gifts as economic objects with lifecycle state, not just decorative media.
- Persist owned-gift identifiers when the API provides them.
- Separate:
  - regular gifts
  - unique gifts
  - gifts that can be upgraded
  - gifts converted to Stars
  - gifts transferred between owners

## Important concerns

- Some gifts are limited, premium-only, or upgradeable.
- Unique gifts include resale, transfer, and color/profile implications.
- Business accounts may have separate rights for viewing, converting, upgrading, or transferring gifts.
- Gift acceptance settings matter on both users and chats.

## Builder patterns

- Gift storefront bots
- Loyalty or reward systems
- Premium subscription gifting
- Business-account asset dashboards

## Common mistakes

- Treating gifts as immutable after receipt.
- Ignoring ownership-transfer or next-transfer-date restrictions.
- Assuming user, chat, and business gift APIs are interchangeable.
- Forgetting that some gifts can affect profile visuals and themed appearance.

## Read next

- `11-payments-stars-and-broadcasting`
- `13-stickers-custom-emoji-and-reactions`
- `16-stories-profile-and-engagement`
