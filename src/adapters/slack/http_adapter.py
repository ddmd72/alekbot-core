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
from typing import Optional, Dict, Any, Callable, Tuple, cast
from slack_bolt.async_app import AsyncApp
from quart import Blueprint, request, jsonify

from .base import SlackAdapter
from .response_channel import SlackResponseChannel
from ...domain.messaging import MessageContext, FileAttachment
from ...domain.ui_messages import UIMessage
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


BUSY_REQUEUE_DELAY_S = 15
BUSY_MAX_REQUEUES = 12  # 3 min: outlasts the 90 s long-turn mark that releases the lock


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

        signing_secret = config.get("SLACK_SIGNING_SECRET")
        if not signing_secret:
            raise ValueError("SLACK_SIGNING_SECRET is required for HTTP Mode")
        self.slack_signing_secret: str = signing_secret
        self.slack_bot_token: str = config.get("SLACK_BOT_TOKEN") or ""
        self.task_service = task_service
        self.session_store = session_store
        self.dedup_store = dedup_store
        self._language_service = language_service
        self._localization = localization
        self._session_locks: weakref.WeakValueDictionary = weakref.WeakValueDictionary()

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

            # Slack names voice memos `audio_message.*` and marks them `slack_audio`.
            # Both are Slack-generated; the mimetype is not a reliable marker (voice
            # memos have been observed as both audio/mp4 and video/mp4).
            name = f.get("name", "unknown")
            is_voice = f.get("subtype") == "slack_audio" or name.startswith("audio_message.")

            attachments.append(FileAttachment(
                url=url,
                mime_type=f.get("mimetype", "application/octet-stream"),
                filename=name,
                size_bytes=f.get("size"),
                is_voice_message=is_voice,
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
        marked_event_id: Optional[str] = None
        try:
            body = cast(bytes, await request.get_data())  # raw body: signature check needs bytes
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
                marked_event_id = event_id

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

            trace_headers: Dict[str, str] = {}
            inject_trace_headers(trace_headers)
            trace_ids = get_trace_ids()
            current_trace_id = trace_ids.get("trace_id")
            if current_trace_id:
                trace_headers["x-trace-id"] = current_trace_id
            logger.info("📬 Event enqueued to Cloud Tasks")

            await self.task_service.enqueue_slack_event(
                event_data=event_data,
                session_id=session_id,
                trace_headers=trace_headers
            )

            return jsonify({"ok": True}), 200

        except Exception as e:
            logger.error(f"❌ Error handling Slack event: {e}", exc_info=True)
            if marked_event_id:
                # Un-mark so Slack's retry is not skipped as a duplicate (the message would be lost).
                try:
                    await self.dedup_store.release(marked_event_id)
                except Exception as release_error:
                    logger.error(f"❌ Could not release Slack intake mark: {release_error}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    async def handle_queued_event(self, payload: Dict[str, Any], headers: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
        try:
            if not payload:
                logger.warning("⚠️ Empty payload in worker task")
                return {"ok": False, "error": "Empty payload"}, 400

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
                logger.warning("⚠️ Missing event_data or session_id in worker task")
                return {"ok": False, "error": "Missing required fields"}, 400

            # Per-session lock: serialize concurrent workers for the same session.
            # Strong local ref keeps the lock alive in WeakValueDictionary until function returns.
            lock = self._session_locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._session_locks[session_id] = lock

            if lock.locked():
                return await self._requeue_busy(payload, event_data, session_id, headers)

            await lock.acquire()
            released = False

            def release_lock() -> None:
                # Called at the long-turn mark (LONG_RUNNING_TURNS_RFC §5.3.5) and in
                # finally below; idempotent so either order is safe.
                nonlocal released
                if not released:
                    released = True
                    lock.release()

            try:
                turn_id = f"slack:{event_data.get('event_id')}" if event_data.get("event_id") else None
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
                        return {"ok": True}, 200

                    if event_type == "message":
                        await self._process_message_event(event, session_id, turn_id=turn_id,
                                                           release_lock=release_lock)
                    elif event_type == "app_mention":
                        await self._process_mention_event(event, session_id, turn_id=turn_id,
                                                           release_lock=release_lock)
                    else:
                        logger.info(f"⏭️ Skipping unsupported event type: {event_type}")

                    return {"ok": True}, 200
            finally:
                release_lock()

        except Exception as e:
            logger.error(f"❌ Error in worker task: {e}", exc_info=True)
            return {"error": str(e)}, 500

    async def _requeue_busy(
        self, payload: Dict[str, Any], event_data: Dict[str, Any], session_id: str, headers: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], int]:
        # The queue's quick retries would burn out inside one running turn; a delayed new task waits it out.
        requeues = int(payload.get("busy_requeues") or 0)
        if requeues >= BUSY_MAX_REQUEUES:
            logger.error(f"❌ Session {session_id[:8]} still busy after {requeues} re-queues, dropping the message")
            await self._notify_busy_drop(event_data.get("event") or {})
            return {"ok": True, "dropped": "session_busy"}, 200
        logger.info(f"⏭️ Session {session_id[:8]} busy, re-queueing in {BUSY_REQUEUE_DELAY_S}s ({requeues + 1}/{BUSY_MAX_REQUEUES})")
        trace_headers = {k: v for k, v in headers.items() if k.lower() in ("traceparent", "tracestate", "x-trace-id")}
        try:
            await self.task_service.enqueue_slack_event(
                event_data=event_data,
                session_id=session_id,
                delay_seconds=BUSY_REQUEUE_DELAY_S,
                trace_headers=trace_headers or None,
                busy_requeues=requeues + 1,
            )
        except Exception as e:
            logger.error(f"❌ Busy re-queue failed: {e}", exc_info=True)
            return {"error": str(e)}, 500
        return {"ok": True, "requeued": requeues + 1}, 200

    async def _notify_busy_drop(self, event: Dict[str, Any]) -> None:
        # Best effort: the message is already lost, a failed notice must not turn the answer into a retry.
        channel = event.get("channel")
        if not channel:
            return
        try:
            ui_lang = await self._busy_notice_language(event.get("user"))
            response_channel = SlackResponseChannel(
                self.app.client, channel, self.slack_bot_token, language=ui_lang, localization=self._localization,
            )
            await response_channel.send_message(
                response_channel._ui_string(UIMessage.MESSAGE_NOT_PROCESSED_BUSY),
                thread_id=event.get("thread_ts"),
            )
        except Exception as e:
            logger.error(f"❌ Could not post the busy-drop notice: {e}", exc_info=True)

    async def _busy_notice_language(self, slack_user_id: Optional[str]):
        from ...domain.language import LanguageCode
        if slack_user_id:
            try:
                decision = await self.iam_service.authorize("slack", platform_user_id=slack_user_id)
                if decision.user is not None:
                    return (await self._resolve_language(decision.user.user_id))[0]
            except Exception as e:
                logger.warning(f"⚠️ Busy notice language lookup failed, using default: {e}")
        return LanguageCode.UK

    async def _handle_worker_task(self):
        # Quart wrapper kept for callers that still go through the request object.
        try:
            payload = await request.get_json()
        except Exception as e:
            logger.error(f"❌ Error in worker task: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500
        body, status = await self.handle_queued_event(payload, dict(request.headers))
        return jsonify(body), status

    def _resolve_session_id(self, user_id: str, channel_id: str) -> str:
        return f"{user_id}:{channel_id}"

    async def _process_message_event(
        self,
        event: Dict[str, Any],
        session_id: str,
        turn_id: Optional[str] = None,
        release_lock: Optional[Callable[[], None]] = None,
    ):
        try:
            subtype = event.get("subtype")
            if subtype and subtype != "file_share":
                logger.debug(f"⏭️ Skipping message subtype: {subtype}")
                return

            text = event.get("text", "")
            channel = event.get("channel")
            if not channel:
                logger.warning("⚠️ Slack event without a channel, skipping")
                return
            files = event.get("files", [])
            slack_user_id = event.get("user", "unknown")

            if files:
                logger.info(
                    f"📎 Message contains {len(files)} file(s), subtype={subtype}, "
                    f"text_len={len(text)}, text_preview={text[:100]!r}"
                )

            if (not text or not text.strip()) and not files:
                logger.warning("⚠️ Empty text and no files in message event, skipping")
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
                if decision.message:
                    await response_channel.send_message(decision.message)
                return

            # User authorized → continue
            user_profile = decision.user
            if user_profile is None:
                logger.error(f"❌ IAM decision '{decision.action}' for {slack_user_id} carries no user, skipping")
                return
            user_id = user_profile.user_id
            account_id = user_profile.account_id or ANONYMOUS_ACCOUNT_ID  # SESSION_26
            session_id = self._resolve_session_id(user_id, channel)
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
                on_long_turn=release_lock,
            )

            # A copied `` `$skill save CODE` `` (any number of wrapping backticks, e.g. a
            # triple-backtick code block) must still dispatch as a command — strip backticks
            # only when they wrap the ENTIRE trimmed text, so a normal sentence like
            # "`$HOME` is wrong" (backticks around a substring, not the whole text) is untouched.
            t = text.strip()
            stripped = t.strip("`").strip() if t.startswith("`") and t.endswith("`") else t

            if stripped.startswith("$"):
                command = stripped.lstrip("$").strip().lower()
                context = MessageContext(
                    text=text,
                    session_id=session_id,
                    user_id=user_id,
                    account_id=account_id,  # SESSION_26
                    language=ui_lang.value,
                    metadata={"event_type": "command", "platform": "slack", "slack_user_id": slack_user_id, "channel": channel}
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
                    "channel": channel,
                    "slack_user_id": slack_user_id,
                    "preferred_language": preferred_language,
                    "agent_mirror": agent_mirror,
                    "turn_id": turn_id,
                    "origin_message_id": event.get("ts"),
                    "event_time": float(event["ts"]) if event.get("ts") else None,
                }
            )

            await self.conversation_handler.handle_message(context, response_channel)

        except Exception as e:
            logger.error(f"❌ Error processing message: {e}", exc_info=True)

    async def _process_mention_event(
        self,
        event: Dict[str, Any],
        session_id: str,
        turn_id: Optional[str] = None,
        release_lock: Optional[Callable[[], None]] = None,
    ):
        try:
            text = event.get("text", "").split(">", 1)[-1].strip()
            channel = event.get("channel")
            if not channel:
                logger.warning("⚠️ Slack event without a channel, skipping")
                return
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
                if decision.message:
                    await response_channel.send_message(decision.message)
                return

            # User authorized → continue
            user_profile = decision.user
            if user_profile is None:
                logger.error(f"❌ IAM decision '{decision.action}' for {slack_user_id} carries no user, skipping")
                return
            user_id = user_profile.user_id
            account_id = user_profile.account_id or ANONYMOUS_ACCOUNT_ID  # SESSION_26
            session_id = self._resolve_session_id(user_id, channel)
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
                    "turn_id": turn_id,
                    "origin_message_id": event.get("ts"),
                    "event_time": float(event["ts"]) if event.get("ts") else None,
                }
            )

            response_channel = SlackResponseChannel(
                self.app.client,
                channel,
                self.slack_bot_token,
                language=ui_lang,
                localization=self._localization,
                on_long_turn=release_lock,
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
                logger.error("⚠️ Signature mismatch!")
                logger.error(f"   Expected: {expected_signature[:20]}...")
                logger.error(f"   Received: {slack_signature[:20]}...")
            else:
                logger.debug("✅ Signature verified successfully")

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
