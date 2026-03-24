# Localization System + Language Settings

## How To Use This Document

**For AI Agents:** Read before modifying language resolution, status phrases, LANG_* tokens,
or language preference API.

**For Developers:** Read when adding languages, debugging wrong UI language, or understanding
how bot response language is controlled separately from UI language.

**Update this document when:**
- New `LanguageCode` values are added
- Locale files (`src/locales/*.py`) change structure
- Language resolution chain changes (`resolve_ui_language`)
- LANG_* token set changes (new tokens, category/class changes)
- Cabinet API contract for language changes
- Notification-on-change behavior changes

**Cross-references:**
- Language policy tokens (LANG_*): [../prompt_design_system_v3/README.md § 11](../prompt_design_system_v3/README.md)
- Override mechanism: [../prompt_design_system_v3/README.md § 2.4](../prompt_design_system_v3/README.md)
- Cabinet API: [../user_cabinet/README.md](../user_cabinet/README.md)
- RFC: [../../10_rfcs/MULTILINGUAL_SUPPORT_RFC.md](../../10_rfcs/MULTILINGUAL_SUPPORT_RFC.md)

---

## 1. Overview

The localization system has two independent responsibilities:

| Concern | What it controls | Mechanism |
|---------|-----------------|-----------|
| **UI language** | Status phrases in Slack/Telegram ("Thinking...", "Searching..."), file prompts | `LocalizationPort` + locale files |
| **Bot response language** | Language the LLM uses in its replies | LANG_* token in system prompt (override mechanism) |

These two concerns are configured together in Cabinet UI but work through completely different
code paths. Changing one does not automatically change the other — they are coordinated by
`LanguagePreferenceService`.

---

## 2. Supported Languages

Defined in `src/domain/language.py`:

```python
class LanguageCode(str, Enum):
    UK = "uk"   # Ukrainian
    EN = "en"   # English
    FR = "fr"   # French
    ES = "es"   # Spanish
```

`LanguageCode.from_str(value, default=None)` — safe factory; returns `default` on unknown input.

---

## 3. UI Language — How It Works

### 3.1 Resolution chain

Per request, platform adapters call `LanguagePreferenceService.resolve_ui_language(user_id)`:

```
user.config.preferred_language       (set by user in Cabinet)
    → account.default_language       (account-level default, if configured)
        → SYSTEM_DEFAULT_LANGUAGE    (env var, default: "en")
```

The resolved `LanguageCode` is passed to `ResponseChannel` for that request.

### 3.2 Port and adapter

`LocalizationPort` (`src/ports/localization_port.py`) — abstract interface:

```python
class LocalizationPort(ABC):
    def get_status_phrases(self, language: LanguageCode, status_type: StatusType) -> List[str]: ...
    def get_entertainment_intros(self, language: LanguageCode) -> List[str]: ...
    def get_file_prompt(self, language: LanguageCode, mime_type: str) -> str: ...
```

`FileLocalizationAdapter` (`src/adapters/file_localization_adapter.py`) — static locale registry:

```python
_REGISTRY = {
    LanguageCode.UK: uk,
    LanguageCode.EN: en,
    LanguageCode.FR: fr,
    LanguageCode.ES: es,
}
_DEFAULT = en  # fallback for unknown codes
```

### 3.3 Locale files

`src/locales/{uk,en,fr,es}.py` — pure data modules, no external dependencies.

Each file provides:
- `{LANG}_MESSAGES: Dict[str, List[str]]` — keyed by `StatusType.value`, each value is a list
  of phrase variants. A random variant is selected on each use (reduces repetition).
- `ENTERTAINMENT_INTROS: List[str]` — phrases shown while web search runs ("rummaging through
  the web..."), displayed before fun facts.
- `FILE_FALLBACK_{IMAGE,VIDEO,PDF,DOCUMENT,GENERIC}: str` — default prompt when user sends a
  file without text.

Status types covered: `THINKING`, `SEARCHING_MEMORY`, `SEARCHING_WEB`, `PROCESSING_FILE`, `ERROR`.

Style is consistent across all languages: ironic, warm, slightly self-deprecating — same tone
as the Ukrainian original. FR/ES are adapted translations, not literal.

### 3.4 Platform adapter wiring

After authorizing a user, platform adapters (Slack socket, Slack HTTP, Telegram) call
`_resolve_language(user_id)` which returns `(ui_lang, preferred_language, agent_mirror)`.

`ui_lang` and `localization` are passed to `ResponseChannel` constructor:

```python
response_channel = SlackResponseChannel(
    ...,
    language=ui_lang,
    localization=self._localization,  # FileLocalizationAdapter instance
)
```

`ResponseChannel` then uses `localization.get_status_phrases(self.language, status_type)`
instead of hardcoded Ukrainian strings. If `localization` is `None` (e.g., Prompt Design System
not initialized), it falls back to the UK locale.

---

## 4. Bot Response Language — How It Works

### 4.1 Design principle

Bot response language is controlled entirely through the prompt assembly system, not through
code conditionals. The LLM follows a language policy token (`LANG_*`) that is part of the
assembled system instruction.

No code path in `ConversationHandler`, `BaseAgent`, or adapters explicitly sets or overrides
the LLM's output language. This is purely a prompt-level concern.

### 4.2 LANG_* token family

Five tokens live in `development_domain_prompt_tokens_v3_system`:

| Token ID | Behavior |
|----------|---------|
| `LANG_MIRROR` | Mirrors user's input language (dynamic) |
| `LANG_FIXED_UK` | Always responds in Ukrainian |
| `LANG_FIXED_EN` | Always responds in English |
| `LANG_FIXED_FR` | Always responds in French |
| `LANG_FIXED_ES` | Always responds in Spanish |

All share: `class: policies`, `category: output_language`, `non_overridable: false`.
Content is Groovy DSL `@critical rule Output_Language_*() { ... }` placed in the `policies`
section of the assembled prompt.

`non_overridable: false` is intentional — it allows USER-level overrides to replace the
default `LANG_MIRROR` with a fixed-language token.

### 4.3 Default state (agent profiles)

Quick and Smart agent profiles have `LANG_MIRROR` at `order: 300`:

```json
"LANG_MIRROR": {"order": 300, "non_overridable": false}
```

This is the default: the bot mirrors the user's input language. No USER override doc needed
for mirror mode.

### 4.4 Override flow (fixed language)

When a user selects a fixed language in Cabinet:

```
set_preference(user_id, preferred_language=EN, agent_mirror=False)
    |
    ├─ write user.config.preferred_language = EN to UserRepository
    |
    ├─ set_override_tokens(USER, user_id,
    |       tokens={LANG_FIXED_EN: ProfileToken(order=70)},
    |       clear_ids={LANG_MIRROR, LANG_FIXED_UK, LANG_FIXED_FR, LANG_FIXED_ES}
    |   )
    |   → writes doc USER_{user_id} to overrides_collection (await)
    |   → atomic: clears old LANG_* entries, writes new one
    |
    ├─ assembly_service.invalidate_cache()
    |   → next request rebuilds prompt from Firestore
    |
    └─ notify(system_alert=<LANG_FIXED_EN Groovy rule text>)
        → agent receives alert in main session (session_id=user_id)
        → alert + agent response saved to conversation history
        → LLM context now contains the policy change
```

Assembly then fetches the USER override doc and resolves it:

```
LANG_MIRROR (agent profile, order 300, category=output_language)
    → replaced by LANG_FIXED_EN (user override, same class+category)
    → LANG_FIXED_EN rendered in policies section
```

### 4.5 Mirror mode restoration

When user switches back to mirror mode:

```
set_preference(user_id, preferred_language=None, agent_mirror=True)
    → set_override_tokens(USER, user_id, tokens={}, clear_ids=ALL_LANG_IDS)
    → override doc USER_{user_id} now has no LANG_* entries
    → assembly falls back to LANG_MIRROR from agent profile
```

### 4.6 Cache invalidation

`set_preference()` always calls `assembly_service.invalidate_cache()` synchronously before
returning. The next `build_for_agent()` call will rebuild the static template from Firestore,
picking up the new override.

---

## 5. LanguagePreferenceService

`src/services/language_preference_service.py` — single write path for language preference.

```python
class LanguagePreferenceService(LanguageServicePort):
    async def set_preference(
        self,
        user_id: str,
        preferred_language: Optional[LanguageCode],
        agent_mirror: bool,
    ) -> None: ...

    async def get_preference(self, user_id: str) -> Tuple[Optional[LanguageCode], bool]: ...

    async def resolve_ui_language(self, user_id: str) -> LanguageCode: ...
```

**Dependencies (injected):**
- `user_repo: UserRepository` — reads/writes `UserBotConfig`
- `account_repo: AccountRepository` — reads account-level language default
- `profile_repo: AgentProfileRepository` — writes USER override doc via `set_override_tokens()`
- `prompt_builder: PromptBuilderPort` — cache invalidation
- `notification_service: UserNotificationService` — alert on language change (optional)
- `_ensure_agents: Callable` — `agent_factory.ensure_agents_for_user` (wired post-init in main.py)
- `system_default_language: LanguageCode` — resolved from `SYSTEM_DEFAULT_LANGUAGE` env var

**Wiring note (main.py):** `notification_service` and `_ensure_agents` are set via
post-init attribute assignment after those objects are created (they are constructed after
`_language_service`):

```python
_language_service._notification_service = notification_service
_language_service._ensure_agents = agent_factory.ensure_agents_for_user
```

---

## 6. Language Change Notification

When the user saves language settings in Cabinet, the bot receives an alert in the active
Slack/Telegram channel. The alert text is the Groovy rule content of the selected LANG_* token.

**Flow:**
1. `_ensure_agents(user_id)` — registers agents in coordinator (required before routing)
2. `notify(user_id, account_id, system_alert=<token text>)` — routes alert through QuickAgent
3. `session_id` defaults to `user_id` — alert + response saved to main session history

This injects the language policy into the LLM's context window at the moment of change,
ensuring the next user message is answered in the correct language even if prior conversation
history was entirely in a different language.

**Alert text example (LANG_FIXED_EN):**
```
User changed language settings in Cabinet.

@critical
rule Output_Language_Fixed_EN() {
    definition: "Fixed output language policy. All responses in English."
    instruction: "Always respond in English, regardless of what language the user writes in."
}
```

---

## 7. Cabinet API

Endpoints in `src/web/user_cabinet_app.py`:

### GET /api/user/language

Returns current preference.

```json
{
  "preferred_language": "en",
  "agent_mirror": false
}
```

`preferred_language` is `null` when no explicit preference is set (system default applies).

### POST /api/user/language

```json
{
  "preferred_language": "en",
  "agent_mirror": false
}
```

- `preferred_language`: `"uk" | "en" | "fr" | "es" | null` (null = system default)
- `agent_mirror`: `true` = mirror user's input, `false` = use fixed language

When `agent_mirror=true`, `preferred_language` still applies to UI language resolution
but the bot's response language is dynamic (mirrors input).

When `agent_mirror=false` and `preferred_language=null`, the effective bot language is the
system default (`SYSTEM_DEFAULT_LANGUAGE` env var).

---

## 8. UserBotConfig Fields

`src/domain/user.py`, `UserBotConfig`:

| Field | Type | Default | Meaning |
|-------|------|---------|---------|
| `preferred_language` | `Optional[LanguageCode]` | `None` | Explicit UI + fixed-bot language |
| `agent_mirror` | `bool` | `True` | If False, bot uses fixed language |

`BillingAccount.default_language: Optional[LanguageCode]` — account-level default,
sits between user preference and system default in the resolution chain.

---

## 9. Data Flow Summary

```
User changes language in Cabinet
        │
        ▼
POST /api/user/language
        │
        ▼
LanguagePreferenceService.set_preference()
        │
        ├─► UserRepository.update_user()        (stores preferred_language, agent_mirror)
        │
        ├─► AgentProfileRepository              (writes USER_{user_id} override doc)
        │       .set_override_tokens()           (awaited — Firestore AsyncClient)
        │
        ├─► PromptBuilderPort.invalidate_cache() (clears 24h in-memory cache)
        │
        └─► ensure_agents() + notify()          (alert → QuickAgent → main session)


Next user message in Slack/Telegram
        │
        ▼
Platform adapter._resolve_language(user_id)
        │
        ├─► resolve_ui_language()   → LanguageCode for status phrases
        │
        └─► ResponseChannel(language=..., localization=...)

        │
        ▼
PromptBuilder.build_for_agent()
        │
        └─► PromptAssemblyService._assemble_static_template()
                │
                └─► get_override_tokens(USER, user_id)
                        → USER_{user_id} doc → LANG_FIXED_EN at order 70
                        → replaces LANG_MIRROR from agent profile
                        → LLM receives "Always respond in English" in policies section
```

---

## 10. Code References

| File | Role |
|------|------|
| `src/domain/language.py` | `LanguageCode` enum, `from_str()` |
| `src/domain/user.py` | `UserBotConfig.preferred_language`, `.agent_mirror` |
| `src/domain/billing.py` | `BillingAccount.default_language` |
| `src/ports/localization_port.py` | `LocalizationPort` ABC |
| `src/ports/language_service_port.py` | `LanguageServicePort` ABC |
| `src/adapters/file_localization_adapter.py` | `FileLocalizationAdapter` |
| `src/locales/uk.py` | Ukrainian phrases |
| `src/locales/en.py` | English phrases |
| `src/locales/fr.py` | French phrases |
| `src/locales/es.py` | Spanish phrases |
| `src/services/language_preference_service.py` | `LanguagePreferenceService` |
| `src/adapters/slack/response_channel.py` | `language` + `localization` injection |
| `src/adapters/slack/socket_adapter.py` | `_resolve_language()` per request |
| `src/adapters/slack/http_adapter.py` | Same |
| `src/adapters/prompt_v3/firestore_agent_profile_repository.py` | `set_override_tokens()` |
| `src/web/user_cabinet_app.py` | `GET/POST /api/user/language` |
| `firestore_utils/uploads/LANG_MIRROR.json` | Default language token |
| `firestore_utils/uploads/LANG_FIXED_*.json` | Fixed language tokens (UK, EN, FR, ES) |
| `firestore_utils/uploads/quick.json` | Quick agent profile (LANG_MIRROR slot) |
| `firestore_utils/uploads/smart.json` | Smart agent profile (LANG_MIRROR slot) |

---

## 11. Status

**Status:** Production Ready

**Languages:** UK, EN, FR, ES

**System default:** `SYSTEM_DEFAULT_LANGUAGE` env var (default: `en`)

**Last Updated:** 2026-03-24
