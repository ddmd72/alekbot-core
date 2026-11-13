"""The shared google-genai 3.x compat constants: the API version every Gemini client pins, and the transport errors
caught from both HTTP libraries (3.x sends through httpx2; the SDK still accepts an httpx client)."""
import httpx
import httpx2
import pytest

from src.utils.genai_sdk_compat import GENAI_API_VERSION, NETWORK_ERRORS, TIMEOUT_ERRORS

REQUEST_URL = "https://example.invalid"


def test_the_api_version_stays_v1beta():
    assert GENAI_API_VERSION == "v1beta"


@pytest.mark.parametrize("lib", [httpx, httpx2], ids=["httpx", "httpx2"])
def test_a_timeout_of_either_library_is_a_timeout_error(lib):
    for cls in (lib.ReadTimeout, lib.ConnectTimeout, lib.PoolTimeout):
        assert isinstance(cls("timed out", request=lib.Request("POST", REQUEST_URL)), TIMEOUT_ERRORS)


@pytest.mark.parametrize("lib", [httpx, httpx2], ids=["httpx", "httpx2"])
def test_a_network_error_of_either_library_is_a_network_error(lib):
    for cls in (lib.ConnectError, lib.ReadError):
        assert isinstance(cls("broken", request=lib.Request("POST", REQUEST_URL)), NETWORK_ERRORS)


@pytest.mark.parametrize("lib", [httpx, httpx2], ids=["httpx", "httpx2"])
def test_timeouts_and_network_errors_are_disjoint(lib):
    request = lib.Request("POST", REQUEST_URL)
    assert not isinstance(lib.ReadTimeout("t", request=request), NETWORK_ERRORS)
    assert not isinstance(lib.ConnectTimeout("t", request=request), NETWORK_ERRORS)
    assert not isinstance(lib.ConnectError("c", request=request), TIMEOUT_ERRORS)
    assert not isinstance(lib.ReadError("r", request=request), TIMEOUT_ERRORS)
