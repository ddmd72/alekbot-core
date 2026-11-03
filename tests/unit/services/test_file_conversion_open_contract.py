"""FileConversionService's open path, frozen in U3's core (EXTERNAL_FILES_RFC §4.2, P18): the document service is
attached, and `open` / `prefetch` have their signatures (the bodies are U3 part C)."""
import inspect
from unittest.mock import MagicMock

import pytest

from src.domain.document_open import OpenedFile, Prefetched
from src.domain.document_read import CALLER_OPEN, ReadOutcome, ReadRequest, ReadStatus
from src.ports.document_read_port import DocumentReadPort
from src.services.file_conversion_service import FileConversionService


class _Documents(DocumentReadPort):
    async def read(self, request: ReadRequest) -> ReadOutcome:
        return ReadOutcome(status=ReadStatus.ERROR)


def _service() -> FileConversionService:
    return FileConversionService(storage=MagicMock(), access=MagicMock())


def test_the_document_service_is_attached():
    service, documents = _service(), _Documents()
    assert service._documents is None
    service.attach_documents(documents)
    assert service._documents is documents


def test_open_signature():
    sig = inspect.signature(FileConversionService.open)
    assert list(sig.parameters) == ["self", "ref", "user_id", "account_id", "consumer", "deadline_at", "caller"]
    params = sig.parameters
    assert all(params[name].kind is inspect.Parameter.KEYWORD_ONLY for name in ("consumer", "deadline_at", "caller"))
    assert params["consumer"].default == () and params["caller"].default == CALLER_OPEN
    assert params["deadline_at"].default is inspect.Parameter.empty
    assert sig.return_annotation in (OpenedFile, "OpenedFile")


def test_prefetch_signature():
    sig = inspect.signature(FileConversionService.prefetch)
    assert list(sig.parameters) == ["self", "ref", "user_id", "account_id", "deadline_at"]
    assert sig.parameters["deadline_at"].kind is inspect.Parameter.KEYWORD_ONLY
    assert sig.return_annotation in (Prefetched, "Prefetched")


async def test_both_are_not_built_yet():
    service = _service()
    with pytest.raises(NotImplementedError):
        await service.open("scan_0012.pdf", "ann", "ann", deadline_at=0.0)
    with pytest.raises(NotImplementedError):
        await service.prefetch("scan_0012.pdf", "ann", "ann", deadline_at=0.0)
