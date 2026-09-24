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
_TRAILING_PUNCT = ".,;:!?)]}\"'"


def _clean_url(url: str) -> str:
    """Strip trailing punctuation picked up from surrounding prose, keeping a balanced ')'."""
    while url and url[-1] in _TRAILING_PUNCT:
        if url[-1] == ")" and url.count("(") >= url.count(")"):
            break
        url = url[:-1]
    return url


def _host_title(url: str) -> str:
    host = urlparse(url).hostname or url
    return host[4:] if host.startswith("www.") else host


def extract_result_links(text: str) -> List[Dict[str, Any]]:
    """Every http(s) URL in ``text``, deduped in order, as [{"anchor", "title", "url"}, ...]
    (the same shape SmartResponse.link_list / send_long_text(link_list=...) render).

    Title: the JSON object's "source" or "title" field when the URL sits inside one
    (WebSearchAgent's findings shape), else the URL's host without "www.".
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

    for match in _URL_RE.finditer(text):
        if _already_covered(match.start()):
            continue
        url = _clean_url(match.group(0))
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
