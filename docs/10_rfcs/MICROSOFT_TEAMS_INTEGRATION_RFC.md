# RFC: Microsoft Teams Platform Integration

**Status:** PROPOSED  
**Date:** 2026-04-07  
**Owner:** Solo dev  
**Milestone:** Platform extensibility  
**Related:** Telegram adapter (2026-02-09), `docs/04_solution_strategy/target_architecture/TARGET_ARCHITECTURE.md`

---

## 1. Problem Statement

The system currently supports Slack (primary, full-featured) and Telegram (secondary, webhook-based).
The family has a Microsoft 365 Family plan with Teams included. Adding Teams as a third platform
eliminates the "install another app" friction — Teams is already on everyone's devices.

**Why the architecture supports this:**
- The Telegram integration (2026-02-09) validated the adapter pattern: new platform = new adapter package,
  zero changes to domain/ports/services/agents/handlers.
- All ports (`PlatformPort`, `ResponseChannel`, `PlatformMediaPort`, `PlatformAuthPort`,
  `DedupStore`, `NotificationChannelFactoryPort`) are stable and platform-agnostic.
- `ConversationHandler` never imports platform-specific code — it only operates through
  `MessageContext` + `ResponseChannel`.

---

## 2. Solution Architecture

### 2.1 High-Level Component Diagram

```
                     ┌─────────────────────────────────────────────────────┐
                     │                  main.py (Quart)                    │
                     │                                                     │
                     │  /slack/events ──► SlackHTTPAdapter                 │
                     │  /telegram/webhook ──► TelegramWebhookAdapter      │
                     │  /teams/messages ──► TeamsWebhookAdapter      [NEW]│
                     │  /worker ──► WorkerHandler                         │
                     │  /auth/* ──► OAuth                                 │
                     │  /cabinet ──► Cabinet                              │
                     └─────────────────────────────────────────────────────┘
                                           │
                     ┌─────────────────────┼─────────────────────────┐
                     │  Composition Layer                            │
                     │  TeamsAdapterFactory.create_adapter()    [NEW]│
                     └─────────────────────┼─────────────────────────┘
                                           │
       ┌───────────────────────────────────┼───────────────────────────────┐
       │                  Adapter Layer (NEW)                              │
       │  src/adapters/teams/                                              │
       │  ├── __init__.py                                                  │
       │  ├── webhook_adapter.py    (TeamsWebhookAdapter : PlatformPort)   │
       │  ├── response_channel.py   (TeamsResponseChannel : ResponseCh.)   │
       │  └── media_adapter.py      (TeamsMediaAdapter : PlatformMediaP.)  │
       └───────────────────────────────────┼───────────────────────────────┘
                                           │
       ┌───────────────────────────────────┼───────────────────────────────┐
       │                  Ports (NO CHANGES)                               │
       │  PlatformPort, ResponseChannel, PlatformMediaPort,               │
       │  PlatformAuthPort, DedupStore, NotificationChannelFactoryPort     │
       └───────────────────────────────────────────────────────────────────┘
```

### 2.2 What Is Reused vs. New

| Component | Status | Notes |
|-----------|--------|-------|
| PlatformPort ABC | REUSED | `start/stop/get_platform_name/_translate_platform_files` |
| ResponseChannel Protocol | REUSED | All 19 methods implemented by TeamsResponseChannel |
| PlatformMediaPort ABC | REUSED | `upload_image/upload_file` implemented by TeamsMediaAdapter |
| PlatformAuthPort / IAMService | REUSED | `authorize("teams", platform_user_id=...)` — already generic |
| DedupStore / FirestoreDedupStore | REUSED | Namespace key: `teams::{activity_id}` |
| NotificationChannelFactory | REUSED | `register_factory("teams", lambda ...)` |
| ConversationHandler | REUSED | Platform-agnostic; receives MessageContext + ResponseChannel |
| RichContentService | REUSED | Platform-agnostic media delivery |
| MessageChunker | REUSED | Platform-agnostic text splitting |
| UserNotificationService | REUSED | `save_channel/notify` already platform-agnostic |
| Platform linking (Cabinet) | REUSED | `link-platform` POST already accepts any platform string |
| `src/adapters/teams/` | **NEW** | 3 files: webhook_adapter, response_channel, media_adapter |
| `src/composition/teams_adapter_factory.py` | **NEW** | Factory wiring |
| `src/config/environment.py` | MODIFIED | Add `validate_teams_config()` |
| `main.py` | MODIFIED | Add Teams blueprint registration block (mirrors Telegram block) |
| `requirements.txt` | MODIFIED | Add `botbuilder-core`, `botbuilder-integration-aiohttp` |

---

## 3. Azure Bot Registration

### 3.1 Setup Steps

1. Azure Portal > "Azure Bot" resource > Create.
2. Choose **Multi Tenant** app type (consumers + organizations — required for personal Microsoft accounts).
3. Note the generated **App ID** (Client ID).
4. Create a **Client Secret** in the App Registration's "Certificates & secrets" blade.
5. In the Bot resource's "Configuration" blade, set **Messaging endpoint** to:
   `https://<CLOUD_RUN_URL>/teams/messages`.
6. In the Bot resource's "Channels" blade, add "Microsoft Teams" channel.
7. Install the bot as a Teams app via Developer Portal or sideload a manifest.

### 3.2 Teams App Manifest (for sideloading)

```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/teams/v1.14/MicrosoftTeams.schema.json",
  "manifestVersion": "1.14",
  "id": "<App ID from Azure>",
  "version": "1.0.0",
  "developer": { "name": "Alek Bot" },
  "name": { "short": "Alek", "full": "Alek Personal Assistant" },
  "description": { "short": "Family AI assistant", "full": "..." },
  "bots": [{
    "botId": "<App ID>",
    "scopes": ["personal"],
    "supportsFiles": true
  }]
}
```

### 3.3 Configuration Variables

```
TEAMS_APP_ID=<Azure Bot App ID / Client ID>
TEAMS_APP_PASSWORD=<Azure Bot Client Secret>
```

Loaded via `validate_teams_config()` in `src/config/environment.py`.
Stored in GCP Secret Manager for production.

---

## 4. Teams Adapter Layer

### 4.1 `src/adapters/teams/webhook_adapter.py` — TeamsWebhookAdapter

**Pattern:** Mirrors `TelegramWebhookAdapter`. Blueprint-based, stateless, Cloud Run compatible.

**SDK choice:** `botbuilder-core` + `botbuilder-integration-aiohttp`. The SDK handles:
- JWT token validation (Azure AD issuer verification, audience check against App ID)
- Activity parsing (typed Python objects)
- Bot Framework Connector Service authentication (OAuth token refresh for outbound API calls)
- `turn_context.send_activity()` / `update_activity()` for responses

**Quart/aiohttp bridge:** The Bot Framework SDK ships with an aiohttp handler.
Since we use Quart (ASGI), we receive the raw HTTP POST in a Quart route, extract
`Authorization` header + JSON body, and call `BotFrameworkAdapter.process_activity()` directly.
This is the officially supported "bring your own server" pattern — `process_activity` is
framework-agnostic, it only needs the activity dict and auth header.

**Fallback if SDK has Quart compat issues:** Manual JWT validation with `PyJWT`:
1. Fetch JWKS from `https://login.botframework.com/v1/.well-known/keys`
2. Decode/verify JWT from `Authorization: Bearer <token>` header
3. Validate claims: `iss` starts with `https://api.botframework.com`, `aud == TEAMS_APP_ID`, `exp` valid
4. Cache JWKS keys with 24h TTL

```python
class TeamsWebhookAdapter(PlatformPort):

    def __init__(
        self,
        app_id: str,
        app_password: str,
        dedup_store,
        session_store,
        conversation_handler: ConversationHandlerPort,
        iam_service: PlatformAuthPort,
        audio_service=None,
        language_service: Optional[LanguageServicePort] = None,
        localization: Optional[LocalizationPort] = None,
    ):
        # Bot Framework adapter handles JWT validation + connector auth
        self._settings = BotFrameworkAdapterSettings(
            app_id=app_id, app_password=app_password,
        )
        self._adapter = BotFrameworkAdapter(self._settings)
        self.blueprint = Blueprint('teams', __name__)
        # Route: POST /messages (registered at /teams prefix in main.py)
        ...

    def get_blueprint(self) -> Blueprint:
        return self.blueprint

    def get_platform_name(self) -> str:
        return "teams"
```

**Webhook handler flow:**

```python
async def _handle_teams_activity(self):
    body = await request.get_json()
    auth_header = request.headers.get("Authorization", "")
    activity = Activity().deserialize(body)
    # process_activity validates JWT and calls _on_turn callback
    response = await self._adapter.process_activity(
        activity, auth_header, self._on_turn
    )
    if response:
        return jsonify(response.body), response.status
    return "", 200

async def _on_turn(self, turn_context: TurnContext):
    activity = turn_context.activity
    if activity.type != ActivityTypes.message:
        return  # Skip ConversationUpdate, etc.

    dedup_key = f"teams::{activity.id}"
    if not await self.dedup_store.try_mark_processed(dedup_key):
        return  # Duplicate

    await self._process_message(turn_context)
```

**Message processing** (mirrors Telegram `_process_message`):

```python
async def _process_message(self, turn_context: TurnContext):
    activity = turn_context.activity

    # Teams user ID: AAD Object ID (stable) or Bot Framework ID
    teams_user_id = (
        activity.from_property.aad_object_id
        or activity.from_property.id
    )
    conversation_id = activity.conversation.id
    text = self._strip_bot_mention(activity.text or "", activity)

    # 1. IAM Authorization
    decision = await self.iam_service.authorize("teams", platform_user_id=teams_user_id)
    if decision.action == "reject":
        rejection_with_id = (
            f"{decision.message}\n\nYour Teams ID: {teams_user_id}\n"
            f"Use this ID to link your account in the Cabinet."
        )
        await turn_context.send_activity(rejection_with_id)
        return

    user_profile = decision.user
    user_id = user_profile.user_id
    account_id = user_profile.account_id

    # 2. Resolve session
    session_id = f"{user_id}:{conversation_id}"

    # 3. Resolve language
    ui_lang = LanguageCode.UK
    preferred_language, agent_mirror = None, True
    if self._language_service:
        ui_lang = await self._language_service.resolve_ui_language(user_id)
        preferred_language, agent_mirror = await self._language_service.get_preference(user_id)

    # 4. Create ResponseChannel
    response_channel = TeamsResponseChannel(
        turn_context=turn_context,
        connector_client=turn_context.adapter.create_connector_client(activity.service_url),
        conversation_id=conversation_id,
        service_url=activity.service_url,
        bot_id=self.app_id,
        language=ui_lang,
        localization=self._localization,
    )

    # 5. Translate attachments
    attachments = []
    if activity.attachments:
        attachments = await self._translate_platform_files(activity.attachments)

    # 6. Create MessageContext
    context = MessageContext(
        text=text,
        session_id=session_id,
        user_id=user_id,
        account_id=account_id,
        language=ui_lang.value,
        attachments=attachments,
        thread_id=activity.reply_to_id,
        metadata={
            "platform": "teams",
            "conversation_id": conversation_id,
            "teams_user_id": teams_user_id,
            "service_url": activity.service_url,
            "preferred_language": preferred_language,
            "agent_mirror": agent_mirror,
        }
    )

    # 7. Call ConversationHandler (platform-agnostic)
    await self.conversation_handler.handle_message(context, response_channel)
```

**Bot mention stripping:**

Teams includes the bot mention as `<at>BotName</at>` entity in `activity.text`.
`_strip_bot_mention(text, activity)` removes it by matching `entity.mentioned.id == app_id`.

**File translation:**

```python
async def _translate_platform_files(self, platform_files: list) -> List[FileAttachment]:
    attachments = []
    for attachment in platform_files:
        content_url = attachment.content_url
        if not content_url:
            continue
        attachments.append(FileAttachment(
            url=content_url,
            mime_type=attachment.content_type or "application/octet-stream",
            filename=attachment.name or "unknown",
            size_bytes=None,  # Teams doesn't provide size in activity
        ))
    return attachments
```

### 4.2 `src/adapters/teams/response_channel.py` — TeamsResponseChannel

**Design decisions:**
- **Message format:** HTML (V1). Teams supports both Markdown and HTML; HTML is more reliable
  for formatting (bold, italic, links, code blocks). `_format_for_platform` converts Markdown → HTML.
- **Message editing:** `update_activity()` via Connector Client. Requires `conversation_id`,
  `activity_id`, `service_url`.
- **Chunking:** Reuses `MessageChunker` with `max_length=3800` (4000 practical limit).
- **Link resolution:** `[N]` anchors → HTML `<a href="url">title</a>`.

```python
TEAMS_MAX_MESSAGE_LENGTH = 4000
TEAMS_CHUNK_SIZE = 3800

class TeamsResponseChannel(ResponseChannel):

    def __init__(
        self,
        turn_context: TurnContext,
        connector_client,
        conversation_id: str,
        service_url: str,
        bot_id: str,
        language: LanguageCode = LanguageCode.UK,
        localization: Optional[LocalizationPort] = None,
    ):
        self._turn_context = turn_context
        self._connector = connector_client
        self._conversation_id = conversation_id
        self._service_url = service_url
        self._bot_id = bot_id
        self.channel_id = conversation_id  # For NotificationService
        self.platform = "teams"
        self.language = language
        self._localization = localization
        self.chunker = MessageChunker(max_length=TEAMS_CHUNK_SIZE)

    @property
    def max_message_length(self) -> int:
        return TEAMS_MAX_MESSAGE_LENGTH

    @property
    def supports_message_editing(self) -> bool:
        return True
```

**Formatting — `_format_for_platform(text) -> str`:**

| Input (Markdown) | Output (HTML) |
|-------------------|---------------|
| `# Title` | `<b>Title</b>` |
| `**bold**` | `<b>bold</b>` |
| `*italic*` / `_italic_` | `<i>italic</i>` |
| `` `code` `` | `<code>code</code>` |
| ` ```block``` ` | `<pre>block</pre>` |
| `- item` | `• item` |
| `[text](url)` | `<a href="url">text</a>` |

**Link resolution — `_resolve_links_teams(text, link_list) -> str`:**

Same logic as Slack: normalize `title [N]` → `[title][N]`, then replace reference-style
`[display][N]` → `<a href="url">display</a>` and bare `[N]` → `<a href="url">title</a>`.

**Core methods:**

| Method | Implementation |
|--------|----------------|
| `send_message(text, thread_id, link_list)` | Resolve links → format HTML → `turn_context.send_activity(Activity(text_format="html"))` → return `activity_id` |
| `update_message(message_id, text, link_list)` | Format → `turn_context.update_activity(Activity(id=message_id))`. Fallback: `send_message` on failure |
| `send_chunked_message(text, message_id, thread_id, link_list)` | Update first chunk, send remaining as new messages |
| `send_flat_response(text, status_message_id)` | Top-level chunks (no threading) |
| `send_rich_content(content, thread_id)` | V1: fallback to `send_message(fallback_text)`. V2: Adaptive Cards |
| `send_status(status_type, thread_id)` | Post `"⏳ {localized_phrase}."` → return activity_id |
| `send_status_with_phrase(status_type, thread_id)` | Return `(activity_id, phrase)` |
| `get_status_phrase(status_type)` | Localized phrase from `_localization` |
| `get_entertainment_intro()` | Localized intro phrase |
| `send_entertainment_message(text, thread_id)` | Post `"💡 {text}"` |
| `update_status_with_phrase_and_dots(msg_id, phrase, dots)` | Fixed phrase + animated dots |
| `update_status(message_id, status_type)` | Update with new phrase |
| `update_status_with_dots(msg_id, status_type, dots)` | New phrase + dots |
| `send_document_link(url, label, thread_id)` | Send `<a href="url">label</a>` |
| `send_file(content, filename, title, thread_id)` | V1: base64 inline attachment (< 4MB) |
| `download_file(url, mime_type)` | `aiohttp.get(url)` → temp file path |

### 4.3 Proactive messaging (notifications)

**Problem:** `turn_context` only lives for the duration of a single webhook request.
For background notifications (reminders, deep research, daily email review), we need to send
messages outside of a user-initiated turn.

**Solution:** `TeamsProactiveChannel` — a variant of `TeamsResponseChannel` that creates its own
`ConnectorClient` from stored credentials instead of using `turn_context`.

```python
class TeamsProactiveChannel(TeamsResponseChannel):
    """
    ResponseChannel for proactive/notification messages.
    Creates its own ConnectorClient from stored conversation reference.
    """

    def __init__(
        self,
        app_id: str,
        app_password: str,
        service_url: str,
        conversation_id: str,
        language: LanguageCode = LanguageCode.UK,
        localization: Optional[LocalizationPort] = None,
    ):
        self.channel_id = conversation_id
        self.platform = "teams"
        self.language = language
        self._localization = localization
        self.chunker = MessageChunker(max_length=TEAMS_CHUNK_SIZE)
        self._service_url = service_url
        self._conversation_id = conversation_id
        self._app_id = app_id
        self._app_password = app_password
        self._turn_context = None  # Not available for proactive

    async def send_message(self, text, thread_id=None, link_list=None):
        """Send proactive message via continue_conversation pattern."""
        settings = BotFrameworkAdapterSettings(self._app_id, self._app_password)
        adapter = BotFrameworkAdapter(settings)

        text = self._resolve_links_teams(text, link_list)
        formatted = self._format_for_platform(text)

        ref = ConversationReference(
            service_url=self._service_url,
            conversation=ConversationAccount(id=self._conversation_id),
            bot=ChannelAccount(id=self._app_id),
        )

        result_id = None
        async def _callback(turn_context: TurnContext):
            nonlocal result_id
            response = await turn_context.send_activity(
                Activity(type=ActivityTypes.message, text=formatted, text_format="html")
            )
            result_id = response.id if response else None

        await adapter.continue_conversation(ref, _callback, self._app_id)
        return result_id
```

### 4.4 `src/adapters/teams/media_adapter.py` — TeamsMediaAdapter

```python
class TeamsMediaAdapter(PlatformMediaPort):
    """V1: base64 inline attachments (< 4MB). V2: OneDrive upload."""

    def __init__(self, app_id: str, app_password: str):
        self._app_id = app_id
        self._app_password = app_password

    async def upload_image(self, image_bytes, alt_text, channel_id):
        # V1: base64 inline attachment via continue_conversation
        ...

    async def upload_file(self, file_bytes, filename, title, channel_id):
        # V1: base64 inline attachment (< 4MB)
        # V2: OneDrive upload for larger files
        ...
```

**Note on media complexity:** `channel_id` for Teams encodes `{conversation_id}|{service_url}`
(see § 7.1). The adapter parses this to create the ConnectorClient.

---

## 5. Composition — TeamsAdapterFactory

**File:** `src/composition/teams_adapter_factory.py`

Mirrors `TelegramAdapterFactory` exactly in structure:

```python
class TeamsAdapterFactory:

    @staticmethod
    def create_adapter(
        app_id: str,
        app_password: str,
        dedup_store,
        session_store,
        coordinator: AgentCoordinator,
        agent_factory: UserAgentFactory,
        iam_service: PlatformAuthPort,
        file_service: FileService,
        consolidation_queue=None,
        consolidation_config=None,
        html_renderer: Optional[HtmlRendererPort] = None,
        notification_service: Optional[UserNotificationService] = None,
        indexed_email_repo=None,
        user_repo=None,
        language_service: Optional[LanguageServicePort] = None,
        localization: Optional[LocalizationService] = None,
        file_conversion_service=None,
    ) -> TeamsWebhookAdapter:
        # 1. Create TeamsMediaAdapter
        # 2. Create RichContentService (with media_adapter + html_renderer)
        # 3. Create ConversationHandler (with all deps)
        # 4. Return TeamsWebhookAdapter (with conversation_handler + iam_service)
        ...
```

---

## 6. main.py Changes

### 6.1 Config validation — `src/config/environment.py`

```python
def validate_teams_config():
    """
    Validate Teams configuration at startup.

    Returns:
        Dict with app_id and app_password if valid, None if not configured.

    Raises:
        ValueError: If TEAMS_APP_PASSWORD missing when TEAMS_APP_ID is set.
    """
    app_id = os.getenv("TEAMS_APP_ID")
    if not app_id:
        return None  # Teams not configured

    app_password = os.getenv("TEAMS_APP_PASSWORD")
    if not app_password:
        raise ValueError(
            "TEAMS_APP_PASSWORD is required when TEAMS_APP_ID is set"
        )

    return {
        "app_id": app_id,
        "app_password": app_password,
    }
```

### 6.2 Integration block — insert after Telegram block (line ~755)

```python
# ================================================================
# Teams Integration (Optional)
# ================================================================
from src.config.environment import validate_teams_config
teams_config = validate_teams_config()

if teams_config:
    logger.info("Initializing Teams adapter...")
    try:
        from src.adapters.firestore_dedup_store import FirestoreDedupStore
        from src.adapters.platform.factory import PlatformAdapterFactory
        from src.composition.teams_adapter_factory import TeamsAdapterFactory
        from src.adapters.teams.response_channel import TeamsProactiveChannel

        teams_dedup_store = FirestoreDedupStore(
            db_client=db_client,
            collection_name=env_config.event_dedup_collection
        )

        teams_adapter = TeamsAdapterFactory.create_adapter(
            app_id=teams_config["app_id"],
            app_password=teams_config["app_password"],
            dedup_store=teams_dedup_store,
            session_store=session_store,
            coordinator=coordinator,
            agent_factory=agent_factory,
            iam_service=iam_service,
            file_service=file_service,
            consolidation_queue=consolidation_queue,
            consolidation_config=config.get("CONSOLIDATION"),
            html_renderer=html_renderer,
            notification_service=notification_service,
            indexed_email_repo=container.indexed_email_repo,
            user_repo=user_repo,
            language_service=_language_service,
            localization=_localization,
            file_conversion_service=container.file_conversion_service,
        )

        # Notification channel factory — encode service_url in channel_id
        def _make_teams_channel(channel_id):
            parts = channel_id.split("|", 1)
            conv_id = parts[0]
            service_url = parts[1] if len(parts) > 1 else "https://smba.trafficmanager.net/emea/"
            return TeamsProactiveChannel(
                app_id=teams_config["app_id"],
                app_password=teams_config["app_password"],
                service_url=service_url,
                conversation_id=conv_id,
            )

        notification_channel_factory.register_factory("teams", _make_teams_channel)

        teams_bp = teams_adapter.get_blueprint()
        main_app.register_blueprint(teams_bp, url_prefix="/teams")
        PlatformAdapterFactory.register("teams", teams_adapter)

        logger.info("Teams adapter registered at /teams/messages")
    except Exception as e:
        logger.error(f"Failed to initialize Teams adapter: {e}", exc_info=True)
        logger.warning("Bot will continue without Teams support")
else:
    logger.info("Teams not configured (TEAMS_APP_ID not set)")
```

---

## 7. Notification Integration

### 7.1 Encoding service_url in channel_id

Teams requires `service_url` to create a `ConnectorClient` for proactive messages.
The `NotificationStatePort` stores only `{platform, channel_id}`. To avoid changing the port:

**Encode as pipe-separated:** `channel_id = "{conversation_id}|{service_url}"`

```python
# In TeamsWebhookAdapter._process_message, when saving last active channel:
encoded_channel_id = f"{conversation_id}|{activity.service_url}"
# notification_service.save_channel(user_id, "teams", encoded_channel_id)
```

The notification factory lambda (§ 6.2) parses this back.

### 7.2 Background delivery flow (unchanged)

1. `UserNotificationService.notify()` → `_state_repo.get(user_id)`
2. Returns `NotificationChannel(platform="teams", channel_id="<encoded>")`
3. `_channel_factory.create("teams", channel_id)` → calls registered lambda
4. Lambda returns `TeamsProactiveChannel`
5. `notify()` calls `response_channel.send_message(formatted_text)`

---

## 8. Auth & Platform Linking

### 8.1 IAM Flow (unchanged)

1. Teams message arrives with `teams_user_id` (AAD Object ID).
2. `iam_service.authorize("teams", platform_user_id=teams_user_id)`.
3. IAMService looks up `UserProfile` by `platform_identities["teams"] == teams_user_id`.
4. If found: `IAMDecision(action="allow", user=profile)`.
5. If not found: `IAMDecision(action="reject", message="...")`.

### 8.2 Platform linking (Cabinet UI)

Cabinet's `POST /api/user/link-platform` already accepts any platform string.

User flow:
1. Open Teams, message the bot → receive rejection with AAD Object ID displayed.
2. Open Cabinet > Settings > Platform Linking.
3. Enter `platform="teams"`, `platform_user_id=<AAD Object ID>` (shown in rejection message).
4. Save → `platform_identities["teams"] = "<aad_object_id>"` stored in UserProfile.

---

## 9. Adaptive Cards (V2 — Deferred)

V1 uses plain HTML for all responses. After V1 is stable, extend
`TeamsResponseChannel.send_rich_content()`:

```python
async def send_rich_content(self, content: RichContent, thread_id=None):
    if content.content_type == "table":
        card = self._build_adaptive_card_table(content.data)
        await self._turn_context.send_activity(
            Activity(
                type=ActivityTypes.message,
                attachments=[Attachment(
                    content_type="application/vnd.microsoft.card.adaptive",
                    content=card,
                )],
                text=content.fallback_text,
            )
        )
        return
    return await self.send_message(content.fallback_text, thread_id)
```

---

## 10. Teams vs Slack vs Telegram — Comparison

| Feature | Slack | Telegram | Teams |
|---------|-------|----------|-------|
| Auth | HMAC signing secret | HMAC webhook secret | JWT token (Azure AD) |
| Message format | mrkdwn | MarkdownV2 | HTML |
| Rich content | Block Kit | Plain text (→ PNG) | Adaptive Cards (V2) |
| Threading | `thread_ts` | No native threads | `replyToId` |
| File upload | `files_upload_v2` | `send_document` | Inline base64 (V1) |
| File download | Bearer token + URL | Public URL | `content_url` from activity |
| Message editing | `chat.update` | `edit_message_text` (48h) | `update_activity` |
| Max message length | ~2500 chars | 4096 chars | ~4000 chars |
| Dedup | `event_id` | `update_id` | `activity.id` |
| SDK | `slack-bolt` | `python-telegram-bot` | `botbuilder-core` |
| Status messages | Post + edit | Post + edit | Post + edit |
| Proactive msgs | Direct API (bot token) | Direct API (bot token) | `continue_conversation` |

---

## 11. Files Summary

### 11.1 New Files

| File | Layer | Purpose | Est. lines |
|------|-------|---------|------------|
| `src/adapters/teams/__init__.py` | Adapters | Package init | 2 |
| `src/adapters/teams/webhook_adapter.py` | Adapters | TeamsWebhookAdapter(PlatformPort) | ~250 |
| `src/adapters/teams/response_channel.py` | Adapters | TeamsResponseChannel + TeamsProactiveChannel | ~450 |
| `src/adapters/teams/media_adapter.py` | Adapters | TeamsMediaAdapter(PlatformMediaPort) | ~70 |
| `src/composition/teams_adapter_factory.py` | Composition | Factory wiring | ~80 |
| `tests/unit/adapters/test_teams_response_channel.py` | Tests | ResponseChannel unit tests | ~200 |
| `tests/unit/adapters/test_teams_webhook_adapter.py` | Tests | Webhook adapter unit tests | ~150 |

### 11.2 Modified Files

| File | Change | Est. lines |
|------|--------|------------|
| `main.py` | Add Teams integration block (mirrors Telegram) | +50 |
| `src/config/environment.py` | Add `validate_teams_config()` | +25 |
| `requirements.txt` | Add `botbuilder-core`, `botbuilder-integration-aiohttp` | +3 |
| `cloudbuild-dev.yaml` | Add Teams secrets | +2 |
| `cloudbuild-prod.yaml` | Add Teams secrets | +2 |

### 11.3 Unchanged (deliberate)

| File | Reason |
|------|--------|
| `src/ports/*` | All ports are platform-agnostic, no changes needed |
| `src/handlers/conversation_handler.py` | Platform-agnostic core |
| `src/services/user_notification_service.py` | Already platform-agnostic |
| `src/domain/messaging.py` | ResponseChannel protocol is complete |
| `src/web/user_cabinet_app.py` | `link-platform` already accepts any platform string |

---

## 12. Tests

### 12.1 Unit Tests

**TeamsResponseChannel:**

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_format_for_platform_headers` | `# Title` → `<b>Title</b>` |
| 2 | `test_format_for_platform_bold` | `**bold**` → `<b>bold</b>` |
| 3 | `test_format_for_platform_bullets` | `- item` → `• item` |
| 4 | `test_format_for_platform_code_blocks` | triple backtick → `<pre>` |
| 5 | `test_resolve_links_bare_anchor` | `[1]` → `<a href>` |
| 6 | `test_resolve_links_reference_style` | `[text][1]` → `<a href>` |
| 7 | `test_send_message_calls_send_activity` | Activity construction correct |
| 8 | `test_update_message_calls_update_activity` | updateActivity call |
| 9 | `test_update_message_fallback` | On failure → `send_message` |
| 10 | `test_max_message_length_truncation` | Truncation at 4000 |
| 11 | `test_chunked_message` | Chunker splits correctly |
| 12 | `test_send_file_inline` | Base64 encoding |

**TeamsWebhookAdapter:**

| # | Test | Validates |
|---|------|-----------|
| 1 | `test_on_turn_ignores_non_message` | ConversationUpdate skipped |
| 2 | `test_on_turn_dedup` | Duplicate `activity.id` rejected |
| 3 | `test_process_message_iam_reject` | Unauthorized → rejection message |
| 4 | `test_process_message_iam_allow` | Authorized → ConversationHandler called |
| 5 | `test_strip_bot_mention` | `<at>Bot</at> hello` → `hello` |
| 6 | `test_translate_platform_files` | Attachments → FileAttachment DTOs |

### 12.2 Port Contract Tests

Verify `TeamsResponseChannel` satisfies `ResponseChannel` protocol,
`TeamsWebhookAdapter` satisfies `PlatformPort` ABC,
`TeamsMediaAdapter` satisfies `PlatformMediaPort` ABC.

### 12.3 Manual E2E Test

1. Deploy to dev Cloud Run
2. Set Teams bot messaging endpoint to dev URL
3. Send message in Teams 1:1 chat → verify response
4. Verify: status animation, file upload, link resolution
5. Link platform in Cabinet → verify IAM allow
6. Trigger a notification (reminder) → verify delivery in Teams

---

## 13. Implementation Phases

### Phase 1: Skeleton + JWT (1-2 hours)
1. Create `src/adapters/teams/__init__.py`
2. Create `TeamsWebhookAdapter` — blueprint, JWT validation, activity logging (no processing)
3. Add `validate_teams_config()` to `environment.py`
4. Add `botbuilder-core`, `botbuilder-integration-aiohttp` to `requirements.txt`
5. Add Teams block to `main.py` (blueprint registration only)
6. Deploy to dev → verify `/teams/messages` responds

### Phase 2: ResponseChannel (2-3 hours)
1. Create `TeamsResponseChannel` — all 19 methods
2. Implement `_format_for_platform` (Markdown → HTML)
3. Implement `_resolve_links_teams`
4. Wire `_process_message` in webhook adapter
5. E2E test: send message in Teams → receive response

### Phase 3: Notifications + Proactive (1-2 hours)
1. Create `TeamsProactiveChannel`
2. Register notification channel factory in `main.py`
3. Store conversation reference (`service_url` encoded in `channel_id`)
4. Test: trigger notification → verify delivery in Teams

### Phase 4: Media + Files (1-2 hours)
1. Create `TeamsMediaAdapter` — inline attachments
2. Implement `send_file` and `download_file` in ResponseChannel
3. Test: document request → verify file delivery

### Phase 5: Polish + Tests (1-2 hours)
1. Write unit tests for ResponseChannel formatting
2. Write unit tests for webhook adapter
3. Azure Bot registration in production
4. Add secrets to Secret Manager + Cloud Build
5. Deploy to production

**Total estimated effort:** 6-11 hours.

---

## 14. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| `botbuilder-core` SDK incompatibility with Quart async | Medium | High | Fallback: manual JWT validation with `PyJWT` (already in deps). Test `process_activity` early in Phase 1. |
| `service_url` varies by region | Low | Medium | Store per conversation (encoded in `channel_id`). Never hardcode. |
| `update_activity` fails for old messages | Low | Low | Same fallback as Telegram: send new message on failure. |
| Bot Framework token refresh race conditions | Low | Medium | SDK handles caching + refresh. For proactive channels, create new adapter per notification (stateless). |
| Azure AD tenant restrictions block personal accounts | Medium | High | Use "Accounts in any org + personal" in App Registration. Verify during Azure setup (Phase 1). |
| File upload size limit (4MB inline) | Medium | Low | V1 accepts the limit. V2: OneDrive. Log warning when file > 4MB. |
| `botbuilder-python` package size bloats Docker | Low | Low | ~5MB total. Acceptable for Cloud Run. |

---

## 15. Decision Summary

| Decision | Choice | Rationale |
|----------|--------|-----------|
| SDK vs raw REST | `botbuilder-core` SDK | Handles JWT, token refresh, activity parsing. Fallback to raw JWT if compat issues. |
| Message format | HTML (V1), Adaptive Cards (V2) | HTML is reliable in Teams, simpler than Markdown escaping. |
| File handling | Inline base64 (V1), OneDrive (V2) | Simplest. Works for files < 4MB. |
| Hosting | Same Quart app, `/teams/messages` blueprint | Consistent with Telegram pattern. Single Cloud Run service. |
| Tenant scope | Multi-tenant (consumers + organizations) | Required for personal Microsoft account access. |
| Notification state | Encode `service_url` in `channel_id` | Avoids port changes. `{conv_id}\|{service_url}` pipe-separated. |
| Channel scope | 1:1 personal chats only (V1) | Team channel conversations require different routing. Deferred. |
| Proactive messaging | Separate `TeamsProactiveChannel` class | Cannot reuse `turn_context` for background notifications. |
| Rich content | Fallback text (V1), Adaptive Cards (V2) | V1 ships faster. Tables/structured content deferred. |
