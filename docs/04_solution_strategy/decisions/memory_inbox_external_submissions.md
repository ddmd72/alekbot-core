# Memory inbox: outside assistants submit, the owner confirms, the final pass writes

**Date:** 2026-11-25. **Status:** Implemented on `feat/memory-inbox`. RFC `docs/10_rfcs/MCP_MEMORY_SUBMISSIONS_RFC.md`; map `docs/05_building_blocks/memory/README.md` §5.7.

## Decision

A connected outside assistant sends what it learned about the owner to a write-only MCP tool on its own resource
(`/memory-inbox-mcp`, scope `memory_submit`). Items wait in a store of their own; a daily triage turns the worthwhile ones
into candidates; the owner checks each in the Cabinet ("True" attests authenticity only); one hour after the owner's last "True" a
final consolidator pass is the only writer of facts. External text never enters a chat session, so it is never read as the
owner's own words before the owner attests it. This is the second owner-confirmed write path: `memory_facts_owner_confirmed.md`
rejected "a pending store with an hourly flush" because the session pair served the bot's own proposals; here the pair is
unavailable, so the inbox is that path, by owner decision.

## Alternatives rejected

- **The `$memory save` session pair.** It reaches memory only at the next overflow (hours to days), makes the consolidator
  read checked text as conversation, and puts external text in a user-role message.
- **No triage.** The noise is expected and would all land on the owner, with the card repeating what memory knows.
- **The outside model reads memory first.** Triage dedups better with full search; an extra call adds latency and pulls
  facts into that model's context. The loop guard is one sentence in both tool descriptions.
- **Triage replaces a waiting candidate with an updated one.** Two dated statements are both true at their time; merging
  hides the change the final pass must see. Two cards, reconciled by the final pass.
- **A second scope on `/mcp`.** One scope per resource; claude.ai registers what the metadata advertises; voice tokens verify there.

## Revise when

- Triage discards nearly everything or the owner confirms almost nothing.
- The final pass discards confirmed candidates through the echo trap in practice (`Domain_Scope`).
- The janitor's overdue-`purge_after` alert should watch the inbox (RFC §15.7; today nothing does).
