<!-- living-doc
covers:
  src/adapters/security/
  src/ports/security_port.py
  src/domain/prompt_v3/security.py
  src/domain/system_fingerprint.py
  src/infrastructure/turn_fingerprint.py
  src/infrastructure/turn_key.py
  scripts/validation/fingerprint_*.py
-->
# Security Validation

## Purpose

This block keeps text that the owner or a model wrote from being read by the model as an instruction of the system. It
has two independent parts:

1. **Pattern validation** (`SecurityPort`): a text goes in, and comes out unchanged, with matched phrases replaced
   by `[REDACTED]`, or is refused by raising `ValueError`. It is a coarse filter for known injection phrasings, not a
   semantic judge.
2. **The system-turn fingerprint**: code-written text of the current turn is marked with a per-turn secret key, so the
   model can tell the system's notes from text that merely looks like one.

What it is not: it owns no stored data, and it does not wrap untrusted file or web content (that is
`src/domain/untrusted_text.py`, see the sandbox and drive docs linked below). Everything user-derived is treated as
untrusted unless a caller passes `TrustZone.TRUSTED`.

## Owned data

None. The only trace pattern validation leaves is the `validation` entry (risk level, score, patterns, action) that
`Token.create` writes into a prompt token's `metadata`. The turn key is never stored: it is a local variable of one
orchestrator run (`src/agents/core/smart_response_agent.py::SmartResponseAgent.execute`,
`src/agents/core/quick_response_agent.py::QuickResponseAgent.execute`).

## Flows

### Pattern validation

`SecurityPort.validate(text, context, zone)` (`src/ports/security_port.py::SecurityPort`) returns a
`ValidationResult` (`src/domain/prompt_v3/security.py::ValidationResult`: `sanitized_text`, `risk_level`,
`action_taken`, `patterns_detected`).

- `src/adapters/security/regex_adapter.py::RegexSecurityAdapter.PATTERNS` holds the regexes by level. CRITICAL and HIGH
  raise `ValueError` (action `blocked`); MEDIUM replaces every matching pattern with `[REDACTED]` (action `sanitized`);
  nothing matched is `passed`. `TrustZone.TRUSTED` returns the text untouched; `UNTRUSTED` and `SEMI_TRUSTED` get the
  same full scan (`tests/unit/adapters/security/test_regex_adapter.py::test_semi_trusted_zone_still_validates`).
- `src/adapters/security/composite_adapter.py::CompositeAdapter` runs its adapters, turns an adapter's `ValueError`
  into a CRITICAL `blocked` result, and aggregates by strategy (`worst_case`, `majority_vote`, `all_pass`).
- Production wiring is one `CompositeAdapter([RegexSecurityAdapter()], "worst_case")`, built in
  `src/composition/service_container.py::ServiceContainer` and in `src/composition/prompt_assembly_setup.py`.
- `src/adapters/security/llm_adapter.py::LLMSecurityAdapter` and
  `src/adapters/security/external_api_adapter.py::ExternalAPIAdapter` are placeholders: each hands the call to the
  `SecurityPort` injected as `fallback` (they are not wired in production).

Callers, each applying its own policy to the result:

| Caller | Zone | On blocked or sanitized |
|---|---|---|
| `src/services/prompt_v3/prompt_assembly_service.py::PromptAssemblyService._inject_runtime_context`: biographical facts, standing directives, query-specific context, conversation history | UNTRUSTED | `ValueError` propagates; a sanitized text is used |
| `src/domain/prompt_v3/token.py::Token.create` (prompt token content, via the token repository) | TRUSTED | see traps |
| `src/services/skill_service.py::SkillService._check_text` (custom skills) | UNTRUSTED | refused (`SkillRejected`) unless `action_taken == "passed"` |
| `src/services/memory_fact_service.py::MemoryFactService._check` (owner-confirmed `$memory save` facts) | UNTRUSTED | refused unless passed |
| `src/services/drive_note_service.py::DriveNoteService._check` (drive notes) | UNTRUSTED | refused unless passed |

### 5.1a System-turn fingerprint (Smart and Quick)

Spec: `docs/10_rfcs/SYSTEM_TURN_FINGERPRINT_RFC.md`. Steps of one turn:

1. The orchestrator draws a key, three words of `src/domain/system_fingerprint.py::WORDS`, with
   `src/infrastructure/turn_key.py::draw_turn_key` (`secrets`), once per `execute`. Tests inject their own source.
2. `src/services/prompt_v3/prompt_assembly_service.py::PromptAssemblyService` renders the key as the `session_key`
   block (`system_fingerprint.py::render_session_key`), the last dynamic block after the cache boundary, so it is
   never cached. The static `provenance` rule (`system_fingerprint.py::PROVENANCE_CLASS`) is kept at the head of the
   template; it tells the model that only a `<system_note>` carrying the current key speaks for the system.
3. Code-written text is wrapped with `system_fingerprint.py::wrap_system` (`<system_note key="…">`): the delegation
   engine's wrap-up and meanwhile notes, Smart's running-jobs header (`src/infrastructure/turn_fingerprint.py::keyed_header`),
   Quick's fallback note.
4. Look-alikes lose their tag: loaded history (`turn_fingerprint.py::neutralize_history`, a copy; persisted text is
   unchanged), tool results and the owner's lines inside keyed notes (`src/infrastructure/delegation_engine.py`, via
   `src/domain/untrusted_text.py::neutralize_system_tag`).
5. Model output is scrubbed of the key: tool-call names and arguments, values and dict keys
   (`delegation_engine.py::_scrub_tool_calls`), and the final reply (text, structured data, link list, history
   summary) via `turn_fingerprint.py::scrub_reply`. `system_fingerprint.py::scrub` matches the key's three words in
   order with any non-letters between them and any case.

## Invariants and traps

- **A new text path carrying owner or model words into a prompt must call the port.** Nothing enforces it
  structurally; `tests/unit/services/prompt_v3/test_prompt_assembly_service.py::test_security_validation_called_for_runtime_data`
  covers the assembly path only.
- **`Token.create` validates in the TRUSTED zone**, so the regex adapter skips the scan and prompt tokens are in
  practice not pattern-checked (`tests/unit/domain/prompt_v3/test_token.py` asserts the zone). Token safety rests on
  admin control of the token store, not on this block.
- **Also unvalidated:** orchestrator-built `pending_notes` in the prompt (`PromptAssemblyService`, commented as TRUSTED);
  and, in the code, model output is not passed through `SecurityPort` anywhere. The old "output" and "RAG" layers are
  not implemented.
- **`ValueError` is the block signal**, not a result flag. A caller that forgets `except ValueError` turns an
  injection phrase in user history into a failed turn; the three refusing services above catch it.
- **MEDIUM is a rewrite, not a refusal** in assembly, but skills, memory facts and notes treat any non-`passed`
  action as a refusal. A benign text containing "new instructions" is refused there.
- **`majority_vote` can outvote a block**: a blocked adapter raises only when the majority risk level is CRITICAL
  (`CompositeAdapter._majority_vote`). Only `worst_case` is wired; do not switch strategy without reading it.
- **The key lives only in locals.** It must never reach `self.*`, `message.context` or engine context
  (comment at `SmartResponseAgent.execute`); a stored key would survive its turn. `require_valid_key` never echoes it.
- **Keyed notes keep their text word for word**; `wrap_system` adds lines around it, never inside, because protocol
  tokens may quote the note verbatim.
- **Scrubbing is spelling-tolerant, wrapping is not**: only the exact `<system_note key="…">` form counts as the
  system's; every other look-alike is neutralised.
- Code-written text sent into the user channel uses `wrap_system` with the turn's key; tool results say
  `Tool note:` or `Tool error:` (root `CLAUDE.md` traps).

## Links

- `docs/10_rfcs/SYSTEM_TURN_FINGERPRINT_RFC.md` (fingerprint spec).
- `docs/05_building_blocks/prompt_design_system_v3/README.md` (the assembly that calls the port).
- `scripts/validation/fingerprint_probe.py` and `fingerprint_cases.py`: owner-run, paid recorded single calls for the
  fingerprint; see their docstrings before running.

## Where to start reading

- `src/ports/security_port.py::SecurityPort`
- `src/adapters/security/regex_adapter.py::RegexSecurityAdapter`
- `src/adapters/security/composite_adapter.py::CompositeAdapter`
- `src/services/prompt_v3/prompt_assembly_service.py::PromptAssemblyService`
- `src/domain/system_fingerprint.py::wrap_system`
- `src/infrastructure/turn_fingerprint.py::scrub_reply`
