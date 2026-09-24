"""Pull reading-shaped links out of a delegation result string (VOICE_COMPANION_RFC §4.10 rule 2,
generalized to any specialist, not only ask_alek). Pure function: no I/O, stdlib only."""
import json
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from .messaging import SmartResponse

# Delegation results reaching this vary wildly in shape (raw JSON, JSON embedded in a
# fan-out "Primary specialist: ..." string, or bare prose with URLs) — a sane cap keeps
# a pathological result from flooding the chat copy with anchors.
MAX_LINKS = 10

_URL_RE = re.compile(r'https?://[^\s<>"\']+')
# Flat (non-nested) {...} objects only — matches WebSearchAgent's per-finding shape.
# A nested envelope like {"findings": [{...}, {...}]} never matches this pattern itself
# (its inner "{" blocks the outer "}" from being reached), so finditer naturally lands
# on the inner finding objects instead.
_JSON_OBJ_RE = re.compile(r"\{[^{}]*\}")
# Slack's own markup, e.g. "<https://x.y/path|Title>" — matched BEFORE the bare-URL scan so
# the title never leaks into the extracted URL and the bare scan skips the covered span.
_SLACK_LINK_RE = re.compile(r'<(https?://[^\s<>|]+)\|([^<>]+)>')
# An escape that can never be part of a URL (newline, tab, quote, backslash, Python's \xNN) ends
# it. Cut on the escaped text: a Python-only escape makes json.loads fail, so relying on
# the decoded newline alone let the match run into the next line.
_ESCAPED_BREAK_RE = re.compile(r"""\\(?:[nrtfbv'"\\]|x[0-9a-fA-F]{2})""")
# Backslash too: _URL_RE stops before a quote, so an escaped quote after a URL leaves its backslash.
_TRAILING_PUNCT = ".,;:!?)]}\"'\\"


def _clean_url(url: str) -> str:
    """Strip trailing punctuation picked up from surrounding prose, keeping a balanced ')'."""
    while url and url[-1] in _TRAILING_PUNCT:
        if url[-1] == ")" and url.count("(") >= url.count(")"):
            break
        url = url[:-1]
    return url


def _json_unescape(fragment: str) -> str:
    """A URL landing on the bare-URL path may still carry JSON string escapes (``\\/``, ``\\uXXXX``)
    when it came from a finding the flat ``_JSON_OBJ_RE`` could not parse (e.g. nested braces).
    Decoding via ``json.loads`` on the quoted fragment handles every escape ``json`` defines,
    instead of a partial hand-rolled replace table; an unparseable fragment is returned as-is."""
    try:
        return json.loads(f'"{fragment}"')
    except ValueError:
        return fragment


def _host_title(url: str) -> str:
    host = urlparse(url).hostname or url
    return host[4:] if host.startswith("www.") else host


def extract_result_links(text: str) -> List[Dict[str, Any]]:
    """Every http(s) URL in ``text``, deduped in order, as [{"anchor", "title", "url"}, ...]
    (the same shape SmartResponse.link_list / send_long_text(link_list=...) render).

    Title: the JSON object's "source" or "title" field when the URL sits inside one
    (WebSearchAgent's findings shape), Slack markup's own title text, else the URL's host
    without "www.".
    """
    if not text:
        return []

    titles: Dict[str, str] = {}  # url -> title, insertion order preserved
    covered_spans: List[Tuple[int, int]] = []
    for match in _JSON_OBJ_RE.finditer(text):
        try:
            obj = json.loads(match.group(0))
        except ValueError:
            continue
        url = obj.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        url = _clean_url(url)
        if url and url not in titles:
            source: Optional[str] = obj.get("source") or obj.get("title")
            titles[url] = source if isinstance(source, str) and source else _host_title(url)
        covered_spans.append(match.span())

    def _already_covered(pos: int) -> bool:
        return any(start <= pos < end for start, end in covered_spans)

    for match in _SLACK_LINK_RE.finditer(text):
        if _already_covered(match.start()):
            continue
        url = _clean_url(match.group(1))
        title = match.group(2).strip()
        if url and url not in titles:
            titles[url] = title or _host_title(url)
        covered_spans.append(match.span())

    for match in _URL_RE.finditer(text):
        if _already_covered(match.start()):
            continue
        # _URL_RE runs over the escaped text, where "\n" is two URL-legal characters: cut at the
        # first escaped break, then re-match after unescaping (a decoded \u000a or \u00a0 ends it too).
        unescaped = _json_unescape(_ESCAPED_BREAK_RE.split(match.group(0), maxsplit=1)[0])
        bounded = _URL_RE.match(unescaped)
        url = _clean_url(bounded.group(0) if bounded else unescaped)
        if url and url not in titles:
            titles[url] = _host_title(url)

    return [
        {"anchor": i + 1, "title": title, "url": url}
        for i, (url, title) in enumerate(titles.items())
    ][:MAX_LINKS]


def build_link_copy(result_str: str) -> Optional[SmartResponse]:
    """The chat-copy shape for a delegation result's links: bare ``[N]`` anchors only — a title
    alongside the anchor would duplicate, since the platform resolvers (``_resolve_links_slack`` /
    ``_resolve_links_telegram``) fold "[N]" into "<url|title>" themselves. Shared by LelikAgent's
    own-result copy and AlekGatewayAgent's fallback (VOICE_COMPANION_RFC §4.10 rule 2) so the two
    callers can't drift on how a link list becomes a chat message. ``None`` when there is nothing
    to post — pure, no I/O: callers own delivery and its failure handling."""
    links = extract_result_links(result_str)
    if not links:
        return None
    text = "\n".join(f"[{link['anchor']}]" for link in links)
    return SmartResponse(text=text, link_list=links)
