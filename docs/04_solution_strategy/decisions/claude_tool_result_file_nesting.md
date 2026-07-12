# Claude: nest tool-output files INSIDE their tool_result, never interleave

**Status:** Accepted — 2026-07-12
**Scope:** `ClaudeAdapter._convert_messages` (`src/adapters/claude_adapter.py`)

## Decision

When a user tool-result turn carries a binary `file_data` part produced by a tool
(e.g. `open_file` returning an image), nest that image/document block **inside the
owning `tool_result`'s `content`** (a list of `text` + `image` blocks) instead of
appending it as a sibling content block after the `tool_result`.

## Why

Anthropic requires the `tool_result` blocks answering a parallel `tool_use` turn to be
**contiguous** at the start of the user message. `build_tool_turn` appends a tool's
`file_data` as a *separate* part right after its `tool_response`, so on the wire a
multi-tool turn produced `[tool_result(A), image, tool_result(B)]`. The image between
the two results made Anthropic treat B's `tool_use` as unanswered →
`HTTP 400 invalid_request_error: "tool_use ids were found without tool_result blocks
immediately after"`. Smart then FAILED and fell back to Quick (degraded).

Trigger (all three): ≥2 `tool_use` in one Smart turn **and** a non-last tool returns
binary `file_data` **and** provider = Claude. The son hits it routinely (upload a
photo + a web question → `open_file` + `search_web` fan-out). Confirmed + reproduced
deterministically 2026-07-12 (incident 2026-07-10).

## Rejected alternatives

- **Reorder to `[tool_result(A), tool_result(B), image]`** — image loses its association
  to the tool that produced it; still a sibling in a tool-result turn.
- **Fix in `build_tool_turn` (domain)** — interleaving is legal for Gemini; this is
  Claude-specific wire serialization, so it belongs in the adapter (REQ-ARCH: adapters
  own provider-specific translation).

## Notes / triggers to revise

- Distinct orphan source from `transcript_integrity_one_provider.md` (that one =
  cross-provider mid-transcript mixing; this one = native same-provider multi-tool + file).
- `_diagnose_tool_pairing` checks id-correspondence only, **not** block order — it did
  not catch this. If block-order bugs recur, extend it to assert contiguity.
- Regression test: `test_parallel_tool_result_file_nests_into_tool_result_not_interleaved`.
