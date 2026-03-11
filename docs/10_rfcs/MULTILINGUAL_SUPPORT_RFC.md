# RFC: Multilingual Support — Hexagonal Localization Architecture

**Status:** DRAFT
**Created:** 2026-02-21
**Scope:** Domain, Ports, Adapters, Services, Web Cabinet, ResponseChannel, PromptBuilder
**Goal:** First-class language support that can be extended without rearchitecting.

---

## 1. Problem Statement

The codebase has dormant localization infrastructure: `language: str = "uk"` in
`PromptPreferences`, and locale files `src/locales/uk.py` / `en.py`. None of it is
wired. Three violations block clean extension:

**1. No port abstraction for localization.**
`SlackResponseChannel` and `TelegramResponseChannel` import locale modules directly:
```python
from ...locales.uk import get_message as get_uk_message, get_entertainment_intros
```
Coupling adapter to a concrete locale strategy. Adding a language means touching every
ResponseChannel.

**2. Language is a raw `str` with no type safety.**
`PromptPreferences.language = "uk"` — unvalidated, no IDE completion, silent failure
on typo.

**3. Language is one-dimensional.**
Real users have distinct needs that a single field cannot express:
- Bilingual users who switch languages and expect the bot to follow (mirror).
- Users who want the bot to always respond in one specific language (fixed).
- Users who want a custom UI language but still mirror in responses.

---

## 2. Goals

- **G1** — Adding a new language: one locale file + one enum entry. Nothing else.
- **G2** — Swapping locale source (files → Firestore): one new adapter. Nothing else.
- **G3** — Language is a typed domain value. Raw strings only at system boundaries.
- **G4** — Single write (`UserProfile`) drives both UI and agent behavior. No dual-write.
- **G5** — `LanguagePreferenceService` is the sole extension point for side-effects.

## 3. Non-Goals

- Language auto-detection from message content (additive, no design changes needed).
- Per-conversation language override.
- Prompt_v3 profile slots for language — PromptBuilder receives language as explicit
  parameter and resolves the token itself.

---

## 4. Domain Model

### 4.1 `LanguageCode` — Supported Languages

```python
# src/domain/language.py
from enum import Enum

class LanguageCode(str, Enum):
    """Supported bot interface languages.

    Adding a new language:
      1. Add entry here.
      2. Create src/locales/{code}.py (copy en.py structure).
      3. Register in FileLocalizationAdapter._REGISTRY.
      4. Add LANG_FIXED_{CODE} token to Firestore (migration script).
      Done — zero other changes.
    """
    UK = "uk"
    EN = "en"
    FR = "fr"
    ES = "es"

    @classmethod
    def from_str(cls, value: str, default: "LanguageCode" = None) -> "LanguageCode":
        """Safe parser — never raises, falls back to default."""
        try:
            return cls(value.lower())
        except (ValueError, AttributeError):
            return default or cls.UK

    @classmethod
    def is_supported(cls, value: str) -> bool:
        return value in {m.value for m in cls}
```

### 4.2 User language settings — two independent fields

```python
# src/domain/user.py  (inside UserBotConfig)

preferred_language: Optional[LanguageCode] = None
# None   = use system default language for UI (status messages, file prompts)
# Set    = override system default with this language for UI

agent_mirror: bool = True
# True   = bot responds in the same language the user writes in
# False  = bot always responds in effective language (preferred_language or system default)
```

These replace the old `language: str = "uk"` in `PromptPreferences`.
`PromptPreferences` retains prompt-specific fields (kernel, examples, anchors, vibe).

### 4.3 Three user-facing modes

| `preferred_language` | `agent_mirror` | UI language | Agent behavior |
|---|---|---|---|
| `None` | `True` | system default | mirrors user's input |
| `EN` | `True` | EN | mirrors user's input |
| `EN` | `False` | EN | always EN |
| `None` | `False` | system default | always system default lang |

Modes 1–3 are the primary user-facing modes. Mode 4 (no override + fixed) is valid
and handled naturally — no special case needed.

---

## 5. Language Resolution Chain

### 5.1 System Default — configuration, not a constant

```python
# src/config/settings.py
class Settings(BaseModel):
    ...
    system_default_language: LanguageCode = LanguageCode.EN
```

Loaded from environment (`SYSTEM_DEFAULT_LANGUAGE=en`). `LanguageCode.EN` is the
baseline — most neutral for a multilingual system.

### 5.2 Account-level override

```python
# src/domain/billing.py  (inside BillingAccount)
default_language: Optional[LanguageCode] = None
# None   = use system config default
# Set    = override system default for all users in this account
# No UI — set directly in Firestore when needed per account.
```

### 5.3 Resolution chain: USER → ACCOUNT → SYSTEM

```
preferred_language (UserBotConfig)
    ↓ None
account.default_language (BillingAccount)
    ↓ None
settings.system_default_language (Settings / env)
```

Resolution is performed by `LanguagePreferenceService.resolve_ui_language()` —
**not** inline in adapters. Adapters call the service.

```python
# src/services/language_preference_service.py
async def resolve_ui_language(self, user_id: str) -> LanguageCode:
    """
    Resolve effective UI language for a user.

    Chain: USER preferred_language → ACCOUNT default_language → SYSTEM default.
    """
    user = await self._user_repo.get_user(user_id)
    if user and user.config.preferred_language:
        return user.config.preferred_language

    if user and user.account_id:
        account = await self._account_repo.get_account(user.account_id)
        if account and account.default_language:
            return account.default_language

    return self._settings.system_default_language
```

`LanguagePreferenceService` gains `account_repo: AccountRepository` and
`settings: Settings` in its constructor.

---

## 6. Language Token Resolution

PromptBuilder resolves the effective language and maps to a Firestore token ID.
The mapping is deterministic — no Firestore lookup needed to know the ID.

```python
# src/domain/language.py
def resolve_lang_token_id(
    preferred_language: Optional[LanguageCode],
    agent_mirror: bool,
    system_default: LanguageCode,
) -> str:
    """
    Resolve Firestore token ID from user language settings.

    Token inventory (N languages + 1):
      LANG_MIRROR
      LANG_FIXED_EN
      LANG_FIXED_UK
      LANG_FIXED_FR
      LANG_FIXED_ES
      ... (one per LanguageCode)
    """
    if agent_mirror:
        return "LANG_MIRROR"
    effective = preferred_language or system_default
    return f"LANG_FIXED_{effective.value.upper()}"
```

This function lives in `src/domain/language.py` — pure logic, no I/O, no imports
beyond stdlib. Testable with no mocks.

---

## 7. Language Tokens in Firestore

Tokens live in the **system tokens collection** alongside all other tokens.
Total: N languages + 1 = 5 tokens currently.

```
Token ID        Category          Content
────────────────────────────────────────────────────────────────────────────
LANG_MIRROR     output_language   "Respond in the same language the user
                                   writes in. Mirror their language exactly.
                                   Do not switch unless they do."

LANG_FIXED_UK   output_language   "Always respond in Ukrainian (uk),
                                   regardless of what language the user
                                   writes in."

LANG_FIXED_EN   output_language   "Always respond in English,
                                   regardless of what language the user
                                   writes in."

LANG_FIXED_FR   output_language   "Always respond in French (français),
                                   regardless of what language the user
                                   writes in."

LANG_FIXED_ES   output_language   "Always respond in Spanish (español),
                                   regardless of what language the user
                                   writes in."
```

Adding language DE: create `LANG_FIXED_DE`. No other Firestore changes.

---

## 8. Port: `LocalizationPort` (UI strings only)

```python
# src/ports/localization_port.py
from abc import ABC, abstractmethod
from typing import List
from ..domain.language import LanguageCode
from ..domain.ui_messages import StatusType


class LocalizationPort(ABC):
    """
    Abstract interface for UI string localization.

    Scope: status messages ("Thinking..."), file prompts, entertainment intros.
    NOT for agent response language — that is PromptBuilderPort's concern.

    Justification for port:
    - 2+ implementations plausible: file-based (now), Firestore-based (future).
    - Application layer must not depend on locale file structure.
    - Enables deterministic test doubles.
    """

    @abstractmethod
    def get_status_phrases(self, lang: LanguageCode, status: StatusType) -> List[str]:
        """All phrase variants for a status type. Caller picks one at random."""

    @abstractmethod
    def get_entertainment_intros(self, lang: LanguageCode) -> List[str]:
        """Intro phrases for the web-search entertainment message."""

    @abstractmethod
    def get_file_prompt(self, lang: LanguageCode, mime_type: str) -> str:
        """Prompt to use when user sends a file without text."""
```

---

## 9. Adapter: `FileLocalizationAdapter`

```python
# src/adapters/file_localization_adapter.py
from ..ports.localization_port import LocalizationPort
from ..domain.language import LanguageCode
from ..domain.ui_messages import StatusType
from ..locales import uk, en, fr, es   # only place in the system that imports locales directly


class FileLocalizationAdapter(LocalizationPort):
    """
    File-backed localization.

    To add a language:
      1. src/locales/{code}.py
      2. LanguageCode.{CODE} in domain/language.py
      3. Entry in _REGISTRY below
      Done.
    """

    _REGISTRY = {
        LanguageCode.UK: uk,
        LanguageCode.EN: en,
        LanguageCode.FR: fr,
        LanguageCode.ES: es,
    }
    _DEFAULT = en  # matches system default language

    def _module(self, lang: LanguageCode):
        return self._REGISTRY.get(lang, self._DEFAULT)

    def get_status_phrases(self, lang, status):
        return self._module(lang).get_message(status)

    def get_entertainment_intros(self, lang):
        return self._module(lang).ENTERTAINMENT_INTROS

    def get_file_prompt(self, lang, mime_type):
        mod = self._module(lang)
        if "image"    in mime_type: return mod.FILE_FALLBACK_IMAGE
        if "video"    in mime_type: return mod.FILE_FALLBACK_VIDEO
        if "pdf"      in mime_type: return mod.FILE_FALLBACK_PDF
        if "document" in mime_type or "text/" in mime_type: return mod.FILE_FALLBACK_DOCUMENT
        return mod.FILE_FALLBACK_GENERIC
```

---

## 10. PromptBuilderPort — language as explicit parameter

```python
# src/ports/prompt_builder_port.py
@abstractmethod
async def build_for_agent(
    self,
    agent_type: str,
    user_id: Optional[str] = None,
    account_id: Optional[str] = None,
    routing_metadata: Optional[RoutingMetadata] = None,
    capabilities: Optional[ProviderCapabilities] = None,
    biographical_facts: Optional[List[Dict]] = None,
    conversation_history: Optional[List[dict]] = None,
    preferred_language: Optional[LanguageCode] = None,   # NEW
    agent_mirror: bool = True,                            # NEW
) -> str: ...
```

`PromptAssemblyService.assemble()` implementation:

```python
async def assemble(self, ..., preferred_language=None, agent_mirror=True):
    # Cache key includes language state — stale cache evicted naturally on change
    cache_key = self._build_cache_key(agent_type, account_id, user_id,
                                       preferred_language, agent_mirror)
    ...
    # After static template assembly:
    token_id = resolve_lang_token_id(
        preferred_language=preferred_language,
        agent_mirror=agent_mirror,
        system_default=settings.system_default_language,
    )
    lang_token = await self.token_repo.get(token_id)
    assembled = _inject_lang_directive(assembled, lang_token.content)
    # Position of injection: PromptBuilder's responsibility, defined in prompt design session
```

---

## 11. Service: `LanguagePreferenceService`

```python
# src/services/language_preference_service.py
from ..ports.user_repository import UserRepository
from ..ports.prompt_builder_port import PromptBuilderPort
from ..domain.language import LanguageCode
from ..utils.logger import logger
from typing import Optional


class LanguagePreferenceService:
    """
    Single write path for language preference changes.

    Writes ONLY to UserProfile. Both UI (LocalizationPort) and agent (PromptBuilderPort)
    read from UserProfile — no dual-write, no profile slots.

    Extension point: add side-effects here, callers never change.
    """

    def __init__(
        self,
        user_repo: UserRepository,
        account_repo: AccountRepository,
        prompt_builder: PromptBuilderPort,
        settings: Settings,
    ):
        self._user_repo = user_repo
        self._account_repo = account_repo
        self._prompt_builder = prompt_builder
        self._settings = settings

    async def set_preference(
        self,
        user_id: str,
        preferred_language: Optional[LanguageCode],
        agent_mirror: bool,
    ) -> None:
        user = await self._user_repo.get_user(user_id)
        if not user:
            raise ValueError(f"User not found: {user_id}")

        user.config.preferred_language = preferred_language
        user.config.agent_mirror = agent_mirror
        await self._user_repo.update_user(user)
        logger.info(
            f"Language preference updated: user={user_id} "
            f"preferred={preferred_language} mirror={agent_mirror}"
        )

        # Defensive cache invalidation — preferred_language/agent_mirror are now
        # in cache key, so this is belt-and-suspenders against 24h stale cache.
        self._prompt_builder.invalidate_cache()

    async def get_preference(self, user_id: str) -> tuple[Optional[LanguageCode], bool]:
        """Returns (preferred_language, agent_mirror). Defaults: (None, True)."""
        user = await self._user_repo.get_user(user_id)
        if not user:
            return None, True
        return user.config.preferred_language, user.config.agent_mirror

    async def resolve_ui_language(self, user_id: str) -> LanguageCode:
        """
        Resolve effective UI language.

        Chain: USER preferred_language → ACCOUNT default_language → SYSTEM default.
        Called once per request by platform adapters.
        """
        user = await self._user_repo.get_user(user_id)
        if user and user.config.preferred_language:
            return user.config.preferred_language

        if user and user.account_id:
            account = await self._account_repo.get_account(user.account_id)
            if account and account.default_language:
                return account.default_language

        return self._settings.system_default_language
```

---

## 12. Data Flow

```
UserProfile.config
  ├── preferred_language: Optional[LanguageCode]
  └── agent_mirror: bool

         │ resolved once per request in platform adapter
         │
         ├──[effective UI lang]──────────────────────────────────────────────────────┐
         │   = preferred_language or SYSTEM_DEFAULT_LANGUAGE                        │
         │                                                                           ▼
         │                                                             ResponseChannel(language=ui_lang)
         │                                                               └──→ LocalizationPort
         │                                                                     └──→ FileLocalizationAdapter
         │                                                                           └──→ src/locales/{code}.py
         │
         └──[preferred_language + agent_mirror]────────────────────────────────────────┐
                                                                                        ▼
                                                            PromptBuilderPort.build_for_agent(
                                                                preferred_language=...,
                                                                agent_mirror=...,
                                                            )
                                                              └──→ resolve_lang_token_id(...)
                                                                    └──→ "LANG_MIRROR" | "LANG_FIXED_EN" | ...
                                                                          └──→ token_repo.get(token_id)
                                                                                └──→ injected into system prompt
```

**Single write. Two consumers. No duplication of logic.**

---

## 13. Integration Points

### 13.1 Platform Adapters — resolve once via service, build both consumers

```python
# socket_adapter.py, after user_profile = decision.user
# Resolution delegated to service — adapter has no resolution logic
ui_lang      = await self._language_service.resolve_ui_language(user_profile.user_id)
agent_mirror = user_profile.config.agent_mirror

context = MessageContext(
    ...,
    language=ui_lang.value,           # str in DTO
)
response_channel = SlackResponseChannel(
    self.app.client, message["channel"], self.slack_bot_token,
    language=ui_lang,
    localization=self._localization,  # singleton injected at adapter init
)
# preferred_language + agent_mirror forwarded via AgentMessage.context for PromptBuilder
```

`self._language_service: LanguagePreferenceService` is injected into the adapter
from `ServiceContainer`. One async call per request — negligible overhead, and
cacheable later if needed.

### 13.2 ResponseChannel

```python
class SlackResponseChannel(ResponseChannel):
    def __init__(self, app_client, channel_id, bot_token,
                 language: LanguageCode,
                 localization: LocalizationPort):
        self.language = language
        self.localization = localization

    async def send_status(self, status_type, thread_id=None):
        phrases = self.localization.get_status_phrases(self.language, status_type)
        phrase = random.choice(phrases)
        ...
```

`language` here is always the effective UI language (`preferred_language or system_default`).
Same for `TelegramResponseChannel`.

### 13.3 ConversationHandler — file prompts + forwarding

```python
class ConversationHandler:
    def __init__(self, ..., localization: LocalizationPort):
        self._localization = localization

    # File without text — use UI language for the file prompt
    ui_lang = LanguageCode.from_str(context.language)
    context.text = self._localization.get_file_prompt(ui_lang, attachment.mime_type)

    # Forward to AgentMessage.context for downstream PromptBuilder use
    agent_context = {
        ...,
        "preferred_language": user_profile.config.preferred_language,
        "agent_mirror": user_profile.config.agent_mirror,
    }
```

### 13.4 Agent layer — `forwards_language_preference` class attribute

Not all agents communicate with users. `WebSearchAgent` serves other agents and its
facts are always stored in English. Language forwarding is currently needed only for
`QuickResponseAgent` and `SmartResponseAgent` — but tomorrow it may be needed for
others. This must not require touching unrelated agent code.

**Rule:** each agent type declares whether it forwards language params as a
**class-level attribute on `BaseAgent`**, defaulting to `False` (safe opt-in).

```python
# src/agents/core/base_agent.py
class BaseAgent(ABC):
    # Subclasses that generate user-visible responses declare True.
    # Agents that serve other agents (WebSearch, Memory, Consolidation) leave False.
    forwards_language_preference: ClassVar[bool] = False
```

```python
# src/agents/core/quick_response_agent.py
class QuickResponseAgent(BaseAgent):
    forwards_language_preference: ClassVar[bool] = True   # talks to user

# src/agents/core/smart_response_agent.py
class SmartResponseAgent(BaseAgent):
    forwards_language_preference: ClassVar[bool] = True   # talks to user

# src/agents/core/web_search_agent.py
class WebSearchAgent(BaseAgent):
    forwards_language_preference: ClassVar[bool] = False  # serves other agents, not users
```

`BaseAgent` uses the flag when calling `build_for_agent()`:

```python
# src/agents/core/base_agent.py
async def _build_prompt(self, message, ...) -> str:
    kwargs = {}
    if self.forwards_language_preference:
        kwargs["preferred_language"] = message.context.get("preferred_language")
        kwargs["agent_mirror"]       = message.context.get("agent_mirror", True)

    return await self._prompt_builder.build_for_agent(
        agent_type=self.agent_type,
        ...,
        **kwargs,
    )
```

This is declarative: the flag is visible at the top of each agent class. Adding a new
agent forces a conscious choice. Changing behavior for one agent is a single-line
attribute change — no logic to hunt down.

### 13.5 Cabinet API

```python
@bp.route("/api/user/language", methods=["POST"])
@auth_required
async def update_language(user_id: str):
    data = await request.get_json()

    lang_str     = data.get("preferred_language")  # optional, null = system default
    agent_mirror = data.get("agent_mirror", True)

    # Boundary validation
    preferred_language = None
    if lang_str is not None:
        if not LanguageCode.is_supported(lang_str):
            return jsonify({"error": f"Unsupported language: {lang_str}"}), 400
        preferred_language = LanguageCode(lang_str)

    if not isinstance(agent_mirror, bool):
        return jsonify({"error": "agent_mirror must be boolean"}), 400

    await language_service.set_preference(user_id, preferred_language, agent_mirror)
    return jsonify({
        "preferred_language": preferred_language.value if preferred_language else None,
        "agent_mirror": agent_mirror,
    }), 200
```

### 13.6 Cabinet UI

```
┌─────────────────────────────────────────────────────┐
│  Bot Language                                        │
│                                                      │
│  UI Language:                                        │
│  [ System default ]  [ 🇺🇦 UK ][ 🇬🇧 EN ]        │
│                      [ 🇫🇷 FR ][ 🇪🇸 ES ]        │
│                                                      │
│  Bot responds:                                       │
│  [● Mirror my language ]  [ Fixed (UI language) ]    │
└─────────────────────────────────────────────────────┘
```

"System default" clears `preferred_language` → `null` in the API call.
"Fixed" uses whatever UI language is currently selected.

---

## 14. Locale File Structure

Pure data modules — no logic, no external imports.

```python
# src/locales/fr.py
"""French UI messages. Style: ironic, warm, slightly self-deprecating — same as uk/en."""
from typing import Dict, List
from ..domain.ui_messages import StatusType

ENTERTAINMENT_INTROS: List[str] = [
    "Divertis-toi pendant que je fouille le web",
    "Pendant que je cherche — prends une pause ironique",
    "Occupe ton cerveau pendant que le mien cherche",
    "Une petite pause factuelle pendant que je googlelise",
    "Détends-toi, c'est une recherche, pas un vol pour Mars",
    "Garde cette petite histoire pendant que je suis en route",
    "Pendant que je farfouille — un mini-dessert pour toi",
]

FILE_FALLBACK_IMAGE    = "Qu'y a-t-il sur cette photo?"
FILE_FALLBACK_VIDEO    = "Que se passe-t-il dans cette vidéo?"
FILE_FALLBACK_PDF      = "Parle-moi de ce document"
FILE_FALLBACK_DOCUMENT = "Qu'y a-t-il dans ce fichier?"
FILE_FALLBACK_GENERIC  = "Regarde ce fichier"

FR_MESSAGES: Dict[str, List[str]] = {
    StatusType.THINKING.value: [
        "Je réfléchis à votre question... c'est douloureux",
        "Synchronisation des neurones en cours",
        "Consultation de mes profondeurs cognitives",
        "Construction de chaînes logiques... espérons qu'elles tiennent",
        "Activation des modules cognitifs à plein régime",
        "J'essaie de ne pas surchauffer face à vos idées brillantes",
        "Consultation du noyau de ma personnalité",
        "Je rassemble mes pensées (elles s'éparpillent)",
    ],
    StatusType.SEARCHING_MEMORY.value: [
        "Je fouille vos archives... ça devrait être quelque part",
        "Plongée dans l'océan de vos souvenirs",
        "Interrogation de mon bibliothécaire intérieur",
        "Extraction de souvenirs des recoins les plus sombres",
        "Inventaire de vos connaissances en cours",
        "Je cherche une aiguille dans votre pile de mémoire",
        "Je feuillette vos archives mentales",
    ],
    StatusType.SEARCHING_WEB.value: [
        "Je plonge dans l'internet sauvage... croisez les doigts",
        "Je googlelise comme si ma vie en dépendait",
        "Exploration des horizons numériques",
        "Consultation des esprits savants du réseau",
        "Je chasse les faits frais sur le web",
        "Je me fraie un chemin dans le bruit informationnel",
        "En quête de réponses dans la toile mondiale",
    ],
    StatusType.PROCESSING_FILE.value: [
        "Analyse de vos fichiers en cours",
        "Décomposition du document en atomes",
        "Étude de vos pièces jointes",
    ],
    StatusType.ERROR.value: [
        "Aïe! Mes neurones se sont emmêlés",
        "Quelque chose s'est mal passé... probablement Mercure rétrograde",
        "Une erreur s'est produite, mais je m'en remettrai (un jour)",
        "Mon processeur interne dit 'oups'",
        "Il semble que j'aie trop compliqué les choses",
        "Le système est tombé dans une crise existentielle",
        "Erreur 404 : Mon cerveau est introuvable",
    ],
}

def get_message(status_type: StatusType, overrides: Dict = None) -> List[str]:
    if overrides and status_type.value in overrides:
        return overrides[status_type.value]
    return FR_MESSAGES.get(status_type.value, ["Traitement..."])
```

`es.py` follows the same structure in Spanish.

---

## 15. Extension Analysis

### Adding language DE
1. `src/locales/de.py`
2. `LanguageCode.DE = "de"`
3. Entry in `FileLocalizationAdapter._REGISTRY`
4. `LANG_FIXED_DE` token in Firestore
5. Button in `cabinet.html`

**Zero other changes.**

### Account-level default language
See Section 5 — already implemented in the resolution chain.

### Language auto-detection (future)
RouterAgent detects language from message → passes `detected_language: LanguageCode`
in `RoutingMetadata`. In mirror mode, PromptBuilder uses detected language to pick
`LANG_FIXED_{detected}` instead of `LANG_MIRROR` for more precise instruction.
Additive — no existing interface changes.

---

## 16. What NOT to Do

**Do not** import from `src/locales/` anywhere except `FileLocalizationAdapter`.

**Do not** write a prompt_v3 profile slot for language. Language reaches PromptBuilder
as explicit parameters. Profile slots are for personality and style tokens.

**Do not** resolve effective language in multiple places. One resolution point:
platform adapter, after `user_profile = decision.user`.

**Do not** create a `LANG_FIXED_SYSTEM_DEFAULT` token. PromptBuilder resolves
`preferred_language or system_default` to get the effective code, then maps to
the existing `LANG_FIXED_{CODE}` token.

**Do not** hardcode `"en"` or any language string in application code.
Use `settings.system_default_language` everywhere.

**Do not** decide inline in each agent whether to extract language from context.
Use the `forwards_language_preference: ClassVar[bool]` attribute on `BaseAgent`.
Each agent's intent is explicit, and the forwarding logic lives in exactly one place.

---

## 17. Implementation Order

1. `src/domain/language.py` — `LanguageCode` + `resolve_lang_token_id()`
2. `src/config/settings.py` — `system_default_language: LanguageCode`
3. `src/ports/localization_port.py`
4. `src/locales/fr.py`, `src/locales/es.py`
5. `src/adapters/file_localization_adapter.py`
6. `src/domain/user.py` — replace `language: str` with `preferred_language` + `agent_mirror`
7. `src/domain/messaging.py` — `MessageContext.language: str` (effective UI lang, str in DTO)
8. `src/ports/prompt_builder_port.py` — add `preferred_language`, `agent_mirror` params
9. `src/services/prompt_v3/prompt_assembly_service.py` — lang token injection + cache key
10. `scripts/migrations/add_lang_tokens.py` — LANG_MIRROR + LANG_FIXED_* to Firestore
11. `src/services/language_preference_service.py`
12. `src/adapters/slack/response_channel.py` — `LocalizationPort` + `language` injection
13. `src/adapters/telegram/response_channel.py` — same
14. `src/adapters/slack/socket_adapter.py` — resolve, build context
15. `src/adapters/slack/http_adapter.py` — same
16. Telegram adapter — same
17. `src/handlers/conversation_handler.py` — port injection + forwarding
18. `src/agents/core/base_agent.py` — add `forwards_language_preference: ClassVar[bool] = False` + `_build_prompt()` forwarding logic; set `True` on `QuickResponseAgent` and `SmartResponseAgent`
19. `src/web/user_cabinet_app.py` — language endpoint
20. `src/web/static/cabinet.html` — UI section
21. `src/composition/service_container.py` — wire everything
22. Tests

---

## 18. Files Summary

| File | Status | Notes |
|------|--------|-------|
| `src/domain/language.py` | **NEW** | `LanguageCode` + `resolve_lang_token_id()` |
| `src/ports/localization_port.py` | **NEW** | UI localization port |
| `src/adapters/file_localization_adapter.py` | **NEW** | File-backed implementation |
| `src/services/language_preference_service.py` | **NEW** | Single write path |
| `src/locales/fr.py` | **NEW** | French phrases |
| `src/locales/es.py` | **NEW** | Spanish phrases |
| `src/config/settings.py` | **MODIFY** | `system_default_language: LanguageCode` |
| `src/domain/user.py` | **MODIFY** | `preferred_language` + `agent_mirror` on `UserBotConfig` |
| `src/domain/billing.py` | **MODIFY** | `default_language: Optional[LanguageCode]` on `BillingAccount` |
| `src/domain/messaging.py` | **MODIFY** | `language: str` (effective UI lang) |
| `src/ports/prompt_builder_port.py` | **MODIFY** | `preferred_language` + `agent_mirror` params |
| `src/services/prompt_v3/prompt_assembly_service.py` | **MODIFY** | Lang token injection + cache key |
| `src/adapters/slack/response_channel.py` | **MODIFY** | Port + language injection |
| `src/adapters/telegram/response_channel.py` | **MODIFY** | Same |
| `src/adapters/slack/socket_adapter.py` | **MODIFY** | Language resolution |
| `src/adapters/slack/http_adapter.py` | **MODIFY** | Same |
| `src/agents/core/base_agent.py` | **MODIFY** | `forwards_language_preference` + `_build_prompt()` forwarding |
| `src/agents/core/quick_response_agent.py` | **MODIFY** | `forwards_language_preference = True` |
| `src/agents/core/smart_response_agent.py` | **MODIFY** | `forwards_language_preference = True` |
| `src/handlers/conversation_handler.py` | **MODIFY** | Port injection + forwarding |
| `src/web/user_cabinet_app.py` | **MODIFY** | Language endpoint |
| `src/web/static/cabinet.html` | **MODIFY** | UI section |
| `src/composition/service_container.py` | **MODIFY** | Wire all new components |
| `src/locales/__init__.py` | **SIMPLIFY** | Dispatch logic moves to adapter |
| `scripts/migrations/add_lang_tokens.py` | **NEW** | LANG_MIRROR + LANG_FIXED_* |
| `tests/unit/ports/test_localization_port.py` | **NEW** | Port contract |
| `tests/unit/test_language_preference_service.py` | **NEW** | Service unit tests |
| `tests/unit/test_file_localization_adapter.py` | **NEW** | Adapter unit tests |
