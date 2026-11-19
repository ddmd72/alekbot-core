"""Every request GmailProviderAdapter sends conforms to the Gmail request contract (P17, TEST_COVERAGE_SYSTEMIC_RFC §4a).

The REAL adapter runs with `tests/support/http_boundary_fake.py` as its aiohttp session. Each Gmail URL is routed with the
smallest invented answer that lets the method run to its end (a list with one message id; a message with a body part,
one attachment handle and one inline attachment; the attachment's data), so every request of the call is recorded. The
URL is rebuilt as aiohttp builds it (`URL.extend_query(params)`), split into path and query, and validated by
`assert_request`. No network, no key. `gmail_request_shapes.SHAPES` is the table.

Meta-tests: the closed subset rejects an unknown query parameter and a path it does not hold; every kept operation and
query parameter is used by a shape (a dead keep-list entry fails); every URL the adapter file builds maps to a kept
operation or to a `NOT_COVERED` entry with its reason (a stale entry fails).
"""
from __future__ import annotations

import ast
import asyncio
import base64
import functools
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import pytest
import yaml
from yarl import URL

from scripts.contracts.extract_subset import PROVIDERS
from src.adapters.gmail_provider_adapter import GmailProviderAdapter
from tests.contracts.openapi_contract import assert_request, operation_path
from tests.support import http_boundary_fake
from tests.support.oauth_credentials_store import creds
from tests.unit.contracts.gmail_request_shapes import (
    ATTACHMENT_ID,
    FILENAME,
    MESSAGE_ID,
    NOT_COVERED,
    SHAPES,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
ADAPTER_FILE = REPO_ROOT / "src" / "adapters" / "gmail_provider_adapter.py"
CONTRACT = REPO_ROOT / "contracts" / "providers" / "gmail" / "openapi.yaml"
BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
CREDS = creds("user-1", "gmail", access_token="x")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


_HEADERS = [
    {"name": "Subject", "value": "Invoice FA-0137"},
    {"name": "From", "value": "Clara Moreira <clara@example.com>"},
    {"name": "Date", "value": "Fri, 02 Oct 2026 10:00:00 +0000"},
]
_MESSAGE = {
    "id": MESSAGE_ID, "threadId": "thread-1", "labelIds": ["INBOX"], "snippet": "Invoice FA-0137",
    "payload": {"mimeType": "multipart/mixed", "headers": _HEADERS, "parts": [
        {"partId": "0", "mimeType": "text/plain", "filename": "", "body": {"size": 15, "data": _b64(b"Invoice FA-0137")}},
        {"partId": "1", "mimeType": "application/pdf", "filename": FILENAME,
         "body": {"size": 12, "attachmentId": ATTACHMENT_ID}},
        {"partId": "2", "mimeType": "application/pdf", "filename": FILENAME, "body": {"size": 5, "data": _b64(b"%PDF-")}},
    ]},
}


def _route(fake: http_boundary_fake.HttpFake) -> None:
    fake.route("GET", f"{BASE}/messages", 200,
               {"messages": [{"id": MESSAGE_ID, "threadId": "thread-1"}], "nextPageToken": "page-2"})
    fake.route("GET", f"{BASE}/messages/{MESSAGE_ID}", 200, _MESSAGE)
    fake.route("GET", f"{BASE}/messages/{MESSAGE_ID}/attachments/{ATTACHMENT_ID}", 200,
               {"size": 12, "data": _b64(b"%PDF-1.7 abc")})


@dataclass(frozen=True)
class Sent:
    method: str
    path: str
    headers: Mapping[str, str]
    body: Any
    query: Mapping[str, tuple[str, ...]]


def _sent(request: http_boundary_fake.RecordedRequest) -> Sent:
    url = URL(request.url)
    if request.params:
        url = url.extend_query(request.params)  # what aiohttp's ClientRequest does with `params`
    query = {name: tuple(url.query.getall(name)) for name in dict.fromkeys(url.query)}
    body = request.json if request.json is not None else request.data
    return Sent(request.method, url.path, dict(request.headers or {}), body, query)


def _ran_through(result: Any) -> bool:
    """The call returned what the routed answers hold: a parsed email, part or byte string, not an empty fallback."""
    return bool(result[0] if isinstance(result, tuple) else result)


@functools.lru_cache(maxsize=None)
def _captured(shape_id: str) -> tuple[Sent, ...]:
    shape = next(s for s in SHAPES if s.id == shape_id)
    with pytest.MonkeyPatch.context() as mp:
        fake = http_boundary_fake.install(mp)
        _route(fake)
        result = asyncio.run(shape.call(GmailProviderAdapter(client_id="x", client_secret="x"), CREDS))
    assert fake.unrouted == [], f"{shape_id}: unrouted {fake.unrouted}"
    assert _ran_through(result), f"{shape_id}: the call did not run to its end ({result!r})"
    return tuple(_sent(r) for r in fake.requests)


def _all_sent() -> list[Sent]:
    return [sent for shape in SHAPES for sent in _captured(shape.id)]


def _check(sent: Sent, **over: Any) -> None:
    fields = {"path": sent.path, "query": sent.query, **over}
    assert_request("gmail", sent.method, fields["path"], sent.headers, sent.body,
                   query={k: list(v) for k, v in fields["query"].items()})


def test_shape_ids_are_unique():
    ids = [s.id for s in SHAPES]
    assert len(ids) == len(set(ids)) and len(ids) >= 10


@pytest.mark.parametrize("shape", SHAPES, ids=[s.id for s in SHAPES])
def test_every_request_of_the_shape_conforms_to_the_contract(shape):
    sent = _captured(shape.id)
    assert sent, f"{shape.id}: no request recorded"
    for request in sent:
        _check(request)


@pytest.mark.parametrize("shape", SHAPES, ids=[s.id for s in SHAPES])
def test_the_closed_subset_rejects_an_unknown_query_parameter(shape):
    for request in _captured(shape.id):
        with pytest.raises(AssertionError, match="query zz_unknown: unknown query parameter"):
            _check(request, query={**request.query, "zz_unknown": ("1",)})


def test_a_path_the_contract_does_not_hold_fails():
    request = _captured(SHAPES[0].id)[0]
    with pytest.raises(AssertionError, match="is not in"):
        _check(request, path="/gmail/v1/users/me/threads")


def test_repeated_metadata_headers_arrive_as_repeated_values():
    """The capture is faithful: a list value of `params` becomes one query value per item, as aiohttp sends it."""
    metadata = [s for s in _all_sent() if s.query.get("format") == ("metadata",)]
    assert metadata and all(s.query["metadataHeaders"] == ("Subject", "From", "Date") for s in metadata)


# ------------------------------------------------------------------------------------------ the keep list is used
def _contract() -> dict:
    return yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))


def _kept_query() -> dict[tuple[str, str], set[str]]:
    return {(method, path): {p["name"] for p in op.get("parameters") or [] if p["in"] == "query"}
            for path, item in _contract()["paths"].items() for method, op in item.items()}


def _operation_of(sent: Sent) -> tuple[str, str]:
    return sent.method.lower(), operation_path("gmail", sent.method, sent.path)


def test_the_contract_keeps_exactly_the_table_query_parameters():
    table = {(op.method, op.path): set(op.query) for op in PROVIDERS["gmail"].operations}
    assert _kept_query() == table


def test_every_kept_query_parameter_is_sent_by_a_shape():
    sent: dict[tuple[str, str], set[str]] = {}
    for request in _all_sent():
        sent.setdefault(_operation_of(request), set()).update(request.query)
    dead = {op: sorted(names - sent.get(op, set())) for op, names in _kept_query().items()}
    assert {op: names for op, names in dead.items() if names} == {}, "kept query parameters no shape sends"


def test_every_kept_operation_is_reached_by_a_shape():
    assert {_operation_of(request) for request in _all_sent()} == set(_kept_query())


# ---------------------------------------------------------------------------------------- the adapter's call sites
_VERBS = {"get", "post", "put", "patch", "delete"}
# adapter helper -> the method it sends the URL it is given with (its own `session.get(url, ...)` is not a call site)
FORWARDERS = {"_gmail_json": "get"}


def _render(node: ast.expr, constants: Mapping[str, str]) -> str:
    """A URL expression as text: module constants substituted, any other interpolation kept as `{expr}`."""
    if isinstance(node, ast.Name) and node.id in constants:
        return constants[node.id]
    if not isinstance(node, ast.JoinedStr):
        raise AssertionError(f"a URL the test cannot read: {ast.unparse(node)}")
    out = ""
    for part in node.values:
        if isinstance(part, ast.Constant):
            out += str(part.value)
        elif isinstance(part, ast.FormattedValue) and isinstance(part.value, ast.Name) and part.value.id in constants:
            out += constants[part.value.id]
        elif isinstance(part, ast.FormattedValue):
            out += "{" + ast.unparse(part.value) + "}"
    return out


def _call_sites() -> set[str]:
    """`<METHOD> <URL>` of every request the adapter file sends, read from its AST. Fails when a URL constant is used
    outside a recognised call, or a session call takes its URL from a variable outside a forwarder."""
    tree = ast.parse(ADAPTER_FILE.read_text(encoding="utf-8"))
    constants = {target.id: node.value.value for node in tree.body if isinstance(node, ast.Assign)
                 for target in node.targets if isinstance(target, ast.Name)
                 and isinstance(node.value, ast.Constant) and str(node.value.value).startswith("https://")}
    sessions = {item.optional_vars.id for node in ast.walk(tree) if isinstance(node, ast.AsyncWith)
                for item in node.items if isinstance(item.optional_vars, ast.Name)
                and ast.unparse(item.context_expr).startswith("aiohttp.ClientSession(")}
    owner: dict[int, str] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner.update({id(node): fn.name for node in ast.walk(fn)})
    sites: set[str] = set()
    covered: set[int] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)):
            continue
        receiver, attr = node.func.value.id, node.func.attr
        if receiver in sessions and attr in _VERBS:
            verb, url = attr, node.args[0]
        elif receiver == "self" and attr in FORWARDERS:
            verb, url = FORWARDERS[attr], node.args[1]
        else:
            continue
        if isinstance(url, ast.Name) and url.id not in constants:
            assert FORWARDERS.get(owner.get(id(node), "")) == verb, f"a URL from a variable: {ast.unparse(node)}"
            continue
        covered |= {id(n) for n in ast.walk(url)}
        sites.add(f"{verb.upper()} {_render(url, constants)}")
    loose = [ast.unparse(n) for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id in constants
             and isinstance(n.ctx, ast.Load) and id(n) not in covered]
    assert not loose, f"URL constants used outside a recognised request call: {loose}"
    return sites


def _site_operation(site: str) -> tuple[str, str] | None:
    verb, url_text = site.split(" ", 1)
    url = URL(re.sub(r"\{[^}]+\}", "x-1", url_text))
    servers = {URL(s["url"]).host for s in _contract()["servers"]}
    if url.host not in servers:
        return None
    try:
        return verb.lower(), operation_path("gmail", verb, url.path)
    except AssertionError:
        return None


def test_every_url_the_adapter_builds_maps_to_a_kept_operation_or_is_not_covered():
    sites = _call_sites()
    assert sites, "no request call site found: the pattern is stale"
    uncovered = {site for site in sites if _site_operation(site) is None}
    assert sorted(uncovered - set(NOT_COVERED)) == [], "call sites with no operation and no NOT_COVERED reason"
    mapped = {_site_operation(site) for site in sites} - {None}
    assert mapped == set(_kept_query()), "kept operations no call site sends, or call sites outside the table"


def test_the_not_covered_list_has_no_stale_entry():
    sites = _call_sites()
    assert sorted(set(NOT_COVERED) - sites) == [], "NOT_COVERED entries the adapter no longer sends"
    assert sorted(s for s in NOT_COVERED if _site_operation(s) is not None) == [], "NOT_COVERED entries now covered"
    assert all(reason.strip() for reason in NOT_COVERED.values())
