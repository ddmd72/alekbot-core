"""The Cabinet's wake-up form reads a pasted `curl` command and a separate secret (EXTERNAL_TASK_QUEUE_RFC §5.6).

A webhook provider shows a ready command with a placeholder variable (`$TOKEN`) and the token on its own. The owner pastes
both; this module turns them into the URL and the header pairs the stored target holds. The bot writes the request body
and the `Content-Type` itself, so the command's data and content type are dropped. The URL then goes through
`ExternalTaskService.save_wake_target` and the headers through `check_headers`, the same rules a hand-typed target meets.

Public interface:
    parse_wake_curl(command, secret) -> (url, headers) | WakeRefusal

Why the option list is closed: a pasted command can ask curl for more than a POST (skip certificate checks, go through
a proxy, pin an address, follow redirects, send credentials). Ignoring such an option would store a target that does
not behave as the command did, so every option outside the short list is refused with `curl_option`, never skipped.
The placeholder rule exists so a secret is substituted only where the command asked for it: one variable name, and the
secret field must agree with the command (a variable needs a secret; a secret needs a variable).
"""
from __future__ import annotations

import re
import shlex
from typing import List, Optional, Tuple, Union

from .wake_refusal import WakeRefusal
from .wake_target import Headers, check_headers

COMMAND_MAX_CHARS = 8192
_CONTINUATION = re.compile(r"\\\r?\n")
_PLACEHOLDER = re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
_SECRET = re.compile(r"[\x21-\x7e]+")
_IGNORED_FLAGS = frozenset({"-s", "--silent", "-S", "--show-error", "-f", "--fail", "-i", "-v"})
_IGNORED_SHORT_LETTERS = frozenset("sSfiv")
_VALUE_SHORT = {"-X": "request", "-H": "header", "-d": "data"}
_VALUE_LONG = frozenset({"request", "header", "data", "data-raw", "data-binary", "json", "url"})
_DATA_LONG = frozenset({"data", "data-raw", "data-binary", "json"})


def _split_option(tokens: List[str], i: int) -> Union[Tuple[Optional[str], Optional[str], int], WakeRefusal]:
    """The option at `tokens[i]` as (kind, value, next index); kind None means an ignored flag. A refusal otherwise."""
    token = tokens[i]
    if token in _IGNORED_FLAGS:
        return None, None, i + 1
    if token.startswith("--"):
        name, eq, value = token[2:].partition("=")
        if name not in _VALUE_LONG:
            return WakeRefusal.CURL_OPTION
        if eq:
            return name, value, i + 1
        if i + 1 >= len(tokens):
            return WakeRefusal.CURL_SYNTAX
        return name, tokens[i + 1], i + 2
    short = token[:2]
    if short in _VALUE_SHORT:
        if len(token) > 2:
            return _VALUE_SHORT[short], token[2:], i + 1
        if i + 1 >= len(tokens):
            return WakeRefusal.CURL_SYNTAX
        return _VALUE_SHORT[short], tokens[i + 1], i + 2
    if len(token) > 1 and all(c in _IGNORED_SHORT_LETTERS for c in token[1:]):
        return None, None, i + 1         # a cluster such as -sS
    return WakeRefusal.CURL_OPTION


def _split_header(raw: str) -> Optional[Tuple[str, str]]:
    name, colon, value = raw.partition(":")
    if not colon or not name.strip():
        return None
    return name.strip(), value.strip()


def parse_wake_curl(command: str, secret: str) -> Union[Tuple[str, Headers], WakeRefusal]:
    """The wake URL and header pairs of a pasted `curl` command, the secret put where its placeholder stands.

    Refusals: `curl_syntax` (not text, too long, unbalanced quote, not `curl`, an option without its value), `curl_option`
    (any option outside the accepted list), `curl_method` (`-X` other than POST), `curl_url` (none or several),
    `header` (a `-H` that is not `Name: value`, or a header the guard refuses), `curl_variables` (two variable names),
    `curl_secret_missing`, `curl_secret_unused`, `curl_secret` (not printable ASCII without spaces)."""
    if not isinstance(command, str) or not isinstance(secret, str) or not command.strip() or len(command) > COMMAND_MAX_CHARS:
        return WakeRefusal.CURL_SYNTAX
    try:
        tokens = shlex.split(_CONTINUATION.sub("", command), posix=True)
    except ValueError:
        return WakeRefusal.CURL_SYNTAX
    if not tokens or tokens[0] != "curl":
        return WakeRefusal.CURL_SYNTAX
    urls: List[str] = []
    raw_headers: List[str] = []
    i = 1
    while i < len(tokens):
        if not tokens[i].startswith("-"):
            urls.append(tokens[i])
            i += 1
            continue
        option = _split_option(tokens, i)
        if isinstance(option, WakeRefusal):
            return option
        kind, value, i = option
        if kind == "request" and (value or "").upper() != "POST":
            return WakeRefusal.CURL_METHOD
        if kind == "url":
            urls.append(value or "")
        elif kind == "header":
            raw_headers.append(value or "")
        # `data` kinds and ignored flags: nothing to keep, the bot writes its own body.
    if len(urls) != 1 or not urls[0]:
        return WakeRefusal.CURL_URL
    pairs = []
    for raw in raw_headers:
        pair = _split_header(raw)
        if pair is None:
            return WakeRefusal.HEADER
        if pair[0].lower() != "content-type":
            pairs.append(pair)
    names = {m.group(1) or m.group(2)
             for text in [urls[0]] + [v for _, v in pairs] for m in _PLACEHOLDER.finditer(text)}
    secret = secret.strip()
    if len(names) > 1:
        return WakeRefusal.CURL_VARIABLES
    if names and not secret:
        return WakeRefusal.CURL_SECRET_MISSING
    if secret and not names:
        return WakeRefusal.CURL_SECRET_UNUSED
    if secret and not _SECRET.fullmatch(secret):
        return WakeRefusal.CURL_SECRET

    def fill(text: str) -> str:
        return _PLACEHOLDER.sub(lambda _m: secret, text)

    headers: Headers = tuple((name, fill(value)) for name, value in pairs)
    return check_headers(headers) or (fill(urls[0]), headers)
