"""
CapturingStub fixtures for adapter integration tests.

Each stub replaces the SDK client method on a real adapter instance,
captures what kwargs the adapter sends, and returns a valid domain
LLMResponse so generate_content() can complete without errors.

The captured data is then validated against ContractRule objects from
tests/contracts/adapter_contracts.py — the "rule repository".

Usage:
    adapter = ClaudeAdapter(api_key="test-key")
    stub = ClaudeCapturingStub().install(adapter)
    await adapter.generate_content(request=...)
    SOME_CONTRACT.validate("claude", stub.captured_kwargs)
"""
import json
from unittest.mock import MagicMock, AsyncMock


# ============================================================================
# Shared mock response builders
# ============================================================================

def _claude_text_response(text="OK"):
    block = MagicMock()
    block.type = "text"
    block.text = text

    usage = MagicMock()
    usage.input_tokens = 10
    usage.output_tokens = 5
    usage.cache_creation_input_tokens = 0
    usage.cache_read_input_tokens = 0

    response = MagicMock()
    response.content = [block]
    response.usage = usage
    return response


def _claude_tool_response(name, args, tc_id="call_1"):
    block = MagicMock()
    block.type = "tool_use"
    block.name = name
    block.input = args
    block.id = tc_id

    usage = MagicMock()
    usage.input_tokens = 10
    usage.output_tokens = 5
    usage.cache_creation_input_tokens = 0
    usage.cache_read_input_tokens = 0

    response = MagicMock()
    response.content = [block]
    response.usage = usage
    return response


def _gemini_text_response(text="OK"):
    part = MagicMock()
    part.text = text
    part.function_call = None

    content = MagicMock()
    content.parts = [part]

    candidate = MagicMock()
    candidate.content = content
    candidate.grounding_metadata = None

    usage = MagicMock()
    usage.prompt_token_count = 10
    usage.candidates_token_count = 5
    usage.total_token_count = 15

    response = MagicMock()
    response.candidates = [candidate]
    response.usage_metadata = usage
    return response


def _openai_text_response(text="OK"):
    message = MagicMock()
    message.content = text
    message.tool_calls = None

    choice = MagicMock()
    choice.message = message

    usage = MagicMock()
    usage.prompt_tokens = 10
    usage.completion_tokens = 5
    usage.total_tokens = 15

    completion = MagicMock()
    completion.choices = [choice]
    completion.usage = usage
    return completion


def _openai_tool_response(name, args, tc_id="call_1"):
    tc = MagicMock()
    tc.id = tc_id
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = json.dumps(args)

    message = MagicMock()
    message.content = None
    message.tool_calls = [tc]

    choice = MagicMock()
    choice.message = message

    usage = MagicMock()
    usage.prompt_tokens = 10
    usage.completion_tokens = 5
    usage.total_tokens = 15

    completion = MagicMock()
    completion.choices = [choice]
    completion.usage = usage
    return completion


# ============================================================================
# CapturingStub implementations — one per adapter SDK boundary
# ============================================================================

class ClaudeCapturingStub:
    """
    Captures kwargs sent to Claude's client.messages.stream().
    Install on a real ClaudeAdapter instance before calling generate_content().
    """

    def __init__(self, sdk_response=None):
        self.captured_kwargs: dict = {}
        self._sdk_response = sdk_response or _claude_text_response()

    def install(self, adapter) -> "ClaudeCapturingStub":
        stub = self
        stream = AsyncMock()
        stream.get_final_message = AsyncMock(return_value=stub._sdk_response)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=stream)
        cm.__aexit__ = AsyncMock(return_value=None)

        def capturing_stream(**kwargs):
            stub.captured_kwargs.update(kwargs)
            return cm

        adapter.client.messages.stream = capturing_stream
        return self

    @classmethod
    def with_tool_response(cls, name, args, tc_id="call_1") -> "ClaudeCapturingStub":
        return cls(sdk_response=_claude_tool_response(name, args, tc_id))


class GeminiCapturingStub:
    """
    Captures kwargs sent to Gemini's client.aio.models.generate_content().
    The captured dict contains: {"model": ..., "contents": ..., "config": ...}.
    """

    def __init__(self, sdk_response=None):
        self.captured_kwargs: dict = {}
        self._sdk_response = sdk_response or _gemini_text_response()

    def install(self, adapter) -> "GeminiCapturingStub":
        stub = self

        async def mock_generate(model=None, contents=None, config=None):
            stub.captured_kwargs["model"] = model
            stub.captured_kwargs["contents"] = contents
            stub.captured_kwargs["config"] = config
            return stub._sdk_response

        adapter.client = MagicMock()
        adapter.client.aio.models.generate_content = mock_generate
        return self


class OpenAILikeCapturingStub:
    """
    Captures kwargs sent to client.chat.completions.create().
    Works for GrokAdapter (Chat Completions API).
    """

    def __init__(self, sdk_response=None):
        self.captured_kwargs: dict = {}
        self._sdk_response = sdk_response or _openai_text_response()

    def install(self, adapter) -> "OpenAILikeCapturingStub":
        stub = self

        async def mock_create(**kwargs):
            stub.captured_kwargs.update(kwargs)
            return stub._sdk_response

        adapter.client = MagicMock()
        adapter.client.chat.completions.create = mock_create
        return self

    @classmethod
    def with_tool_response(cls, name, args, tc_id="call_1") -> "OpenAILikeCapturingStub":
        return cls(sdk_response=_openai_tool_response(name, args, tc_id))


# ---- OpenAI Responses API mock responses ----

def _openai_responses_text_response(text="OK"):
    """Build a mock Responses API response with a text output item."""
    output_text = MagicMock()
    output_text.type = "output_text"
    output_text.text = text
    output_text.annotations = []

    message = MagicMock()
    message.type = "message"
    message.content = [output_text]

    usage = MagicMock()
    usage.input_tokens = 10
    usage.output_tokens = 5
    usage.total_tokens = 15
    usage.input_tokens_details = None

    response = MagicMock()
    response.output = [message]
    response.output_text = text
    response.usage = usage
    return response


def _openai_responses_tool_response(name, args, call_id="call_1"):
    """Build a mock Responses API response with a function_call output item."""
    fc = MagicMock()
    fc.type = "function_call"
    fc.name = name
    fc.arguments = json.dumps(args)
    fc.call_id = call_id

    usage = MagicMock()
    usage.input_tokens = 10
    usage.output_tokens = 5
    usage.total_tokens = 15
    usage.input_tokens_details = None

    response = MagicMock()
    response.output = [fc]
    response.output_text = ""
    response.usage = usage
    return response


class OpenAIResponsesCapturingStub:
    """
    Captures kwargs sent to client.responses.create() (Responses API).
    For OpenAIAdapter which uses the Responses API, not Chat Completions.
    """

    def __init__(self, sdk_response=None):
        self.captured_kwargs: dict = {}
        self._sdk_response = sdk_response or _openai_responses_text_response()

    def install(self, adapter) -> "OpenAIResponsesCapturingStub":
        stub = self

        async def mock_create(**kwargs):
            stub.captured_kwargs.update(kwargs)
            return stub._sdk_response

        adapter.client = MagicMock()
        adapter.client.responses.create = mock_create
        return self

    @classmethod
    def with_tool_response(cls, name, args, call_id="call_1") -> "OpenAIResponsesCapturingStub":
        return cls(sdk_response=_openai_responses_tool_response(name, args, call_id))


# ============================================================================
# Non-LLM adapter stubs
#
# These stubs capture at the same level as the LLM ones — the outermost SDK
# call inside the adapter — but the underlying transport varies. For HTTP
# adapters (Gmail), we patch aiohttp.ClientSession; the captured "kwargs"
# becomes a list of request records {method, url, headers, params, data}.
# ContractRule validators for these adapters receive one such record.
# ============================================================================


class _FakeAiohttpResponse:
    """Mock aiohttp response. Async context manager + json/raise_for_status."""

    def __init__(self, data, status=200):
        self._data = data
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def json(self):
        return self._data

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"HTTP {self.status}")


class _FakeAiohttpSession:
    """Mock aiohttp ClientSession. Records GET/POST and returns prepared responses."""

    def __init__(self, stub: "GmailCapturingStub"):
        self._stub = stub

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def get(self, url, headers=None, params=None):
        self._stub._record("GET", url, headers, params, None)
        return _FakeAiohttpResponse(self._stub._response_for(url))

    def post(self, url, headers=None, data=None, params=None):
        self._stub._record("POST", url, headers, params, data)
        return _FakeAiohttpResponse(self._stub._response_for(url))


class GmailCapturingStub:
    """
    Captures HTTP requests sent by GmailProviderAdapter via aiohttp.ClientSession.

    Usage:
        stub = GmailCapturingStub().set_response_for("/messages", {...})
        stub.install(monkeypatch)
        await adapter.list_emails(credentials=..., page_token="abc")
        # stub.captured_requests is a list of request records.

    Each request record has shape: {method, url, headers, params, data}.
    The first matching url substring decides the response payload (default
    empty messages list). ContractRule validators receive ONE record.
    """

    def __init__(self):
        self.captured_requests: list[dict] = []
        self._responses_by_url_substring: dict[str, dict] = {}
        self._default_response: dict = {"messages": [], "nextPageToken": None}

    def set_response_for(self, url_substring: str, data: dict) -> "GmailCapturingStub":
        self._responses_by_url_substring[url_substring] = data
        return self

    def install(self, monkeypatch) -> "GmailCapturingStub":
        stub = self
        monkeypatch.setattr(
            "src.adapters.gmail_provider_adapter.aiohttp.ClientSession",
            lambda *args, **kwargs: _FakeAiohttpSession(stub),
        )
        return self

    def _record(self, method, url, headers, params, data):
        self.captured_requests.append({
            "method": method,
            "url": url,
            "headers": dict(headers or {}),
            "params": dict(params or {}),
            "data": data,
        })

    def _response_for(self, url):
        for substring, data in self._responses_by_url_substring.items():
            if substring in url:
                return data
        return self._default_response


# ============================================================================
# Firestore boundary — captures chained query API + batch operations
# ============================================================================


class _FsRecordingQuery:
    """Records where-filters chained on a Firestore collection or query.

    Each .where() returns a NEW _FsRecordingQuery with the appended filter
    (matching real Firestore Query immutability). .find_nearest() records the
    call (including accumulated filters) and returns an awaitable .get().
    """

    def __init__(self, stub: "FirestoreCapturingStub", where_filters=None):
        self._stub = stub
        self.where_filters = list(where_filters or [])

    def where(self, filter=None, **kwargs):
        # FieldFilter is a real google.cloud.firestore object — keep as-is so
        # validators can inspect .field_path / .op_string / .value.
        return _FsRecordingQuery(self._stub, self.where_filters + [filter])

    def find_nearest(self, **kwargs):
        self._stub.find_nearest_calls.append({
            "where_filters": list(self.where_filters),
            "kwargs": kwargs,
        })
        return _FsAsyncGettable(self._stub, [])

    def limit(self, n):
        return self

    def order_by(self, *args, **kwargs):
        return self

    async def get(self):
        return []

    async def stream(self):
        for _ in []:  # async generator with zero yields
            yield  # pragma: no cover


class _FsAsyncGettable:
    """Result of find_nearest()/query.get() — async-gets docs."""

    def __init__(self, stub, docs):
        self._stub = stub
        self._docs = docs

    async def get(self):
        return list(self._docs)


class _FsRecordingDocRef:
    """Captures set/update/delete/get on a single document."""

    def __init__(self, stub, doc_id):
        self._stub = stub
        self.id = doc_id

    async def get(self):
        doc = MagicMock()
        doc.exists = False
        doc.to_dict = MagicMock(return_value={})
        doc.id = self.id
        return doc

    async def set(self, data):
        self._stub.doc_set_calls.append({"doc_id": self.id, "data": data})

    async def update(self, data):
        self._stub.doc_update_calls.append({"doc_id": self.id, "data": data})

    async def delete(self):
        self._stub.doc_delete_calls.append({"doc_id": self.id})


class _FsRecordingCollection(_FsRecordingQuery):
    """A collection acts as a query root + .document() factory."""

    def __init__(self, stub, name):
        super().__init__(stub)
        self.name = name

    def document(self, doc_id):
        ref = _FsRecordingDocRef(self._stub, doc_id)
        self._stub.document_refs.append(ref)
        return ref


class _FsRecordingBatch:
    """Captures batch.set() calls and the final .commit()."""

    def __init__(self, stub):
        self._stub = stub

    def set(self, doc_ref, data):
        self._stub.batch_set_calls.append({
            "doc_id": doc_ref.id,
            "data": data,
        })

    async def commit(self):
        self._stub.batch_commits += 1


class FirestoreCapturingStub:
    """
    Captures Firestore SDK operations on a mock client. Hands out the same
    `_FsRecordingCollection` per name so chained query state and doc-id history
    are preserved across calls within one test.

    Usage:
        stub = FirestoreCapturingStub()
        repo = FirestoreIndexedEmailRepository(stub.build_db(), env_config)
        await repo.save_batch([email])
        assert stub.batch_set_calls[0]["doc_id"] == "user1_em1"

    Captured surfaces:
        - find_nearest_calls: list[{where_filters, kwargs}]
        - batch_set_calls:    list[{doc_id, data}]
        - doc_set_calls / doc_update_calls / doc_delete_calls
        - document_refs:      every collection.document(id) call
    """

    def __init__(self):
        self._collections: dict = {}
        self.find_nearest_calls: list = []
        self.batch_set_calls: list = []
        self.batch_commits: int = 0
        self.doc_set_calls: list = []
        self.doc_update_calls: list = []
        self.doc_delete_calls: list = []
        self.document_refs: list = []

    def build_db(self):
        db = MagicMock()
        db.collection = self._collection
        db.batch = lambda: _FsRecordingBatch(self)
        return db

    def _collection(self, name):
        if name not in self._collections:
            self._collections[name] = _FsRecordingCollection(self, name)
        return self._collections[name]


def field_filter_matches(filter_obj, field_path: str, op_string: str) -> bool:
    """Inspect a google.cloud.firestore FieldFilter for a (field, op) match.

    FieldFilter exposes field_path / op_string / value as attributes; this
    helper centralizes the introspection so contract validators stay short.
    """
    return (
        getattr(filter_obj, "field_path", None) == field_path
        and getattr(filter_obj, "op_string", None) == op_string
    )


# ============================================================================
# Subprocess boundary — captures asyncio.create_subprocess_exec + communicate
# ============================================================================


class _FakeSubprocess:
    """Mimics asyncio subprocess Process. Records communicate() input + exit code."""

    def __init__(self, stub: "NodeSubprocessCapturingStub", returncode: int = 0,
                 stdout: bytes = b"FAKE_DOCX_BYTES", stderr: bytes = b""):
        self._stub = stub
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self, input: bytes = b""):
        self._stub.communicate_inputs.append(input)
        return self._stdout, self._stderr

    def kill(self):
        self._stub.killed = True

    async def wait(self):
        return self.returncode


class NodeSubprocessCapturingStub:
    """
    Captures asyncio.create_subprocess_exec calls + the stdin payload passed
    via communicate(). Designed for subprocess-boundary adapters like
    NodeDocxRunner.

    Captured surfaces:
        - exec_calls:         list[{args: tuple, kwargs: dict}]
        - communicate_inputs: list[bytes] — what was sent to stdin per call

    Usage:
        stub = NodeSubprocessCapturingStub().install(monkeypatch)
        result = await runner.run(js_code="...", spec_json='{}', timeout=10)
        assert stub.exec_calls[0]["args"][0] == "node"
    """

    def __init__(self, returncode: int = 0, stdout: bytes = b"FAKE_DOCX_BYTES",
                 stderr: bytes = b""):
        self.exec_calls: list = []
        self.communicate_inputs: list = []
        self.killed: bool = False
        self._returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    def install(self, monkeypatch, target_module: str) -> "NodeSubprocessCapturingStub":
        stub = self

        async def fake_create_subprocess_exec(*args, **kwargs):
            stub.exec_calls.append({"args": args, "kwargs": kwargs})
            return _FakeSubprocess(
                stub,
                returncode=stub._returncode,
                stdout=stub._stdout,
                stderr=stub._stderr,
            )

        monkeypatch.setattr(
            f"{target_module}.asyncio.create_subprocess_exec",
            fake_create_subprocess_exec,
        )
        return self
