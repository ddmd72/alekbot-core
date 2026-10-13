"""ClaudeAdapter error mapping at the SDK boundary: an account-level 4xx is LLMAccountError, not a plain refusal.

Anthropic sends an exhausted credit balance as HTTP 400 `invalid_request_error` ("Your credit balance is too low to
access the Anthropic API..."), the same status and type as a request it refuses for its content. Only the message
tells them apart, and only the adapter sees the provider's error body. LLMAccountError is an LLMClientError (no
failover, still alerted); the drive indexer reads it as an outage instead of a refusal of the file
(USER_DRIVE_INDEXING_RFC §24.14). The exceptions are built the way the SDK's client builds them from a response."""
import anthropic
import httpx
import pytest

from src.adapters.claude_adapter import ClaudeAdapter
from src.domain.exceptions import LLMAccountError, LLMClientError
from src.domain.llm import LLMRequest, Message, MessagePart
from tests.unit.adapters.test_claude_adapter import _make_failing_cm, _make_raising_cm

_MESSAGES = [Message(role="user", parts=[MessagePart(text="Hi")])]
_CREDIT = ("Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to upgrade or "
           "purchase credits.")


def _sdk_error(status: int, error_type: str, message: str) -> anthropic.APIStatusError:
    """What anthropic's client raises for this response (`_make_status_error_from_response`)."""
    body = {"type": "error", "error": {"type": error_type, "message": message}}
    response = httpx.Response(status, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    return anthropic.AsyncAnthropic(api_key="k")._make_status_error(
        f"Error code: {status} - {body}", body=body, response=response)


async def _raised(sdk_exc, grounded=False):
    adapter = ClaudeAdapter(api_key="test-key")
    adapter.client.messages.stream = lambda **kw: (_make_raising_cm if grounded else _make_failing_cm)(sdk_exc)
    with pytest.raises(LLMClientError) as exc_info:
        await adapter.generate_content(request=LLMRequest(model_name="claude-sonnet-4-6", messages=_MESSAGES,
                                                          use_grounding=grounded))
    return exc_info.value


def test_the_sdk_builds_a_bad_request_with_the_error_type():
    exc = _sdk_error(400, "invalid_request_error", _CREDIT)
    assert isinstance(exc, anthropic.BadRequestError) and exc.type == "invalid_request_error"


@pytest.mark.parametrize("grounded", [False, True], ids=["plain", "grounded"])
async def test_a_credit_balance_400_is_an_account_error(grounded):
    raised = await _raised(_sdk_error(400, "invalid_request_error", _CREDIT), grounded)
    assert isinstance(raised, LLMAccountError) and raised.http_status == 400


@pytest.mark.parametrize("grounded", [False, True], ids=["plain", "grounded"])
async def test_a_billing_error_is_an_account_error(grounded):
    raised = await _raised(_sdk_error(402, "billing_error", "There is an issue with your billing."), grounded)
    assert isinstance(raised, LLMAccountError) and raised.http_status == 402


@pytest.mark.parametrize("grounded", [False, True], ids=["plain", "grounded"])
async def test_an_ordinary_400_stays_a_plain_client_error(grounded):
    raised = await _raised(_sdk_error(400, "invalid_request_error", "messages.0.content.0.image: Could not process "
                                                                    "image"), grounded)
    assert type(raised) is LLMClientError and raised.http_status == 400
