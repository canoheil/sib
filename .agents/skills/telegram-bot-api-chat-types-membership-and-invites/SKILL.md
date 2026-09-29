---
name: telegram-bot-api-chat-types-membership-and-invites
description: Explains private chats, groups, supergroups, channels, invite links, join requests, and chat/member lookup semantics. Use when building bots that must behave correctly across Telegram chat types or manage chat entry flows.
---

# Telegram Bot API: Chat Types, Membership, and Invites

## Purpose

Use this skill when a bot spans multiple Telegram chat contexts.

## Chat-type model

- Private chat
- Group
- Supergroup
- Channel
- Channel direct-messages chat
- Forum-enabled supergroup

## Core API surface

- `getChat`, `getChatMember`, `getChatMemberCount`, `getChatAdministrators`
- `Chat`, `ChatFullInfo`, `ChatMember*`
- invite-link methods
- join-request methods
- `leaveChat`

## Developer guidance

1. Normalize chat-type behavior in your app layer.
2. Use numeric chat IDs as stable identity, not titles or usernames.
3. Expect capabilities to differ by chat type.
4. Persist bot membership state because permissions can change at runtime.

## Invite flows

- Primary invite links
- Additional invite links
- Join-request links
- Subscription invite links for channels
- Deep links such as `startgroup`

## Membership events

- Track `my_chat_member` for the bot itself.
- Track `chat_member` only when you truly need member lifecycle events.
- Use join-request updates when onboarding or approval logic matters.

## Common mistakes

- Assuming usernames always exist.
- Ignoring group-to-supergroup migration IDs.
- Using public titles as identifiers.
- Treating channel chats and channel direct messages as the same thing.

## Read next

- `08-admin-moderation-and-permissions`
- `09-topics-forums-and-threading`
- `10-business-connections-and-suggested-posts`
