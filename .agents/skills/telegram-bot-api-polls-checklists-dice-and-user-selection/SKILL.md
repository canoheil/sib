---
name: telegram-bot-api-polls-checklists-dice-and-user-selection
description: Covers polls, quizzes, checklists, dice, contact/location requests, and user/chat selection request buttons. Use when building structured interaction flows that go beyond plain text input.
---

# Telegram Bot API: Polls, Checklists, Dice, and User Selection

## Purpose

Use this skill when the bot needs structured input or lightweight interaction widgets.

## Core API surface

- `sendPoll`
- poll updates and poll answers
- checklist objects and checklist send/edit flows
- `sendDice`
- keyboard buttons that request:
  - contact
  - location
  - poll creation
  - users
  - chats

## Developer guidance

- Prefer native widgets when they reduce parsing ambiguity.
- Store poll IDs and map them to domain objects.
- For quizzes, persist correct-answer state outside the message itself.
- Treat shared chat/user identifiers as user-selected references, not as proof of future access.

## Checklists

- Use checklists when a task list is part of the actual product, not just bot-internal state.
- Model task IDs carefully because replies can target specific checklist tasks.
- Consider whether other users may add or complete tasks.

## Common mistakes

- Assuming every poll answer update arrives for every poll in Telegram.
- Forgetting that some widgets are private-chat only.
- Treating shared chat/user IDs as if the bot automatically has full access to them.
- Using dice as business logic randomness without considering UX expectations.

## Read next

- `05-commands-keyboards-and-input`
- `03-messages-and-formatting`
- `21-schema-and-type-modeling`
