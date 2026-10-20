"""GcsWorkspaceAdapter over REAL google-cloud-storage objects, stubbed only at the HTTP layer
(docs/how_to/ADAPTER_WIRE_TESTING.md): what actually goes on the wire. The delete must carry ifGenerationMatch and
never generation — a generation-addressed DELETE erases that version (AGENT_WORKSPACE_RFC §5.3); a create must carry
ifGenerationMatch=0 and the write_id."""
import json

from google.auth.credentials import AnonymousCredentials
from google.cloud import storage

from src.adapters.gcs_workspace_adapter import GcsWorkspaceAdapter
from src.domain.workspace import WorkspaceDeleteMeta, WorkspaceWriteMeta

KEY = "u1/projects/a.md"


def _client():
    client = storage.Client(project="p", credentials=AnonymousCredentials())
    calls = []

    def api_request(**kw):
        calls.append(kw)
        if kw["method"] == "PATCH":
            return {"name": KEY, "bucket": "ws-bucket", "generation": "101", "metageneration": "2"}
        return {}

    client._connection.api_request = api_request
    return client, calls


class _Response:
    status_code = 200

    def __init__(self, body):
        self._body, self.headers = body, {}
        self.content = json.dumps(body).encode()

    def json(self):
        return self._body


class _Http:
    is_mtls = False

    def __init__(self):
        self.requests = []

    def request(self, method, url, data=None, headers=None, timeout=None, **_):
        self.requests.append((method, url, data))
        return _Response({"name": KEY, "bucket": "ws-bucket", "generation": "101", "size": "5",
                          "timeCreated": "2026-10-20T06:01:00.000Z",
                          "metadata": {"origin": "chat", "write_id": "w1"}})


async def test_the_delete_is_never_generation_addressed():
    client, calls = _client()
    adapter = GcsWorkspaceAdapter("ws-bucket", client=client, new_id=lambda: "d1")
    await adapter.delete("u1", "projects/a.md", 101, WorkspaceDeleteMeta(deleted_by="chat"))
    patch, delete = calls
    assert patch["method"] == "PATCH" and patch["query_params"]["ifGenerationMatch"] == 101
    assert patch["data"] == {"metadata": {"deleted_by": "chat", "delete_id": "d1", "deleted_fire": None,
                                           "renamed_to": None}}
    assert delete["method"] == "DELETE"
    assert delete["query_params"] == {"ifGenerationMatch": 101}  # no "generation": that would erase version 101


async def test_a_create_carries_precondition_zero_and_the_write_id():
    client, _ = _client()
    http = _Http()
    client._http_internal = http
    adapter = GcsWorkspaceAdapter("ws-bucket", client=client, new_id=lambda: "w1")
    entry = await adapter.put("u1", "projects/a.md", b"hello", WorkspaceWriteMeta(origin="chat"), 0)
    [(method, url, data)] = http.requests
    assert method == "POST" and "ifGenerationMatch=0" in url
    assert b'"metadata": {"origin": "chat", "write_id": "w1"}' in data
    assert (entry.generation, entry.write_id, entry.live) == (101, "w1", True)
