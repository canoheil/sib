---
name: telegram-bot-api-business-connections-and-suggested-posts
description: Covers Telegram Business connections, business messages, business rights, channel direct-message chats, and suggested posts. Use when building bots that operate on behalf of businesses or manage inbound customer messaging and suggested-post workflows.
---

# Telegram Bot API: Business Connections and Suggested Posts

## Purpose

Use this skill when the bot acts for a Telegram Business account instead of only as its own bot user.

## Core API surface

- `BusinessConnection`
- `getBusinessConnection`
- `business_message`, `edited_business_message`, `deleted_business_messages`
- business read/delete/profile methods
- `business_connection_id` on outbound methods
- suggested-post methods and objects

## Business architecture guidance

- Model each business connection as a tenant-like unit.
- Store connection rights and refresh them when connection updates arrive.
- Keep business-account chat identity separate from the bot's own private chat with the same human.
- Gate actions by rights such as:
  - reply
  - read messages
  - delete messages
  - edit name/bio/profile photo/username
  - manage gifts, stars, and stories

## Suggested posts

- Treat suggested posts as a distinct workflow with approval state.
- Preserve pricing and proposed send dates.
- Respect the 24-hour rule and refund-sensitive post lifecycle.
- Separate approval, rejection, payment success, and refund handling.

## Direct-message channel topics

- Suggested posts often live alongside channel direct-message topics.
- The bot may need channel-side admin rights such as post-message or direct-message management rights.

## Common mistakes

- Sending on behalf of a business account without checking `can_reply`.
- Mixing bot-owned chat IDs with business-managed chat IDs.
- Ignoring business-specific deletion/edit windows.
- Treating suggested posts like normal editable posts.

## Read next

- `09-topics-forums-and-threading`
- `11-payments-stars-and-broadcasting`
- `12-gifts-collectibles-and-star-economy`
