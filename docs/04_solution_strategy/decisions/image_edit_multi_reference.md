# Decision: multi-reference images for `edit_image`

**Status:** Implemented
**Date:** 2026-08-23

## Context

`ImageGenerationAgent` shipped 2026-08-21 with `edit_image` scoped to a single reference image
(RFC §6 decision #6), even though `grok-imagine-image-2.0` supports up to 3 reference images per
edit. That scoping was deliberate — ship the simpler path first, prove it live, extend once real
traffic confirmed the basics worked (415/duplicate-delivery/timeout fixes all landed against the
single-image path in the days after launch). With those fixed, the gap to xAI's actual capability
was closed.

Confirmed against xAI's REST reference (`docs.x.ai/developers/rest-api-reference/inference/images`):
a single reference uses `body["image"] = {"url": ..., "type": "image_url"}` (unchanged, live since
launch); 2 or 3 references use a **different, plural field** — `body["images"] = [...]` — mutually
exclusive with `"image"`, not the same key holding an array. xAI's own convention: when multiple
images are present, the prompt text addresses them positionally as `<IMAGE_0>`, `<IMAGE_1>`,
`<IMAGE_2>`.

## Decision

1. **`ReferenceImage(data, mime_type)` dataclass replaces the bare `bytes` + standalone `mime_type`
   kwarg on `ImageGenerationPort.edit()`.** Up to 3 references can legitimately be different formats
   (one JPEG upload + one PNG upload), so mime type has to travel with each image, not be declared
   once for the whole call.

2. **`GrokImageAdapter.edit()` branches on count.** Exactly 1 reference is byte-identical to the
   pre-existing wire behavior (`body["image"]`). 2 or 3 use `body["images"]` instead, in input order,
   with `"image"` absent. A defensive `ValueError` on `len() not in [1,3]` backstops the agent's own
   validation — this codebase's convention for a port implementation at a system boundary (don't
   trust the caller blindly, even when it's known to validate first).

3. **`context_schemas["image_refs"]` is an array (1-3 filenames), replacing the singular
   `image_ref` string field outright — no back-compat shim.** Smart is the only caller, driven
   entirely by the `PROTOCOL_SMART_AGENT_SELECTION` Firestore token; both the code and the prompt
   change in the same deploy, so there is no external contract to preserve. This is the first
   array-typed `context_schemas` field in the codebase (every other agent's context field is a
   plain description string). `AgentDescriptor.context_schemas` widened from
   `Dict[str, Dict[str, str]]` to `Dict[str, Dict[str, Any]]`;
   `BaseAgent._build_delegate_tool_declaration` now accepts either a plain string (existing
   shorthand, unchanged for all other agents) or an already-JSON-schema-shaped dict, chosen by
   `isinstance(field_spec, str)`. Verified live before adopting: constructed a real
   `google.genai.types.Schema` with the exact planned shape and confirmed `"type": "array"` /
   nested `"items": {"type": "string"}` coerce correctly through the Gemini tool-declaration path.

4. **`ImageGenerationAgent.execute()` validates `image_refs` (empty, or >3) before the
   prompt-crafting LLM call runs, not after.** The pre-existing single-image code paid for a full
   LLM call on a request that was already invalid (missing `image_ref` was only checked inside
   `_execute_edit`, after `_craft_prompt` had already run). Moving validation earlier was free while
   touching this code for the rename, so it's fixed here rather than filed separately.

5. **Reference resolution is concurrent and fail-fast.**
   `asyncio.gather(*[resolve_bytes(ref, user_id) for ref in image_refs], return_exceptions=True)` —
   `return_exceptions=True` is required, not optional: the bare default lets sibling tasks run
   unawaited/uncancelled after the first exception. On any failure the whole request fails, naming
   the failed filename(s), before `.edit()` is ever called — matches the port's own "no
   partial-success shape" contract. `gather()` preserves input order in its results regardless of
   completion order, so the `<IMAGE_n>` ↔ array-index correspondence holds as long as nothing
   reindexes after a partial failure — fail-fast guarantees that.

6. **The count-instruction, not the placeholder semantics, lives in code.** At `len(image_refs) > 1`,
   `execute()` appends a bracketed fact to the crafting-LLM's input query
   (`"[N reference images attached, in order: <IMAGE_0>, <IMAGE_1>, ...]"`) — the same idiom
   `AgentCoordinator.handle_delegation()` already uses to prepend a UTC timestamp fact. At count == 1
   this is a complete no-op (`craft_query == query`), so the live single-image path is unchanged.
   What to *do* with that fact (use the tokens verbatim, never invent labels like "the first photo")
   is specialist know-how and belongs in the `COGNITIVE_PROCESS_IMAGE_GEN` Firestore prompt token,
   per the same split RFC decision #2 already established for the single-reference prompting
   techniques.

## Alternatives rejected

- **Numbered flat fields** (`image_ref`, `image_ref_2`, `image_ref_3`) instead of an array. Rejected
  by the owner in favor of `image_refs: [...]` — an array is the more natural shape for "1 to 3 of
  the same thing" and avoids three near-duplicate context_schema entries. The array form's only cost
  was being the first non-string `context_schemas` field in the codebase; verified as low-risk (see
  decision 3) before committing to it.
- **Keeping `mime_type` as a single call-level kwarg**, inferring per-reference type only where
  needed. Rejected: with up to 3 uploads of possibly different formats, a single mime_type is simply
  wrong for the non-first images — there's no reduced-risk version of getting this wrong.

## Consequences

- The single-image live path (proven in production since 2026-08-21) is unchanged in wire behavior —
  same `body["image"]` shape, same validation outcome, same crafting-LLM input text.
- Rollout order matters: code (port/adapter/agent/delegation plumbing) must land before the
  Firestore `PROTOCOL_SMART_AGENT_SELECTION` prompt update. The LLM-visible tool schema is
  auto-generated from `agent_manifest.py` and is correct the moment code deploys; the reverse order
  would have Smart calling `image_refs` before the deployed agent could read it, breaking every
  `edit_image` call between the two deploys.
- No `minItems`/`maxItems` JSON Schema hint on `image_refs` — cardinality is enforced entirely in
  code (decision 4), not by trusting the LLM's schema-conformance for a bound most providers
  express awkwardly through `types.Schema`'s constructor.

## Verification

`tests/unit/agents/test_image_generation_agent.py` (multi-reference happy path incl. order
preservation and the `<IMAGE_n>` count-signal, >3 refs failure, empty-list failure, partial-resolve
failure, single-ref no-count-signal regression guard) and
`tests/unit/adapters/test_grok_image_adapter.py` (`body["images"]` array shape for 2-3 refs with
`"image"` absent, defensive `ValueError` outside 1-3) plus
`tests/unit/test_base_agent.py::TestBuildDelegateToolDeclaration` (dict-shaped `context_schema`
field passes through verbatim, unwrapped). Full `make test-unit` green (4908 passed) and
`ruff check src/` clean after the change.
