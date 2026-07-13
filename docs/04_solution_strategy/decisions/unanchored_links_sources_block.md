# Decision: Rescue unanchored links as a Sources block

**Status:** Adopted (2026-07-13).

## Context

`link_list` is a parallel delivery channel: the LLM returns `[{anchor, title, url}]` and embeds `[N]`
anchors in `full_response`; the platform channels (`_resolve_links_slack` / `_resolve_links_telegram`)
replace those anchors with native links. If the model returns a populated `link_list` but places **no
`[N]` anchor** in the text, resolution matches nothing and every link is silently dropped. Incident
2026-07-12: Quick returned 11 links, zero anchors — all lost, and the bot's text claimed it had sent
them.

## Decision

`ConversationHandler._append_unanchored_sources(text, link_list, context)`: when `link_list` is
non-empty and no anchor is present in the text (substring check catches bare `[N]` and `[title][N]`),
append a localized **Sources** heading (`UIMessage.SOURCES_HEADING`) followed by bare `[N]` lines. The
existing per-platform resolvers then render them. No-op when links are already anchored, empty, or
malformed (missing `anchor`).

**Placement — the handler, once, pre-chunk — NOT the adapters.** Two reasons:

- **Telegram resolves per chunk.** `send_chunked_message` splits the raw text first, then each chunk
  flows through `_resolve_links_telegram`. An adapter-level append would duplicate the Sources block
  across every chunk. The handler holds the full text before chunking, so it appends exactly once.
- **Bare `[N]` is platform-agnostic.** Both resolvers already handle `[N]`, so the handler emits
  provider-free markup and reuses the platform link rendering — no Slack/Telegram syntax leaks into the
  handler, no duplicated resolution logic.

## Rejected alternatives

- **Append in each response channel** (`_resolve_links_*`): duplicates the block across Telegram chunks
  (per-chunk resolution), and forks the "Sources" rendering into two adapters.
- **Emit `<url|title>` / `[title](url)` directly from the handler:** leaks platform-specific link syntax
  into the platform-agnostic handler. Bare `[N]` defers formatting to the adapters that own it.
- **Scope to zero-anchors only (not partial).** If the model anchored *some* links, that is treated as
  intentional citation — only the all-unanchored case is rescued (matches the incident).

## Triggers to revise

- A future need to also surface *partially* unanchored links (some cited, some not) → widen the guard
  from "no anchor present" to per-link tracking.

## See also

- Building block: `docs/05_building_blocks/rich_content_protocol/README.md` § 9.4.
- Implementation: `src/handlers/conversation_handler.py` (`_append_unanchored_sources`),
  `src/domain/ui_messages.py` (`UIMessage.SOURCES_HEADING`), `src/locales/{uk,en,fr,es}.py`.
