---
name: telegram-bot-api-stories-profile-and-engagement
description: Covers stories, boosts, profile audio/photo surfaces, message effects, emoji status, and other engagement-facing capabilities. Use when implementing bots that manage richer Telegram-native social or profile features.
---

# Telegram Bot API: Stories, Profile, and Engagement

## Purpose

Use this skill when the bot interacts with Telegram-native profile or engagement surfaces beyond regular messages.

## Core API surface

- story objects and story-related methods exposed through the Bot API
- profile-photo methods for bots
- `getUserProfilePhotos`, `getUserProfileAudios`
- `setUserEmojiStatus`
- chat boosts
- message effects

## Developer guidance

- Treat stories as ephemeral content with their own permissions, lifecycle, and sharing model.
- Separate profile-surface features from core message flows.
- Respect capability differences between bot-owned features and business-account-managed features.
- Track engagement signals, but do not couple critical product logic to them.

## Common feature areas

- posting or editing stories on behalf of business accounts
- reacting to story-related context
- using emoji status or profile media for identity and presence
- reading boosts or engagement-related updates when community features depend on them

## Common mistakes

- Assuming story operations work like message operations.
- Ignoring expiration windows and archive behavior.
- Treating profile surface APIs as globally available for any user.
- Forgetting that some engagement APIs are only relevant in business or admin contexts.

## Read next

- `10-business-connections-and-suggested-posts`
- `12-gifts-collectibles-and-star-economy`
- `13-stickers-custom-emoji-and-reactions`
