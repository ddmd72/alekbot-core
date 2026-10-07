"""
Public front door: the landing page at `/`, its share image and `robots.txt`.

`/` used to redirect everyone to the login. A visitor without a session now gets a static page
(it is the GitHub repo's homepage); a signed-in owner still lands in the Cabinet. Nothing here
reads user data, so every route is public on purpose.
"""
import os

from quart import Blueprint, redirect, request, send_file

_ROBOTS = "User-agent: *\nAllow: /$\nDisallow: /\n"


def create_landing_blueprint(session_service) -> Blueprint:
    bp = Blueprint("landing", __name__)
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

    @bp.route("/", methods=["GET"])
    async def root():
        # Login sets the `access_token` JWT *cookie* (oauth_app), not a Quart session entry —
        # checking `session` here sent a signed-in owner to the landing page every time.
        token = request.cookies.get("access_token")
        if token:
            try:
                session_service.verify_access_token(token)
                return redirect("/cabinet")
            except Exception:
                pass  # expired or forged: show the public page, Sign in starts a fresh login
        response = await send_file(os.path.join(static_dir, "landing.html"), mimetype="text/html")
        # Revalidate on every load: send_file's 12 h max-age would serve a stale page after deploys.
        response.headers["Cache-Control"] = "no-cache"
        return response

    @bp.route("/og.png", methods=["GET"])
    async def og_image():
        return await send_file(os.path.join(static_dir, "og.png"), mimetype="image/png")

    # `Allow: /$` is the exact-root match (Google and Bing honour `$`); everything else stays closed.
    @bp.route("/robots.txt", methods=["GET"])
    async def robots():
        return _ROBOTS, 200, {"Content-Type": "text/plain"}

    return bp
