"""A routed fake of `aiohttp.ClientSession` for the HTTP boundary (P15 card B).

Every adapter under `src/` does `import aiohttp` and calls `aiohttp.ClientSession()` at call time, so
`install()` patches that one attribute. The fake serves Graph To Do, Gmail and the Slack incoming webhook
through routes; a test asserts on what the adapter returned and on `requests`, never on a call.

Routes match the exact path (the query string is ignored); `prefix=True` is an explicit opt-in. Anything
else is unrouted and loud: it raises `ClientConnectionError`, and is recorded in `unrouted` AND in `requests`.
Gmail swallows per-message errors (`except Exception` -> skip), so a Gmail test must also assert
`fake.unrouted == []`, or an unrouted per-message fetch passes silently.

Shape promoted from `tests/unit/adapters/test_todo_time_instants_wire.py` (`_Resp`, `_Session`) and
`tests/support/gmail_wire.py`: a request is both awaitable (Slack: `resp = await session.post(...)`) and an
async context manager (To Do, Gmail: `async with session.get(...) as resp`); the response has a sync
`raise_for_status()` (Gmail).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple

import json as _json

import aiohttp
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

_GRAPH = "https://graph.microsoft.com/v1.0"
_GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    url: str
    json: Any
    params: Any
    headers: Any = None
    data: Any = None

    def __iter__(self):  # unpacks as (method, url, json, params)
        return iter((self.method, self.url, self.json, self.params))


Handler = Callable[[RecordedRequest], Tuple[int, Any]]


@dataclass
class _Route:
    method: str
    prefix: str
    exact: bool
    handler: Handler


class FakeResponse:
    def __init__(self, status: int, body: Any, url: str = "", method: str = "GET"):
        self._method = method
        self.status = status
        self._body = body
        self._url = url

    @property
    def ok(self) -> bool:
        return self.status < 400

    def _request_info(self) -> aiohttp.RequestInfo:
        u = URL(self._url or "http://fake.invalid/")
        return aiohttp.RequestInfo(url=u, method=self._method, headers=CIMultiDictProxy(CIMultiDict()), real_url=u)

    async def json(self, *a: Any, content_type: Optional[str] = "application/json", **k: Any) -> Any:
        """As aiohttp: a non-JSON body raises ContentTypeError unless called with `content_type=None`."""
        if isinstance(self._body, (dict, list)):
            return self._body
        if content_type is not None or self._body is None:
            raise aiohttp.ContentTypeError(self._request_info(), (), message="Attempt to decode JSON with unexpected mimetype: text/plain")
        return _json.loads(await self.text())

    async def text(self) -> str:
        if isinstance(self._body, (dict, list)):
            return _json.dumps(self._body)
        if isinstance(self._body, bytes):
            return self._body.decode()
        return self._body if isinstance(self._body, str) else str(self._body)

    async def read(self) -> bytes:
        if isinstance(self._body, bytes):
            return self._body
        return (await self.text()).encode()

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=self._request_info(), history=(), status=self.status, message=f"fake {self.status}",
            )

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *a: Any) -> bool:
        return False


class _Request:
    """Awaitable and an async context manager, like aiohttp's `_RequestContextManager`."""

    def __init__(self, fake: "HttpFake", req: RecordedRequest):
        self._fake, self._req = fake, req

    async def _resolve(self) -> FakeResponse:
        return self._fake._serve(self._req)

    def __await__(self):
        return self._resolve().__await__()

    async def __aenter__(self) -> FakeResponse:
        return await self._resolve()

    async def __aexit__(self, *a: Any) -> bool:
        return False


class FakeSession:
    def __init__(self, fake: "HttpFake"):
        self._fake = fake

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *a: Any) -> bool:
        return False

    def _req(self, method: str, url: str, headers: Any, json: Any, params: Any, data: Any) -> _Request:
        return _Request(self._fake, RecordedRequest(method, url, json, params, headers, data))

    def get(self, url: str, headers: Any = None, json: Any = None, params: Any = None,
            data: Any = None, **kw: Any) -> _Request:
        return self._req("GET", url, headers, json, params, data)

    def post(self, url: str, headers: Any = None, json: Any = None, params: Any = None,
            data: Any = None, **kw: Any) -> _Request:
        return self._req("POST", url, headers, json, params, data)

    def patch(self, url: str, headers: Any = None, json: Any = None, params: Any = None,
            data: Any = None, **kw: Any) -> _Request:
        return self._req("PATCH", url, headers, json, params, data)

    def delete(self, url: str, headers: Any = None, json: Any = None, params: Any = None,
            data: Any = None, **kw: Any) -> _Request:
        return self._req("DELETE", url, headers, json, params, data)


class HttpFake:
    def __init__(self) -> None:
        self._routes: List[_Route] = []
        self.requests: List[RecordedRequest] = []
        self.unrouted: List[RecordedRequest] = []
        self.posts: List[Any] = []  # JSON bodies of POSTs to a `slack_webhook` route

    def route(self, method: str, url: str, status: int = 200, body: Any = None,
              handler: Optional[Handler] = None, prefix: bool = False) -> None:
        """Exact path (query ignored) unless `prefix=True`. An exact match beats a prefix; the longest prefix wins;
        a later route with the same method and url replaces the earlier one."""
        method = method.upper()
        self._routes = [r for r in self._routes if not (r.method == method and r.prefix == url and r.exact != prefix)]
        self._routes.append(_Route(method, url, not prefix, handler or (lambda _r, s=status, b=body: (s, b))))

    def requests_to(self, method: str, url_prefix: str) -> List[RecordedRequest]:
        return [r for r in self.requests if r.method == method.upper() and r.url.startswith(url_prefix)]

    def _serve(self, req: RecordedRequest) -> FakeResponse:
        self.requests.append(req)
        path = req.url.split("?", 1)[0]
        exact = [r for r in self._routes if r.method == req.method and r.exact and path == r.prefix]
        matches = exact or [r for r in self._routes
                            if r.method == req.method and not r.exact and req.url.startswith(r.prefix)]
        if not matches:
            self.unrouted.append(req)
            raise aiohttp.ClientConnectionError(f"no route in the fake: {req.method} {req.url}")
        route = max(matches, key=lambda r: len(r.prefix))
        status, body = route.handler(req)
        return FakeResponse(status, body, req.url, req.method)

    # ------------------------------------------------------------------ presets

    def todo_empty_account(self, list_id: str = "l1", sub_id: str = "s1") -> None:
        """A Graph account with no lists: the list appears once `ensure_primary_list` POSTs it."""
        created = {"done": False}
        listing = f"{_GRAPH}/me/todo/lists"
        row = {"id": list_id, "displayName": "Alek Bot Tasks"}

        def lists(_r: RecordedRequest) -> Tuple[int, Any]:
            return 200, {"value": [row] if created["done"] else []}

        def create(_r: RecordedRequest) -> Tuple[int, Any]:
            created["done"] = True
            return 201, dict(row)

        self.route("GET", listing, handler=lists)
        self.route("POST", listing, handler=create)
        self.route("POST", f"{_GRAPH}/subscriptions", 201, {"id": sub_id})
        self.route("GET", f"{listing}/{list_id}", 200, dict(row))
        self.route("GET", f"{listing}/{list_id}/tasks", 200, {"value": []})

    def gmail_empty(self) -> None:
        self.route("GET", f"{_GMAIL}/messages", 200, {"messages": []})

    def slack_webhook(self, url: str) -> None:
        def take(req: RecordedRequest) -> Tuple[int, Any]:
            self.posts.append(req.json)
            return 200, "ok"

        self.route("POST", url, handler=take)


def install(monkeypatch: Any) -> HttpFake:
    """Patch `aiohttp.ClientSession` (every adapter looks it up at call time) with a session over a new `HttpFake`."""
    fake = HttpFake()
    monkeypatch.setattr("aiohttp.ClientSession", lambda *a, **k: FakeSession(fake))
    return fake
