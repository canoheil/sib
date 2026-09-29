---
name: telegram-bot-api-schema-and-type-modeling
description: Explains how to model Telegram Bot API types from the official schema and object docs, including unions, optional fields, evolving versions, and code generation tradeoffs. Use when building typed SDK layers, validators, or update/domain adapters.
---

# Telegram Bot API: Schema and Type Modeling

## Purpose

Use this skill when building or maintaining the typed integration layer between your code and Telegram.

## Primary sources

- Bot API object and method docs
- `https://core.telegram.org/schema/json`

## What to model carefully

- `Update` as a tagged union
- `Message` as a multi-shape object with many optional content branches
- `User`, `Chat`, `ChatFullInfo`
- reply/callback/business/payment/story/gift-specific objects
- entity arrays and nested media objects

## Developer guidance

1. Treat Telegram objects as versioned, sparse payloads.
2. Prefer tagged unions or discriminated unions where your language supports them.
3. Keep raw payload capture available for debugging unknown fields.
4. Separate generated transport models from hand-written domain models.

## Codegen strategy

- Use schema-driven generation for breadth and consistency.
- Use handwritten adapters for:
  - business rules
  - convenience helpers
  - backward-compatible fallbacks
  - normalization across update families

## Compatibility guidance

- Expect new optional fields to appear.
- Never assume absent fields are invalid.
- Watch recent Bot API changes such as:
  - `sendMessageDraft`
  - private-chat topics
  - direct-message topics
  - tags
  - stories/gifts/business additions

## Common mistakes

- Flattening `Message` into one rigid shape.
- Treating Telegram integer identifiers as small signed ints in weakly typed stacks.
- Generating types once and never revisiting them after API updates.
- Hiding raw responses so debugging schema drift becomes impossible.

## Read next

- `02-getting-updates`
- `03-messages-and-formatting`
- `04-media-and-files`
