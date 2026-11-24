"""The world of the tasks MCP resource's tests (`/tasks-mcp`, EXTERNAL_TASK_QUEUE_RFC §5.4): the REST endpoint's world
(`tests/unit/web/test_external_task_api.py`) with the tasks app on top.

The real `ExternalTaskService` over the in-memory stores of `external_task_stores.py`, the real `FirestoreUserRepository`
on `EvalDb`, the real limiter over a `FirestoreWindowCounter` on a second `EvalDb`, the real `ExternalWorkerUserGate`
over the whitelist fake, the real `InactiveUserCheck`, and the texts loaded by `ConnectorTextService` from marker tokens
(`task_mcp_texts.py`). `post` builds a fresh app per request (a stateless app's session manager starts once per
instance) and sends one JSON-RPC body with a worker key as the bearer.

Alex Doe is `usr-1` (account `acc-1`) with the workers `research` and `calendar-watch`; `usr-2` is another user.
"""
import json
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import httpx

from src.domain.external_task_entry import TaskEntry
from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.adapters.firestore_window_counter import FirestoreWindowCounter
from src.composition import tasks_mcp_setup as tms
from src.domain import external_task_mcp_contract as c
from src.domain.external_task import TaskOrigin
from src.domain.external_worker_key import WORKER_KEY_PREFIX
from src.domain.user import UserProfile
from src.services.connector_text_service import ConnectorTextService
from src.services.external_task_rate_limiter import ExternalTaskRateLimiter
from src.services.external_task_service import ExternalTaskService
from src.services.external_worker_user_gate import ExternalWorkerUserGate
from src.services.inactive_user_check import InactiveUserCheck
from tests.support.clock_instants import UTC_MIDNIGHT
from tests.support.external_task_stores import FakeExternalTaskStore, FakeExternalWorkerStore
from tests.support.firestore_eval import EvalDb
from tests.support.mcp_gate_fakes import FakeWhitelistRepo
from tests.support.recording_task_queue import make_task_queue
from tests.support.task_mcp_texts import RESULT_TOKEN, TOOL_TOKEN, TaskTokenStore

T0 = UTC_MIDNIGHT.utc
DIGEST_KEY = bytes(range(32))
USERS = "users"
RATES = "eval_worker_task_dedup"
BASE = "https://mcp.example.test"
AUTH_CONFIG = SimpleNamespace(mcp_resource_uri=f"{BASE}/mcp")
TASKS_URI = f"{BASE}/tasks-mcp"
ORIGIN = TaskOrigin(platform="slack", channel_id="C-garden", session_id="usr-1:C-garden")
TITLE = "Irrigation kits"
TEXT = "compare three community-garden irrigation kits for Shelbyville"
RESULT = "Kit B waters the most beds per litre."
UNKNOWN_KEY = WORKER_KEY_PREFIX + "A" * 43


class World:
    def __init__(self) -> None:
        self.db = EvalDb()
        self.rates_db = EvalDb()
        env = SimpleNamespace(domain_users_collection=USERS, is_production=False)
        self.users = FirestoreUserRepository(self.db, env, FirestoreAccountRepository(self.db, "accounts"))
        self.tasks = FakeExternalTaskStore()
        self.workers = FakeExternalWorkerStore()
        self.queue, _ = make_task_queue()
        self.now = T0
        self._ids = 0
        self._bytes = 0
        self.inactive = InactiveUserCheck(self.users)
        self.service = ExternalTaskService(self.tasks, self.workers, self.queue, self.users, self.inactive,
                                           digest_key=DIGEST_KEY, clock=lambda: self.now, new_id=self._new_id,
                                           random_bytes=self._random_bytes)
        self.whitelist = FakeWhitelistRepo(emails=("alex@example.com", "other@example.com"))
        self.gate = ExternalWorkerUserGate(self.users, self.whitelist)
        self.limiter = ExternalTaskRateLimiter(
            FirestoreWindowCounter(self.rates_db, SimpleNamespace(worker_task_dedup_collection=RATES)),
            clock=lambda: self.now)
        self.store = TaskTokenStore()
        self.texts = ConnectorTextService(self.store, tool_token_id=TOOL_TOKEN, result_token_id=RESULT_TOKEN,
                                          required_tool_keys=c.REQUIRED_TOOL_KEYS,
                                          required_result_keys=c.REQUIRED_RESULT_KEYS)
        self.texts_override: Any = None

    def _new_id(self) -> str:
        self._ids += 1
        return f"id-{self._ids}"

    def _random_bytes(self, n: int) -> bytes:
        self._bytes += 1
        return bytes([self._bytes % 256]) * n

    async def seed(self) -> "World":
        for uid, acc, email in (("usr-1", "acc-1", "alex@example.com"), ("usr-2", "acc-2", "other@example.com")):
            await self.users.create_user(UserProfile(user_id=uid, account_id=acc, email=email, display_name=uid))
            await self.flag(uid, True)
        self.research = await self.service.create_worker("usr-1", "research", "web research")
        self.watch = await self.service.create_worker("usr-1", "calendar-watch")
        self.theirs = await self.service.create_worker("usr-2", "research")
        self.key = (await self.service.mint_key("usr-1", self.research.worker_id)).key
        self.watch_key = (await self.service.mint_key("usr-1", self.watch.worker_id)).key
        self.their_key = (await self.service.mint_key("usr-2", self.theirs.worker_id)).key
        return self

    async def flag(self, user_id: str, on: bool) -> None:
        await self.db.collection(USERS).document(user_id).update({"config.external_tasks_enabled": on})

    async def create(self, user_id: str = "usr-1", label: str = "research", entry: TaskEntry = TaskEntry.REMINDER):
        return await self.service.create(user_id, label, TITLE, TEXT, ORIGIN, entry=entry)

    def app(self, inactive_users: Any = None):
        """A fresh app per request: a stateless app's session manager starts once per instance."""
        return tms.build_tasks_mcp_app(external_tasks=self.service, limiter=self.limiter, gate=self.gate,
                                       inactive_users=inactive_users or self.inactive,
                                       texts=self.texts_override or self.texts,
                                       initial_texts=self.initial, auth_config=AUTH_CONFIG)

    async def post(self, body: Dict[str, Any], key: Optional[str], *, inactive_users: Any = None) -> httpx.Response:
        app = self.app(inactive_users)
        headers = {"Accept": "application/json, text/event-stream"}
        if key is not None:
            headers["Authorization"] = f"Bearer {key}"
        transport = httpx.ASGITransport(app=app.streamable_http_app(), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url=BASE) as http:
            async with app.session_manager.run():
                return await http.post("/tasks-mcp", json=body, headers=headers)

    async def rpc(self, body: Dict[str, Any], key: Optional[str] = None, **kw: Any) -> Dict[str, Any]:
        resp = await self.post(body, self.key if key is None else key, **kw)
        assert resp.status_code == 200, resp.text
        data = next(line[len("data: "):] for line in resp.text.splitlines() if line.startswith("data: ")) \
            if resp.headers["content-type"].startswith("text/event-stream") else resp.text
        return json.loads(data)

    async def call(self, name: str, key: Optional[str] = None, **arguments: Any) -> Dict[str, Any]:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        return (await self.rpc(body, key))["result"]

    async def list_tools(self, key: Optional[str] = None) -> List[Dict[str, Any]]:
        return (await self.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, key))["result"]["tools"]

    async def claim(self, key: Optional[str] = None) -> Dict[str, str]:
        """Claims through the tool and returns the claimed task's ids, read back from the store."""
        text = ok(await self.call("claim_task", key))
        task_id = text.split("\n")[0].split()[-1].rstrip(">")
        task = await self.tasks.get("usr-2" if key == self.their_key else "usr-1", task_id)
        return {"task_id": task_id, "lease_id": task.lease.lease_id, "text": text}

    def rate_docs(self) -> int:
        return len(self.rates_db.collection(RATES).docs)


def ok(res: Dict[str, Any]) -> str:
    """The text of a successful tool result: one text block, no structured content."""
    assert res["isError"] is False, res
    assert [block["type"] for block in res["content"]] == ["text"]
    assert res.get("structuredContent") is None
    return res["content"][0]["text"]


def err(res: Dict[str, Any]) -> str:
    assert res["isError"] is True, res
    return res["content"][0]["text"]
