from quart import Blueprint, request, jsonify, g, send_file, redirect, abort, send_from_directory
from functools import wraps
from typing import Dict, Any
import os

from ..services.invite_code_service import InviteCodeService
from ..services.session_service import SessionService
from ..ports.user_repository import UserRepository
from ..ports.repository import FactRepository
from ..ports.embedding_service import EmbeddingService
from ..utils.logger import logger

# Documentation owner (loaded from environment variable - Secret Manager)
DOCS_OWNER_USER_ID = os.getenv('DOCS_OWNER_USER_ID')


def create_user_cabinet_blueprint(
    invite_service: InviteCodeService,
    session_service: SessionService,
    user_repo: UserRepository,
    fact_repo: FactRepository,
    embedding_service: EmbeddingService,
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

    return bp
