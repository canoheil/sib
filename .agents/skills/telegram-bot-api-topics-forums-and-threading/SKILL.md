---
name: telegram-bot-api-topics-forums-and-threading
description: Covers forum topics in supergroups, private-chat topics, direct-message topics, threaded replies, and topic-aware message routing. Use when building bots that must organize conversation into threads or operate in forum-like chat structures.
---

# Telegram Bot API: Topics, Forums, and Threading

## Purpose

Use this skill when the bot must send or route messages by topic.

## Core API surface

- `message_thread_id`
- `createForumTopic`, `editForumTopic`, `closeForumTopic`, `reopenForumTopic`, `deleteForumTopic`
- `unpinAllForumTopicMessages`
- general-topic management methods
- direct-messages topics
- private-chat topic support

## Thread model

Telegram now has multiple thread-like contexts:

- forum topics in supergroups
- topics in private chats when enabled for the bot
- direct-message topics in channel direct-messages chats

## Developer guidance

1. Store topic context separately from chat context.
2. Include topic identifiers in routing keys, permissions checks, and outbound message APIs.
3. Keep handler logic topic-aware when reply chains matter.
4. Distinguish:
   - replying to a message
   - sending into a topic
   - acting on the topic object itself

## Design patterns

- Per-topic workflow state
- Per-topic moderation queues
- AI assistants that isolate separate conversations by topic
- Team support channels where each case is mapped to a topic

## Common mistakes

- Treating `chat_id` alone as enough to address the destination.
- Confusing forum topics with direct-message topics.
- Ignoring whether the bot has topic-management rights.
- Forgetting that edits, deletions, and chat actions may also need topic context.

## Read next

- `02-getting-updates`
- `07-chat-types-membership-and-invites`
- `10-business-connections-and-suggested-posts`
