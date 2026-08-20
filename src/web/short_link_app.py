"""
Short-link blueprint — the `/s/<code>` redirect route.

A short link is `https://<domain>/s/<code>`, where code is a random
10-character base62 string minted by ShortLinkService. This route resolves
it to a target URL (typically an `/f/<token>` capability link) and 302s
there. It knows nothing about files, tokens, or gating — that's the target
URL's problem; this route is a generic short-link redirect.

Malformed codes are rejected by a format check before touching the
resolver, so scanning garbage paths never costs a Firestore read.
"""
import re

from quart import Blueprint, Response, redirect

from ..services.short_link_service import ShortLinkService

_CODE_PATTERN = re.compile(r"^[A-Za-z0-9]{10}$")


def create_short_link_blueprint(short_links: ShortLinkService) -> Blueprint:
    bp = Blueprint("short_link", __name__)

    @bp.get("/s/<code>")
    async def resolve_short_link(code: str):
        if not _CODE_PATTERN.match(code):
            return Response("Invalid or expired link.", status=404)

        target = await short_links.resolve(code)
        if not target:
            return Response("This link has expired.", status=404)

        return redirect(target)

    return bp
