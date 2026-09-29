---
name: telegram-bot-api-stickers-custom-emoji-and-reactions
description: Covers stickers, sticker sets, custom emoji, reaction handling, and reaction configuration. Use when implementing expressive messaging features, sticker workflows, custom emoji rendering, or reaction-aware engagement flows.
---

# Telegram Bot API: Stickers, Custom Emoji, and Reactions

## Purpose

Use this skill when the bot works with expressive content rather than plain text.

## Core API surface

- `sendSticker`
- sticker-set retrieval and management methods
- custom emoji sticker lookup
- `setMessageReaction`
- reaction update families
- button and entity support for custom emoji

## Sticker guidance

- Distinguish regular stickers, mask stickers, and custom emoji.
- Sticker set ownership matters for edit operations.
- Preserve sticker metadata such as emoji associations and keywords.
- Consider whether the bot needs creation flows or only send/reuse flows.

## Custom emoji guidance

- Custom emoji appear in:
  - entities
  - buttons
  - sticker sets
  - emoji status and some profile surfaces
- Capability varies by context and premium requirements.

## Reactions guidance

- Reaction handling can be either:
  - command-like feedback
  - analytics/engagement signal
  - moderation or ranking signal
- Bots must explicitly opt into some reaction-related updates and may need admin rights.
- Paid reactions are not available to bots in normal reaction-sending flows.

## Common mistakes

- Assuming any sticker can be edited by any bot.
- Forgetting that custom emoji identifiers are not ordinary Unicode emoji.
- Ignoring chat-specific reaction availability rules.
- Treating reaction updates as immediate, strictly synchronous counters.

## Read next

- `03-messages-and-formatting`
- `16-stories-profile-and-engagement`
- `21-schema-and-type-modeling`
