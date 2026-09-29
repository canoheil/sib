---
name: telegram-bot-api-admin-moderation-and-permissions
description: Covers administrator rights, moderation APIs, restrictions, bans, promotions, pins, and chat settings. Use when implementing moderation bots, admin tooling, or permission-aware workflows for groups, supergroups, and channels.
---

# Telegram Bot API: Admin, Moderation, and Permissions

## Purpose

Use this skill when the bot manages people, permissions, or safety in chats.

## Core API surface

- `restrictChatMember`
- `banChatMember`, `unbanChatMember`
- `promoteChatMember`
- `setChatAdministratorCustomTitle`
- `setChatMemberTag`
- `setChatPermissions`
- `pinChatMessage`, `unpin*`
- chat title/description/photo methods

## Rights model

- Always check the bot's current rights, not just your intended configuration.
- Channel and supergroup rights differ.
- Some rights imply others operationally, but your code should still check explicit capability needs.
- Newer surfaces include tags and direct-message management rights.

## Moderation patterns

- Soft restriction for temporary friction
- Hard ban for abuse removal
- Approval queue for join requests
- Admin-only tools for audits and content cleanup

## Safety guidance

- Log every moderation action with actor, target, chat, reason, and timestamps.
- Make destructive actions reversible when possible.
- Avoid silent permission failures; expose missing-right diagnostics.

## Common mistakes

- Assuming the bot still has admin rights after a role change.
- Using one permission matrix for both channels and supergroups.
- Forgetting to re-add the bot after privacy-mode changes that require it.
- Treating content deletion rules as identical across private chats, groups, and channels.

## Read next

- `07-chat-types-membership-and-invites`
- `09-topics-forums-and-threading`
- `19-security-privacy-and-identity`
