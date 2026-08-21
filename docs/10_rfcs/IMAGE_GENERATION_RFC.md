# RFC: ImageGenerationAgent — Image Generation & Editing via grok-imagine-image-2.0

**Status:** IMPLEMENTED
**Date:** 2026-08-21
**Owner:** AI Engineering
**Milestone:** Specialist Agents — new capability
**Related:** `docs/how_to/NEW_AGENT_PLAYBOOK.md`, `HtmlPageGeneratorAgent` (closest structural precedent),
`DeepResearchPort`/`job_registry` (provider-abstraction precedent)

---

## 1. Problem Statement

The bot has no way to create or edit images. Users can only receive images that already exist
(Unsplash stock photos resolved inside `HtmlPageGeneratorAgent`, or files they themselves upload).
There is no path from "draw me X" / "change this photo" to a delivered image.

**Desired outcome:** the user keeps talking to the orchestrator (Smart) exactly as today — no
new UI, no new command. When a request calls for an image, Smart delegates to a new
`ImageGenerationAgent`, which produces the image via `grok-imagine-image-2.0` (xAI's "Aurora"
model) and delivers it back into the same conversation. The user's subjective experience is a
single continuous conversation; the actual pixel generation happens on a separate, purpose-built
model.

---

## 2. Market Research Summary

Researched 2026-08-21 (model shipped 2026-08-07/08, ~2 weeks old, generally available — not preview).

| Aspect | Finding |
|---|---|
| Arena ranking | **#2 in the world** on both text-to-image (1320) and image editing (1439), behind OpenAI's GPT-Image-2 (1380/1463). Ahead of Imagen 4, FLUX.2, well ahead of SD 3.5. |
| Comparative strength | Midjourney remains the aesthetic/style-range benchmark. Grok Imagine's edge is **tight instruction-following on photoreal edits** and **typography/dense-layout precision** (small in-image text stays legible). |
| Architecture | **Aurora** — xAI's own autoregressive Mixture-of-Experts model, generates image tokens sequentially from interleaved text+image data (like an LLM generates text tokens), not a diffusion model. Native language understanding is part of the generation core, not a bolt-on. |
| Pricing | $0.04/image (1K, low quality) – $0.08/image (2K, medium). Edits billed for **both** input and output image (~2x a generation). Cheap — comparable to GPT-Image tier, far below Midjourney's subscription model. |
| API surface | Two REST endpoints, OpenAI-compatible SDK: `POST /v1/images/generations` (text→image) and `POST /v1/images/edits` (up to 3 reference images, URL or base64 input). `client.images.generate(...)` / `client.images.edit(...)`. |
| Generation params | `model`, `prompt`, `n`, `aspect_ratio` (13 ratios incl. social presets), `resolution` (1k/2k), `quality` (low/medium), `response_format` (url/b64_json). |
| Templates (game assets, icons, product shots, mascots, headshots) | Confirmed via direct doc fetch: **consumer-app-only** (grok.com/imagine). Not exposed by the API — confirmed no `template` parameter in the generation endpoint docs. We cannot delegate use-case-specific handling to xAI; it has to live in our own prompt. |

Sources: [xAI Imagine Image 2.0 announcement](https://x.ai/news/grok-imagine-image-2) ·
[xAI Images API docs](https://docs.x.ai/developers/model-capabilities/images/generation) ·
[xAI Imagine editing docs](https://docs.x.ai/developers/model-capabilities/imagine) ·
[Grok's Arena ranking claim](https://x.com/grok/status/2085931545659949481) ·
[Morphed 2026 comparison](https://morphed.app/blog/grok-imagine-vs-midjourney-vs-flux-vs-dalle)

### 2.1 Prompting technique — why this is NOT a zero-LLM agent

Initial assumption was that Aurora's strong native language understanding makes external prompt
engineering unnecessary (pass the orchestrator's `query` straight through, zero-LLM agent —
`FileManagementAgent` pattern). Deeper research falsified this: there are **three distinct,
non-obvious technique clusters**, not one generic heuristic, and none of them are things a
general-purpose orchestrator's natural conversational phrasing would reliably produce:

1. **Photoreal scenes** — lead with a photographic anchor ("Photorealistic photograph of…",
   camera/lens details), real-world light/texture cues, emotion words over generic adjectives,
   scene-first framing, 50–200 words.
2. **Text-heavy / dense layouts** (infographics, UI mockups, diagrams) — a completely different
   mechanic: *"spell the exact words in quotes and say where they sit, and they render as
   written."* Skipping this produces garbled in-image text — not an edge case, the default failure
   mode for this task class.
3. **Asset sets** (icons, sprites, props) — **style-lock** (fix style via repeated phrase or
   reference image so a whole set shares palette/outline/rendering) + explicit transparent-background
   / isolated-subject framing.

Because Templates (§2, last row) aren't API-accessible, there's no shortcut — if we want good
results for these task classes, the technique has to live somewhere in our own prompt.

**Where it lives is an architecture decision, not a research one:** the technique clusters go in
the **specialist's own isolated Firestore prompt** (loaded only when `generate_image`/`edit_image`
fires), not in Smart's always-on `PROTOCOL_SMART_AGENT_SELECTION` token (loaded on every single
request, for every intent). Smart's protocol section stays thin — same shape as every other
section (`when`/`how`/`examples`/`anti_patterns`), instructing it to interactively clarify intent
and write a natural-language creative brief, not to know Aurora's prompting mechanics. See §3.5.

---

## 3. Architecture

### 3.1 Core Design Principles

1. **Commissioning model, unchanged.** Smart still owns eliciting *what* the user wants —
   interactively, through normal conversation, exactly as it already does for `deep_research`
   (validated by the owner's own usage experience). Smart's `query` is a natural-language creative
   brief, not a finished image prompt.
2. **The specialist owns *how* to phrase it for Aurora specifically.** One LLM call inside
   `ImageGenerationAgent` translates the brief into a technically-correct prompt using the three
   technique clusters from §2.1. This mirrors `HtmlPageGeneratorAgent`'s shape (single LLM call,
   own `PromptBuilder` token) — not `FileManagementAgent`'s (zero-LLM, pure port call).
3. **Provider is behind a port from day one**, not because we need multiple providers today, but
   because the abstraction costs nothing extra to build correctly now and everything to retrofit
   later. See §3.7.
4. **Video generation is explicitly out of scope**, deferred to its own future RFC + its own
   agent + its own port. See §3.9.

### 3.2 Intent Design — Two Intents, One Agent

Mirrors `EmailSearchAgent` (3 intents) / `ComputeAgent` (4 intents) — typed variants sharing one
execution mechanism (Phase 0 of the playbook explicitly allows 2–4 intents per agent when they
share infrastructure).

| Intent | Orchestrator signal | xAI endpoint |
|---|---|---|
| `generate_image` | User wants a new image from a description | `/v1/images/generations` |
| `edit_image` | User wants an existing image (their upload) changed | `/v1/images/edits` |

### 3.3 Full Flow — `generate_image`

```
User (interactive, multi-turn): describes what they want; Smart asks 1-2 clarifying
questions if the request is vague (style? mood? purpose? exact text needed?)
  |
  v Smart -> delegate_to_specialist(intent="generate_image", query="<natural-language brief>")
      |
      +-- ImageGenerationAgent.execute()
      |     |
      |     +-- Build system prompt via PromptBuilder (agent_type="image_generation")
      |     |     — contains the 3 technique clusters + instruction to pick the matching one
      |     |
      |     +-- LLM call #1 (text): brief -> Aurora-ready prompt (+ aspect_ratio if implied)
      |     |
      |     +-- ImageGenerationPort.generate(prompt, aspect_ratio) -> GeneratedImage
      |     |     (xAI images.generate, response_format="b64_json")
      |     |
      |     +-- AgentResponse.success(delivery_items=[DeliveryItem(type="document", ...)])
      |
      +-- Co-emitted with `deliver_response` in the same Smart turn (same pattern as
          `create_html_page`) — Smart answers immediately ("generating that now"), the
          agent runs async, the image lands in the channel when ready.
```

### 3.4 Full Flow — `edit_image` (and a mechanism trap avoided)

`CREATE_HTML_PAGE`'s `context_schemas` declares `file_ref` for an *optional source document*, and
`AgentCoordinator._resolve_file_refs()` (`agent_coordinator.py:513-542`) auto-injects it as
**text** into `payload["file_content"]` before the specialist even runs — it unconditionally calls
`FileConversionService`'s text-resolution path and logs `"%d chars"`. That pipeline is text-only;
for an `image/*` mime type it falls into the `markitdown` branch of `convert_file_to_text`
(`file_conversion.py:191-252`), which is not built for raw image bytes and would not give us
usable pixels.

The codebase already has the *correct* mechanism for binary content — `FileManagementAgent._fetch_binary`
(`file_management_agent.py`) branches on `is_native_binary(mime_type)` and calls
`FileConversionService.resolve_bytes(ref, user_id)` directly, bypassing the text pipeline entirely.

**Decision:** `edit_image`'s context schema uses a **different key name, `image_ref`** (not
`file_ref`) specifically so `_resolve_file_refs`'s literal `"file_ref"` check does not fire.
`ImageGenerationAgent` takes a constructor dependency on `FileConversionService` (same as
`FileManagementAgent`) and resolves `image_ref` itself via `resolve_bytes()`, base64-encoding the
result for the xAI edits payload. This avoids touching shared coordinator infrastructure used by
every other `file_ref`-declaring agent, for a need only this one intent has.

```
User: [uploads photo] "remove the person in the background"
  |
  v Smart -> delegate_to_specialist(
        intent="edit_image",
        query="remove the person standing in the background, keep everything else unchanged",
        context={"image_ref": "<filename from [File: name (size)]>"})
      |
      +-- ImageGenerationAgent.execute()
      |     |
      |     +-- Resolve image_ref -> raw bytes via FileConversionService.resolve_bytes()
      |     |     (NOT via the coordinator's auto file_content injection)
      |     |
      |     +-- LLM call #1 (text): edit instruction -> precise edit prompt
      |     |     (surgical, NOT elaborated — §2.1 cluster 3 note: don't "improve" an edit brief)
      |     |
      |     +-- ImageGenerationPort.edit(prompt, reference_images=[bytes]) -> GeneratedImage
      |     |
      |     +-- AgentResponse.success(delivery_items=[...])
```

### 3.5 Prompt-Crafting Step — Protocol Token vs Specialist Prompt (boundary)

`PROTOCOL_SMART_AGENT_SELECTION` gets a new section, same shape as existing ones
(`firestore_utils/downloads/PROTOCOL_SMART_AGENT_SELECTION.groovy` — `when`/`how`/`examples`/
`anti_patterns`, ~5-8 lines, matching `memory_search_agent`'s size):

```groovy
image_generation_agent {
    intent: "generate_image" | "edit_image"
    when: "User wants a new image created from a description, or an existing uploaded
           image changed."
    how: [
        "If the request is vague (no style/mood/subject detail), ask 1-2 clarifying
         questions in chat before delegating.",
        "Compose query as a natural-language creative brief — what to depict, for what
         purpose, any exact text that must appear, style preference if known. Do NOT
         write a technical image-model prompt yourself — the specialist handles that.",
        "For edit_image: pass the precise instruction (what to change) plus
         context={\"image_ref\": \"<filename>\"} from the file label.",
    ]
    anti_patterns: [
        "❌ DON'T try to write Aurora-specific prompt syntax yourself — pass the brief,
         the specialist translates it.",
    ]
}
```

The specialist's own token (`COGNITIVE_PROCESS_IMAGE_GEN`, new, Phase 2 of the playbook) carries
the three technique clusters from §2.1 and instructs the LLM to (a) identify which task class the
brief falls into, (b) apply that cluster's mechanics, (c) for `edit_image` specifically, stay
surgical — do not creatively embellish an edit instruction.

### 3.6 New Port — `ImageGenerationPort`

Mirrors `ImageSearchPort`'s shape (`ports/image_search_port.py`): frozen dataclass result + ABC,
`@abstractmethod async def`, one port per file.

```python
# src/ports/image_generation_port.py
@dataclass(frozen=True)
class GeneratedImage:
    data: bytes
    mime_type: str  # confirm exact value returned by xAI during implementation — see §10

class ImageGenerationPort(ABC):
    @abstractmethod
    async def generate(self, prompt: str, *, aspect_ratio: str = "auto", n: int = 1) -> list[GeneratedImage]:
        """Text -> image(s). Returns [] on failure (mirrors ImageSearchPort's contract)."""

    @abstractmethod
    async def edit(self, prompt: str, reference_images: list[bytes], *, mime_type: str = "image/png") -> GeneratedImage:
        """Edit existing image(s) per instruction. Raises on failure (single result, no partial-success shape)."""
```

`GrokImageAdapter` (`src/adapters/grok_image_adapter.py`, new file — separate from `GrokAdapter`
because it speaks a structurally different API surface: `/v1/images/*`, not `/v1/responses`;
one-class-per-file convention) implements this as an **independent** `AsyncOpenAI` client
construction (base_url + api_key from config) — same pattern as `grok_adapter.py`, but not a
shared import from it. The two adapters implement different ports (`ImageGenerationPort` vs
`LLMPort`) and should have no coupling to each other; duplicating ~3 lines of client setup is
cheaper than introducing cross-adapter dependency for it.

### 3.7 Provider Abstraction — Two Independent Axes, Both Config-Switchable

There are genuinely **two separate provider decisions** for this agent, and the existing
architecture already has the exact mechanism for both — extending it, not inventing something new:

**Axis 1 — the text LLM doing brief→prompt translation (§3.5).** Standard `LLMPort` /
`AgentProviderStrategy` / `ProviderRegistry[LLMPort]` path, identical to every other agent. New
entry in `AgentProviderStrategy.STRATEGIES`:

```python
"image_generation": {
    "default_provider": "grok",
    "allowed_providers": ["grok"],
    "required_capabilities": [],
    "fallback": None,
},
```

**Axis 2 — the image-generation port itself (which model actually renders pixels).**
`ProviderRegistry[T]` is already documented as a generic mechanism serving "two provider
families" (`provider_registry.py:6-13`: `LLMPort`, `DeepResearchPort`) — `UserAgentFactory`
already holds a second, separately-typed registry (`self.job_registry: Optional[ProviderRegistry]`,
`user_agent_factory.py:178`) alongside the main one, and `AgentContextBuilder.resolve_async_context()`
(`agent_context_builder.py:358-378`) is the precedent for resolving a non-`LLMPort` port through
the same 3-level name resolution (`resolve_provider_name()`). We add a **third** family:
`ProviderRegistry[ImageGenerationPort]` (`image_registry`, new constructor param on
`UserAgentFactory`, mirroring `job_registry`), with `GrokImageAdapter` registered under `"grok"`.

Both axes are resolved from the **same** `agent_type="image_generation"` strategy entry (one
resolved provider name, looked up in two different registries) — deliberately, so a future
per-user override (`config.agent_providers["image_generation"] = "openai"`) switches both the
prompt-crafting LLM and the pixel-rendering model together, rather than requiring two independent
config knobs for what is conceptually one choice ("use xAI for this" vs "use OpenAI for this").

**What this buys, concretely:** adding a second image-gen provider later (e.g. `gpt-image-2` when/if
it becomes worth comparing) means writing one new adapter class + one line in `allowed_providers` —
a one-time deploy. After that, **which registered provider serves a given user is a Firestore
config value** (`UserBotConfig.agent_providers`), exactly like Smart's existing per-user
provider/tier overrides — no deploy to switch. This directly satisfies the "config, not deploys"
ask, using a mechanism the codebase already trusts (2 of its 3 production usages predate this RFC).

### 3.8 Delivery — Zero New Plumbing

`DeliveryItem(type="document", data={content_b64, filename, content_type, label, file_upload: True,
storage_class})` already does everything needed, confirmed by reading `document_delivery_service.py`
and `conversation_handler.py:217-246`: uploads to private GCS (`/f/<token>` capability link) **and**
calls `response_channel.send_file()` for native inline upload when `file_upload: True` — exactly
the mechanism `PdfGenerator` already uses ("PDF + Slack upload", per `src/agents/CLAUDE.md`). Both
`SlackResponseChannel.send_file` and `TelegramResponseChannel.send_file` exist. No new
`DeliveryItem` type, no new delivery service.

`content_b64` comes directly from xAI's `response_format="b64_json"` — no second HTTP round-trip
to fetch a temporary URL.

### 3.9 Forward Compatibility — Video (deferred)

`grok-imagine-video-1.5` (text-to-video / image-to-video / reference-to-video) is explicitly
**not** built now, and not bundled into `ImageGenerationAgent` as more intents when it is. Reasons:

- Different prompting framework entirely (Subject+Motion / Background+Motion / Camera+Motion —
  not an extension of the image technique clusters).
- Different latency profile — video generation is expected to be materially slower than image
  generation; likely needs an ACK+poll shape (`DeepResearchPort`-style) rather than
  `HtmlPageGeneratorAgent`'s fire-and-forget-but-fast pattern.
- Different `DeliveryItem` shape/size, different port contract.

This is "mixed responsibilities" per the CLAUDE.md file-size convention, not a size problem. Cost
of deferring is zero: video becomes its own future RFC, its own agent, its own port — using the
exact pattern this RFC establishes (§3.6, §3.7) as the template. Nothing built here needs to
change to accommodate it later.

### 3.10 Hexagonal Compliance Check

Verified against CLAUDE.md's Import Rules / Layer Semantics and `ports/CLAUDE.md`, against actual
current imports (not assumed) — checked `file_management_agent.py` and `agent_context_builder.py`
directly, not from memory.

| File (layer) | Depends on | Compliant because |
|---|---|---|
| `ports/image_generation_port.py` | stdlib only (`abc`, `dataclasses`) | Matches `image_search_port.py` exactly — zero domain/adapter imports |
| `adapters/grok_image_adapter.py` | `ports/`, `config/`, `openai` SDK | `adapters/ → domain/, ports/, config/`. Independent client construction, not imported from `grok_adapter.py` (fixed ambiguous wording in §3.6) — no cross-adapter coupling |
| `services/agent_context_builder.py` (new method) | `ports.image_generation_port.ImageGenerationPort` (top-level import) | `services/ → domain/, ports/`. Confirmed `DeepResearchPort` is imported the same way, not `TYPE_CHECKING`-guarded (`agent_context_builder.py:7`) — ports are always-allowed for services |
| `agents/image_generation_agent.py` | `ports.image_generation_port.ImageGenerationPort` (direct constructor param) + `services.file_conversion_service.FileConversionService` (constructor param) | Confirmed `HtmlPageGeneratorAgent` takes `ImageSearchPort` directly in its constructor; confirmed `FileManagementAgent` takes `FileConversionService` the same way (`file_management_agent.py:22-26`) — both are established, not novel |
| `composition/*` | `GrokImageAdapter` concrete class (only place it's named) | `composition/` is the only layer allowed to import concrete adapters + register them into a registry the services/agents layers only see by port type |

**One documented deviation carried forward, not introduced here:** `ports/CLAUDE.md` says shared
data models belong in `domain/`, with `AgentExecutionContext` as the sole named exception.
`ImageResult` in `image_search_port.py` already lives in its port file, not `domain/`, and ships in
production. `GeneratedImage` follows that same existing precedent rather than inventing a new,
inconsistent convention unilaterally. If this gets tightened later, both should move together.

---

## 4. New Components

| Component | File | Notes |
|---|---|---|
| `ImageGenerationPort` | `src/ports/image_generation_port.py` | New. `generate()`, `edit()`. |
| `GrokImageAdapter` | `src/adapters/grok_image_adapter.py` | New. Implements `ImageGenerationPort` via xAI `/v1/images/*`. |
| `ImageGenerationAgent` | `src/agents/image_generation_agent.py` | New. `HtmlPageGeneratorAgent`-shaped: 1 LLM call + port call + `DeliveryItem`. |
| `ImageGenerationAgentConfig` | `src/infrastructure/agent_config.py` | New `@dataclass` — temperature, timeouts (see §10). |
| Intent constants | `src/infrastructure/agent_manifest.py` | `GENERATE_IMAGE`, `EDIT_IMAGE`. |
| `AgentDescriptor` | `src/infrastructure/agent_manifest.py` | `eager=False`, `ExecutionMode.ASYNC` both intents, `context_schemas={EDIT_IMAGE: {"image_ref": ...}}`. |
| `"image_generation"` strategy | `src/services/agent_context_builder.py` | Text-LLM axis (§3.7 Axis 1). |
| `resolve_image_generation_context()` | `src/services/agent_context_builder.py` | New method, mirrors `resolve_async_context()`. Image-port axis (§3.7 Axis 2). |
| `image_registry` | `src/composition/user_agent_factory.py` | New constructor param, mirrors `job_registry`. |
| Firestore token | `COGNITIVE_PROCESS_IMAGE_GEN` | New. 3 technique clusters (§2.1) + task-class routing instruction. |
| Firestore blueprint/profile | `image_generation_agent_v1` / `image_generation` | New, per playbook Phase 2. |
| `PROTOCOL_SMART_AGENT_SELECTION` section | Firestore (manual edit) | §3.5. |

---

## 5. Files Changed (checklist, mirrors playbook §"Quick Reference")

```
Code:
  src/ports/image_generation_port.py         NEW — port + GeneratedImage dataclass
  src/adapters/grok_image_adapter.py         NEW — GrokImageAdapter
  src/infrastructure/agent_manifest.py       Intent.GENERATE_IMAGE/EDIT_IMAGE + AgentDescriptor
  src/infrastructure/agent_config.py         ImageGenerationAgentConfig
  src/services/agent_context_builder.py      "image_generation" strategy entry +
                                              resolve_image_generation_context()
  src/agents/image_generation_agent.py       NEW — ImageGenerationAgent
  src/composition/user_agent_factory.py      image_registry param + wiring (eager=False path)
  src/composition/*.py (bootstrap)           register GrokImageAdapter under "grok" in
                                              both the LLMPort registry (n/a) and new image_registry
  src/utils/capabilities.py                  user-facing capabilities entry (per playbook step 5)
  tests/unit/agents/test_image_generation_agent.py   NEW
  tests/unit/adapters/test_grok_image_adapter.py     NEW — wire test, mock at SDK boundary
  tests/contracts/adapter_contracts.py               new ContractRule for ImageGenerationPort

Prompt files:
  firestore_utils/uploads/COGNITIVE_PROCESS_IMAGE_GEN.groovy / .json
  firestore_utils/uploads/image_generation_agent_v1.json
  firestore_utils/uploads/image_generation.json

Firestore token updates (manual):
  PROTOCOL_SMART_AGENT_SELECTION   add generate_image/edit_image section (§3.5)
```

---

## 6. Design Decisions

Decisions reached during design discussion, with rationale. Items #6–#8 required explicit owner
sign-off before implementation per CLAUDE.md's delta-declaration gate — all three are now
`[APPROVED]`; nothing in this section is pending.

| # | Decision | Rationale |
|---|---|---|
| 1 | `ImageGenerationAgent` is a **full LLM agent** (1 call), not zero-LLM | §2.1 — 3 non-obvious technique clusters exist; passthrough would produce mediocre results on infographics/assets specifically |
| 2 | Technique lives in the **specialist's own prompt**, not Smart's protocol token | Keeps Smart's always-loaded prompt thin; technique is only relevant when the intent actually fires |
| 3 | `edit_image` uses context key `image_ref`, **not** `file_ref` | Avoids the coordinator's generic text-only auto-injection (§3.4) misfiring on binary content |
| 4 | Provider resolved via **two independent registries**, same resolved name | Reuses proven `DeepResearchPort`/`job_registry` pattern; §3.7 |
| 5 | Video generation **out of scope**, deferred to its own future RFC | Different technique/latency/delivery shape — §3.9 |
| 6 | **[APPROVED]** `edit_image` v1 supports a **single** reference image (`image_ref`), not xAI's max of 3 | Matches existing single-`file_ref` precedent (`CREATE_HTML_PAGE`) exactly, zero new plumbing. Multi-image compositing (styles/subjects from separate references) deferred until a real use case appears — extending to `image_ref_2`/`image_ref_3` or a list is additive, not a rewrite. |
| 7 | **[APPROVED]** `n=1` (single image per request) in v1, not xAI's max of 10 | Keeps the delivery/UX simple (one image per turn). "Give me variations" becomes a follow-up delegation. Revisit if users ask for options routinely. |
| 8 | **[APPROVED]** `allowed_providers: ["grok"]` only, no fallback | No second image-gen adapter exists yet (§3.9-adjacent — this is about images, not video). A `fallback` would need a second working adapter; adding one now would be speculative. |

---

## 7. Prompt Work (Phase 2 of playbook)

`COGNITIVE_PROCESS_IMAGE_GEN` token structure (Groovy source, uploaded as JSON):

```groovy
identity: "You are an image-generation prompt specialist. You receive a creative brief
           (from the orchestrator, already clarified with the user) and produce a
           technically precise prompt for grok-imagine-image-2.0 (Aurora)."

task_classification: [
    "photoreal: a realistic scene/subject -> photographic anchor + camera/lens + real-world
     light/texture cues + emotion words, natural sentence, 50-200 words.",
    "text_heavy: infographic/UI mock/diagram/dense layout -> quote exact text verbatim,
     specify where on the canvas each element sits.",
    "asset_set: icon/sprite/prop/mascot -> style-lock phrase (or note a reference image is
     attached), explicit transparent-background / isolated-subject framing.",
]

edit_mode_rule: "For edit_image tasks: stay surgical. Translate the instruction precisely —
                 do NOT add creative elaboration the user did not ask for."

output_format: "Return ONLY the final prompt text (and aspect_ratio if the brief implies
                a specific format), nothing else."
```

Upload order (dev then prod, per playbook §Step 9 — **human-executed only**, never by AI):
```bash
python firestore_utils/upload.py development_domain_prompt_tokens_v3_system COGNITIVE_PROCESS_IMAGE_GEN --format json
python firestore_utils/upload.py development_domain_prompt_blueprints_v3 image_generation_agent_v1 --format json
python firestore_utils/upload.py development_domain_prompt_profiles_v3 image_generation --format json
```

---

## 8. Cost Impact

| Metric | Value |
|---|---|
| Image generation | $0.04 (1K/low) – $0.08 (2K/medium) per image |
| Image edit | ~2x generation cost (input + output both billed) |
| Extra LLM call per request | 1 short text call (brief → prompt), cheap/fast — no code-execution or grounding fees |
| Comparison | Below GPT-Image tier pricing reported in market scan; no subscription model like Midjourney |

---

## 9. Test Strategy

### Unit (`tests/unit/agents/test_image_generation_agent.py`)
1. `can_handle` — correct intents, wrong intent, empty query
2. `execute` `generate_image` happy path — LLM call #1 → port `.generate()` → `DeliveryItem`
3. `execute` `edit_image` happy path — `image_ref` resolved via `FileConversionService.resolve_bytes()` (NOT via coordinator auto-injection — assert `file_content` is never read), port `.edit()` called with bytes
4. `edit_image` missing `image_ref` — failure response
5. Prompt builder failure — `AgentResponse.failure()`, no silent fallback
6. Image port returns empty/raises — failure response, no partial `DeliveryItem`
7. `edit_image` prompt stays close to instruction — regression guard against the LLM over-elaborating an edit brief (contract-style assertion, not exact string match)

### Adapter wire tests (`tests/unit/adapters/test_grok_image_adapter.py`)
Mock at the `AsyncOpenAI` SDK boundary (per `ADAPTER_WIRE_TESTING.md`), not the port — cover both
`images.generate()` and `images.edit()` call shapes, `response_format="b64_json"` decoding,
and error mapping to the port's declared failure contract.

### Contract (`tests/contracts/adapter_contracts.py`)
New `ContractRule` for `ImageGenerationPort` — validated by both unit and integration suites.

---

## 10. Open Questions / Follow-ups (not blocking, but not silently assumed either)

1. **Exact output `mime_type`.** xAI docs didn't specify the image format returned (PNG assumed,
   unconfirmed) — confirm against a live call before hardcoding `content_type` in `DeliveryItem`.
2. **`images.edit()` multi-reference parameter shape.** Docs confirm "up to 3 source images... via
   URL or base64" but not confirmed to me exact request field name/structure for >1 image — moot
   for v1 (single reference, decision #6) but relevant if/when extended.
3. **Timeout values.** No latency data for `grok-imagine-image-2.0` yet. Propose conservative
   starting points (`request_timeout_s≈60`, `dispatch_deadline_s≈180`) to be tuned after first live
   measurements — do **not** copy `HTML_PAGE_GENERATOR`'s 720s uncritically; that number is sized
   for long text generation, not image rendering.
4. Per-user provider override key name (`agent_providers["image_generation"]`) — confirm this
   doesn't collide with any existing config validation allow-list.

---

## 11. Implementation Order

1. `src/ports/image_generation_port.py` — port + `GeneratedImage`
2. `src/adapters/grok_image_adapter.py` — adapter + wire tests
3. `src/infrastructure/agent_manifest.py` — Intents + `AgentDescriptor`
4. `src/infrastructure/agent_config.py` — `ImageGenerationAgentConfig`
5. `src/services/agent_context_builder.py` — strategy entry + `resolve_image_generation_context()`
6. `src/agents/image_generation_agent.py` — agent + unit tests
7. `src/composition/user_agent_factory.py` + bootstrap — `image_registry` wiring
8. `src/utils/capabilities.py` — user-facing capability entry
9. Prompt tokens (§7) — human uploads dev, validate, then prod
10. `PROTOCOL_SMART_AGENT_SELECTION` update (§3.5) — human upload
11. `make test-unit` + `make test-e2e-all`
12. Manual spot-check in Slack/Telegram — both intents, check `_on_agent_start`/`_on_agent_success` logs, confirm inline image delivery on both channels
13. `make deploy`
