"""FirestoreTokenRepository.save: the identity and content fields of the written document.

`save` calls `doc_ref.set(data)` without `await` (P10-D1, pinned in tests/unit/adapters/test_prompt_token_store_kit.py),
so the fake document reference records the dict at call time and hands back an already-completed future: the write is
seen whether or not the adapter awaits it, and the test stays valid once the defect is fixed. A document missing one of these fields cannot
be read back by `get`/`list` (`data["category"]`, `data["class"]`, `data["content"]`, `token_id`).
"""
import asyncio

import pytest

from src.adapters.prompt_v3.firestore_token_repository import FirestoreTokenRepository
from src.adapters.security.composite_adapter import CompositeAdapter
from src.adapters.security.regex_adapter import RegexSecurityAdapter
from src.domain.prompt_v3.token import Token, TokenCategory, TokenClass, TokenId

SYS, USR = "wd_tokens_system", "wd_tokens_user"


class _DocRef:
    def __init__(self, written: dict, collection: str, doc_id: str) -> None:
        self._written, self._key = written, (collection, doc_id)

    def set(self, data: dict) -> "asyncio.Future[None]":
        self._written[self._key] = data
        return _done()


def _done() -> "asyncio.Future[None]":
    future = asyncio.get_running_loop().create_future()
    future.set_result(None)
    return future


class _Collection:
    def __init__(self, written: dict, name: str) -> None:
        self._written, self._name = written, name

    def document(self, doc_id: str) -> _DocRef:
        return _DocRef(self._written, self._name, doc_id)


class _RecordingDb:
    def __init__(self) -> None:
        self.written: dict = {}

    def collection(self, name: str) -> _Collection:
        return _Collection(self.written, name)


@pytest.mark.asyncio
@pytest.mark.parametrize("collection, name", [("system", SYS), ("user", USR)])
async def test_save_writes_identity_and_content_fields(collection, name):
    db = _RecordingDb()
    security = CompositeAdapter(adapters=[RegexSecurityAdapter()], strategy="worst_case")
    repo = FirestoreTokenRepository(db=db, system_collection=SYS, user_collection=USR, security_port=security)
    token = Token(id=TokenId("tok_a"), category=TokenCategory("style"), class_=TokenClass("properties"),
                  content="Answer in two short sentences.", metadata={"version": "1.0"})

    await repo.save(token, collection=collection)

    doc = db.written[(name, "tok_a")]
    assert doc["token_id"] == "tok_a"
    assert doc["category"] == "style"
    assert doc["class"] == "properties"
    assert doc["content"] == "Answer in two short sentences."
    assert doc["metadata"] == {"version": "1.0"}
