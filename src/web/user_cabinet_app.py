from quart import Blueprint, request, jsonify, g, send_file, redirect, abort, send_from_directory
from functools import wraps
from typing import Dict, Any, Optional
from datetime import datetime, timezone
import os

from ..services.invite_code_service import InviteCodeService
from ..services.session_service import SessionService
from ..services.google_oauth_service import GoogleOAuthService
from ..services.email_indexing_service import EmailIndexingService
from ..ports.user_repository import UserRepository
from ..ports.repository import FactRepository
from ..ports.embedding_service import EmbeddingService
from ..ports.oauth_credentials_port import OAuthCredentialsPort
from ..ports.indexed_email_repository import IndexedEmailRepository
from ..ports.email_indexing_job_repository import EmailIndexingJobRepository
from ..ports.task_queue import TaskQueue
from ..ports.language_service_port import LanguageServicePort
from ..ports.agent_note_port import AgentNotePort
from ..utils.logger import logger

# Documentation owner (loaded from environment variable - Secret Manager)
DOCS_OWNER_USER_ID = os.getenv('DOCS_OWNER_USER_ID')


def create_user_cabinet_blueprint(
    invite_service: InviteCodeService,
    session_service: SessionService,
    user_repo: UserRepository,
    fact_repo: FactRepository,
    embedding_service: EmbeddingService,
    oauth_credentials_port: Optional[OAuthCredentialsPort] = None,
    gmail_oauth_service: Optional[GoogleOAuthService] = None,
    google_tasks_oauth_service: Optional[GoogleOAuthService] = None,
    indexed_email_repo: Optional[IndexedEmailRepository] = None,
    email_indexing_service: Optional[EmailIndexingService] = None,
    email_job_repo: Optional[EmailIndexingJobRepository] = None,
    task_queue: Optional[TaskQueue] = None,
    task_setup=None,
    tasks_provider=None,
    language_service: Optional[LanguageServicePort] = None,
    agent_note_port: Optional[AgentNotePort] = None,
) -> Blueprint:
    """
    Create and configure the User Cabinet Blueprint.
    """
    bp = Blueprint("cabinet", __name__)

    # Calculate absolute paths
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(os.path.dirname(current_dir))
    SITE_DIR = os.path.join(project_root, 'site')

    # Authentication Middleware
    def auth_required(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Check Authorization header (API)
            auth_header = request.headers.get("Authorization")
            token = None
            
            if auth_header and auth_header.startswith("Bearer "):
                token = auth_header.split(" ")[1]
            else:
                # Check Cookie (Web UI)
                token = request.cookies.get("access_token")

            if not token:
                return jsonify({"error": "Missing authorization"}), 401

            try:
                payload = session_service.verify_access_token(token)
                g.user_id = payload["sub"]
                g.account_id = payload["account_id"]
                g.role = payload.get("role", "viewer")
                return await func(*args, **kwargs)
            except Exception as e:
                logger.warning(f"Auth failed: {e}")
                return jsonify({"error": "Invalid or expired token"}), 401
        return wrapper

    # Owner-only Middleware (for internal features like docs)
    def owner_only(func):
        """Decorator: Only specific user (by user_id from Secret Manager) can access."""
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # If secret not configured - feature disabled
            if not DOCS_OWNER_USER_ID:
                abort(404)  # Pretend page doesn't exist
            
            # Check Authorization header or Cookie
            auth_header = request.headers.get("Authorization")
            token = None
            
            if auth_header and auth_header.startswith("Bearer "):
                token = auth_header.split(" ")[1]
            else:
                token = request.cookies.get("access_token")

            if not token:
                abort(404)  # Pretend page doesn't exist (don't reveal it requires auth)

            try:
                payload = session_service.verify_access_token(token)
                user_id = payload["sub"]
                
                # Check if user_id matches owner
                if str(user_id) != DOCS_OWNER_USER_ID:
                    abort(404)  # Pretend page doesn't exist
                
                # Store in g for potential use in handler
                g.user_id = user_id
                return await func(*args, **kwargs)
            except Exception as e:
                logger.debug(f"Owner-only auth failed: {e}")
                abort(404)  # Pretend page doesn't exist
        return wrapper

    @bp.route("/health", methods=["GET"])
    async def health():
        return jsonify({"status": "ok"}), 200

    # ========================================================================
    # UI Routes
    # ========================================================================
    @bp.route("/cabinet")
    async def serve_cabinet():
        """Serve the cabinet HTML page."""
        # Use absolute path relative to this file
        current_dir = os.path.dirname(os.path.abspath(__file__))
        static_file = os.path.join(current_dir, "static", "cabinet.html")
        return await send_file(static_file)

    @bp.route("/join")
    async def join_team_page():
        """
        Handle deep link for joining a team.
        Query params: ?code=XYZ
        """
        code = request.args.get("code")
        if not code:
            return "Invalid link: missing code", 400
        
        # Check authentication via cookie
        token = request.cookies.get("access_token")
        
        if token:
            try:
                # Validate token
                payload = session_service.verify_access_token(token)
                user_id = payload["sub"]
                
                # Consume code immediately
                await invite_service.consume_team_invite(code, user_id)
                
                # Redirect to cabinet with success message
                return redirect("/cabinet?joined=true")
            except Exception as e:
                logger.warning(f"Auto-join failed (token invalid or code error): {e}")
                # Fallthrough to login flow
        
        # If not authenticated or failed, redirect to login with code state
        # We use 'next' param which OAuth endpoint handles
        return redirect(f"/auth/login?next=/cabinet&code={code}")

    # ========================================================================
    # Internal Documentation Routes (Owner-only, Hidden URL)
    # ========================================================================
    @bp.route("/cabinet/docs")
    @bp.route("/cabinet/docs/")
    @owner_only
    async def serve_docs_index():
        """
        Serve Arc42 documentation index (owner-only).
        Security: user_id checked via Secret Manager (not in git).
        """
        try:
            return await send_from_directory(SITE_DIR, 'index.html')
        except FileNotFoundError:
            logger.error("Documentation not found - MkDocs build may have failed")
            abort(404)

    @bp.route("/cabinet/docs/<path:filename>")
    @owner_only
    async def serve_docs_file(filename):
        """
        Serve Arc42 documentation files (owner-only).
        Security: user_id checked via Secret Manager (not in git).
        """
        try:
            return await send_from_directory(SITE_DIR, filename)
        except FileNotFoundError:
            logger.debug(f"Documentation file not found: {filename}")
            abort(404)

    # ========================================================================
    # API Routes
    # ========================================================================

    @bp.route("/api/user/link-platform", methods=["POST"])
    @auth_required
    async def link_platform():
        """
        Link user account to platform (Slack, Telegram, etc).
        Body:
            platform: "slack" | "telegram"
            platform_user_id: "U0AC0NL3L8K" (Slack) or "@username" (Telegram)
        """
        try:
            body = await request.get_json()
            platform = body.get("platform")
            platform_user_id = body.get("platform_user_id")
            
            if not platform or not platform_user_id:
                return jsonify({"error": "platform and platform_user_id are required"}), 400
            
            # Link platform ID to user (uses user_repo from closure)
            await user_repo.add_platform_id(g.user_id, platform, platform_user_id)
            
            logger.info(f"✅ Linked {platform}:{platform_user_id} to user {g.user_id}")
            return jsonify({"success": True}), 200
            
        except ValueError as e:
            # Business logic errors (duplicate ID, user not found, etc)
            error_msg = str(e)
            if "already linked" in error_msg:
                return jsonify({"error": "This Slack ID is already linked to another account"}), 409
            return jsonify({"error": error_msg}), 400
        except Exception as e:
            logger.error(f"Error linking platform: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @bp.route("/api/user/link-platform", methods=["DELETE"])
    @auth_required
    async def unlink_platform():
        """
        Unlink platform from user account.
        Query params:
            platform: "slack" | "telegram"
        """
        try:
            platform = request.args.get("platform")
            if not platform:
                return jsonify({"error": "platform parameter is required"}), 400
            
            # Remove platform ID from user (uses user_repo from closure)
            await user_repo.remove_platform_id(g.user_id, platform)
            
            logger.info(f"🗑️ Unlinked {platform} from user {g.user_id}")
            return jsonify({"success": True}), 200
            
        except Exception as e:
            logger.error(f"Error unlinking platform: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    @bp.route("/api/user/link-telegram", methods=["POST"])
    @auth_required
    async def link_telegram():
        """
        Link Telegram ID to user account.
        Body:
            telegram_user_id: "123456789" (numeric Telegram user ID)
        """
        try:
            body = await request.get_json()
            telegram_user_id = body.get("telegram_user_id")
            
            if not telegram_user_id:
                return jsonify({"error": "telegram_user_id is required"}), 400
            
            # Validate format (Telegram user IDs are numeric)
            telegram_user_id = str(telegram_user_id).strip()
            if not telegram_user_id.isdigit():
                return jsonify({"error": "Telegram ID must be numeric"}), 400
            
            # Link Telegram ID to user
            await user_repo.add_platform_id(g.user_id, "telegram", telegram_user_id)
            
            logger.info(f"✅ Linked telegram:{telegram_user_id} to user {g.user_id}")
            return jsonify({"success": True}), 200
            
        except ValueError as e:
            # Business logic errors (duplicate ID, user not found, etc)
            error_msg = str(e)
            if "already linked" in error_msg:
                return jsonify({"error": "This Telegram ID is already linked to another account"}), 409
            return jsonify({"error": error_msg}), 400
        except Exception as e:
            logger.error(f"Error linking Telegram: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @bp.route("/api/user/invite-codes", methods=["POST"])
    @auth_required
    async def create_invite():
        """
        Generate a new invite code.
        Body:
            type: "team_invite"
            email: "colleague@example.com" (required for team_invite)
            role: "MEMBER" (optional, default MEMBER)
        """
        try:
            body = await request.get_json()
            invite_type = body.get("type", "team_invite")

            if invite_type == "team_invite":
                email = body.get("email")
                role = body.get("role", "MEMBER")
                
                if not email:
                    return jsonify({"error": "email is required for team_invite"}), 400
                
                # Only OWNER can generate team invites
                if g.role != "owner":
                    return jsonify({"error": "Only account owners can invite members"}), 403
                
                # Whitelist check via InviteCodeService (handles whitelist internally)
                code = await invite_service.generate_team_invite(
                    user_id=g.user_id,
                    account_id=g.account_id,
                    role=role
                )
            else:
                return jsonify({"error": "Invalid invite type"}), 400

            return jsonify({
                "code": code.code,
                "type": code.type.value,
                "expires_at": code.expires_at.isoformat()
            }), 201

        except ValueError as e:
            # Whitelist or validation errors
            error_msg = str(e)
            if "not authorized" in error_msg.lower() or "whitelist" in error_msg.lower():
                return jsonify({"error": error_msg}), 403
            return jsonify({"error": error_msg}), 400
        except Exception as e:
            logger.error(f"Error creating invite: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @bp.route("/api/user/invite-codes", methods=["GET"])
    @auth_required
    async def list_invites():
        """List active invite codes created by user."""
        try:
            codes = await invite_service.repo.list_by_user(g.user_id)
            # Filter only valid codes? Or return all? Let's return all but mark validity
            result = []
            for code in codes:
                result.append({
                    "code": code.code,
                    "type": code.type.value,
                    "is_valid": code.is_valid(),
                    "expires_at": code.expires_at.isoformat(),
                    "created_at": code.created_at.isoformat(),
                    "used_at": code.used_at.isoformat() if code.used_at else None
                })
            return jsonify({"invites": result}), 200
        except Exception as e:
            logger.error(f"Error listing invites: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @bp.route("/api/user/platforms", methods=["GET"])
    @auth_required
    async def get_platforms():
        """Return current platform link state for the authenticated user."""
        try:
            user = await user_repo.get_user(g.user_id)
            platforms = user.platform_identities if user else {}
            return jsonify({"platforms": platforms}), 200
        except Exception as e:
            logger.error(f"Error fetching platforms: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @bp.route("/api/user/timezone", methods=["GET"])
    @auth_required
    async def get_timezone():
        """Return the user's current timezone setting."""
        try:
            user = await user_repo.get_user(g.user_id)
            tz = user.config.timezone if user else "UTC"
            return jsonify({"timezone": tz}), 200
        except Exception as e:
            logger.error(f"Error fetching timezone: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @bp.route("/api/user/timezone", methods=["PUT"])
    @auth_required
    async def set_timezone():
        """Update the user's timezone. Body: {\"timezone\": \"Europe/Kyiv\"}"""
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            body = await request.get_json(force=True) or {}
            tz_name = body.get("timezone", "").strip()
            if not tz_name:
                return jsonify({"error": "timezone field is required"}), 400
            try:
                ZoneInfo(tz_name)  # validate IANA name
            except (ZoneInfoNotFoundError, KeyError):
                return jsonify({"error": f"Unknown timezone: {tz_name!r}"}), 400

            user = await user_repo.get_user(g.user_id)
            if not user:
                return jsonify({"error": "User not found"}), 404
            user.config.timezone = tz_name
            await user_repo.update_user(user)
            return jsonify({"timezone": tz_name}), 200
        except Exception as e:
            logger.error(f"Error updating timezone: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @bp.route("/api/user/location", methods=["GET"])
    @auth_required
    async def get_location():
        """Return the user's current location setting."""
        try:
            user = await user_repo.get_user(g.user_id)
            loc = user.config.location if user else None
            return jsonify({"location": loc or ""}), 200
        except Exception as e:
            logger.error(f"Error fetching location: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @bp.route("/api/user/location", methods=["PUT"])
    @auth_required
    async def set_location():
        """Update the user's location. Body: {"location": "Valencia, Spain"}"""
        try:
            body = await request.get_json(force=True) or {}
            location = body.get("location", "").strip()
            user = await user_repo.get_user(g.user_id)
            if not user:
                return jsonify({"error": "User not found"}), 404
            user.config.location = location or None
            await user_repo.update_user(user)
            return jsonify({"location": location}), 200
        except Exception as e:
            logger.error(f"Error updating location: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @bp.route("/api/user/language", methods=["GET"])
    @auth_required
    async def get_language():
        """Return the user's current language preference."""
        try:
            if language_service:
                preferred, mirror = await language_service.get_preference(g.user_id)
            else:
                preferred, mirror = None, True
            return jsonify({
                "preferred_language": preferred.value if preferred else None,
                "agent_mirror": mirror,
            }), 200
        except Exception as e:
            logger.error(f"Error fetching language preference: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @bp.route("/api/user/language", methods=["POST"])
    @auth_required
    async def set_language():
        """Update language preference. Body: {\"preferred_language\": \"en\"|null, \"agent_mirror\": bool}"""
        from ..domain.language import LanguageCode
        try:
            body = await request.get_json(force=True) or {}
            lang_raw = body.get("preferred_language")
            mirror = body.get("agent_mirror", True)

            if not isinstance(mirror, bool):
                return jsonify({"error": "agent_mirror must be a boolean"}), 400

            preferred: Optional[LanguageCode] = None
            if lang_raw is not None:
                preferred = LanguageCode.from_str(lang_raw, default=None)
                if preferred is None:
                    return jsonify({"error": f"Unsupported language: {lang_raw!r}"}), 400

            if language_service:
                await language_service.set_preference(g.user_id, preferred, mirror)

            return jsonify({
                "preferred_language": preferred.value if preferred else None,
                "agent_mirror": mirror,
            }), 200
        except Exception as e:
            logger.error(f"Error updating language preference: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    # ------------------------------------------------------------------
    # Bot Reminders (self-reminders / orchestrator notes)
    # ------------------------------------------------------------------

    @bp.route("/api/user/reminders", methods=["GET"])
    @auth_required
    async def list_reminders():
        """Return all reminders for the user."""
        if not agent_note_port:
            return jsonify({"error": "Reminders not configured"}), 501
        try:
            from datetime import timezone as tz
            notes = await agent_note_port.list_active_notes(
                g.user_id, datetime(1970, 1, 1, tzinfo=tz.utc),
            )
            user = await user_repo.get_user(g.user_id)
            user_tz_name = user.config.timezone if user else "UTC"
            from zoneinfo import ZoneInfo
            user_tz = ZoneInfo(user_tz_name)
            return jsonify({
                "reminders": [
                    {
                        "note_id": n.note_id,
                        "text": n.text,
                        "instruction": n.instruction,
                        "due": n.due.astimezone(user_tz).isoformat() if n.due else None,
                        "recurrence": (
                            {"type": n.recurrence.type, "interval": n.recurrence.interval}
                            if n.recurrence else None
                        ),
                        "created_at": n.created_at.isoformat() if n.created_at else None,
                    }
                    for n in notes
                ],
                "timezone": user_tz_name,
            }), 200
        except Exception as e:
            logger.error(f"Error listing reminders: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @bp.route("/api/user/reminders", methods=["POST"])
    @auth_required
    async def create_reminder():
        """Create a new reminder. Body: {text, instruction, due (ISO), recurrence?}"""
        if not agent_note_port:
            return jsonify({"error": "Reminders not configured"}), 501
        from ..domain.agent_note import NoteCreate, ReminderRecurrence
        from zoneinfo import ZoneInfo
        try:
            body = await request.get_json(force=True) or {}
            text = (body.get("text") or "").strip()
            instruction = (body.get("instruction") or "").strip()
            due_raw = body.get("due")
            if not text or not instruction or not due_raw:
                return jsonify({"error": "text, instruction, and due are required"}), 400

            user = await user_repo.get_user(g.user_id)
            user_tz = ZoneInfo(user.config.timezone if user else "UTC")
            due = datetime.fromisoformat(due_raw)
            if due.tzinfo is None:
                due = due.replace(tzinfo=user_tz)
            from datetime import timezone as tz
            due_utc = due.astimezone(tz.utc)

            recurrence = None
            rec = body.get("recurrence")
            if rec and isinstance(rec, dict) and rec.get("type"):
                recurrence = ReminderRecurrence(
                    type=rec["type"],
                    interval=rec.get("interval", 1),
                )

            note = await agent_note_port.create_note(NoteCreate(
                user_id=g.user_id,
                text=text,
                instruction=instruction,
                due=due_utc,
                recurrence=recurrence,
            ))
            return jsonify({"note_id": note.note_id, "status": "created"}), 201
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            logger.error(f"Error creating reminder: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @bp.route("/api/user/reminders/<note_id>", methods=["PUT"])
    @auth_required
    async def update_reminder(note_id: str):
        """Update a reminder. Body: {text?, instruction?, due?, recurrence?}"""
        if not agent_note_port:
            return jsonify({"error": "Reminders not configured"}), 501
        from ..domain.agent_note import NoteUpdate, ReminderRecurrence
        from zoneinfo import ZoneInfo
        try:
            body = await request.get_json(force=True) or {}

            user = await user_repo.get_user(g.user_id)
            user_tz = ZoneInfo(user.config.timezone if user else "UTC")

            due_utc = None
            due_raw = body.get("due")
            if due_raw:
                due = datetime.fromisoformat(due_raw)
                if due.tzinfo is None:
                    due = due.replace(tzinfo=user_tz)
                from datetime import timezone as tz
                due_utc = due.astimezone(tz.utc)

            recurrence = None
            rec = body.get("recurrence")
            if rec is not None:
                if isinstance(rec, dict) and rec.get("type"):
                    recurrence = ReminderRecurrence(
                        type=rec["type"],
                        interval=rec.get("interval", 1),
                    )

            update = NoteUpdate(
                note_id=note_id,
                user_id=g.user_id,
                text=body.get("text"),
                instruction=body.get("instruction"),
                due=due_utc,
                recurrence=recurrence,
            )
            note = await agent_note_port.update_note(update)
            return jsonify({"note_id": note.note_id, "status": "updated"}), 200
        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            logger.error(f"Error updating reminder: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @bp.route("/api/user/reminders/<note_id>", methods=["DELETE"])
    @auth_required
    async def delete_reminder(note_id: str):
        """Delete a reminder."""
        if not agent_note_port:
            return jsonify({"error": "Reminders not configured"}), 501
        try:
            deleted = await agent_note_port.delete_note(note_id, g.user_id)
            if not deleted:
                return jsonify({"error": "Reminder not found"}), 404
            return jsonify({"note_id": note_id, "status": "deleted"}), 200
        except Exception as e:
            logger.error(f"Error deleting reminder: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @bp.route("/api/user/facts", methods=["GET"])
    @auth_required
    async def get_facts():
        """Return active facts for the user's account (read-only)."""
        try:
            facts = await fact_repo.get_active_facts(owner_id=g.account_id)
            items = [
                {
                    "text": f.text,
                    "domain": f.domain.value if f.domain else None,
                    "type": f.type.value if f.type else None,
                    "created_at": f.created_at.isoformat() if f.created_at else None,
                }
                for f in facts
            ]
            items.sort(key=lambda x: x["created_at"] or "", reverse=True)
            return jsonify({"facts": items, "total": len(items)}), 200
        except Exception as e:
            logger.error(f"Error fetching facts: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @bp.route("/api/user/facts/browse", methods=["GET"])
    @auth_required
    async def browse_facts():
        """Paginated facts for the Cabinet browser. Supports domain filter + cursor."""
        try:
            limit = min(int(request.args.get("limit", 100)), 500)
            cursor = request.args.get("cursor") or None
            domain = request.args.get("domain") or None

            facts, next_cursor = await fact_repo.get_paginated_facts(
                owner_id=g.account_id,
                limit=limit,
                cursor_doc_id=cursor,
                domain=domain,
            )
            items = [
                {
                    "id": f.id,
                    "text": f.text,
                    "domain": f.domain.value if f.domain else None,
                    "type": f.type.value if f.type else None,
                    "created_at": f.created_at.isoformat() if f.created_at else None,
                }
                for f in facts
            ]
            return jsonify({"facts": items, "next_cursor": next_cursor}), 200
        except Exception as e:
            logger.error(f"Error browsing facts: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @bp.route("/api/user/facts/search", methods=["POST"])
    @auth_required
    async def search_facts():
        """Semantic vector search over the user's facts."""
        try:
            body = await request.get_json()
            query = (body or {}).get("query", "").strip()
            if not query:
                return jsonify({"error": "query is required"}), 400

            vector = await embedding_service.get_embedding(query, task_type="RETRIEVAL_QUERY")
            facts = await fact_repo.search_facts(
                query_vector=vector,
                limit=50,
                user_id=g.user_id,
                account_id=g.account_id,
            )
            items = [
                {
                    "id": f.id,
                    "text": f.text,
                    "domain": f.domain.value if f.domain else None,
                    "type": f.type.value if f.type else None,
                    "created_at": f.created_at.isoformat() if f.created_at else None,
                }
                for f in facts
            ]
            return jsonify({"facts": items, "query": query}), 200
        except Exception as e:
            logger.error(f"Error searching facts: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @bp.route("/api/user/facts/<fact_id>/invalidate", methods=["POST"])
    @auth_required
    async def invalidate_fact(fact_id: str):
        """Mark a specific fact as invalidated. User Cabinet write path."""
        try:
            await fact_repo.invalidate_fact(fact_id=fact_id, account_id=g.account_id)
            logger.info(f"🗑️ Fact {fact_id} invalidated by user {g.user_id}")
            return jsonify({"status": "invalidated"}), 200
        except PermissionError:
            return jsonify({"error": "Not authorized"}), 403
        except ValueError as e:
            return jsonify({"error": str(e)}), 404
        except Exception as e:
            logger.error(f"Error invalidating fact {fact_id}: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @bp.route("/api/user/join-team", methods=["POST"])
    @auth_required
    async def join_team():
        """
        Consume a team invite code to join an account.
        Body:
            code: "INV-123..."
        """
        try:
            body = await request.get_json()
            code = body.get("code")
            if not code:
                return jsonify({"error": "code is required"}), 400

            await invite_service.consume_team_invite(code, g.user_id)

            return jsonify({"success": True, "message": "Joined team successfully"}), 200

        except ValueError as e:
            return jsonify({"error": str(e)}), 400
        except Exception as e:
            logger.error(f"Error joining team: {e}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    # ========================================================================
    # Gmail integration API
    # ========================================================================

    @bp.route("/api/gmail/status", methods=["GET"])
    @auth_required
    async def gmail_status():
        """Return Gmail connection state for the authenticated user."""
        if not oauth_credentials_port:
            return jsonify({"connected": False}), 200
        try:
            creds = await oauth_credentials_port.get_credentials(g.user_id, "gmail")
            if not creds:
                return jsonify({"connected": False}), 200

            indexed_through = None
            oldest_indexed_through = None
            if indexed_email_repo:
                state = await indexed_email_repo.get_indexing_state(g.user_id, "gmail")
                if state:
                    indexed_through = state.indexed_through.isoformat() if state.indexed_through else None
                    oldest_indexed_through = state.oldest_indexed_through.isoformat() if state.oldest_indexed_through else None

            indexing_active = False
            active_job_id = None
            if email_job_repo:
                latest_job = await email_job_repo.get_latest_job(g.user_id, "gmail")
                if latest_job and latest_job.status == "running":
                    indexing_active = True
                    active_job_id = latest_job.job_id

            return jsonify({
                "connected": True,
                "email_address": creds.email_address or None,
                "indexed_through": indexed_through,
                "oldest_indexed_through": oldest_indexed_through,
                "indexing_active": indexing_active,
                "active_job_id": active_job_id,
            }), 200
        except Exception as exc:
            logger.error(f"Error fetching Gmail status: {exc}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @bp.route("/api/gmail/index", methods=["POST"])
    @auth_required
    async def gmail_index():
        """
        Trigger Gmail indexing for the current user.

        Request body (optional JSON):
          mode:           "incremental" | "reindex" | "backfill"  (default: "incremental")
          backfill_until: str  — "YYYY-MM-DD" stop date for backfill mode.
          max_pages:      int  — sync-only; ignored in async (Cloud Tasks) mode.

        Async path (production): returns 202 immediately with job_id.
        Sync fallback (local dev): runs one page, returns job summary.
        """
        if not email_indexing_service or not oauth_credentials_port:
            return jsonify({"error": "Gmail indexing not configured"}), 501

        body = await request.get_json(silent=True) or {}

        mode = body.get("mode", "incremental")
        if mode not in ("incremental", "reindex", "backfill"):
            return jsonify({"error": f"Invalid mode '{mode}'. Must be incremental, reindex, or backfill"}), 400

        backfill_until = None
        if raw_until := body.get("backfill_until"):
            try:
                backfill_until = datetime.strptime(raw_until, "%Y-%m-%d")
            except ValueError:
                return jsonify({"error": "Invalid backfill_until format, expected YYYY-MM-DD"}), 400

        try:
            creds = await oauth_credentials_port.get_credentials(g.user_id, "gmail")
            if not creds:
                return jsonify({"error": "Gmail not connected"}), 404

            # Async path: Cloud Tasks (production)
            if task_queue and email_job_repo:
                job = email_indexing_service.create_job(
                    user_id=g.user_id,
                    provider="gmail",
                    triggered_by="cabinet",
                    mode=mode,
                    account_id=g.account_id,
                    backfill_until=backfill_until,
                )
                await task_queue.enqueue_email_indexing_task(job.job_id)
                await email_job_repo.create_job(job)
                logger.info(
                    f"📧 Cabinet async indexing enqueued: user={g.user_id[:8]} "
                    f"mode={mode} job={job.job_id[:8]}"
                )
                return jsonify({"job_id": job.job_id, "status": "running"}), 202

            # Sync fallback: local dev (no task_queue configured)
            server_cap = int(os.getenv("GMAIL_INDEX_MAX_PAGES", "50"))
            if mode in ("reindex", "backfill") and "max_pages" not in body:
                max_pages = None
            else:
                max_pages = min(int(body.get("max_pages", 1)), server_cap)

            logger.info(
                f"📧 Cabinet sync indexing: user={g.user_id[:8]} "
                f"mode={mode} max_pages={max_pages}"
            )
            job = await email_indexing_service.start_job(
                user_id=g.user_id,
                account_id=g.account_id,
                credentials=creds,
                triggered_by="cabinet",
                max_pages=max_pages,
                mode=mode,
                backfill_until=backfill_until,
            )
            return jsonify({
                "job_id": job.job_id,
                "status": job.status,
                "emails_fetched": job.emails_fetched,
                "emails_stored": job.emails_stored,
                "emails_failed": job.emails_failed,
                "has_more": bool(job.next_page_token),
            }), 200

        except Exception as exc:
            logger.error(f"Error running Gmail indexing: {exc}", exc_info=True)
            return jsonify({"error": "Indexing failed", "detail": str(exc)}), 500

    @bp.route("/api/gmail/jobs/<job_id>", methods=["GET"])
    @auth_required
    async def gmail_job_status(job_id: str):
        """Return current status and progress counters for a specific indexing job."""
        if not email_job_repo:
            return jsonify({"error": "Gmail integration not configured"}), 501
        try:
            job = await email_job_repo.get_job(job_id)
            if not job or job.user_id != g.user_id:
                return jsonify({"error": "Job not found"}), 404
            return jsonify({
                "job_id": job.job_id,
                "status": job.status,
                "mode": job.mode,
                "emails_fetched": job.emails_fetched,
                "emails_stored": job.emails_stored,
                "emails_failed": job.emails_failed,
                "has_more": bool(job.next_page_token),
                "started_at": job.started_at.isoformat(),
                "updated_at": job.updated_at.isoformat(),
                "completed_at": job.completed_at.isoformat() if job.completed_at else None,
            }), 200
        except Exception as exc:
            logger.error(f"Error fetching job {job_id}: {exc}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @bp.route("/api/gmail/jobs/<job_id>/cancel", methods=["POST"])
    @auth_required
    async def gmail_cancel_job(job_id: str):
        """Cancel a running indexing job. Worker will stop chaining on the next task."""
        if not email_job_repo:
            return jsonify({"error": "Gmail integration not configured"}), 501
        try:
            job = await email_job_repo.get_job(job_id)
            if not job or job.user_id != g.user_id:
                return jsonify({"error": "Job not found"}), 404
            if job.status != "running":
                return jsonify({"error": f"Job is not running (status: {job.status})"}), 409
            await email_job_repo.update_job(job_id, {
                "status": "cancelled",
                "updated_at": datetime.now(timezone.utc),
            })
            logger.info(f"📧 Job {job_id[:8]} cancelled by user {g.user_id[:8]}")
            return jsonify({"status": "cancelled"}), 200
        except Exception as exc:
            logger.error(f"Error cancelling job {job_id}: {exc}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @bp.route("/api/gmail/disconnect", methods=["DELETE"])
    @auth_required
    async def gmail_disconnect():
        """Revoke Gmail access token and delete credentials. Indexed data is preserved."""
        if not oauth_credentials_port:
            return jsonify({"error": "Gmail integration not configured"}), 501
        try:
            creds = await oauth_credentials_port.get_credentials(g.user_id, "gmail")
            if not creds:
                return jsonify({"error": "Gmail not connected"}), 404

            # Best-effort token revocation at Google
            if gmail_oauth_service:
                await gmail_oauth_service.revoke_token(creds.access_token)

            # Delete credentials only — indexed data stays intact
            await oauth_credentials_port.revoke_credentials(g.user_id, "gmail")

            logger.info(f"🔌 Gmail disconnected for user={g.user_id[:8]} (indexed data preserved)")
            return jsonify({"success": True}), 200
        except Exception as exc:
            logger.error(f"Error disconnecting Gmail: {exc}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @bp.route("/api/gmail/auto-index", methods=["GET"])
    @auth_required
    async def gmail_auto_index_get():
        """Return current auto-index schedule settings for the authenticated user."""
        try:
            user = await user_repo.get_user(g.user_id)
            if not user:
                return jsonify({"error": "User not found"}), 404
            return jsonify({
                "enabled": user.config.gmail_auto_index,
                "hour": user.config.gmail_auto_index_hour,
            }), 200
        except Exception as exc:
            logger.error(f"Error fetching auto-index settings: {exc}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @bp.route("/api/gmail/auto-index", methods=["PUT"])
    @auth_required
    async def gmail_auto_index_set():
        """Update auto-index schedule. Body: {\"enabled\": bool, \"hour\": 0-23}"""
        try:
            body = await request.get_json(force=True) or {}
            enabled = body.get("enabled")
            hour = body.get("hour")

            if enabled is None or not isinstance(enabled, bool):
                return jsonify({"error": "enabled (bool) required"}), 400
            if hour is None or not isinstance(hour, int) or not (0 <= hour <= 23):
                return jsonify({"error": "hour must be integer 0-23"}), 400

            user = await user_repo.get_user(g.user_id)
            if not user:
                return jsonify({"error": "User not found"}), 404

            user.config.gmail_auto_index = enabled
            user.config.gmail_auto_index_hour = hour
            await user_repo.update_user(user)
            return jsonify({"enabled": enabled, "hour": hour}), 200
        except Exception as exc:
            logger.error(f"Error updating auto-index settings: {exc}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @bp.route("/api/gmail/daily-review", methods=["GET"])
    @auth_required
    async def gmail_daily_review_get():
        """Return current daily review schedule settings for the authenticated user."""
        try:
            user = await user_repo.get_user(g.user_id)
            if not user:
                return jsonify({"error": "User not found"}), 404
            return jsonify({
                "enabled": user.config.gmail_daily_review,
                "hour": user.config.gmail_daily_review_hour,
            }), 200
        except Exception as exc:
            logger.error(f"Error fetching daily-review settings: {exc}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @bp.route("/api/gmail/daily-review", methods=["PUT"])
    @auth_required
    async def gmail_daily_review_set():
        """Update daily review schedule. Body: {\"enabled\": bool, \"hour\": 0-23}"""
        try:
            body = await request.get_json(force=True) or {}
            enabled = body.get("enabled")
            hour = body.get("hour")

            if enabled is None or not isinstance(enabled, bool):
                return jsonify({"error": "enabled (bool) required"}), 400
            if hour is None or not isinstance(hour, int) or not (0 <= hour <= 23):
                return jsonify({"error": "hour must be integer 0-23"}), 400

            user = await user_repo.get_user(g.user_id)
            if not user:
                return jsonify({"error": "User not found"}), 404

            user.config.gmail_daily_review = enabled
            user.config.gmail_daily_review_hour = hour
            await user_repo.update_user(user)
            return jsonify({"enabled": enabled, "hour": hour}), 200
        except Exception as exc:
            logger.error(f"Error updating daily-review settings: {exc}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    # =========================================================================
    # Deep Research settings
    # =========================================================================

    @bp.route("/api/user/deep-research", methods=["GET"])
    @auth_required
    async def deep_research_get():
        """Return deep research settings for the authenticated user."""
        try:
            user = await user_repo.get_user(g.user_id)
            if not user:
                return jsonify({"error": "User not found"}), 404
            return jsonify({
                "second_pass": user.config.deep_research_second_pass,
            }), 200
        except Exception as exc:
            logger.error(f"Error fetching deep-research settings: {exc}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @bp.route("/api/user/deep-research", methods=["PUT"])
    @auth_required
    async def deep_research_set():
        """Update deep research settings. Body: {\"second_pass\": bool}"""
        try:
            body = await request.get_json(force=True) or {}
            second_pass = body.get("second_pass")

            if second_pass is None or not isinstance(second_pass, bool):
                return jsonify({"error": "second_pass (bool) required"}), 400

            user = await user_repo.get_user(g.user_id)
            if not user:
                return jsonify({"error": "User not found"}), 404

            user.config.deep_research_second_pass = second_pass
            await user_repo.update_user(user)
            return jsonify({"second_pass": second_pass}), 200
        except Exception as exc:
            logger.error(f"Error updating deep-research settings: {exc}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    # =========================================================================
    # Tasks integration endpoints
    # =========================================================================

    @bp.route("/api/tasks/status", methods=["GET"])
    @auth_required
    async def tasks_status():
        """Return task list connection state for the authenticated user."""
        if not oauth_credentials_port:
            return jsonify({"connected": False}), 200
        try:
            creds = await oauth_credentials_port.get_credentials(g.user_id, "google_tasks")
            if not creds:
                return jsonify({"connected": False}), 200
            return jsonify({
                "connected": True,
                "email_address": creds.email_address or None,
            }), 200
        except Exception as exc:
            logger.error(f"Error fetching Tasks status: {exc}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @bp.route("/api/tasks/disconnect", methods=["DELETE"])
    @auth_required
    async def tasks_disconnect():
        """Revoke task list access token and delete credentials."""
        if not oauth_credentials_port:
            return jsonify({"error": "Tasks integration not configured"}), 501
        try:
            creds = await oauth_credentials_port.get_credentials(g.user_id, "google_tasks")
            if not creds:
                return jsonify({"error": "Tasks not connected"}), 404
            if google_tasks_oauth_service:
                await google_tasks_oauth_service.revoke_token(creds.access_token)
            await oauth_credentials_port.revoke_credentials(g.user_id, "google_tasks")
            logger.info(f"🔌 Tasks disconnected for user={g.user_id[:8]}")
            return jsonify({"success": True}), 200
        except Exception as exc:
            logger.error(f"Error disconnecting Tasks: {exc}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    # =========================================================================
    # Microsoft To Do task endpoints
    # =========================================================================

    @bp.route("/api/tasks/microsoft/status", methods=["GET"])
    @auth_required
    async def ms_tasks_status():
        """Return MS To Do connection state and active subscriptions."""
        if not task_setup:
            return jsonify({"connected": False}), 200
        try:
            status = await task_setup.get_status(g.user_id)
            return jsonify(status), 200
        except Exception as exc:
            logger.error(f"Error fetching MS Tasks status: {exc}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @bp.route("/api/tasks/microsoft/reindex", methods=["POST"])
    @auth_required
    async def ms_tasks_reindex():
        """Trigger full re-index of all MS To Do task lists."""
        if not task_setup:
            return jsonify({"error": "MS Tasks integration not configured"}), 501
        try:
            await task_setup.reindex_all(g.user_id)
            return jsonify({"status": "ok"}), 200
        except Exception as exc:
            logger.error(f"Error triggering MS Tasks reindex: {exc}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @bp.route("/api/tasks/microsoft/lists", methods=["GET"])
    @auth_required
    async def ms_tasks_lists():
        """Return all MS To Do task lists for the authenticated user."""
        if not tasks_provider:
            return jsonify({"error": "MS Tasks integration not configured"}), 501
        try:
            lists = await tasks_provider.list_task_lists(g.user_id)
            return jsonify({"lists": [{"id": tl.id, "name": tl.name} for tl in lists]}), 200
        except Exception as exc:
            logger.error(f"Error fetching MS task lists: {exc}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @bp.route("/api/tasks/microsoft/disconnect", methods=["DELETE"])
    @auth_required
    async def ms_tasks_disconnect():
        """Disconnect MS To Do: delete subscriptions, revoke token, clear index."""
        if not task_setup:
            return jsonify({"error": "MS Tasks integration not configured"}), 501
        try:
            await task_setup.disconnect(g.user_id)
            logger.info(f"🔌 MS Tasks disconnected for user={g.user_id[:8]}")
            return jsonify({"success": True}), 200
        except Exception as exc:
            logger.error(f"Error disconnecting MS Tasks: {exc}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    @bp.route("/api/gmail/data", methods=["DELETE"])
    @auth_required
    async def gmail_delete_data():
        """Delete all indexed email facts. Irreversible — requires re-indexing."""
        if not indexed_email_repo:
            return jsonify({"error": "Gmail integration not configured"}), 501
        try:
            await indexed_email_repo.delete_by_user(g.user_id)
            logger.info(f"🗑️ Gmail indexed data deleted for user={g.user_id[:8]}")
            return jsonify({"success": True}), 200
        except Exception as exc:
            logger.error(f"Error deleting Gmail data: {exc}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500

    return bp
