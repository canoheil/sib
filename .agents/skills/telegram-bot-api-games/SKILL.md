---
name: telegram-bot-api-games
description: Covers Telegram HTML5 game integration, game callbacks, score handling, and leaderboard flows. Use when building bots that launch games, track scores, or use Telegram's game platform features.
---

# Telegram Bot API: Games

## Purpose

Use this skill when the bot launches playable games through Telegram.

## Core API surface

- `sendGame`
- game callback buttons
- `answerCallbackQuery` for game launches
- `setGameScore`
- `getGameHighScores`

## Developer guidance

- Create and manage the game identity through `@BotFather`.
- Treat the Telegram bot as launcher, scorekeeper, and transport layer.
- Use callback queries to issue per-user launch URLs.
- Decide whether score updates should edit the chat-visible scoreboard automatically.

## Product patterns

- single-player scoreboards
- group competition
- inline game launch
- reward loops tied to score milestones

## Common mistakes

- Forgetting that the first button in the first row must launch the game when using a game keyboard.
- Treating callback-query handling for games as optional.
- Updating scores without protecting against replay or cheating.
- Mixing game transport logic with actual game runtime logic.

## Read next

- `06-inline-mode-and-callbacks`
- `03-messages-and-formatting`
- `20-localization-testing-and-local-bot-api`
