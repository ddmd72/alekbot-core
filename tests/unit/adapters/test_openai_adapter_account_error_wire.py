"""OpenAIAdapter error mapping at the SDK boundary for account-level failures (USER_DRIVE_INDEXING_RFC §24.14).

OpenAI names the account condition in the error body's `code`, which the SDK lifts onto the exception (`.code`,
`.type`, from the inner `error` object). An exhausted quota arrives as HTTP 429 `insufficient_quota`: the SDK raises
RateLimitError and the adapter maps it to LLMRateLimitError (a failover trigger, an outage for the drive indexer) —
pinned here. A hard billing limit arrives as a 4xx other than 429 (`billing_hard_limit_reached`): that is
LLMAccountError, not a refusal of the request. The exceptions are built the way the SDK's client builds them."""
from unittest.mock import AsyncMock

import httpx
import openai
import pytest

from src.adapters.openai_adapter import OpenAIAdapter
from src.domain.exceptions import LLMAccountError, LLMClientError, LLMRateLimitError
from src.domain.llm import LLMRequest, Message, MessagePart

_REQUEST = LLMRequest(model_name="gpt-5-mini", system_instruction="test",
                      messages=[Message(role="user", parts=[MessagePart(text="hi")])])


def _sdk_error(status: int, code: str, error_type: str, message: str) -> openai.APIStatusError:
    """What openai's client raises for this response (`_make_status_error`: the inner `error` object is the body)."""
    body = {"error": {"message": message, "type": error_type, "param": None, "code": code}}
    response = httpx.Response(status, request=httpx.Request("POST", "https://api.openai.com/v1/responses"))
    return openai.AsyncOpenAI(api_key="k")._make_status_error(f"Error code: {status} - {body}", body=body,
                                                              response=response)


async def _raised(sdk_exc, expected=LLMClientError):
    adapter = OpenAIAdapter(api_key="test-key")
    adapter.client.responses.create = AsyncMock(side_effect=sdk_exc)
    with pytest.raises(expected) as exc_info:
        await adapter.generate_content(request=_REQUEST)
    return exc_info.value


def test_the_sdk_lifts_the_code_onto_the_exception():
    exc = _sdk_error(400, "billing_hard_limit_reached", "invalid_request_error", "Billing hard limit has been reached")
    assert isinstance(exc, openai.BadRequestError) and exc.code == "billing_hard_limit_reached"


async def test_an_exhausted_quota_429_stays_a_rate_limit():
    exc = _sdk_error(429, "insufficient_quota", "insufficient_quota", "You exceeded your current quota.")
    assert isinstance(exc, openai.RateLimitError)
    raised = await _raised(exc, LLMRateLimitError)
    assert raised.http_status == 429


@pytest.mark.parametrize("code", ["billing_hard_limit_reached", "insufficient_quota", "billing_not_active"])
async def test_an_account_code_on_another_4xx_is_an_account_error(code):
    raised = await _raised(_sdk_error(400, code, "invalid_request_error", "account"))
    assert isinstance(raised, LLMAccountError) and raised.http_status == 400


async def test_an_ordinary_400_stays_a_plain_client_error():
    raised = await _raised(_sdk_error(400, "invalid_value", "invalid_request_error", "Invalid image."))
    assert type(raised) is LLMClientError and raised.http_status == 400
