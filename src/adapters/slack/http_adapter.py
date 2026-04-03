"""
HTTP Mode Adapter
Enterprise-level implementation using platform-agnostic ConversationHandler
Implements Slack Events API with async queue processing via Cloud Tasks
"""
import asyncio
import hashlib
import hmac
import json
import time
import weakref
from typing import Optional, Dict, Any
from slack_bolt.async_app import AsyncApp
from quart import Blueprint, request, jsonify

from .base import SlackAdapter
from .response_channel import SlackResponseChannel
from ...domain.messaging import MessageContext, FileAttachment
from ...domain.prompt import ANONYMOUS_ACCOUNT_ID  # SESSION_26
from ...ports.task_queue import TaskQueue
from ...ports.conversation_handler_port import ConversationHandlerPort
from ...ports.platform_auth_port import PlatformAuthPort
from ...ports.session_store import SessionStore
from ...ports.dedup_store import DedupStore
from ...ports.language_service_port import LanguageServicePort
from ...ports.localization_port import LocalizationPort
from ...utils.logger import logger
from ...utils.telemetry import (
    start_span,
    build_trace_id,
    set_request_context,
    inject_trace_headers,
    extract_context,
    get_trace_ids
)
from ...utils.logging_context import set_log_context


class HTTPModeAdapter(SlackAdapter):
    """
    HTTP Events API adapter for Slack integration.
    Stateless, suitable for Cloud Run scale-to-zero deployment.

    Translates Slack events into platform-agnostic MessageContext and
    delegates processing to ConversationHandler.
    """

    def __init__(
        self,
        app: AsyncApp,
        config: dict,
        task_service: TaskQueue,
        session_store: SessionStore,
        conversation_handler: ConversationHandlerPort,
        iam_service: PlatformAuthPort,
        dedup_store: DedupStore,
        audio_service: Optional[Any] = None,
        language_service: Optional[LanguageServicePort] = None,
        localization: Optional[LocalizationPort] = None,
    ):
        super().__init__(
            app,
            config,
            conversation_handler=conversation_handler,
            iam_service=iam_service,
            audio_service=audio_service,
        )

        self.slack_bot_token = config.get("SLACK_BOT_TOKEN")
        self.slack_signing_secret = config.get("SLACK_SIGNING_SECRET")
        self.task_service = task_service
        self.session_store = session_store
        self.dedup_store = dedup_store
        self._language_service = language_service
        self._localization = localization
        self._session_locks: weakref.WeakValueDictionary = weakref.WeakValueDictionary()

        if not self.slack_signing_secret:
            raise ValueError("SLACK_SIGNING_SECRET is required for HTTP Mode")

        # ✅ Create Blueprint instead of Quart app
        self.blueprint = Blueprint('slack', __name__)
        self._setup_routes()

        logger.info("🌐 HTTP Mode adapter initialized (Multi-Tenant)")

    async def _resolve_language(self, user_id: str):
        """Return (ui_lang, preferred_language, agent_mirror) for a user."""
        from ...domain.language import LanguageCode
        if self._language_service:
            ui_lang = await self._language_service.resolve_ui_language(user_id)
            preferred, mirror = await self._language_service.get_preference(user_id)
        else:
            ui_lang = LanguageCode.UK
            preferred, mirror = None, True
        return ui_lang, preferred, mirror

    def _translate_files(self, slack_files: list) -> list:
        attachments = []
        for f in slack_files:
            url = f.get("url_private") or f.get("url_private_download")
            if not url:
                logger.warning(f"⚠️ Skipping file without URL: {f.get('name', 'unknown')}")
                continue

            attachments.append(FileAttachment(
                url=url,
                mime_type=f.get("mimetype", "application/octet-stream"),
                filename=f.get("name", "unknown"),
                size_bytes=f.get("size")
            ))
        return attachments

    def _setup_routes(self) -> None:
        @self.blueprint.route("/events", methods=["POST"])
        async def slack_events():
            return await self._handle_slack_event()

    def get_blueprint(self):
        """Return blueprint for registration in main app."""
        return self.blueprint

    async def _handle_slack_event(self):
        try:
            body = await request.get_data()
            headers = dict(request.headers)

            event_data = json.loads(body)

            if event_data.get("type") != "url_verification":
                if not self._verify_signature(body, headers):
                    logger.warning("⚠️ Invalid Slack signature")
                    return jsonify({"error": "Invalid signature"}), 401

            if event_data.get("type") == "url_verification":
                challenge = event_data.get("challenge", "")
                logger.info(f"✅ URL verification received, challenge: {challenge[:16]}...")
                return challenge, 200, {"Content-Type": "text/plain"}

            event = event_data.get("event", {})
            event_id = event_data.get("event_id")
            event_type = event.get("type", "unknown")

            trace_id = build_trace_id(event_id)
            set_request_context(trace_id=trace_id, event_id=event_id)
            set_log_context(trace_id=trace_id, event_id=event_id)

            with start_span("slack.event.received", {
                "slack.event_id": event_id or "unknown",
                "slack.event_type": event_type,
                "slack.team_id": event_data.get("team_id", "unknown")
            }):
                logger.info(f"📨 Event received: type={event_type}")

                # Atomic check-and-set to prevent race conditions during Slack retries
                if not await self.dedup_store.try_mark_processed(event_id):
                    logger.info(f"⏭️ Duplicate event {event_id[:16]}... skipped")
                    return jsonify({"ok": True}), 200

            session_id = (
                event.get("thread_ts") or
                event.get("ts") or
                event.get("channel") or
                event.get("item", {}).get("ts") or
                event.get("file_id")
            )

            if not session_id:
                logger.warning(f"⚠️ Cannot determine session_id from event type={event_type}, event keys: {list(event.keys())}")
                return jsonify({"ok": True}), 200

            trace_headers = {}
            inject_trace_headers(trace_headers)
            trace_ids = get_trace_ids()
            if trace_ids.get("trace_id"):
                trace_headers["x-trace-id"] = trace_ids["trace_id"]
            logger.info("📬 Event enqueued to Cloud Tasks")

            await self.task_service.enqueue_slack_event(
                event_data=event_data,
                session_id=session_id,
                trace_headers=trace_headers
            )

            return jsonify({"ok": True}), 200

        except Exception as e:
            logger.error(f"❌ Error handling Slack event: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    async def _handle_worker_task(self):
        try:
            payload = await request.get_json()
            if not payload:
                logger.warning("⚠️ Empty payload in worker task")
                return jsonify({"ok": False, "error": "Empty payload"}), 400

            headers = dict(request.headers)
            ctx = extract_context(headers)
            header_trace_id = headers.get("x-trace-id") or headers.get("X-Trace-Id")
            if header_trace_id:
                set_request_context(trace_id=header_trace_id)
                set_log_context(trace_id=header_trace_id)
            else:
                trace_ids = get_trace_ids()
                if trace_ids.get("trace_id"):
                    set_request_context(trace_id=trace_ids.get("trace_id"))
                    set_log_context(trace_id=trace_ids.get("trace_id"))

            event_data = payload.get("event")
            session_id = payload.get("session_id")

            if not event_data or not session_id:
                logger.warning(f"⚠️ Missing event_data or session_id in worker task")
                return jsonify({"ok": False, "error": "Missing required fields"}), 400

            # Per-session lock: serialize concurrent workers for the same session.
            # Strong local ref keeps the lock alive in WeakValueDictionary until function returns.
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._session_locks[session_id] = lock

            if lock.locked():
                logger.info(f"⏭️ Session {session_id[:8]} busy, returning 429 for Cloud Tasks retry")
                return jsonify({"ok": False, "error": "session_busy"}), 429

            async with lock:
                with start_span("worker.process_event", {
                    "session_id": session_id,
                    "trace_id": get_trace_ids().get("trace_id")
                }, ctx=ctx):
                    set_request_context(session_id=session_id)
                    set_log_context(session_id=session_id)
                    logger.info(f"🔧 Processing worker task for session {session_id[:8]}...")

                    event = event_data.get("event", {})
                    event_type = event.get("type")

                    if event.get("bot_id"):
                        return jsonify({"ok": True}), 200

                    if event_type == "message":
                        await self._process_message_event(event, session_id)
                    elif event_type == "app_mention":
                        await self._process_mention_event(event, session_id)
                    else:
                        logger.info(f"⏭️ Skipping unsupported event type: {event_type}")

                    return jsonify({"ok": True}), 200

        except Exception as e:
            logger.error(f"❌ Error in worker task: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    async def _resolve_session_id(self, user_id: str) -> str:
        latest = await self.session_store.get_latest_session_id(user_id)
        return latest or user_id

    async def _process_message_event(self, event: Dict[str, Any], session_id: str):
        try:
            subtype = event.get("subtype")
            if subtype and subtype != "file_share":
                logger.debug(f"⏭️ Skipping message subtype: {subtype}")
                return

            text = event.get("text", "")
            channel = event.get("channel")
            files = event.get("files", [])
            slack_user_id = event.get("user", "unknown")

            if files:
                logger.info(
                    f"📎 Message contains {len(files)} file(s), subtype={subtype}, "
                    f"text_len={len(text)}, text_preview={text[:100]!r}"
                )

            if (not text or not text.strip()) and not files:
                logger.warning(f"⚠️ Empty text and no files in message event, skipping")
                return

            # IAM Authorization
            decision = await self.iam_service.authorize("slack", platform_user_id=slack_user_id)

            if decision.action == "reject":
                logger.warning(f"⛔ Unauthorized Slack user: {slack_user_id}")
                response_channel = SlackResponseChannel(
                    self.app.client,
                    channel,
                    self.slack_bot_token
                )
                await response_channel.send_message(decision.message)
                return

            # User authorized → continue
            user_profile = decision.user
            user_id = user_profile.user_id
            account_id = user_profile.account_id or ANONYMOUS_ACCOUNT_ID  # SESSION_26
            session_id = await self._resolve_session_id(user_id)
            set_request_context(user_id=user_id, session_id=session_id)
            set_log_context(user_id=user_id, session_id=session_id)
            logger.info(f"👤 Processing message for user {user_id} ({user_profile.display_name})")

            ui_lang, preferred_language, agent_mirror = await self._resolve_language(user_id)

            response_channel = SlackResponseChannel(
                self.app.client,
                channel,
                self.slack_bot_token,
                language=ui_lang,
                localization=self._localization,
            )

            if text.startswith("$"):
                command = text.lstrip("$").strip().lower()
                context = MessageContext(
                    text=text,
                    session_id=session_id,
                    user_id=user_id,
                    account_id=account_id,  # SESSION_26
                    language=ui_lang.value,
                    metadata={"event_type": "command", "slack_user_id": slack_user_id}
                )
                await self.conversation_handler.handle_command(command, context, response_channel)
                return

            context = MessageContext(
                text=text,
                session_id=session_id,
                user_id=user_id,
                account_id=account_id,  # SESSION_26
                language=ui_lang.value,
                attachments=self._translate_files(event.get("files", [])),
                thread_id=event.get("thread_ts"),
                metadata={
                    "event_type": "message",
                    "channel_type": event.get("channel_type", "im"),
                    "slack_user_id": slack_user_id,
                    "preferred_language": preferred_language,
                    "agent_mirror": agent_mirror,
                }
            )

            await self.conversation_handler.handle_message(context, response_channel)

        except Exception as e:
            logger.error(f"❌ Error processing message: {e}", exc_info=True)

    async def _process_mention_event(self, event: Dict[str, Any], session_id: str):
        try:
            text = event.get("text", "").split(">", 1)[-1].strip()
            channel = event.get("channel")
            thread_ts = event.get("ts")
            slack_user_id = event.get("user", "unknown")

            # IAM Authorization
            decision = await self.iam_service.authorize("slack", platform_user_id=slack_user_id)

            if decision.action == "reject":
                logger.warning(f"⛔ Unauthorized Slack user: {slack_user_id}")
                response_channel = SlackResponseChannel(
                    self.app.client,
                    channel,
                    self.slack_bot_token
                )
                await response_channel.send_message(decision.message)
                return

            # User authorized → continue
            user_profile = decision.user
            user_id = user_profile.user_id
            account_id = user_profile.account_id or ANONYMOUS_ACCOUNT_ID  # SESSION_26
            session_id = await self._resolve_session_id(user_id)
            set_request_context(user_id=user_id, session_id=session_id)
            set_log_context(user_id=user_id, session_id=session_id)
            logger.info(f"👤 Processing mention for user {user_id} ({user_profile.display_name})")

            ui_lang, preferred_language, agent_mirror = await self._resolve_language(user_id)

            context = MessageContext(
                text=text,
                session_id=session_id,
                user_id=user_id,
                account_id=account_id,  # SESSION_26
                language=ui_lang.value,
                attachments=self._translate_files(event.get("files", [])),
                thread_id=thread_ts,
                metadata={
                    "event_type": "app_mention",
                    "slack_user_id": slack_user_id,
                    "preferred_language": preferred_language,
                    "agent_mirror": agent_mirror,
                }
            )

            response_channel = SlackResponseChannel(
                self.app.client,
                channel,
                self.slack_bot_token,
                language=ui_lang,
                localization=self._localization,
            )

            await self.conversation_handler.handle_message(context, response_channel)

        except Exception as e:
            logger.error(f"❌ Error processing mention: {e}", exc_info=True)

    def _verify_signature(self, body: bytes, headers: Dict[str, str]) -> bool:
        try:
            logger.debug(f"🔍 [Signature] Headers: {list(headers.keys())}")

            timestamp = headers.get("X-Slack-Request-Timestamp", "")
            slack_signature = headers.get("X-Slack-Signature", "")

            if not timestamp or not slack_signature:
                logger.error(f"⚠️ Missing Slack signature headers. Timestamp: {bool(timestamp)}, Signature: {bool(slack_signature)}")
                logger.error(f"⚠️ Available headers: {list(headers.keys())}")
                return False

            try:
                time_diff = abs(time.time() - int(timestamp))
                if time_diff > 300:
                    logger.warning(f"⚠️ Slack request timestamp too old: {time_diff}s")
                    return False
            except ValueError as e:
                logger.error(f"⚠️ Invalid timestamp format: {timestamp}, error: {e}")
                return False

            sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
            expected_signature = "v0=" + hmac.new(
                self.slack_signing_secret.encode(),
                sig_basestring.encode(),
                hashlib.sha256
            ).hexdigest()

            is_valid = hmac.compare_digest(expected_signature, slack_signature)

            if not is_valid:
                logger.error(f"⚠️ Signature mismatch!")
                logger.error(f"   Expected: {expected_signature[:20]}...")
                logger.error(f"   Received: {slack_signature[:20]}...")
            else:
                logger.debug(f"✅ Signature verified successfully")

            return is_valid

        except Exception as e:
            logger.error(f"❌ Signature verification error: {e}", exc_info=True)
            return False

    def register_handlers(self) -> None:
        logger.info("✅ HTTP Mode handlers ready (implicit via routes)")

    async def start(self) -> None:
        # Blueprint pattern: server lifecycle managed by main.py shared app
        logger.info("✅ Slack HTTP adapter ready (blueprint registered)")

    async def stop(self) -> None:
        logger.info("✅ HTTP server stopping (handled by Cloud Run)")

    def get_mode_name(self) -> str:
        return "HTTPMode"
