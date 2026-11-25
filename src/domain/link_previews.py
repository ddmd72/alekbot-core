"""Link-preview policy: an answer with more than `LINK_PREVIEW_MAX_LINKS` distinct links is posted without previews,
because a wall of preview cards in the middle of the text buries the answer (Slack unfurls every link of a new post,
Telegram shows one preview per message, so a chunked answer multiplies them)."""
import html
import re

LINK_PREVIEW_MAX_LINKS = 2

# A link ends at whitespace or at a delimiter of the rendered forms: `<url|title>`, `<url>`, `[title](url)`.
_URL_RE = re.compile(r"https?://[^\s<>|()\[\]\"']+")
_TRAILING_PUNCTUATION = ".,;:!?*_~`"


def distinct_link_count(text: str) -> int:
    """Number of distinct http(s) URLs in `text`; `&amp;` and `&` forms of one URL count once."""
    if not text:
        return 0
    urls = set()
    for match in _URL_RE.finditer(text):
        url = html.unescape(match.group(0)).rstrip(_TRAILING_PUNCTUATION)
        if url:
            urls.add(url)
    return len(urls)


def link_previews_allowed(text: str) -> bool:
    """True when the answer has at most `LINK_PREVIEW_MAX_LINKS` distinct links, so previews stay on."""
    return distinct_link_count(text) <= LINK_PREVIEW_MAX_LINKS
