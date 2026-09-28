from functools import wraps

from quart import g, jsonify, request

from ..utils.logger import logger


def make_auth_required(session_service):
    """The Cabinet's JWT gate (Bearer header for API clients, `access_token` cookie for the web UI),
    shared by every Cabinet blueprint."""

    def auth_required(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            auth_header = request.headers.get("Authorization")
            if auth_header and auth_header.startswith("Bearer "):
                token = auth_header.split(" ")[1]
            else:
                token = request.cookies.get("access_token")
            if not token:
                return jsonify({"error": "Missing authorization"}), 401
            try:
                payload = session_service.verify_access_token(token)
                g.user_id = payload["sub"]
                g.account_id = payload["account_id"]
                g.role = payload.get("role", "viewer")
            except Exception as e:
                logger.warning(f"Auth failed: {e}")
                return jsonify({"error": "Invalid or expired token"}), 401
            return await func(*args, **kwargs)
        return wrapper

    return auth_required
