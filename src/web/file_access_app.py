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
      3. video_generation/ keys: render a landing page (Download + Share
         buttons) instead of redirecting — see _VIDEO_KEY_PREFIX below.
         Everything else: mint a fresh short-lived (5 min) GCS V4 signed URL
         for the object key and 302-redirect the browser to it (these are
         HTML reports/docs meant to be viewed, not downloaded).

    GET /f/<token>/raw
      Same-origin byte stream backing the video landing page's buttons —
      proxies MediaStoragePort.fetch() (the same server-side read open_file
      uses). Needed because client JS can't fetch() the GCS signed URL
      directly: the bucket is private with no CORS configured for browser
      reads, only for top-level navigation/redirect.

The bucket is private; the token is the long-lived capability, the signed URL is
the short-lived storage grant minted per click. This decouples link lifetime
(token TTL: 5d/30d) from the 7-day GCS signing ceiling.
"""
from urllib.parse import quote

from quart import Blueprint, Response, redirect, request

from ..ports.media_storage_port import MediaStoragePort
from ..services.file_access_token_service import (
    FileAccessToken,
    FileAccessTokenService,
    FileAccessTokenExpired,
    FileAccessTokenInvalid,
)
from ..services.session_service import SessionService
from ..utils.logger import logger

# Lifetime of the minted GCS signed URL (one click). Short — only needs to
# survive the redirect + the browser/provider fetch.
_SIGNED_URL_TTL = 300  # 5 minutes

# Delivered video keys get the landing page; everything else (HTML reports,
# docs) keeps the plain redirect-to-view behavior.
_VIDEO_KEY_PREFIX = "video_generation/"


def create_file_access_blueprint(
    token_service: FileAccessTokenService,
    media_storage: MediaStoragePort,
    session_service: SessionService,
) -> Blueprint:
    bp = Blueprint("file_access", __name__)

    async def _verify_and_gate(
        token: str,
    ) -> tuple[FileAccessToken | None, Response | None]:
        """Token verify + Cabinet-cookie gating, shared by both routes below.

        Returns (FileAccessToken, None) on success, or (None, error_response).
        """
        try:
            access = token_service.verify(token)
        except FileAccessTokenExpired:
            logger.info("[FileAccess] expired token")
            return None, Response("This link has expired.", status=401)
        except FileAccessTokenInvalid as e:
            logger.warning("[FileAccess] invalid token: %s", e)
            return None, Response("Invalid or malformed link.", status=401)

        if access.gated:
            cabinet_token = request.cookies.get("access_token")
            if not cabinet_token:
                next_url = quote(request.url, safe="")
                return None, redirect(f"/auth/login?next={next_url}")
            try:
                payload = session_service.verify_access_token(cabinet_token)
            except Exception as e:  # noqa: BLE001
                logger.warning("[FileAccess] gated: invalid Cabinet token — %s", e)
                next_url = quote(request.url, safe="")
                return None, redirect(f"/auth/login?next={next_url}")
            if payload.get("sub") != access.user_id:
                # Logged in, but as a different user than the file's owner.
                logger.warning("[FileAccess] gated: user mismatch")
                return None, Response("You do not have access to this file.", status=403)

        return access, None

    @bp.get("/f/<token>")
    async def access_file(token: str):
        access, error = await _verify_and_gate(token)
        if error:
            return error

        if access.key.startswith(_VIDEO_KEY_PREFIX):
            return Response(_video_landing_page(token, access.key), mimetype="text/html")

        try:
            signed = await media_storage.generate_signed_url(access.key, _SIGNED_URL_TTL)
        except Exception as e:  # noqa: BLE001
            logger.error("[FileAccess] signing failed for %s: %s", access.key, e, exc_info=True)
            return Response("File temporarily unavailable.", status=503)

        return redirect(signed)

    @bp.get("/f/<token>/raw")
    async def access_file_raw(token: str):
        access, error = await _verify_and_gate(token)
        if error:
            return error

        try:
            data = await media_storage.fetch(access.key)
        except Exception as e:  # noqa: BLE001
            logger.error("[FileAccess] raw fetch failed for %s: %s", access.key, e, exc_info=True)
            return Response("File temporarily unavailable.", status=503)

        filename = access.key.rsplit("/", 1)[-1]
        total = len(data)
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Accept-Ranges": "bytes",
        }

        # WebKit's <video> pipeline (Safari/iOS) probes with a Range request
        # before it will play anything, even a small buffered-whole file — no
        # Range support means the video silently never plays.
        range_header = request.headers.get("Range", "")
        if range_header.startswith("bytes="):
            start_s, _, end_s = range_header[len("bytes="):].partition("-")
            try:
                start = int(start_s) if start_s else 0
                end = int(end_s) if end_s else total - 1
            except ValueError:
                start, end = 0, total - 1
            end = min(end, total - 1)
            if 0 <= start <= end:
                chunk = data[start : end + 1]
                headers["Content-Range"] = f"bytes {start}-{end}/{total}"
                headers["Content-Length"] = str(len(chunk))
                return Response(chunk, status=206, mimetype="video/mp4", headers=headers)

        headers["Content-Length"] = str(total)
        return Response(data, mimetype="video/mp4", headers=headers)

    return bp


def _video_landing_page(token: str, key: str) -> str:
    """Minimal Download + Share landing page for a delivered video.

    Share uses the Web Share API's file form (navigator.share({files})), which
    hands the actual video to the OS share sheet (Telegram/WhatsApp/etc. can
    receive it as a real attachment) — not the page link. Hidden when the
    browser doesn't support sharing files (desktop Firefox, older browsers);
    Download always works.
    """
    filename = key.rsplit("/", 1)[-1]
    raw_url = f"/f/{token}/raw"
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Your video</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         background: #0b0b0f; color: #f2f2f5; margin: 0;
         display: flex; align-items: center; justify-content: center;
         min-height: 100vh; padding: 24px; box-sizing: border-box; }}
  .card {{ max-width: 360px; width: 100%; text-align: center; }}
  h1 {{ font-size: 17px; font-weight: 600; margin: 0 0 20px; }}
  video {{ width: 100%; max-height: 70vh; border-radius: 12px; background: #000;
          margin-bottom: 20px; display: block; }}
  .row {{ display: flex; gap: 12px; }}
  button, a.btn {{ flex: 1; padding: 14px 20px; border-radius: 10px; border: none;
        font-size: 15px; font-weight: 600; cursor: pointer; text-decoration: none;
        text-align: center; box-sizing: border-box; font-family: inherit; }}
  .primary {{ background: #4f7cff; color: #fff; }}
  .secondary {{ background: #23232b; color: #f2f2f5; }}
  #status {{ margin-top: 14px; font-size: 13px; color: #9a9aa5; min-height: 16px; }}
</style>
</head>
<body>
<div class="card">
  <h1>Your video is ready</h1>
  <video controls playsinline src="{raw_url}"></video>
  <div class="row">
    <a class="btn secondary" href="{raw_url}" download="{filename}">Download</a>
    <button class="primary" id="shareBtn" onclick="shareVideo()">Share</button>
  </div>
  <div id="status"></div>
</div>
<script>
async function shareVideo() {{
  var status = document.getElementById('status');
  var btn = document.getElementById('shareBtn');
  try {{
    btn.disabled = true;
    status.textContent = 'Preparing...';
    var res = await fetch("{raw_url}");
    var blob = await res.blob();
    var file = new File([blob], "{filename}", {{ type: "video/mp4" }});
    if (navigator.canShare && navigator.canShare({{ files: [file] }})) {{
      await navigator.share({{ files: [file] }});
      status.textContent = '';
    }} else {{
      status.textContent = "Sharing isn't supported in this browser — use Download instead.";
    }}
  }} catch (e) {{
    if (e && e.name !== 'AbortError') {{
      status.textContent = "Couldn't share — try Download instead.";
    }}
  }} finally {{
    btn.disabled = false;
  }}
}}
(function() {{
  var probe = new File([""], "t.mp4", {{ type: "video/mp4" }});
  if (!(navigator.canShare && navigator.canShare({{ files: [probe] }}))) {{
    document.getElementById('shareBtn').style.display = 'none';
  }}
}})();
</script>
</body>
</html>"""
