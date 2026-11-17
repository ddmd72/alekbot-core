"""FileConversionService's open path, frozen in U3's core (EXTERNAL_FILES_RFC §4.2, P18): `open` / `prefetch` have
their signatures (their behaviour: test_file_conversion_open.py and test_file_conversion_prefetch.py)."""
import inspect

from src.domain.document_open import OpenedFile, Prefetched
from src.domain.document_read import CALLER_OPEN
from src.services.file_conversion_service import FileConversionService


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

