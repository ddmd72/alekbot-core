# Rich content: one delivery routine for chat and background paths

**Date:** 2026-10-29 · **Status:** accepted

## Context

Smart can return `rich_content` of type `file` (text it wrote: `.txt`, `.md`, `.xlsx`, `.docx`, `.html`), `widget`,
`map_image` or `table`. A chat turn routed media types through `RichContentService` (convert, upload, or a GCS link);
that routing lived inside `ConversationHandler._deliver_rich_content`. The background paths of
`UserNotificationService` (`notify()`: reminders, the daily review; `notify_answer_copy()`: the chat copy of an
`ask_alek` / `tell_alek` answer from a call) called `ResponseChannel.send_rich_content` directly. That renders only a
table, so every other type posted its fallback line ("…in the attached file x.txt") and no file. Found on a call:
Alek said a file was attached, and none arrived.

## Decision

- `RichContentService.deliver(content, response_channel, thread_id, user_id)` is the one routing: media types
  (`MEDIA_CONTENT_TYPES`, `domain/messaging.py`) → `process()` on the channel's id, a returned URL as a message;
  everything else → `send_rich_content`. `ConversationHandler` calls it.
- An `.html` file is stored in the private media bucket and delivered as a `/f/<token>` capability link minted for
  `user_id` (`FileLinkService`), the route every stored file uses (owner decision O8, P11-G4-5), never as its storage
  key; without a user id or a link service, or when the private store fails (an ERROR), the page is uploaded as a file.
- `application_graph` builds one `RichContentService` per platform, passes it to that platform's adapter factory
  (a required argument), and hands the same instance to `UserNotificationService` through that platform's `PlatformKit` (`composition/platform_kit.py`).
- On Slack, a channel stored as a user id (`U…`) is turned into the DM id before an upload (a `📎` marker when no
  text went first), as `notify_file_bytes` already does.

## Rejected

- Teach each `ResponseChannel.send_rich_content` to upload files: conversion (CSV → xlsx, Markdown → docx, HTML → GCS)
  would move into two adapters or be duplicated.
- Keep the routing in `ConversationHandler` and copy it into the notification service: two copies drift.
