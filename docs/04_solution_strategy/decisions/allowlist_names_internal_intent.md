# Decision: an explicit allowlist may name an internal intent

**Status:** Adopted (2026-09-23).

## Decision

`internal=True` means "not offered by default" — excluded from `get_available_intents()` and from `get_available_intents_for(d)` when `d.allowed_intents is None`. A descriptor with an explicit `allowed_intents` frozenset may still name an internal intent; `get_available_intents_for` then returns it. This is how Lelik will name `ask_alek` (VOICE_COMPANION_RFC §4.7, Task 13) without making `ask_alek` visible to Smart or Quick.

## Rejected alternatives

- **A separate `visible_to` field on the target descriptor**: a second visibility mechanism alongside `internal` + `allowed_intents`, duplicating what an allowlist already expresses on the caller side.
- **Make `ask_alek` public**: Smart and Quick would see a delegation path to themselves; the dispatch chain guard would refuse it, but only after a wasted turn.
- **A voice-only dispatch path bypassing the registry** (§4.7 alternative): duplicates the existing delegation machinery for one caller instead of reusing the filter that already exists.

## Verified

`test_no_existing_allowlist_changes_its_tool_list` (`tests/unit/infrastructure/test_registry_internal_allowlist.py`) enumerates every `ALL_DESCRIPTORS` entry with a non-`None` allowlist and asserts it gains no internal intent. On 2026-09-23: NOTES (`COMPUTE_DATETIME`, `COMPUTE`), DOMAIN_RESEARCHER (`OPEN_FILE`), TUTOR (`SEARCH_WEB`), and `lelik_agent` (`SEARCH_MEMORY`, `SEARCH_WEB`) — none names an internal intent yet.

## Revisit if

An internal intent appears in an allowlist by accident. The test above lists every allowlist and fails if visibility changes unintentionally.
