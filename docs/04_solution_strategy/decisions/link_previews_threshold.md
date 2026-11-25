# Decision: previews off for an answer with more than two distinct links

**Status:** Implemented
**Date:** 2026-11-25

## Context

Slack unfurls every link of a new `chat.postMessage` (probed: 3 links gave 3 attachments; with `unfurl_links=False,
unfurl_media=False`, none). A long answer split into chunks shows previews under each chunk; one real answer had 4 links
in its first chunk, so 4 preview cards sat in the middle of the text. Telegram shows one preview per message, so 3 chunks
gave 3 previews. `chat.update` never unfurls (probed: 0 attachments after 40 s, whatever the original post's flags), so
only new posts need a decision. Telegram shows previews on edits too.

## Decision

Count the distinct http(s) URLs over the whole answer as delivered (after `[N]` anchors become links), not per chunk.
More than two (`LINK_PREVIEW_MAX_LINKS`) turns previews off on every post of that answer, and on Telegram on the edit
too; two or fewer change nothing, and the SDK call keeps its default shape. The pure policy is
`src/domain/link_previews.py`; the Slack and Telegram response channels carry one decision to all chunks through a private
keyword on their internal post helpers, so the `ResponseChannel` port is unchanged.

## Alternatives rejected

- **Preview only the first link.** Slack cannot pick which link to unfurl: it is all or none per message.
- **Count per chunk.** An answer of 3 chunks with 2 links each would still give 3 preview groups.
- **Delete the status message and repost.** Unnecessary: `chat.update` never unfurls, so the edited status message is
  already preview-free.

## Trigger to revisit

The owner wants previews on single-chunk replies. Those arrive by `chat.update` and never unfurl on Slack; that would
need a repost (a new post) instead of the edit.
