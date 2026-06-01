"""
File access blueprint — the `/f/<token>` capability-link route.

A delivered file link is `https://<domain>/f/<token>`, where token is an HS256
capability JWT minted by FileLinkService. This route is the ONLY public way to
reach a private storage object:

    GET /f/<token>
      1. Verify the capability token (signature + expiry + type).
      2. If the token is `gated` (daily email review — PII): require a valid
         Cabinet JWT cookie whose user matches the token's user_id; otherwise
         redirect to /auth/login. Non-gated tokens are openable from any client.
      3. Mint a fresh short-lived (5 min) GCS V4 signed URL for the object key.
      4. 302-redirect the browser to it.

The bucket is private; the token is the long-lived capability, the signed URL is
the short-lived storage grant minted per click. This decouples link lifetime
(token TTL: 5d/30d) from the 7-day GCS signing ceiling.
"""
from urllib.parse import quote

from quart import Blueprint, Response, redirect, request

from ..ports.media_storage_port import MediaStoragePort
from ..services.file_access_token_service import (
    FileAccessTokenService,
    FileAccessTokenExpired,
    FileAccessTokenInvalid,
)
from ..services.session_service import SessionService
from ..utils.logger import logger

# Lifetime of the minted GCS signed URL (one click). Short — only needs to
# survive the redirect + the browser/provider fetch.
_SIGNED_URL_TTL = 300  # 5 minutes


def create_file_access_blueprint(
    token_service: FileAccessTokenService,
    media_storage: MediaStoragePort,
    session_service: SessionService,
) -> Blueprint:
    bp = Blueprint("file_access", __name__)

    @bp.get("/f/<token>")
    async def access_file(token: str):
        # 1. Verify capability token.
        try:
            access = token_service.verify(token)
        except FileAccessTokenExpired:
            logger.info("[FileAccess] expired token")
            return Response("This link has expired.", status=401)
        except FileAccessTokenInvalid as e:
            logger.warning("[FileAccess] invalid token: %s", e)
            return Response("Invalid or malformed link.", status=401)

        # 2. Gated artifacts (email review) require a matching Cabinet session.
        if access.gated:
            cabinet_token = request.cookies.get("access_token")
            if not cabinet_token:
                next_url = quote(request.url, safe="")
                return redirect(f"/auth/login?next={next_url}")
            try:
                payload = session_service.verify_access_token(cabinet_token)
            except Exception as e:  # noqa: BLE001
                logger.warning("[FileAccess] gated: invalid Cabinet token — %s", e)
                next_url = quote(request.url, safe="")
                return redirect(f"/auth/login?next={next_url}")
            if payload.get("sub") != access.user_id:
                # Logged in, but as a different user than the file's owner.
                logger.warning("[FileAccess] gated: user mismatch")
                return Response("You do not have access to this file.", status=403)

        # 3. Mint a fresh short-lived signed URL and 302 to it.
        try:
            signed = await media_storage.generate_signed_url(access.key, _SIGNED_URL_TTL)
        except Exception as e:  # noqa: BLE001
            logger.error("[FileAccess] signing failed for %s: %s", access.key, e, exc_info=True)
            return Response("File temporarily unavailable.", status=503)

        return redirect(signed)

    return bp
