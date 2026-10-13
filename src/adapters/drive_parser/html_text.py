"""
Parser@2's HTML policy (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §25.3 "Parser@2", D23, with the review thresholds the
arbiter set under the owner's delegation). Runs only inside the sandboxed child (`drive_parser.child`), like every
reader: BeautifulSoup and markdownify are imported inside the functions, after the child applied its limits.

- **When Markdown or text is HTML** (`text_is_html`): outside fenced and inline code, it holds a document marker
  (`<!DOCTYPE`, `<html`, `<head`, `<body`) or tags make up more than HTML_TAG_SHARE_MIN of it. The share counts tag
  names (`<td`, `</td`, `>`), comments and <script>/<style> bodies, never attribute values: a README's badge URLs are
  not a page. Code is never measured, so quoted HTML, `List<String>` and `<T>` keep a file as written.
- **Content** (`html_page_text`, one parse, the chosen element converted in place): the first visible <main> (or
  role="main"); else the largest <article> when it holds at least ARTICLE_SHARE_MIN of the body's text; else the
  body without its top-level header, footer, nav and aside (and their landmark roles). Top-level: the element's
  parent is the body or a layout wrapper — a <div> whose children are all structural (header, footer, nav, aside,
  main, section, article, div) and whose own parent is one too. Nested ones (an invoice's <header> in a <section>, a
  note's <aside> beside a paragraph) are content and stay.
- Hidden elements (`hidden`, aria-hidden="true"), scripts, styles, frames, forms' option lists are dropped.
- **Links:** a block made only of in-page links (a table of contents, a skip link, "Back to top") is dropped; an
  in-page link inside a sentence keeps its text. An external link is kept as its text plus its domain ("Read more
  (example.org)"); any other link as its text. An image becomes its alt text.
- Title, meta description, og:* (media and URL values left out) and the page language are written as plain lines at
  the head of the text.
- **`script_rendered`** (§25.3: a `template` profile, no doc2query): under SCRIPT_RENDERED_TEXT_MAX characters of
  visible text and at least one <script>. No byte ratio: a page saved as one file (inline CSS, data: images) is not
  script-rendered.

Public interface:
    HTML_TAG_SHARE_MIN, SCRIPT_RENDERED_TEXT_MAX, ARTICLE_SHARE_MIN
    without_code(text) -> str
    tag_share(text) -> float
    text_is_html(text) -> bool
    html_page_text(markup) -> Tuple[str, bool]     # (text, script_rendered)
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit

HTML_TAG_SHARE_MIN = 0.30          # tag names over 30 % of the text outside code (review M1, arbiter)
SCRIPT_RENDERED_TEXT_MAX = 500     # visible characters below which a page with a <script> is script-rendered (M2)
ARTICLE_SHARE_MIN = 0.5            # the largest <article> is the content only with half the body's text (M3)

_MARKER = re.compile(r"<(?:!doctype|html|head|body)\b", re.IGNORECASE)
_TAG = re.compile(r"(</?[A-Za-z][A-Za-z0-9:-]*)(?:\s[^<>]*)?/?>")
_CODE_OPEN = re.compile(r"<(script|style)\b[^<>]*>", re.IGNORECASE)
_CODE_CLOSE = {"script": re.compile(r"</script\s*>", re.IGNORECASE), "style": re.compile(r"</style\s*>", re.IGNORECASE)}
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")

_DROPPED = frozenset({"script", "style", "noscript", "template", "svg", "math", "iframe", "object", "embed", "canvas",
                      "head", "title", "meta", "link", "base", "select", "datalist"})
_BOILERPLATE = frozenset({"header", "footer", "nav", "aside"})
_BOILERPLATE_ROLES = frozenset({"banner", "contentinfo", "navigation", "complementary"})
_STRUCTURAL = frozenset({"header", "footer", "nav", "aside", "main", "section", "article", "div"})
_ROOTS = frozenset({"[document]", "html", "body", "main", "article", "section"})
_IN_FLOW = frozenset({"p", "li", "span", "td", "th", "dd", "dt", "h1", "h2", "h3", "h4", "h5", "h6", "label", "em",
                      "strong", "b", "i", "u", "small", "blockquote", "figcaption", "caption", "q", "cite"})
_MEDIA_OG = ("og:image", "og:video", "og:audio")
_META_VALUE_MAX = 300
_OG_LINES_MAX = 10
_LANG_MAX = 35


# -- is it HTML? ------------------------------------------------------------------------------------------------------

def without_code(text: str) -> str:
    """`text` without fenced code blocks (``` or ~~~, an unclosed fence runs to the end) and inline code spans."""
    out: List[str] = []
    fence = ""
    for line in text.split("\n"):
        opened = _FENCE.match(line)
        if fence:
            if opened and opened.group(1)[0] == fence[0] and len(opened.group(1)) >= len(fence):
                fence = ""
            continue
        if opened:
            fence = opened.group(1)
            continue
        out.append(_without_inline_code(line))
    return "\n".join(out)


def _without_inline_code(line: str) -> str:
    """Code spans removed: a run of n backticks up to the next run of exactly n. Linear: a run length with no closer
    on the line is remembered, so later runs of it are text at once."""
    if "`" not in line:
        return line
    parts: List[str] = []
    unclosed: Set[int] = set()
    position = 0
    start = line.find("`")
    while start >= 0:
        end = start
        while end < len(line) and line[end] == "`":
            end += 1
        run = end - start
        close = -1 if run in unclosed else _closing_run(line, end, run)
        if close < 0:
            unclosed.add(run)
            start = line.find("`", end)
            continue
        parts.append(line[position:start])
        position = close + run
        start = line.find("`", position)
    parts.append(line[position:])
    return "".join(parts)


def _closing_run(line: str, start: int, run: int) -> int:
    ticks = "`" * run
    found = line.find(ticks, start)
    while found >= 0:
        end = found + run
        if (found == 0 or line[found - 1] != "`") and (end >= len(line) or line[end] != "`"):
            return found
        while end < len(line) and line[end] == "`":
            end += 1
        found = line.find(ticks, end)
    return -1


def tag_share(text: str) -> float:
    """Share of `text` that is markup: tag names with their brackets (attribute values not counted), comments, and
    <script>/<style> blocks whole. One linear pass over the original text."""
    if not text:
        return 0.0
    markup = 0
    position = text.find("<")
    while position >= 0:
        if text.startswith("<!--", position):
            end = text.find("-->", position + 4)
            if end < 0:                                    # an unclosed comment runs to the end
                markup += len(text) - position
                break
            markup += end + 3 - position
            position = text.find("<", end + 3)
            continue
        code = _CODE_OPEN.match(text, position)
        if code is not None:
            close = _CODE_CLOSE[code.group(1).lower()].search(text, code.end())
            end = len(text) if close is None else close.end()
            markup += end - position
            position = text.find("<", end)
            continue
        tag = _TAG.match(text, position)
        if tag is not None:
            markup += len(tag.group(1)) + 1                # "<name" or "</name", and ">"
            position = text.find("<", tag.end())
        else:
            position = text.find("<", position + 1)
    return min(1.0, markup / len(text))


def text_is_html(text: str) -> bool:
    """Markdown or text that is an HTML page or fragment (review M1)."""
    prose = without_code(text)
    return _MARKER.search(prose) is not None or tag_share(prose) > HTML_TAG_SHARE_MIN


# -- one page ---------------------------------------------------------------------------------------------------------

def html_page_text(markup: str) -> Tuple[str, bool]:
    """(text, script_rendered) of one HTML page: the metadata lines, a blank line, then the content as Markdown. The
    page is parsed once and the chosen element converted in place (review M4)."""
    from bs4 import BeautifulSoup, Tag

    soup = BeautifulSoup(markup, "html.parser")
    head = _metadata_lines(soup)
    language = _language(soup)
    has_script = False
    dropped: List[Any] = []
    links: List[Any] = []
    images: List[Any] = []
    for element in soup.descendants:
        if not isinstance(element, Tag):
            continue
        name = element.name
        has_script = has_script or name == "script"
        if name in _DROPPED or _hidden(element):
            dropped.append(element)
        elif name == "a":
            links.append(element)
        elif name == "img":
            images.append(element)
    for element in dropped:
        _drop(element)
    for image in images:
        _rewrite_image(image)
    _rewrite_links(links)
    body = _content_markdown(soup)
    if language and (head or body):                           # a language alone says nothing: no text
        head.append(f"Language: {language}")
    text = "\n".join(head) + ("\n\n" if head and body else "") + body
    return text, has_script and len(body) < SCRIPT_RENDERED_TEXT_MAX


def _hidden(element: Any) -> bool:
    return element.has_attr("hidden") or str(element.get("aria-hidden") or "").strip().lower() == "true"


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

def _rewrite_image(image: Any) -> None:
    if _detached(image):
        return
    alt = _clean(str(image.get("alt") or ""))
    if alt:
        image.replace_with(f" {alt} ")
    else:
        image.decompose()


def _rewrite_links(links: List[Any]) -> None:
    only_page_links: Dict[int, bool] = {}
    for link in links:
        if _detached(link):
            continue
        href = str(link.get("href") or "").strip()
        if href.startswith("#"):
            _page_link(link, only_page_links)
            continue
        domain = _external_domain(href)
        text = " ".join(link.get_text(" ", strip=True).split())
        if domain and text and domain not in text.lower():
            link.replace_with(f"{text} ({domain})")
        else:
            link.unwrap()


def _page_link(link: Any, cache: Dict[int, bool]) -> None:
    """Drops the largest block around an in-page link that holds nothing but in-page links (a contents list, a
    "Back to top" paragraph); a link standing alone in a container goes too; one inside a sentence keeps its text."""
    node = link
    while (node.parent is not None and node.parent.name not in _ROOTS
           and _only_page_links(node.parent, cache)):
        node = node.parent
    if node is not link or link.parent is None or link.parent.name not in _IN_FLOW:
        node.decompose()
    else:
        link.unwrap()


def _only_page_links(element: Any, cache: Dict[int, bool]) -> bool:
    key = id(element)
    if key not in cache:
        own = "".join(element.get_text("", strip=False).split())
        linked = "".join("".join(a.get_text("", strip=False).split())
                         for a in element.find_all("a") if str(a.get("href") or "").strip().startswith("#"))
        cache[key] = own == linked
    return cache[key]


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
    """The first visible <main>, else the largest <article> with at least ARTICLE_SHARE_MIN of the body's text, else
    the body without its top-level boilerplate (review M3). A <main> that converts to nothing falls through."""
    for main in soup.find_all("main") + soup.find_all(attrs={"role": "main"}):
        if not _detached(main):
            text = _markdown(main)
            if text:
                return text
            break
    root = soup.find("body") or soup
    articles = root.find_all("article")
    if articles:
        sizes = [(len(_visible(a)), i) for i, a in enumerate(articles)]
        size, index = max(sizes, key=lambda s: (s[0], -s[1]))
        if size and size >= ARTICLE_SHARE_MIN * len(_visible(root)):
            return _markdown(articles[index])
    wrappers: Dict[int, bool] = {}
    for element in [e for e in root.find_all(True) if _is_boilerplate(e)]:
        if not _detached(element) and _layout_wrapper(element.parent, wrappers):
            element.decompose()
    return _markdown(root)


def _is_boilerplate(element: Any) -> bool:
    return element.name in _BOILERPLATE or str(element.get("role") or "").strip().lower() in _BOILERPLATE_ROLES


def _layout_wrapper(element: Optional[Any], cache: Dict[int, bool]) -> bool:
    """The body (or the document), or a <div> holding only structural children inside another layout wrapper."""
    if element is None or element.name in ("body", "html", "[document]"):
        return True
    if element.name != "div":
        return False
    key = id(element)
    if key not in cache:
        from bs4 import NavigableString, Tag

        structural = all((isinstance(child, Tag) and child.name in _STRUCTURAL)
                         or (isinstance(child, NavigableString) and not child.strip())
                         for child in element.children)
        cache[key] = structural and _layout_wrapper(element.parent, cache)
    return cache[key]


def _visible(element: Any) -> str:
    return "".join(element.get_text("", strip=False).split())


def _markdown(element: Any) -> str:
    """The element as Markdown, converted in place (no serialise and re-parse). Nesting past the recursion limit
    falls back to its plain text, as markitdown 0.1.8 does."""
    from markdownify import ATX, MarkdownConverter

    try:
        text = MarkdownConverter(heading_style=ATX, escape_underscores=False, escape_asterisks=False,
                                 escape_misc=False).convert_soup(element)
    except RecursionError:
        text = element.get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", str(text)).strip()


def _drop(element: Any) -> None:
    if not _detached(element):
        element.decompose()


def _detached(element: Optional[Any]) -> bool:
    return element is None or bool(getattr(element, "decomposed", False)) or element.parent is None
