"""Fakes for the external task queue's task files (EXTERNAL_TASK_QUEUE_RFC §5.12).

- `InMemoryMediaStorage`: a `MediaStoragePort` over a dict, with the port's semantics: `store` overwrites, `fetch` of a
  missing key raises FileNotFoundError, `exists` answers what is stored now, `generate_signed_url` does not check
  existence and mints a distinct URL per call (a counter), carrying the key and the lifetime so a test can read which
  object and how long. `fail_sign` / `fail_exists` make the next calls raise as the signer or the bucket would.
- `ScriptedFileSource`: the resolver face `ExternalTaskFiles` reads (`TaskFileSource`): each reference answers its
  scripted `ResolvedFile` or raises its scripted exception; an unscripted reference is FileNotFoundError, as
  `resolve_file` answers a ref of a wrong shape. `active` scripts `active_kind_of` per file name. Every resolve is
  recorded with its action, so a test can see that nothing was read past a refusal.

The examples are the invented universe's: `Tom's plan.docx` and `birth-certificate.pdf`.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple, Union

from src.domain.document_open import ResolvedFile
from src.ports.media_storage_port import MediaStoragePort

PLAN_REF = "drive:p1:item-plan"
CERTIFICATE_REF = "email:msg-7/part-2"
PLAN = ResolvedFile(data=b"P" * 2048, name="Tom's plan.docx",
                    mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
CERTIFICATE = ResolvedFile(data=b"%PDF" + b"C" * 4092, name="birth-certificate.pdf", mime="application/pdf")


class InMemoryMediaStorage(MediaStoragePort):
    SIGNED_BASE = "https://storage.example.test/signed"

    def __init__(self) -> None:
        self.objects: Dict[str, Tuple[bytes, str]] = {}
        self.signed: List[Tuple[str, int]] = []
        self.fail_sign = False
        self.fail_exists = False

    async def store(self, data: bytes, key: str, content_type: str) -> str:
        self.objects[key] = (bytes(data), content_type)
        return key

    async def fetch(self, key: str) -> bytes:
        if key not in self.objects:
            raise FileNotFoundError(key)
        return self.objects[key][0]

    async def exists(self, key: str) -> bool:
        if self.fail_exists:
            raise ConnectionError("the bucket did not answer")
        return key in self.objects

    async def generate_signed_url(self, key: str, ttl_seconds: int) -> str:
        if self.fail_sign:
            raise RuntimeError("signBlob refused")
        self.signed.append((key, ttl_seconds))
        return f"{self.SIGNED_BASE}/{key}?ttl={ttl_seconds}&n={len(self.signed)}"


Answer = Union[ResolvedFile, BaseException]


class ScriptedFileSource:
    def __init__(self, answers: Optional[Dict[str, Answer]] = None, active: Optional[Dict[str, str]] = None) -> None:
        self.answers: Dict[str, Answer] = dict(answers or {PLAN_REF: PLAN, CERTIFICATE_REF: CERTIFICATE})
        self.active: Dict[str, str] = dict(active or {})
        self.resolved: List[Tuple[str, str, str]] = []
        self.checked: List[str] = []

    async def resolve_file(self, ref: str, user_id: str, *, action: str) -> ResolvedFile:
        self.resolved.append((ref, user_id, action))
        answer = self.answers.get(ref)
        if answer is None:
            raise FileNotFoundError(ref)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    async def active_kind_of(self, resolved: ResolvedFile, *, deadline_at: float) -> Optional[str]:
        self.checked.append(resolved.name)
        return self.active.get(resolved.name)
