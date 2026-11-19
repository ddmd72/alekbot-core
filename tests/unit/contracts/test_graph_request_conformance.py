"""Every Graph request the To Do and OneDrive adapters send conforms to the Graph request contract (P17, RFC §4a).

The REAL `MicrosoftToDoAdapter` and `OneDriveAdapter` run with `tests/support/http_boundary_fake.py` as their aiohttp
session, their tokens read from the real credential store over the evaluating Firestore fake (a valid access token, so
no refresh is sent). Each Graph URL is routed with the smallest invented answer that lets the method run to its end (the
App Folder item, a file item, a delta page with its deltaLink, an upload session's URL), so every request of the call is
recorded. The URL is parsed as aiohttp parses it (`yarl.URL`), split into the decoded path and the query, and validated by
`assert_request`; the contract's aliases map the adapter's `/me/drive/...` paths onto the spec's `/drives/{drive-id}/...`
operations. An upload's bytes are not validated (the operation's body is `application/octet-stream`). No network, no key.
`graph_request_shapes.SHAPES` is the table.

The shared fake has no `put` (the To Do and Gmail adapters never send one): `_Session` adds it here, recording a PUT the
way the fake records every other method; the fake's file is not changed.

Meta-tests: the closed subset rejects an unknown query parameter, an unknown body field (top level and one level down), a
path it does not hold and a JSON body sent to the upload; every kept operation, top-level body field, query parameter and
alias rule is used by a shape (a dead keep-list entry or alias fails); every alias target is an operation of the subset;
every URL the two adapter files build (read from their AST) maps to a kept operation or to a `NOT_COVERED` entry with its
reason (a stale entry fails).
"""
from __future__ import annotations

import ast
import asyncio
import functools
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import quote

import pytest
import yaml
from yarl import URL

from scripts.contracts.extract_subset import PROVIDERS
from src.adapters.firestore_task_config_repository import FirestoreTaskConfigRepository
from src.adapters.microsoft.onedrive_adapter import OneDriveAdapter
from src.adapters.microsoft.todo_adapter import MicrosoftToDoAdapter
from src.composition.connector_registry import ConnectorRegistry
from src.config.environment import EnvironmentConfig
from tests.contracts.openapi_contract import applied_alias, assert_request, operation_path
from tests.support import http_boundary_fake
from tests.support.drive_handles import drive_handle
from tests.support.firestore_eval import EvalDb
from tests.support.oauth_credentials_store import CredentialsStore, creds
from tests.unit.contracts import graph_request_shapes as shapes
from tests.unit.contracts.graph_request_shapes import INDIRECT, NOT_COVERED, SHAPES

REPO_ROOT = Path(__file__).resolve().parents[3]
ADAPTER_FILES = {
    "todo_adapter": REPO_ROOT / "src" / "adapters" / "microsoft" / "todo_adapter.py",
    "onedrive_adapter": REPO_ROOT / "src" / "adapters" / "microsoft" / "onedrive_adapter.py",
}
CONTRACT = REPO_ROOT / "contracts" / "providers" / "graph" / "openapi.yaml"
GRAPH = "https://graph.microsoft.com/v1.0"
GRAPH_HOST = "graph.microsoft.com"
CONTENT_HOST = "example.com"   # the invented pre-authenticated download and upload URLs
TODO = f"{GRAPH}/me/todo/lists/{shapes.LIST_ID}"
DRIVE = f"{GRAPH}/me/drive"
UPLOAD_URL = f"https://{CONTENT_HOST}/upload/{shapes.FOLDER_ID}"
DOWNLOAD_URL = f"https://{CONTENT_HOST}/download/{shapes.FILE_ID}"


# ------------------------------------------------------------------------------------------------- the routed fake
class _Session(http_boundary_fake.FakeSession):
    def put(self, url: str, headers: Any = None, json: Any = None, params: Any = None, data: Any = None,
            **kw: Any) -> Any:
        return self._req("PUT", url, headers, json, params, data)


def _install(monkeypatch: pytest.MonkeyPatch) -> http_boundary_fake.HttpFake:
    fake = http_boundary_fake.HttpFake()
    monkeypatch.setattr("aiohttp.ClientSession", lambda *a, **k: _Session(fake))
    return fake


_LIST = {"id": shapes.LIST_ID, "displayName": "Errands", "isOwner": True, "isShared": False}
_TASK = {"id": shapes.TASK_ID, "title": "car service Nov 4, bring the logbook", "status": "notStarted",
         "importance": "normal", "body": {"content": "Invoice FA-0137", "contentType": "text"},
         "checklistItems": [], "linkedResources": []}
_CHECKLIST = {"value": [{"id": "item-1", "displayName": "bring the logbook", "isChecked": False},
                        {"id": "item-2", "displayName": "a library card renewal due Nov 30", "isChecked": False}]}


def _drive_item(item_id: str, name: str, **extra: Any) -> dict:
    return {"id": item_id, "name": name, "size": 24, "cTag": "c1", "lastModifiedDateTime": "2026-11-03T09:00:00Z",
            "parentReference": {"driveId": shapes.DRIVE_ID, "id": shapes.ROOT_ID,
                                "path": f"/drive/root:/{quote(shapes.ROOT_NAME)}"}, **extra}


_ROOT = {"id": shapes.ROOT_ID, "name": shapes.ROOT_NAME, "folder": {"childCount": 1},
         "parentReference": {"driveId": shapes.DRIVE_ID, "path": "/drive/root:"}}
_FILE = _drive_item(shapes.FILE_ID, shapes.FILE_NAME, file={"mimeType": "application/pdf"},
                    **{"@microsoft.graph.downloadUrl": DOWNLOAD_URL})
_PDF = _drive_item(shapes.FOLDER_ID, shapes.PDF_NAME, file={"mimeType": "application/pdf"})
_FOLDER = _drive_item(shapes.FOLDER_ID, shapes.ROOT_NAME, folder={"childCount": 0})


def _route(fake: http_boundary_fake.HttpFake) -> None:
    lists_created = {"done": False}

    def lists(_r: Any) -> tuple[int, Any]:
        return 200, {"value": [_LIST] if lists_created["done"] else []}

    def create_list(_r: Any) -> tuple[int, Any]:
        lists_created["done"] = True
        return 201, dict(_LIST)

    fake.route("GET", f"{GRAPH}/me/todo/lists", handler=lists)
    fake.route("POST", f"{GRAPH}/me/todo/lists", handler=create_list)
    fake.route("GET", TODO, 200, _LIST)
    fake.route("GET", f"{TODO}/tasks", 200, {"value": [_TASK]})
    fake.route("POST", f"{TODO}/tasks", 201, _TASK)
    task = f"{TODO}/tasks/{shapes.TASK_ID}"
    for method, status, body in (("GET", 200, _TASK), ("PATCH", 200, _TASK), ("DELETE", 204, None)):
        fake.route(method, task, status, body)
    fake.route("GET", f"{task}/checklistItems", 200, _CHECKLIST)
    fake.route("POST", f"{task}/checklistItems", 201, {"id": "item-3", "displayName": "bring the logbook"})
    for item in ("item-1", "item-2"):
        fake.route("PATCH", f"{task}/checklistItems/{item}", 200, {"id": item})
        fake.route("DELETE", f"{task}/checklistItems/{item}", 204, None)
    fake.route("POST", f"{GRAPH}/subscriptions", 201, {"id": shapes.SUB_ID})
    fake.route("PATCH", f"{GRAPH}/subscriptions/{shapes.SUB_ID}", 200,
               {"id": shapes.SUB_ID, "resource": f"/me/todo/lists/{shapes.LIST_ID}/tasks"})
    fake.route("DELETE", f"{GRAPH}/subscriptions/{shapes.SUB_ID}", 204, None)

    approot = f"{DRIVE}/special/approot"
    item = f"{DRIVE}/items/{shapes.FILE_ID}"
    root = f"{DRIVE}/items/{shapes.ROOT_ID}"
    pdf = f"{root}:/{quote(shapes.PDF_NAME, safe='')}:"
    search = shapes.SEARCH_TEXT.replace("'", "''")
    fake.route("GET", approot, 200, _ROOT)
    fake.route("GET", f"{approot}/delta", 200, {"value": [_FILE], "@odata.deltaLink": f"{approot}/delta?token=page-2"})
    fake.route("GET", f"{approot}/search(q='{quote(search, safe='')}')", 200, {"value": [_FILE]})
    for method, status, body in (("GET", 200, _FILE), ("PATCH", 200, {**_FILE, "name": shapes.RENAMED}),
                                 ("DELETE", 204, None)):
        fake.route(method, item, status, body)
    fake.route("GET", f"{item}/thumbnails", 200, {"value": [{}]})   # no rendition: the method ends at the listing
    fake.route("PUT", f"{item}/content", 200, _FILE)
    fake.route("POST", f"{item}/createUploadSession", 200, {"uploadUrl": UPLOAD_URL})
    fake.route("GET", f"{root}/children", 200, {"value": [_FILE]})
    fake.route("POST", f"{root}/children", 201, _FOLDER)
    fake.route("GET", f"{root}:/{quote(shapes.FILE_NAME, safe='')}:", 200, _FILE)
    fake.route("PUT", f"{pdf}/content", 201, _PDF)
    fake.route("POST", f"{pdf}/createUploadSession", 200, {"uploadUrl": UPLOAD_URL})
    fake.route("PUT", UPLOAD_URL, 201, _PDF)
    fake.route("GET", DOWNLOAD_URL, 200, shapes.SMALL)


# ---------------------------------------------------------------------------------------------------- the capture
@dataclass(frozen=True)
class Sent:
    method: str
    host: str
    path: str                               # decoded, with the server's base path (/v1.0/...)
    headers: Mapping[str, str]
    body: Any
    query: Mapping[str, tuple[str, ...]]
    raw_path: str = ""                      # the same path as sent, still percent-encoded


def _sent(request: http_boundary_fake.RecordedRequest) -> Sent:
    url = URL(request.url)                   # what aiohttp's ClientRequest does with a str URL
    if request.params:
        url = url.extend_query(request.params)
    query = {name: tuple(url.query.getall(name)) for name in dict.fromkeys(url.query)}
    body = request.json if request.json is not None else request.data
    return Sent(request.method, url.host or "", url.path, dict(request.headers or {}), body, query, url.raw_path)


async def _run(shape: shapes.Shape) -> Any:
    store = CredentialsStore()
    valid = datetime.now(timezone.utc) + timedelta(days=1)
    await store.repo.save(creds(shapes.USER, "microsoft_todo", access_token="x", token_expiry=valid))
    credential_id = await store.repo.save(creds(shapes.USER, "microsoft_onedrive", access_token="x",
                                                token_expiry=valid))
    registry = ConnectorRegistry()
    if shape.adapter == "todo":
        adapter: Any = MicrosoftToDoAdapter(
            store.repo, FirestoreTaskConfigRepository(EvalDb(), EnvironmentConfig()), "client-id", "client-secret",
            webhook_secret=shapes.CLIENT_STATE, connector=registry.get("microsoft_todo"))
        return await shape.call(adapter, None)
    adapter = OneDriveAdapter(store.repo, "client-id", "client-secret", connector=registry.get("microsoft_onedrive"))
    return await shape.call(adapter, drive_handle(shapes.USER, credential_id=credential_id))


@functools.lru_cache(maxsize=None)
def _captured(shape_id: str) -> tuple[Sent, ...]:
    shape = next(s for s in SHAPES if s.id == shape_id)
    with pytest.MonkeyPatch.context() as mp:
        fake = _install(mp)
        _route(fake)
        asyncio.run(_run(shape))   # an unrouted request or a refused answer raises: the call did not run to its end
    assert fake.unrouted == [], f"{shape_id}: unrouted {fake.unrouted}"
    return tuple(_sent(r) for r in fake.requests)


def _graph(sent: tuple[Sent, ...]) -> list[Sent]:
    return [s for s in sent if s.host == GRAPH_HOST]


def _all_graph() -> list[Sent]:
    return [s for shape in SHAPES for s in _graph(_captured(shape.id))]


def _check(sent: Sent, **over: Any) -> None:
    """`path` overridden alone: an ASCII path whose raw form is itself."""
    fields = {"path": sent.path, "query": sent.query, "body": sent.body, **over}
    raw = over.get("raw_path", over["path"] if "path" in over else sent.raw_path)
    assert_request("graph", sent.method, fields["path"], sent.headers, fields["body"],
                   query={k: list(v) for k, v in fields["query"].items()}, raw_path=raw)


def _op(sent: Sent) -> tuple[str, str]:
    return sent.method.lower(), operation_path("graph", sent.method, sent.path, raw_path=sent.raw_path)


# ------------------------------------------------------------------------------------------------------ the shapes
def test_shape_ids_are_unique():
    ids = [s.id for s in SHAPES]
    assert len(ids) == len(set(ids)) and len(ids) >= 30


@pytest.mark.parametrize("shape", SHAPES, ids=[s.id for s in SHAPES])
def test_every_graph_request_of_the_shape_conforms_to_the_contract(shape):
    sent = _captured(shape.id)
    assert _graph(sent), f"{shape.id}: no Graph request recorded"
    for request in _graph(sent):
        _check(request)
    others = {s.host for s in sent if s.host != GRAPH_HOST}
    assert others <= {CONTENT_HOST}, f"{shape.id}: requests to {others}, neither Graph nor a pre-authenticated URL"


@pytest.mark.parametrize("shape", SHAPES, ids=[s.id for s in SHAPES])
def test_the_closed_subset_rejects_an_unknown_query_parameter(shape):
    for request in _graph(_captured(shape.id)):
        with pytest.raises(AssertionError, match="query zz_unknown: unknown query parameter"):
            _check(request, query={**request.query, "zz_unknown": ("1",)})


def _json_bodies() -> list[Sent]:
    return [s for s in _all_graph() if isinstance(s.body, dict)]


def test_the_closed_subset_rejects_an_unknown_top_level_body_field():
    assert _json_bodies()
    for request in _json_bodies():
        with pytest.raises(AssertionError, match=r"\$\.zz_unknown: unknown field"):
            _check(request, body={**request.body, "zz_unknown": 1})


def _nested(body: dict) -> Iterator[tuple[str, dict]]:
    """(JSON path, body with `zz_unknown` added one level down) for each object-valued field and each object item."""
    for name, value in body.items():
        if isinstance(value, dict):
            yield f"$.{name}.zz_unknown", {**body, name: {**value, "zz_unknown": 1}}
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            yield f"$.{name}[0].zz_unknown", {**body, name: [{**value[0], "zz_unknown": 1}, *value[1:]]}


def test_the_closed_subset_rejects_an_unknown_body_field_one_level_down():
    tried = []
    for request in _json_bodies():
        for where, body in _nested(request.body):
            tried.append(where)
            with pytest.raises(AssertionError, match=re.escape(where) + ": unknown field"):
                _check(request, body=body)
    # every nested object we send: dates, recurrence, checklist, linked resources, parent reference, upload item
    assert {"$.dueDateTime.zz_unknown", "$.recurrence.zz_unknown", "$.checklistItems[0].zz_unknown",
            "$.linkedResources[0].zz_unknown", "$.parentReference.zz_unknown", "$.item.zz_unknown",
            "$.body.zz_unknown"} <= set(tried)


def test_a_value_outside_the_spec_is_rejected():
    """The flattened, converted schemas still judge values: an enum outside its literals, a date-time off its pattern."""
    create = next(s for s in _json_bodies() if _op(s) == ("post", "/me/todo/lists/{todoTaskList-id}/tasks"))
    with pytest.raises(AssertionError, match=r"\$\.importance: value not allowed \('urgent'\)"):
        _check(create, body={**create.body, "importance": "urgent"})
    renew = next(s for s in _json_bodies() if _op(s) == ("patch", "/subscriptions/{subscription-id}"))
    with pytest.raises(AssertionError, match=r"\$\.expirationDateTime: value not allowed \(pattern"):
        _check(renew, body={"expirationDateTime": "in three days"})


def test_a_path_the_contract_does_not_hold_fails():
    request = _graph(_captured("todo/get_task"))[0]
    with pytest.raises(AssertionError, match="is not in"):
        _check(request, path="/v1.0/me/todo/lists/list-1/tasks/task-1/attachments")
    drive = next(s for s in _graph(_captured("drive/get_item")) if "/items/" in s.path)
    with pytest.raises(AssertionError, match=r"\(aliased to /drives/\(alias\)/items/item-2/permissions\) is not in"):
        _check(drive, path="/v1.0/me/drive/items/item-2/permissions")


def _uploads() -> list[Sent]:
    return [s for s in _all_graph() if s.method == "PUT"]


def test_an_upload_takes_bytes_unvalidated_and_refuses_a_json_body():
    assert _uploads() and all(isinstance(s.body, bytes) for s in _uploads())
    for request in _uploads():
        with pytest.raises(AssertionError, match=r"\$: wrong type \(the operation takes a application/octet-stream"):
            _check(request, body={"name": shapes.PDF_NAME})


def test_the_conflict_behaviour_query_value_is_checked():
    request = next(s for s in _uploads() if s.query)
    with pytest.raises(AssertionError, match="query @microsoft.graph.conflictBehavior: value not allowed"):
        _check(request, query={**request.query, "@microsoft.graph.conflictBehavior": ("overwrite",)})


def test_the_search_literal_doubles_the_quote_of_the_text():
    """The capture is faithful: the search text reaches Graph inside q='...' with its quote doubled."""
    search = [s for s in _all_graph() if "search(" in s.path]
    assert search and all(s.path.endswith("/search(q='Tom''s plan')") for s in search)


# ------------------------------------------------------------------------------------------ the keep list is used
@functools.lru_cache(maxsize=None)
def _contract() -> dict:
    return yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))


def _kept_query() -> dict[tuple[str, str], set[str]]:
    return {(method, path): {p["name"] for p in op.get("parameters") or [] if p["in"] == "query"}
            for path, item in _contract()["paths"].items() for method, op in item.items()}


def test_the_contract_keeps_exactly_the_table_query_parameters():
    table = {(op.method, op.path): set(op.query) | {a.field for a in op.allowed if a.path == "query"}
             for op in PROVIDERS["graph"].operations}
    assert _kept_query() == table


def test_every_kept_query_parameter_is_sent_by_a_shape():
    sent: dict[tuple[str, str], set[str]] = {}
    for request in _all_graph():
        sent.setdefault(_op(request), set()).update(request.query)
    dead = {op: sorted(names - sent.get(op, set())) for op, names in _kept_query().items()}
    assert {op: names for op, names in dead.items() if names} == {}, "kept query parameters no shape sends"


def test_every_kept_operation_is_reached_by_a_shape():
    assert {_op(request) for request in _all_graph()} == set(_kept_query())


def test_every_listed_body_field_is_sent_by_a_shape():
    sent: dict[tuple[str, str], set[str]] = {}
    for request in _json_bodies():
        sent.setdefault(_op(request), set()).update(request.body)
    dead = {(op.method, op.path): sorted(set(op.fields) - sent.get((op.method, op.path), set()))
            for op in PROVIDERS["graph"].operations}
    assert {op: names for op, names in dead.items() if names} == {}, "listed body fields no shape sends"


def test_every_alias_rule_is_used_by_a_shape():
    used = {applied_alias("graph", request.path, raw_path=request.raw_path) for request in _all_graph()}
    patterns = [rule["pattern"] for rule in _contract()["x-alek-aliases"]]
    assert sorted(set(patterns) - used) == [], "alias rules no shape's path goes through"


def test_every_alias_target_is_an_operation_of_the_subset():
    paths = _contract()["paths"]
    for rule in _contract()["x-alek-aliases"]:
        target = rule["target"]
        if target.endswith("/**"):
            assert any(p.startswith(target[:-2]) for p in paths), target
        else:
            assert target in paths, target
        assert rule["reason"].strip()


# ---------------------------------------------------------------------------------------- the adapters' call sites
_VERBS = {"get", "post", "put", "patch", "delete"}
# adapter helper -> (method, index of the URL argument); the method None: read from the argument before the URL
FORWARDERS: dict[str, dict[str, tuple[str | None, int]]] = {
    "todo_adapter": {"_get": ("get", 1), "_post": ("post", 1), "_patch": ("patch", 1), "_delete": ("delete", 1)},
    "onedrive_adapter": {"_call": (None, 3), "_session_upload": ("post", 2)},
}


class _Unreadable(Exception):
    """A URL expression the reader cannot render: a value Graph or a caller produced at run time."""


class _Reader:
    """Renders the URL expressions of one adapter file: module string constants and f-strings over them substituted,
    a local variable as each value assigned to it in its function, any other interpolation kept as `{expr}`."""

    def __init__(self, tree: ast.Module):
        self.constants: dict[str, ast.expr] = {
            target.id: node.value for node in tree.body if isinstance(node, ast.Assign)
            for target in node.targets if isinstance(target, ast.Name)
            and isinstance(node.value, (ast.Constant, ast.JoinedStr))}

    def render(self, node: ast.expr, fn: ast.AST | None) -> set[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.Name) and node.id in self.constants:
            return self.render(self.constants[node.id], None)
        if isinstance(node, ast.Name) and fn is not None and self._assigned(fn, node.id):
            out: set[str] = set()
            for value in self._assigned(fn, node.id):
                out |= self.render(value, fn)
            return out
        if isinstance(node, ast.JoinedStr):
            texts = {""}
            for part in node.values:
                if isinstance(part, ast.Constant):
                    pieces = {str(part.value)}
                else:
                    pieces = {"{" + ast.unparse(part.value) + "}"}   # an id or a name: one segment's value
                    if isinstance(part.value, ast.Name) and (part.value.id in self.constants or fn is not None
                                                             and self._assigned(fn, part.value.id)):
                        try:
                            pieces = self.render(part.value, fn)       # a URL piece held in a variable
                        except _Unreadable:
                            pass
                texts = {t + p for t in texts for p in pieces}
            return texts
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return {a + b for a in self.render(node.left, fn) for b in self.render(node.right, fn)}
        if isinstance(node, ast.IfExp):
            return self.render(node.body, fn) | self.render(node.orelse, fn)
        raise _Unreadable(ast.unparse(node))

    def alternatives(self, node: ast.expr, fn: ast.AST | None) -> list[ast.expr]:
        """A URL argument that is a local variable: each value assigned to it (each is a URL the call can send)."""
        if isinstance(node, ast.Name) and node.id not in self.constants and fn is not None \
                and self._assigned(fn, node.id):
            return [alt for value in self._assigned(fn, node.id) for alt in self.alternatives(value, fn)]
        return [node]

    @staticmethod
    def _assigned(fn: ast.AST, name: str) -> list[ast.expr]:
        out: list[ast.expr] = []
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                out.append(node.value)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                    and node.target.id == name and node.value is not None:
                out.append(node.value)
            elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Tuple) and any(
                    isinstance(e, ast.Name) and e.id == name for e in t.elts) for t in node.targets):
                out.append(node.value)   # a tuple unpacked: the whole call stands for the value
        return out


def _sessions(tree: ast.Module) -> set[str]:
    named = {item.optional_vars.id for node in ast.walk(tree) if isinstance(node, ast.AsyncWith)
             for item in node.items if isinstance(item.optional_vars, ast.Name)
             and ast.unparse(item.context_expr).startswith("aiohttp.ClientSession(")}
    typed = {arg.arg for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             for arg in node.args.args if arg.annotation is not None
             and ast.unparse(arg.annotation) == "aiohttp.ClientSession"}
    return named | typed


def _call_sites(stem: str) -> set[str]:
    """`<stem> <METHOD> <URL>` of every request the adapter file sends, read from its AST; an expression the reader
    cannot render gives `<stem> <METHOD> <expression>` (a NOT_COVERED or INDIRECT key)."""
    tree = ast.parse(ADAPTER_FILES[stem].read_text(encoding="utf-8"))
    reader, sessions, forwarders = _Reader(tree), _sessions(tree), FORWARDERS[stem]
    owner: dict[int, ast.AST] = {}
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner.update({id(node): fn for node in ast.walk(fn)})
    sites: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)):
            continue
        receiver, attr = node.func.value.id, node.func.attr
        if receiver in sessions and attr in _VERBS:
            verb, url = attr, node.args[0]
        elif receiver == "self" and attr in forwarders:
            fixed, index = forwarders[attr]
            verb_arg = node.args[index - 1] if fixed is None else None
            if verb_arg is not None and not (isinstance(verb_arg, ast.Constant) and isinstance(verb_arg.value, str)):
                raise AssertionError(f"a method from a variable: {ast.unparse(node)}")
            verb, url = (fixed or str(verb_arg.value)).lower(), node.args[index]  # type: ignore[union-attr]
        else:
            continue
        fn = owner.get(id(node))
        params = {a.arg for a in fn.args.args} if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) else set()
        if getattr(fn, "name", "") in forwarders and {n.id for n in ast.walk(url) if isinstance(n, ast.Name)} & params:
            continue   # the forwarder sends the URL its caller gave: the caller's call is the site
        for alternative in reader.alternatives(url, fn):
            try:
                sites |= {f"{stem} {verb.upper()} {text}" for text in reader.render(alternative, fn)}
            except _Unreadable:
                sites.add(f"{stem} {verb.upper()} {ast.unparse(alternative)}")
    return sites


def _all_sites() -> set[str]:
    return set().union(*(_call_sites(stem) for stem in ADAPTER_FILES))


def _site_operation(site: str) -> tuple[str, str] | None:
    stem, verb, text = site.split(" ", 2)
    if site in INDIRECT:
        tree = ast.parse(ADAPTER_FILES[stem].read_text(encoding="utf-8"))
        text = next(iter(_Reader(tree).render(ast.Name(id=INDIRECT[site], ctx=ast.Load()), None)))
    if text.startswith("/"):
        text = GRAPH + text
    if not text.startswith("https://"):
        return None
    url = URL(re.sub(r"\{[^}]+\}", "x-1", text.split("?", 1)[0]))
    if url.host != GRAPH_HOST:
        return None
    try:
        return verb.lower(), operation_path("graph", verb, url.path, raw_path=url.raw_path)
    except AssertionError:
        return None


def test_every_url_the_adapters_build_maps_to_a_kept_operation_or_is_not_covered():
    sites = _all_sites()
    assert {s.split(" ", 1)[0] for s in sites} == set(ADAPTER_FILES), "an adapter without a call site: stale pattern"
    uncovered = {site for site in sites if _site_operation(site) is None}
    assert sorted(uncovered - set(NOT_COVERED)) == [], "call sites with no operation and no NOT_COVERED reason"
    mapped = {_site_operation(site) for site in sites} - {None}
    assert mapped == set(_kept_query()), "kept operations no call site sends, or call sites outside the table"


def test_the_not_covered_and_indirect_lists_have_no_stale_entry():
    sites = _all_sites()
    assert sorted((set(NOT_COVERED) | set(INDIRECT)) - sites) == [], "entries the adapters no longer send"
    assert sorted(s for s in NOT_COVERED if _site_operation(s) is not None) == [], "NOT_COVERED entries now covered"
    assert all(_site_operation(s) is not None for s in INDIRECT), "INDIRECT entries that map to no operation"
    assert all(reason.strip() for reason in NOT_COVERED.values())
