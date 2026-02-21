"""
Telegram webhook adapter with HMAC verification and IAM integration.
"""
import asyncio
import hmac
import mimetypes
from typing import List
from quart import Blueprint, request, jsonify
from telegram import Bot, Update

from ..platform.base_adapter import PlatformAdapter
from .response_channel import TelegramResponseChannel
from ...domain.messaging import MessageContext, FileAttachment
from ...ports.conversation_handler_port import ConversationHandlerPort
from ...ports.platform_auth_port import PlatformAuthPort
from ...utils.logger import logger


class TelegramWebhookAdapter(PlatformAdapter):
    """
    Telegram webhook adapter (production mode).

    Security: Uses HMAC verification via X-Telegram-Bot-Api-Secret-Token.
    Onboarding: Unauthorized users receive Web UI link (same as Slack).
    """

    def __init__(
        self,
        token: str,
        webhook_secret: str,
        dedup_store,
        session_store,
        conversation_handler: ConversationHandlerPort,
        iam_service: PlatformAuthPort,
        audio_service=None,
    ):
        """
        Initialize Telegram webhook adapter.

        Args:
            token: Telegram bot token
            webhook_secret: HMAC secret for webhook verification
            dedup_store: Deduplication store for update_id tracking
            session_store: Session store for resolving user sessions
            conversation_handler: ConversationHandlerPort for processing messages
            iam_service: PlatformAuthPort for authorization
            audio_service: Optional audio transcription port
        """
        super().__init__(
            conversation_handler=conversation_handler,
            iam_service=iam_service,
            audio_service=audio_service,
        )
        
        self.token = token
        self.webhook_secret = webhook_secret
        self.bot = Bot(token)
        self.dedup_store = dedup_store
        self.session_store = session_store
        
        # Create Quart Blueprint for webhook endpoint
        self.blueprint = Blueprint('telegram', __name__)
        self._setup_routes()

        logger.info("🤖 Telegram webhook adapter initialized")

    def _setup_routes(self):
        """Setup webhook route."""
        @self.blueprint.route("/webhook", methods=["POST"])
        async def telegram_webhook():
            """Handle incoming Telegram updates."""
            return await self._handle_telegram_update()

    def get_blueprint(self) -> Blueprint:
        """Return Quart blueprint for registration in main.py."""
        return self.blueprint

    async def _resolve_session_id(self, user_id: str) -> str:
        """
        Find the most recently active session for a given user.
        
        This ensures conversation continuity across multiple Telegram chats
        (same pattern as Slack adapter).
        
        Args:
            user_id: Internal user ID
            
        Returns:
            Latest session_id or user_id if no session exists
        """
        latest = await self.session_store.get_latest_session_id(user_id)
        return latest or user_id

    async def _verify_webhook_signature(self, request_obj) -> bool:
        """
        Verify Telegram webhook using X-Telegram-Bot-Api-Secret-Token.
        https://core.telegram.org/bots/api#setwebhook
        """
        received_token = request_obj.headers.get("X-Telegram-Bot-Api-Secret-Token")

        if not received_token:
            logger.error("❌ Missing X-Telegram-Bot-Api-Secret-Token header")
            return False

        # Constant-time comparison (prevents timing attacks)
        is_valid = hmac.compare_digest(received_token, self.webhook_secret)

        if not is_valid:
            logger.error("❌ Invalid webhook secret token")

        return is_valid

    async def _handle_telegram_update(self):
        """Handle incoming Telegram update with verification and dedup."""
        try:
            # 1. Verify HMAC signature
            if not await self._verify_webhook_signature(request):
                return jsonify({"error": "Invalid signature"}), 403

            # 2. Parse update
            body = await request.get_json()
            update = Update.de_json(body, self.bot)

            if not update or not update.message:
                return jsonify({"ok": True}), 200

            message = update.message
            update_id = update.update_id

            # 3. Dedup check with namespace
            dedup_key = f"telegram::{update_id}"
            if not await self.dedup_store.try_mark_processed(dedup_key):
                logger.info(f"⏭️ Duplicate update {update_id} skipped")
                return jsonify({"ok": True}), 200

            # 4. Process message
            await self._process_message(message)

            return jsonify({"ok": True}), 200

        except Exception as e:
            logger.error(f"❌ Error handling Telegram update: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    async def _process_message(self, message):
        """Process Telegram message with IAM authorization."""
        try:
            telegram_user_id = str(message.from_user.id)
            chat_id = message.chat.id
            
            # Extract text from message or caption (for photos/videos)
            text = message.text or message.caption or ""
            
            logger.debug(f"📝 Extracted text: '{text}' (text={message.text is not None}, caption={message.caption is not None})")

            # 1. IAM Authorization (SAME PATTERN AS SLACK)
            decision = await self.iam_service.authorize("telegram", platform_user_id=telegram_user_id)

            response_channel = TelegramResponseChannel(self.bot, chat_id)

            if decision.action == "reject":
                # User NOT authorized → send centralized IAM message
                logger.warning(f"⛔ Unauthorized Telegram user: {telegram_user_id}")
                
                # ✅ Use centralized message from IAMService
                await response_channel.send_message(decision.message)
                return

            # 2. User authorized → process message
            user_profile = decision.user
            user_id = user_profile.user_id
            account_id = user_profile.account_id

            # 3. Resolve session (SAME PATTERN AS SLACK)
            session_id = await self._resolve_session_id(user_id)
            
            logger.info(f"👤 Processing Telegram message for user {user_id}, session {session_id[:8]}...")

            # 4. Translate attachments (ASYNC!)
            attachments = []
            if message.photo or message.document:
                # Telegram sends photos as array of sizes - take largest (last element)
                if message.photo:
                    files = [message.photo[-1]]  # Largest photo
                    logger.info(f"📸 Photo received: {len(message.photo)} sizes, using largest file_id={files[0].file_id}")
                else:
                    files = [message.document]
                    logger.info(f"📎 Document received: {message.document.file_name}")
                
                attachments = await self._translate_platform_files(files)
                logger.info(f"✅ File translation complete: {len(attachments)}/{len(files)} successful")

            # 5. Create MessageContext
            context = MessageContext(
                text=text,
                session_id=session_id,
                user_id=user_id,
                account_id=account_id,
                attachments=attachments,
                thread_id=str(message.message_thread_id) if message.is_topic_message else None,
                metadata={
                    "platform": "telegram",
                    "chat_id": chat_id,
                    "telegram_user_id": telegram_user_id
                }
            )

            # 5. Call ConversationHandler (platform-agnostic!)
            await self.conversation_handler.handle_message(context, response_channel)

        except Exception as e:
            logger.error(f"❌ Error processing Telegram message: {e}", exc_info=True)

    async def _translate_platform_files(self, platform_files: list) -> List[FileAttachment]:
        """
        Translate Telegram files to FileAttachment DTOs.

        ASYNC because Telegram requires API call to get file_path.
        Uses parallel processing for multiple files.
        """
        async def translate_single_file(file_obj):
            """Translate single Telegram file."""
            try:
                # Get file metadata
                file_id = getattr(file_obj, 'file_id', None)
                file_name = getattr(file_obj, 'file_name', None) or 'unknown'
                
                if not file_id:
                    logger.error(f"❌ File object missing file_id: {file_obj}")
                    return None
                
                # Telegram file → API call to get file_path
                logger.debug(f"📂 Translating file: {file_name} (ID: {file_id})")
                file_info = await self.bot.get_file(file_id)
                
                # Validate response
                if not file_info:
                    logger.error(f"❌ bot.get_file() returned None for {file_id}")
                    return None
                
                if not hasattr(file_info, 'file_path') or not file_info.file_path:
                    logger.error(f"❌ file_info missing file_path: {file_info}")
                    return None

                # file_path is already a full URL from bot.get_file()
                file_url = file_info.file_path
                
                # Determine MIME type properly
                # 1. Try to get from file object (Documents have mime_type)
                mime_type = getattr(file_obj, 'mime_type', None)
                
                # 2. If not available, guess from file path extension
                if not mime_type and file_info.file_path:
                    guessed_mime, _ = mimetypes.guess_type(file_info.file_path)
                    mime_type = guessed_mime
                
                # 3. Fallback to generic binary
                if not mime_type:
                    mime_type = "application/octet-stream"
                    logger.warning(f"⚠️ Could not determine MIME type for {file_name}, using fallback")
                
                logger.info(f"✅ File translated: {file_name} ({mime_type}) → {file_url}")

                return FileAttachment(
                    url=file_url,
                    mime_type=mime_type,
                    filename=file_name,
                    size_bytes=getattr(file_obj, 'file_size', None)
                )

            except Exception as e:
                logger.error(
                    f"❌ Failed to translate Telegram file: {e}\n"
                    f"   File object: {file_obj}\n"
                    f"   File ID: {getattr(file_obj, 'file_id', 'N/A')}",
                    exc_info=True
                )
                return None

        # Parallel translation for multiple files
        tasks = [translate_single_file(f) for f in platform_files]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Filter out None and exceptions
        attachments = [r for r in results if r and not isinstance(r, Exception)]
        
        if len(attachments) < len(platform_files):
            logger.warning(
                f"⚠️ Some files failed translation: "
                f"{len(attachments)}/{len(platform_files)} successful"
            )
        
        return attachments

    def get_platform_name(self) -> str:
        """Return platform name."""
        return "telegram"

    async def start(self) -> None:
        """
        Start the adapter.
        
        Note: Webhook is set externally via Cloud Run deployment.
        This method just confirms readiness.
        """
        logger.info("✅ Telegram webhook adapter ready at /telegram/webhook")

    async def stop(self) -> None:
        """Gracefully stop the adapter."""
        logger.info("✅ Telegram webhook adapter stopped")
