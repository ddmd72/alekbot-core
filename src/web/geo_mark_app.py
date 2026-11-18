"""The Slack `$geo` page and its fix route, `GET|POST /geo/<link_id>` (GEO_MARKS_RFC §4.5).

Anonymous by design: the 128-bit link id is the capability (`GeoMarkService` keeps it under a keyed digest, ten
minutes, single use).

- `GET` shows the page for a live link and consumes nothing (`peek_link`), so Slack's unfurler or a reload is harmless.
  Every GET refusal (a wrong-shaped, unknown or expired id) is one plain 404 with no store read for a wrong shape.
- `POST` takes `{latitude, longitude, accuracy}` as JSON. Origin, content type, body size and the coordinates are all
  checked before `redeem_link`, so a refused request leaves the link usable for the page's retry.

The page's one inline script is allowed by its hash in the CSP; the owner's strings travel in a JSON data block that
is never executed. Logs never carry the link id or coordinates.
"""
import base64
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlsplit

from quart import Blueprint, Response, request
from werkzeug.exceptions import RequestEntityTooLarge

from ..domain.geo_mark import LINK_ID_PATTERN, GeoFix, GeoSource
from ..services.geo_mark_service import GeoMarkService, GeoRedeemStatus
from ..domain.ui_messages import UIMessage
from ..utils.logger import logger

BODY_CAP_BYTES = 1024
_SCRIPT = re.compile(r"<script>(.*?)</script>", re.DOTALL)
_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

_BASE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex",
    "Permissions-Policy": "geolocation=(self)",
}


def script_csp(html: str) -> str:
    """The CSP of the page: nothing but its own inline script (by hash) and requests back to this host."""
    match = _SCRIPT.search(html)
    if match is None:
        raise ValueError("geo.html has no inline script")
    digest = base64.b64encode(hashlib.sha256(match.group(1).encode("utf-8")).digest()).decode("ascii")
    return (f"default-src 'none'; script-src 'sha256-{digest}'; style-src 'unsafe-inline'; connect-src 'self'; "
            f"base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def _json_for_html(value: Any) -> str:
    """JSON safe inside a `<script>` data block: no `<`, `>` or `&` survives to end the block."""
    return (json.dumps(value, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def _text(status: int, body: str) -> Response:
    return Response(body, status=status, mimetype="text/plain", headers=dict(_BASE_HEADERS))


def _json(status: int, body: dict) -> Response:
    return Response(json.dumps(body), status=status, mimetype="application/json", headers=dict(_BASE_HEADERS))


def _same_origin(origin: Optional[str]) -> bool:
    """No Origin header (a same-origin fetch may omit it) is fine; a present one must name this host."""
    if origin is None:
        return True
    return urlsplit(origin).netloc == request.host


def create_geo_mark_blueprint(geo_marks: GeoMarkService) -> Blueprint:
    bp = Blueprint("geo_mark", __name__)

    with open(os.path.join(_STATIC, "geo.html"), encoding="utf-8") as f:
        template = f.read()
    csp = script_csp(template)
    gone = geo_marks.page_strings("en")[UIMessage.GEO_PAGE_GONE.value]

    @bp.route("/geo/<link_id>", methods=["GET"])
    async def geo_page(link_id: str) -> Response:
        if not LINK_ID_PATTERN.fullmatch(link_id):
            return _text(404, gone)
        binding = await geo_marks.peek_link(link_id)
        if binding is None:
            return _text(404, gone)
        html = (template.replace("__LANG__", re.sub(r"[^A-Za-z-]", "", binding.language) or "en")
                .replace("__STRINGS__", _json_for_html(geo_marks.page_strings(binding.language))))
        headers = dict(_BASE_HEADERS)
        headers["Content-Security-Policy"] = csp
        return Response(html, mimetype="text/html", headers=headers)

    @bp.route("/geo/<link_id>", methods=["POST"])
    async def geo_fix(link_id: str) -> Response:
        if not LINK_ID_PATTERN.fullmatch(link_id):
            return _text(404, gone)
        if not _same_origin(request.headers.get("Origin")):
            return _json(403, {"status": "forbidden"})
        if request.mimetype != "application/json":
            return _json(415, {"status": "unsupported_media_type"})
        if (request.content_length or 0) > BODY_CAP_BYTES:
            return _json(413, {"status": "too_large"})
        request.max_content_length = BODY_CAP_BYTES
        try:
            raw = await request.get_data()
        except RequestEntityTooLarge:
            return _json(413, {"status": "too_large"})
        if len(raw) > BODY_CAP_BYTES:
            return _json(413, {"status": "too_large"})
        try:
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("body is not an object")
            fix = GeoFix.parse(body.get("latitude"), body.get("longitude"), body.get("accuracy"),
                               datetime.now(timezone.utc), GeoSource.SLACK_LINK)
        except ValueError:      # JSONDecodeError and UnicodeDecodeError are ValueErrors
            return _json(400, {"status": "bad_request"})
        outcome = await geo_marks.redeem_link(link_id, fix)
        if outcome.status is GeoRedeemStatus.GONE:
            return _json(410, {"status": outcome.status.value})
        if outcome.status is GeoRedeemStatus.NOT_SAVED or outcome.mark is None:
            logger.error("[GeoMark] fix taken but not saved")
            return _json(500, {"status": GeoRedeemStatus.NOT_SAVED.value})
        return _json(200, {"status": outcome.status.value, "address": outcome.mark.address,
                           "accuracy_m": outcome.mark.fix.accuracy_m})

    return bp
