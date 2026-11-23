"""The label and the platform hint of a new device session, read from its User-Agent (CABINET_DEVICE_SESSIONS_RFC §7.1).

A User-Agent is untrusted text the client chose. None of it is copied: it only selects words from a fixed vocabulary
(a device, a browser), so a stored label can carry no markup, mention or instruction whatever the client sent. The
words can still be wrong (a client can claim to be anything): they name a session for its owner in the devices list
and the new-sign-in notice, and they decide nothing.

An iOS Home Screen web app sends Safari's User-Agent without the `Safari/` token; that is how its own cookie jar (RFC
F4) shows up as "Home Screen". The label adds the entry point the sign-in returns to ("call" for the call pages),
because each Home Screen app is one entry point and the owner tells them apart by it.
"""
from typing import Tuple

_CALL_PAGES = "/cabinet/call"

# (marker in the User-Agent, the word shown); the first match wins, so the more specific marker comes first.
_DEVICES = (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("CrOS", "ChromeOS"),
            ("Macintosh", "Mac"), ("Windows", "Windows"), ("Linux", "Linux"))
# Edge and Chrome both carry `Safari/`, Edge also `Chrome/`: the order matters.
_BROWSERS = (("EdgiOS", "Edge"), ("Edg/", "Edge"), ("FxiOS", "Firefox"), ("Firefox/", "Firefox"),
             ("CriOS", "Chrome"), ("Chrome/", "Chrome"), ("Safari/", "Safari"))


def _first(user_agent: str, table: Tuple[Tuple[str, str], ...]) -> str:
    return next((word for marker, word in table if marker in user_agent), "")


def describe_device(user_agent: str, return_path: str) -> Tuple[str, str]:
    """(label, platform_hint) of a session signed in with `user_agent`, returning to `return_path`: e.g.
    ("iPhone — Home Screen (call)", "iPhone — Home Screen") or ("Mac — Chrome", "Mac — Chrome")."""
    device = _first(user_agent, _DEVICES) or "Unknown device"
    browser = _first(user_agent, _BROWSERS)
    if not browser:
        browser = "Home Screen" if device in ("iPhone", "iPad") else "Browser"
    platform_hint = f"{device} — {browser}"
    path = return_path.split("?", 1)[0]
    on_call_page = path == _CALL_PAGES or path.startswith(_CALL_PAGES + "/")
    label = f"{platform_hint} (call)" if on_call_page else platform_hint
    return label, platform_hint
