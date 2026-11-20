<!-- living-doc
covers:
  src/locales/
  src/adapters/file_localization_adapter.py
  src/ports/localization_port.py
  src/ports/language_service_port.py
  src/services/language_preference_service.py
  src/services/language_resolver.py
  src/services/localization_service.py
  src/domain/language.py
  src/composition/language_setup.py
-->
# Localization System and Language Settings

## Purpose

A user has two language settings that look like one in the Cabinet and work through unrelated code paths:

| | UI language | Response language |
|---|---|---|
| What it controls | The bot's own fixed strings: status phrases ("thinking"), file-without-text prompts, short system messages | The language the model answers in |
| Mechanism | `LocalizationPort` over the locale modules in `src/locales/`, chosen per request | A `LANG_*` policy token in the assembled system prompt |
| Chosen by | `preferred_language`, else the account default, else the deployment default | `agent_mirror`: mirror the user's input (default), or a fixed language |

Nothing in the chat path sets the model's output language in code: the response language is a prompt-level fact, changed
only by swapping a token in the user's prompt override. On the chat path the UI language never reaches the prompt. The
exception is the call-file profiles: `src/composition/companion_extractor_runner.py` puts `resolve_ui_language` into the
payload as `owner_language`, and `src/agents/lelik_summarizer_agent.py` renders it as a prompt block, so those outputs
follow the UI chain, not a `LANG_*` token. The two settings are written together by one call (`set_preference`) from one Cabinet form, and nothing else keeps them in step.

What it is not: it does not translate model output, does not detect the user's language, and the voice transcription
languages (`normalize_voice_languages`) are a separate, open-ended list that is deliberately not validated against
`LanguageCode`.

## Owned data

- **`UserBotConfig.preferred_language`** (`Optional[LanguageCode]`) and **`agent_mirror`** (`bool`; mirror unless set), `src/domain/user.py::UserBotConfig`. Written only by `LanguagePreferenceService.set_preference`, as field-level
  updates (`config.preferred_language`, `config.agent_mirror`) so a concurrent change of another config field survives.
  Read through `LanguageResolver`.
- **`BillingAccount.default_language`** (`src/domain/billing.py`): the account-level default between the user's
  preference and the deployment default. This block only reads it; nothing here writes it.
- **The USER prompt override document** `USER_{user_id}` in the profile overrides collection: holds at most one `LANG_*`
  token (`FirestoreAgentProfileRepository.set_override_tokens`). Written only by `set_preference`. Its absence, or the
  absence of a `LANG_*` entry in it, means mirror mode.
- **The `LANG_*` tokens** (`LANG_MIRROR`, `LANG_FIXED_UK/EN/FR/ES`): class `policies`, category `output_language`,
  Groovy rule text. Firestore is the source of truth; `prompts_snapshot/` (`tokens/system/LANG_*.groovy`, `profiles/`) is
  a git-tracked read-only mirror (`prompts_snapshot/README.md`). They ship by token upload, not by deploy. The default
  `LANG_MIRROR` slot is in the agent profiles (Quick, Smart and the Lelik profiles) and must stay `non_overridable: false`
  (see traps).
- **Locale modules** `src/locales/{uk,en,fr,es}.py`: pure data, all with the members of `en.py` (the template), among them
  `UI_STRINGS` keyed by `UIMessage.value` (`src/domain/ui_messages.py`). They ship by code deploy.
- **`SYSTEM_DEFAULT_LANGUAGE`** (env, read in `build_languages`): the last link of the UI chain and the language of a fixed-mode user
  with no explicit choice.

## Flows

### UI language per request

1. A platform adapter resolves the language after authorizing the user: Slack HTTP in
   `src/adapters/slack/http_adapter.py::HTTPModeAdapter._resolve_language`, Telegram inline in
   `src/adapters/telegram/webhook_adapter.py` (step "Resolve language"). Both call
   `LanguageResolver.resolve_ui_language` and `get_preference`.
2. `resolve_ui_language` walks user `preferred_language`, then the user's account `default_language`, then
   `LanguageResolver.system_default`.
3. The adapter builds the `ResponseChannel` with `language=ui_lang` and `localization=` the
   `LocalizationService` wrapper (`src/adapters/slack/response_channel.py::SlackResponseChannel`), which asks it for status
   phrases, entertainment intros and fixed strings. A random variant of a phrase list is picked per use.
4. The adapters pass only the UI language; the response language comes only from the USER override document.

Other consumers of the resolver (notification service, drive operations and indexing, call text files, voice web routes,
review question texts) call `resolve_ui_language` or `get_preference` directly.

Wiring: `src/composition/language_setup.py::build_languages` builds the adapter, `LocalizationService` (the wrapper
handlers receive instead of the port), the resolver and the system default. The resolver is built first and handed to
the notification service and the agent factory; the writer is built after the agent factory
(`src/composition/agents_setup.py`) because it refreshes agents.

### Changing the setting (fixed language)

Entry: `POST /api/user/language` in `src/web/user_cabinet_app.py` calls
`src/services/language_preference_service.py::LanguagePreferenceService.set_preference`. It:

1. Writes the two fields to the user (field-level).
2. Swaps the token in the USER override: for fixed mode `LANG_FIXED_<code>` at an order set inline in `set_preference`, clearing every other
   `LANG_*` id (`_ALL_LANG_TOKEN_IDS`) in the same document write (a read then a set, not a transaction); the language is the preference, else the system default.
3. Calls `invalidate_cache()` on its `prompt_builder`, which `src/composition/agents_setup.py` wires to the assembly service; the next `build_for_agent()` rebuilds the static prompt.
4. If a notification service is configured: refreshes the user's agents (`ensure_agents`), then sends an interactive
   `system_alert` whose text is the chosen policy's Groovy rule (`_LANG_ALERTS`). The alert and the agent's reply land in
   the active channel's session history, so the model has the policy in context even when earlier turns were in another
   language. A failure here is logged and swallowed; the preference is already saved.

Assembly (`src/services/prompt_v3/prompt_assembly_service.py`, USER overrides read through `get_override_tokens`)
replaces an active token with the override token of the same class and category, so `LANG_FIXED_*` displaces the
profile's `LANG_MIRROR`.

### Back to mirror mode

`set_preference(..., agent_mirror=True)` clears all `LANG_*` ids from the USER override and adds nothing, so the profile's
`LANG_MIRROR` is active again. The cache is invalidated and a "mirror" alert is sent, as above. `preferred_language` is
still stored and still drives the UI language.

### Cabinet API

`GET /api/user/language` returns `{preferred_language, agent_mirror}` (`null` for no explicit preference).
`POST /api/user/language` takes the same two fields; `agent_mirror` must be a boolean (400 otherwise) and defaults to
`true`. With `agent_mirror=false` and no `preferred_language`, the effective response language is the system default.

## Invariants and traps

- **The two reads are separate classes on purpose.** `LanguageResolver` (the only `LanguageServicePort` implementation)
  reads; `LanguagePreferenceService` writes and takes the resolver for the system default. Readers must never depend on
  the writer, which notifies and refreshes agents. One resolver instance is shared by design
  (`tests/unit/composition/wiring_instance_ledger.txt`).
- **An unsupported language code in the Cabinet POST is stored as Ukrainian, not rejected.** `LanguageCode.from_str`
  returns `default or cls.UK`, so `from_str(x, default=None)` never returns `None`, and the route's
  `if preferred is None: 400` branch in `set_language` cannot fire. Do not rely on that 400, and do not assume
  `from_str(..., default=None)` signals failure.
- **The "fixed" rule has several copies of one decision.** `set_preference` builds `LANG_FIXED_<CODE>` inline; the same
  rule (with a third copy for the alert key) is `src/domain/language.py::resolve_lang_token_id`, which no production code calls (tests only). Change both, or
  call the function from the service.
- **The override works only against an overridable `LANG_MIRROR` slot.** In `PromptAssemblyService` a matching active token
  that is `non_overridable` makes the override skip silently, and an agent whose profile has no `output_language` token
  ignores it with a debug line only. Every agent profile that should honour the setting keeps `LANG_MIRROR` with
  `non_overridable: false`.
- **Adding a language touches six places:** a `LanguageCode` value, `src/locales/<code>.py` (copy `en.py`, including
  `UI_STRINGS`, which `get_ui_string_variants` iterates for every registered module), an entry in
  `FileLocalizationAdapter._REGISTRY`, a `LANG_FIXED_<CODE>` token upload, and an id in `_ALL_LANG_TOKEN_IDS` plus a
  `_LANG_ALERTS` entry in the preference service, and the hand-written language `<select>` in
  `src/web/static/cabinet.html` (without it the code cannot be chosen). Missing the id or the alert leaves a user stuck:
  the old token is never cleared, or no alert is sent. The "zero other changes" docstrings in `language.py` and the
  adapter are stale.
- **Unknown codes fall back, they do not fail.** The adapter falls back to English (`FileLocalizationAdapter._DEFAULT`);
  a `ResponseChannel` built with no `localization` falls back to the Ukrainian module, and an adapter with no language
  service uses Ukrainian as the UI language (not the system default).
- **Mirror is the only mode that follows a mid-conversation switch.** A fixed policy tells the model to keep the configured
  language whatever the user writes in; for calls this made the model refuse an explicit request to switch, so the spoken-reply anchor in
  `src/domain/llm.py` states that the configured language is a default, not a lock (`tests/unit/domain/test_persona_anchor.py`).
  Text chats keep the strict rule.
- **A cache must be invalidated or the change is invisible.** The assembled static prompt is cached
  (`PromptAssemblyService.__init__` `cache_ttl`); `set_preference` calls `invalidate_cache()` itself, but it
  clears only the process that served the request (the cache is in memory).

## Links

- [`MULTILINGUAL_SUPPORT_RFC.md`](../../10_rfcs/MULTILINGUAL_SUPPORT_RFC.md): the design record (history only).
- [Prompt design system v3](../prompt_design_system_v3/README.md): the override mechanism and the language policy tokens.
- [User cabinet](../user_cabinet/README.md): the Cabinet page that calls the API.
- [`composition_root_consumers.md`](../../04_solution_strategy/decisions/composition_root_consumers.md): why reads and
  writes are built in separate places.

## Where to start reading

- `src/services/language_preference_service.py::LanguagePreferenceService.set_preference`: the one write path.
- `src/services/language_resolver.py::LanguageResolver.resolve_ui_language`: the UI chain.
- `src/adapters/file_localization_adapter.py::FileLocalizationAdapter`: the locale registry and its fallbacks.
- `src/ports/localization_port.py::LocalizationPort`: what a UI string consumer can ask for.
- `src/composition/language_setup.py::build_languages`: what is built, in which order.
- `src/web/user_cabinet_app.py::create_user_cabinet_blueprint`, routes `/api/user/language`: the API.
