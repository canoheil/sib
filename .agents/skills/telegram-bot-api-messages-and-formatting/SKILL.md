---
name: telegram-bot-api-messages-and-formatting
description: Covers text messaging, replies, edits, forwarding, copying, pinning, deleting, and Telegram formatting/entities. Use when implementing bot responses, message editing flows, quote/reply behavior, or safe rich-text rendering.
---

# Telegram Bot API: Messages and Formatting

## Purpose

Use this skill when building text-first bot UX.

## Core API surface

- `sendMessage`
- `sendMessageDraft`
- `forwardMessage`, `forwardMessages`
- `copyMessage`, `copyMessages`
- `editMessageText`, `editMessageReplyMarkup`
- `deleteMessage`, `deleteMessages`
- `pinChatMessage`, `unpinChatMessage`, `unpinAllChatMessages`
- `ReplyParameters`
- `MessageEntity`
- `LinkPreviewOptions`

## Developer guidance

1. Pick one rendering strategy:
   - raw entities for maximum control
   - `HTML`
   - `MarkdownV2`
2. Escape content rigorously if you generate markup dynamically.
3. Treat editing as a first-class pattern for interactive bots:
   - update one message instead of sending clutter
   - especially for inline keyboards and settings menus
4. Use `ReplyParameters` when reply context matters.
5. Use `sendMessageDraft` for streaming or partial AI-style responses in supported private-chat/topic flows.

## Formatting rules that matter

- `MarkdownV2` is powerful but escape-heavy.
- `HTML` is simpler for many server-side renderers.
- Custom emoji and date-time entities have extra capability rules.
- Link previews are configurable and should be intentional.
- Quotes must match source text exactly when using quoted replies.

## Forward vs copy

- Use forward when provenance should stay visible.
- Use copy when you need a clean repost without a backlink.
- Protected content and some service/payment media cannot be forwarded or copied.

## Editing constraints

- Inline messages require inline-message identifiers.
- Some business messages are only editable within limited windows.
- Some paid or suggested-post content has extra restrictions.

## Common mistakes

- Unescaped user data in `MarkdownV2`.
- Assuming message text always exists; many messages are media-only.
- Editing messages that Telegram does not allow you to edit.
- Deleting messages without checking age/admin constraints.

## Read next

- `04-media-and-files`
- `06-inline-mode-and-callbacks`
- `19-security-privacy-and-identity`
