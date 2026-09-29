---
name: telegram-bot-api-media-and-files
description: Covers sending, receiving, reusing, and downloading Telegram media and files, including albums and paid media. Use when implementing file workflows, media uploads, file_id reuse, or getFile-based retrieval.
---

# Telegram Bot API: Media and Files

## Purpose

Use this skill when your bot handles photos, videos, audio, voice notes, documents, or paid media.

## Core API surface

- `sendPhoto`, `sendVideo`, `sendAnimation`
- `sendAudio`, `sendVoice`, `sendVideoNote`
- `sendDocument`, `sendMediaGroup`, `sendPaidMedia`
- `getFile`
- `InputMedia*`
- `File`

## Sending model

Telegram supports three main input strategies:

1. Reuse a Telegram-hosted `file_id`
2. Give Telegram a public URL
3. Upload a new file with multipart form data

## Developer guidance

- Prefer `file_id` reuse when sending the same asset repeatedly.
- Persist original file metadata you care about; `getFile` is not a full metadata archive.
- Build media handling per type because Telegram differentiates photo, document, audio, voice, animation, and video note semantics.
- For albums, understand caption behavior and per-item media constraints.
- Validate file size limits before upload.

## Important constraints

- `file_id` is bot-specific and cannot be shared across bots.
- `getFile` on the hosted Bot API is capped for downloads; large-file needs may push you toward the local Bot API.
- Some send-by-URL flows only work for certain media types.
- Thumbnails often have special rules and cannot always be reused.

## Paid media

- Treat paid media as its own product flow, not just another attachment.
- Keep internal payloads for entitlement resolution.
- Expect preview-vs-purchased behavior differences.

## Common mistakes

- Sending a reused file under a different media type.
- Assuming every file can be sent by URL.
- Forgetting that voice notes, audio tracks, and documents behave differently in clients.
- Failing to store `file_id` after receiving user uploads.

## Read next

- `11-payments-stars-and-broadcasting`
- `12-gifts-collectibles-and-star-economy`
- `20-localization-testing-and-local-bot-api`
