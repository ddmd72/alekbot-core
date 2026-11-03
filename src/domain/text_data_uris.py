"""Text with its inline `data:` images dropped (drive RFC, G1b).

A Markdown or text file whose bulk is base64 images (a document's Markdown export, or any uploaded `.md`) is read as
its text: each image becomes its alt text, and a base64 payload never reaches an index or a prompt. Stdlib only; one
forward scan, no backtracking over the payload.
"""
import re
from typing import Dict, List

_START = re.compile(r"!\[|<img[\s/]", re.IGNORECASE)
_TARGET_END = re.compile(r"[\s)]")
_ATTR = re.compile(r"""\s(src|alt)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)


def _is_data(text: str, pos: int) -> bool:
    return text[pos:pos + 5].lower() == "data:"


def _strip_definitions(text: str) -> "tuple[str, Dict[str, bool]]":
    """The text without its `[id]: data:…` definition lines, and the (lower-cased) ids they defined."""
    ids: Dict[str, bool] = {}
    kept: List[str] = []
    for line in text.splitlines(keepends=True):
        body = line.lstrip()
        if body.startswith("["):
            close = body.find("]:")
            if close > 1:
                target = body[close + 2:].strip()
                if target.startswith("<"):
                    target = target[1:]
                if _is_data(target, 0):
                    ids[body[1:close].strip().lower()] = True
                    continue
        kept.append(line)
    return "".join(kept), ids


def _alt_end(text: str, start: int) -> int:
    """Index of the `]` closing an alt that opens at `start` (just after `![`), -1 when there is none."""
    depth = 0
    i = start
    n = len(text)
    while i < n:
        c = text[i]
        if c == "[":
            depth += 1
        elif c == "]":
            if depth == 0:
                return i
            depth -= 1
        i += 1
    return -1


def _inline_target_end(text: str, pos: int) -> int:
    """End (exclusive) of an inline `(data:…)` target starting at `pos` (the char after `(`); -1 when unterminated."""
    m = _TARGET_END.search(text, pos)
    if m is None:
        return -1
    if m.group() == ")":
        return m.end()
    j = m.end()
    n = len(text)
    while j < n and text[j].isspace():
        j += 1
    if j < n and text[j] in "\"'":
        q = text.find(text[j], j + 1)
        if q == -1:
            return -1
        j = q + 1
    close = text.find(")", j)
    return -1 if close == -1 else close + 1


def _html_img(text: str, pos: int) -> "tuple[int, str]":
    """(end, alt) of a `<img … src="data:…">` tag at `pos`; end is -1 when it is no data image."""
    gt = text.find(">", pos)
    if gt == -1:
        return -1, ""
    attrs = {m.group(1).lower(): (m.group(2) if m.group(2) is not None else m.group(3))
             for m in _ATTR.finditer(text, pos, gt)}
    src = attrs.get("src")
    if src is None or not _is_data(src.strip(), 0):
        return -1, ""
    return gt + 1, attrs.get("alt", "")


def drop_data_uri_images(text: str) -> str:
    """`text` with every inline, reference-style, HTML and cut-off `data:` image replaced by its alt text.
    Links that are not images and `http(s)` images stay."""
    if "data:" not in text.lower():
        return text
    text, ref_ids = _strip_definitions(text)
    out: List[str] = []
    pos = 0
    n = len(text)
    while pos < n:
        m = _START.search(text, pos)
        if m is None:
            break
        i = m.start()
        if m.group().startswith("!"):
            alt_start = m.end()
            close = _alt_end(text, alt_start)
            if close == -1:
                out.append(text[pos:m.end()])
                pos = m.end()
                continue
            alt = text[alt_start:close]
            after = close + 1
            nxt = text[after:after + 1]
            end = -1
            if nxt == "(":
                t = after + 1
                while t < n and text[t] in " \t":
                    t += 1
                if _is_data(text, t):
                    end = _inline_target_end(text, t)
                    if end == -1:  # cut mid-URI: the rest of the file is the payload
                        out.append(text[pos:i])
                        out.append(alt)
                        pos = n
                        break
            elif nxt == "[":
                ref_close = text.find("]", after + 1)
                if ref_close != -1:
                    ref_id = (text[after + 1:ref_close] or alt).strip().lower()
                    if ref_id in ref_ids:
                        end = ref_close + 1
            elif alt.strip().lower() in ref_ids:
                end = after
            if end == -1:
                out.append(text[pos:m.end()])
                pos = m.end()
                continue
            out.append(text[pos:i])
            out.append(alt)
            pos = end
        else:
            end, alt = _html_img(text, i)
            if end == -1:
                out.append(text[pos:m.end()])
                pos = m.end()
                continue
            out.append(text[pos:i])
            out.append(alt)
            pos = end
    out.append(text[pos:])
    return "".join(out)
