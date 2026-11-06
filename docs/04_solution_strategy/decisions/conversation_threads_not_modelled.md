# Decision: conversation threads are not modelled in history

**Status:** Adopted
**Date:** 2026-11-06

## Context

Session history is one feed per channel in arrival order (`session_id = f"{user_id}:{channel_id}"`). A Slack
`thread_ts` or a Telegram `reply_to_message` reaches the handler only as `reply_to_message_id` in the per-turn
metadata (`src/adapters/slack/http_adapter.py`, `src/adapters/telegram/webhook_adapter.py`), read by one consumer,
the memory-review reply intercept (`src/handlers/conversation_handler.py`), and is never saved. A thread reply thus
looks to the orchestrator and to consolidation like the next sentence of whatever preceded it in time.

A concept draft (private design notes, 2026-11-04) proposed **conversation chains**: a bot-owned store of threads,
each shown whole and contiguous at its latest activity, consolidated only when quiet; and a smaller first step that
injects the current thread into the turn only (Slack via `conversations.replies`, Telegram from the update).

## Decision

Neither is built. Threads stay flat in history.

- The parent of a thread reply is normally still inside the orchestrator's window: replies come in short bursts
  minutes after the parent, so the feed already carries it; the injected block would duplicate what the model sees.
- The chain store needs a durable platform-id-indexed log of every message (the session cuts and deletes the
  oldest batch), platform ids recorded on every post across many posting paths, and consolidation of quiet chains
  that breaks fact order (`valid_from` is processing time). That is a second history, not a small store.
- The in-turn block for Telegram rests on undocumented API behaviour (whether `reply_to_message.text` carries the
  bot's edited status bubble or its original text) for a channel whose demand did not justify a probe.
- The owner's judgement: complexity added where there is no demand.

## Rejected alternatives

- A quote of the parent on every reply: five replies in a thread carry five identical quotes.
- `↪ #N` references in the flat feed: the model has to reassemble the thread; part of it may be consolidated.
- Collapsing a thread into one line of the feed: it drops out of the bot's context.
- The in-turn thread block itself: structurally sound (a transient part on the last user message at the Smart/Quick
  seam, like the running-jobs note; nothing persisted), and the first thing to build if a trigger below fires.
  It still needs: merging the bot's Slack chunks back into answers, filtering status placeholders and "Response
  ready" headers, the Slack history scopes verified, and the parent shown to the router too.

## Triggers to reopen

- The orchestrator visibly misreads a thread reply whose parent fell out of the window.
- Telegram comes into regular use: a private chat with a bot supports forum topics (`has_topics_enabled`,
  `is_topic_message`), and a topic maps a thread onto its own session key without any store; that is the cheaper
  shape there, not reply chains.
- A measured need: record thread replies as a span attribute (platform, is-reply, parent age, parent author), never
  as a log line, which Logfire does not export.
