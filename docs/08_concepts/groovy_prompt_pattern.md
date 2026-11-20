<!-- living-doc
covers: none
-->

# Groovy-Style Prompt Pattern

Why every system prompt in this repo is a Groovy-looking class definition, and which rules keep that form working.
The assembly mechanics (slots, profiles, caching, the full block order) are in
[Prompt Design System v3](../05_building_blocks/prompt_design_system_v3/README.md); this page holds the reasoning.

## 1. Why a class definition and not prose

Models are trained heavily on code. They follow syntax, scoping, inheritance and ordered steps more reliably than loose
natural-language instructions. The prompt is therefore written as `class <Agent> { ... }` with named blocks, not as a
story. What this buys:

- **Scope.** A `{ ... }` block delimits where an instruction applies. Text outside any block applies "everywhere" and
  bleeds into every context, so tokens never contain loose instructions.
- **Separation of identity and function.** Personality blocks (`archetype`, `voice`, `humor_engine`) are kept apart from
  functional blocks (`policies`, `protocols`, `cognitive_process`, `output_format`). That separation is what lets a user
  swap a personality token without touching behaviour.
- **Procedures as steps.** A protocol or `cognitive_process` is a numbered `steps: [...]` list with a verb per step.
  Ordered, verb-led steps make the model plan, execute, verify and only then answer.
- **Modularity.** Each block is a separately stored token, so one block is changed without rewriting the prompt.

The form is a convention the model reads, not code anything executes. Nothing parses the Groovy.

## 2. How the form is produced

A prompt is assembled, not hand-written: tokens (stored fragments of bare block content) are placed under the section
classes of a blueprint, in the blueprint's class order, wrapped in the blueprint's outer class
(`PromptAssemblyService` in `src/services/prompt_v3/prompt_assembly_service.py`; the section render loop sits in the
static-template step). Runtime data is added afterwards by code, never inside a token.

Override resolution, from weakest to strongest: the agent profile, then the account, then the user. Overrides replace
only a token the agent already activates, matched by class and category; they cannot add a section the agent does not
have, and a token marked `non_overridable` in the agent profile is never replaced
(`PromptAssemblyService._apply_overrides`). The only levels are agent, account and user: there is no separate system
level (`src/domain/prompt_v3/slot.py::OwnerType`).

## 3. Invariants

1. **Tokens are static text.** A token holds bare block content, without the outer section wrapper
   (`src/domain/prompt_v3/token.py::Token`). Biography, conversation history, date and reminders are injected by code
   after substitution (`PromptAssemblyService._inject_runtime_context`). A variable inside a token would either be
   dead text or break caching.
2. **Validation happens when a token is created, not when it is used.** `Token.create` runs the content through
   `SecurityPort`; direct construction skips that and is wrong.
3. **Static before the boundary, dynamic after.** `src/domain/llm.py::PROMPT_CACHE_BOUNDARY` splits the system
   instruction. Everything before it is identical across requests of one agent and user, so the Claude adapter marks
   it for prompt caching (`src/adapters/claude_adapter.py`, the split on the boundary). Reminders, the optional
   `current_date_time`, query-specific context and the turn key come after it. Anything that varies per request placed
   before the boundary silently destroys the cache hit.
4. **Runtime blocks exist only when they have content.** `knowledge_base`, `available_skills` and `standing_directives`
   are appended by code and never render as empty wrappers. `standing_directives` is the last static block on purpose:
   binding rules get the strongest recency, and they change only on consolidation, so caching survives.
5. **Block position carries meaning.** Agents that reason over the knowledge base ask for `kb_preamble=True`, which
   puts `knowledge_base` before the template so that `cognitive_process` is the last thing read (recency). The default
   places it after the template.
6. **The time is not in the prompt by default.** It is stamped into user messages; an agent that needs a
   `current_date_time {}` block passes `include_datetime=True`.
7. **No fallback prompt.** If assembly fails the agent returns a failure; it never degrades to inline text
   (rule in the root `CLAUDE.md`).

## 4. Writing a token (traps)

- **Scope everything.** A sentence outside a block is global and leaks into every agent that includes the token.
- **Do not redefine personality** in a non-personality token. Tokens extend the agent class implicitly.
- **Steps must be concrete.** "Think", "do something", "return result" give the model nothing to follow. Name the
  action and the check: what to identify, which tool to call, what to verify, what to deliver.
- **Use `//` comments for intent.** The model reads them as the developer's reason. Per the root `CLAUDE.md`, a prompt
  text first explains what its output is for and which gap it bridges, then states the rules; it contains no examples
  (models copy them) and what must hold is enforced in code.
- **Choose between rules and judgment per case.** A domain default ("this data type goes in this format") gives
  consistent behaviour for known types; a quality question ("would this help?") leaves novel cases to the model but
  tends to be answered "no" for simple ones. Mixed — defaults plus an override — is the usual choice.
- **A token is not live until uploaded.** Tokens ship by token upload, not by code deploy, and the assembled static
  template is cached per instance (`PromptAssemblyService._assembled_cache`, TTL in `cache_ttl`), so an upload reaches
  a warm instance after the TTL, a restart or `invalidate_cache()`.

## 5. Where to start reading

- A real token to copy the shape from: `firestore_utils/uploads/` (`COGNITIVE_PROCESS_*`, `PROTOCOL_*`,
  `OUTPUT_FORMAT_*`).
- Assembly and block order: `src/services/prompt_v3/prompt_assembly_service.py` and the building-block README above.
- Prompt text rules: the "Code Conventions" section of the root `CLAUDE.md`.
