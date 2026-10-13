"""
Parser@2's HTML policy (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §25.3 "Parser@2", D23). Runs only inside the
sandboxed child (`drive_parser.child`), like every reader: BeautifulSoup is imported inside the function, after the
child applied its limits.

- HTML inside Markdown or text goes through the HTML converter when tags hold more than HTML_TAG_SHARE_MIN of its
  characters (the whole text, never a part of it).
- A page keeps <main> (or role="main"), else its top-level <article>s, else the body without header, footer and
  aside (and their landmark roles); navigation (<nav>, role="navigation") is dropped everywhere.
- In-page links (`#…`) are dropped with their text ("Skip to content", "Back to top", a table of contents); an
  external link is kept as its text plus its domain ("Read more (example.org)"); any other link as its text. An
  image becomes its alt text.
- Title, meta description, og:* (media and URL values left out) and the page language are written as plain lines at
  the head of the text.
- A page whose body text is under SCRIPT_RENDERED_TEXT_SHARE of its bytes is `script_rendered` (§25.3: a `template`
  profile, no doc2query); its metadata lines are then most of what it says.

Public interface:
    HTML_TAG_SHARE_MIN, SCRIPT_RENDERED_TEXT_SHARE
    tag_share(text) -> float
    html_page_text(markup, size_bytes) -> Tuple[str, bool]     # (text, script_rendered)
"""
from __future__ import annotations

import re
from typing import Any, List, Optional, Tuple
from urllib.parse import urlsplit

HTML_TAG_SHARE_MIN = 0.05              # §25.3: a tag share over 5 % sends the whole Markdown or text file
SCRIPT_RENDERED_TEXT_SHARE = 0.01      # §25.3: body text under 1 % of the page's bytes

# A tag, a comment or a doctype. A comment is bounded so an unclosed "<!--" costs one bounded scan, never the rest
# of a 2 MB text per occurrence. "<https://…>" autolinks and "3 < 5" are not tags.
_TAG = re.compile(r"<!--.{0,4096}?-->|<!doctype[^<>]{0,256}>|</?[A-Za-z][A-Za-z0-9:-]*(?:\s[^<>]*)?/?>",
                  re.IGNORECASE | re.DOTALL)
_CODE_OPEN = re.compile(r"<(script|style)\b[^<>]*>")
_DROPPED = ("script", "style", "noscript", "template", "svg", "math", "iframe", "object", "embed", "canvas",
            "head", "title", "meta", "link", "base")
_BOILERPLATE = ("header", "footer", "aside")
_BOILERPLATE_ROLES = ("banner", "contentinfo", "complementary")
_MEDIA_OG = ("og:image", "og:video", "og:audio")
_META_VALUE_MAX = 300
_OG_LINES_MAX = 10
_LANG_MAX = 35


def tag_share(text: str) -> float:
    """Share of the characters of `text` that are markup: tags, comments, doctypes, and the bodies of <script> and
    <style> elements (code, never prose). One pass: an unclosed <script> or <style> runs to the end of the text."""
    if not text:
        return 0.0
    lowered = text.lower()
    markup = 0
    position = 0
    for opening in _CODE_OPEN.finditer(lowered):
        if opening.start() < position:
            continue                                         # inside the previous code block
        markup += _tags_in(text, position, opening.start())
        close = lowered.find(f"</{opening.group(1)}", opening.end())
        end = len(text) if close < 0 else lowered.find(">", close) + 1 or len(text)
        markup += end - opening.start()
        position = end
    markup += _tags_in(text, position, len(text))
    return min(1.0, markup / len(text))


def _tags_in(text: str, start: int, end: int) -> int:
    return sum(len(m.group(0)) for m in _TAG.finditer(text, start, end))


def html_page_text(markup: str, size_bytes: int) -> Tuple[str, bool]:
    """(text, script_rendered) of one HTML page: the metadata lines, a blank line, then the content as Markdown.
    `size_bytes` is the page's size as read (the share of §25.3 is over bytes, not characters)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(markup, "html.parser")
    head = _metadata_lines(soup)
    language = _language(soup)
    for element in soup(list(_DROPPED)):
        _drop(element)
    for element in soup.find_all("nav") + soup.find_all(attrs={"role": "navigation"}):
        _drop(element)
    _rewrite_images(soup)
    _rewrite_links(soup)
    body = _content_markdown(soup)
    flagged = size_bytes > 0 and len(body) < SCRIPT_RENDERED_TEXT_SHARE * size_bytes
    if language and (head or body):                           # a language alone says nothing: no text
        head.append(f"Language: {language}")
    text = "\n".join(head) + ("\n\n" if head and body else "") + body
    return text, flagged


# -- metadata ---------------------------------------------------------------------------------------------------------

def _metadata_lines(soup: Any) -> List[str]:
    title = ""
    for element in soup.find_all("title"):
        if element.find_parent("svg") is None:               # an inline icon's <title> is not the page's
            title = _clean(element.get_text(" ", strip=True))
            break
    description = ""
    og: List[Tuple[str, str]] = []
    for meta in soup.find_all("meta"):
        name = str(meta.get("name") or "").strip().lower()
        prop = str(meta.get("property") or "").strip().lower() or name
        value = _clean(str(meta.get("content") or ""))
        if not value:
            continue
        if name == "description" and not description:
            description = value
        elif prop.startswith("og:") and not prop.startswith(_MEDIA_OG) and not _is_url(value):
            og.append((prop, value))
    lines: List[str] = []
    seen = set()
    for label, value in (("Title", title), ("Description", description)):
        if value:
            lines.append(f"{label}: {value}")
            seen.add(value.casefold())
    for prop, value in og:
        if value.casefold() in seen or len(lines) >= 2 + _OG_LINES_MAX:
            continue
        lines.append(f"{prop}: {value}")
        seen.add(value.casefold())
    return lines


def _language(soup: Any) -> str:
    root = soup.find("html")
    return _clean(str(root.get("lang") or root.get("xml:lang") or ""))[:_LANG_MAX] if root is not None else ""


def _clean(value: str) -> str:
    return " ".join(value.split())[:_META_VALUE_MAX]


def _is_url(value: str) -> bool:
    return bool(re.match(r"(?:[a-z][a-z0-9+.-]*:)?//", value, re.IGNORECASE))


# -- links and images -------------------------------------------------------------------------------------------------

def _rewrite_images(soup: Any) -> None:
    for image in soup.find_all("img"):
        if _detached(image):
            continue
        alt = _clean(str(image.get("alt") or ""))
        if alt:
            image.replace_with(f" {alt} ")
        else:
            image.decompose()


def _rewrite_links(soup: Any) -> None:
    for link in soup.find_all("a"):
        if _detached(link):
            continue
        href = str(link.get("href") or "").strip()
        if href.startswith("#"):
            link.decompose()                                  # in-page: skip links, back-to-top, contents
            continue
        domain = _external_domain(href)
        text = " ".join(link.get_text(" ", strip=True).split())
        if domain and text and domain not in text.lower():
            link.replace_with(f"{text} ({domain})")
        else:
            link.unwrap()


def _external_domain(href: str) -> str:
    """The host of an http(s) link without "www.", or "" (relative, mailto:, javascript:, malformed)."""
    if href.startswith("//"):
        href = "https:" + href
    try:
        parts = urlsplit(href)
        host = parts.hostname or ""
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https") or not host:
        return ""
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


# -- content ----------------------------------------------------------------------------------------------------------

def _content_markdown(soup: Any) -> str:
    """<main>, else the top-level <article>s, else the body without its boilerplate; a candidate that converts to
    nothing (an empty <main> placeholder) falls through to the next."""
    main = soup.find("main") or soup.find(attrs={"role": "main"})
    if main is not None:
        text = _markdown(main)
        if text:
            return text
    articles = [a for a in soup.find_all("article") if a.find_parent("article") is None]
    if articles:
        text = "\n\n".join(t for t in (_markdown(a) for a in articles) if t)
        if text:
            return text
    root = soup.find("body") or soup
    for element in root.find_all(list(_BOILERPLATE)) + [e for role in _BOILERPLATE_ROLES
                                                        for e in root.find_all(attrs={"role": role})]:
        _drop(element)
    return _markdown(root)


def _markdown(element: Any) -> str:
    from markitdown.converters import HtmlConverter

    result = HtmlConverter().convert_string(str(element), escape_underscores=False, escape_asterisks=False)
    return str(result.markdown or "").strip()


def _drop(element: Any) -> None:
    if not _detached(element):
        element.decompose()


def _detached(element: Optional[Any]) -> bool:
    return element is None or bool(getattr(element, "decomposed", False)) or element.parent is None
