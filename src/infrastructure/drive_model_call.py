"""
What the drive indexer's two model agents share (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §24.2, §24.8): how a failed
call is sorted, and how the Firestore prompt is loaded (`build_drive_prompt`: no biography, no directives, no user
overrides; a prompt missing a section is an outage, never a fallback prompt).

Two outcomes, decided by what the failure says about the input:
- DriveModelCallFailed: this one input cannot be read now — the provider refused it (HTTP 400/413/422, a safety
  refusal, a file part the adapter could not build) or the output stayed invalid after the parse retry. The runner
  turns it into an `error` outcome (a per-file transient), except its two refusal cases, DriveInputTooLarge (413)
  and DriveInputRefused (400/422), where the same bytes get the same answer (R2 P-m5, R3-P1):
  - a PDF window is halved, down to one page; a single page still refused is skipped and the rest of the file is
    read; a file whose every page was refused is `too_large` (a 413 among them) or `unsupported`;
  - an image still refused is terminal: `too_large` (413) or `unsupported` (400/422);
  - triage halves a refused batch down to the refused image, a photograph without a caption; any other failed
    triage call is not halved (R3 F1-6).
- DriveModelUnavailable (domain): the model cannot be used now for anyone — 429, 5xx, timeout, network, an open
  breaker, an account-level 4xx (key, permission, model id; and LLMAccountError — credit balance, billing — which
  the adapter recognises by the provider's error body, since Anthropic sends it as a 400, §24.14), a missing
  prompt, or anything unexpected. The tick
  defers model work for the user and keeps parsing (§24.8), and never counts it in a file's `attempts`. Its
  DriveModelTimedOut case first halves a multi-page PDF window once; a half that times out again is a per-file
  failure (`error`), not an outage (T-M2b). `charges_file` (F1-1) marks the outages that may still be the file's
  own — a 5xx, a timeout, an unexpected error: a reader outage that called the model for a file is charged to that
  file's `model_outages`, which fails it at MAX_ATTEMPTS. A 429, an account-level 4xx, a network failure or an open
  breaker never charges a file.

Search v2 (§25.4.4, §25.4.6): the profiler, passages and triage v2 runners report every outcome as a
`ModelReplyStatus` (`reply_status`), never as an exception. A refusal raised by the FALLBACK
(`LLMFallbackClientError`) is a charged outage, retried on the primary after the backoff, never a refusal of the input;
output still invalid after the parse retry is `DriveOutputInvalid` (charged, no backoff). `ledger_spend` splits an
execution's cost into the model that served, the whole cost and the fallback's share in dollars.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from ..domain.billing import TokenLedger
from ..domain.drive_enrichment_calls import ModelReplyStatus
from ..domain.drive_extraction import DriveModelUnavailable
from ..domain.exceptions import (
    BothProvidersUnavailableError, LLMAccountError, LLMClientError, LLMError, LLMFallbackClientError, LLMServerError,
    LLMTimeoutError, LLMUnavailableError, TranscriptLockedError,
)
from ..ports.prompt_builder_port import PromptBuilderPort
from ..utils.logger import logger

# A client error with one of these statuses is about the request's content, not the account (None: raised before
# any HTTP exchange — a refusal or a part the adapter could not build).
_PER_INPUT_STATUSES = frozenset({None, 400, 413, 422})
_REFUSED_STATUSES = frozenset({400, 422})    # the provider read the request and refused this input: terminal
# The assembled prompt must carry both sections of the blueprint; a profile or token missing from Firestore
# assembles silently without them (§24.2: a missing prompt fails the call, no fallback prompt).
_REQUIRED_SECTIONS = ("cognitive_process {", "output_format {")


class DriveModelCallFailed(Exception):
    """The provider will not read this input, or its answer stayed unusable: a per-file failure (§24.8). The agent
    sets the calls it made and their cost (its ledger) before raising it."""
    model_calls: int = 0
    model_cost: float = 0.0


class DriveInputTooLarge(DriveModelCallFailed):
    """HTTP 413: the request was too large for the provider. The reader halves a multi-page window, down to one page;
    a single page still too large is skipped and the rest of the file is read (R3-P1); an image still too large is
    `too_large`; triage halves the batch down to the image (§24.3, §24.5)."""


class DriveInputRefused(DriveModelCallFailed):
    """HTTP 400/422: the provider refused this input outright; the same bytes get the same answer (R2 P-m5). A PDF
    window is halved and a single page still refused is skipped (R3-P1); an image is terminal (`unsupported`);
    triage halves the batch down to the image. A refusal raised before any HTTP exchange stays a plain
    DriveModelCallFailed (an adapter bug is not the file's)."""


class DriveOutputInvalid(DriveModelCallFailed):
    """The answer stayed unusable after the parse retry (and any correction turn): charged, no backoff (§25.4.6,
    `invalid`). The v1 reader and triage raise the plain DriveModelCallFailed for the same case."""


class DriveModelTimedOut(DriveModelUnavailable):
    """The call ran past its timeout. An outage, except that the reader first halves a multi-page window once — ten
    dense pages may simply take longer than the timeout — and a half that times out again fails the file (§24.3,
    §24.8)."""


def classify_model_error(exc: BaseException, agent_type: str) -> Exception:
    """The exception the agent raises for `exc`: DriveModelCallFailed or DriveModelUnavailable (or their
    DriveInputTooLarge / DriveModelTimedOut cases). Logs it."""
    if isinstance(exc, (DriveModelCallFailed, DriveModelUnavailable)):
        return exc
    if isinstance(exc, LLMAccountError):
        # Credit balance or billing: its 400 is not a refusal of this input (§24.14) — an outage for everyone.
        logger.error(f"[{agent_type}] provider rejected the account (http={exc.http_status}): {str(exc)[:300]}")
        return DriveModelUnavailable(f"{type(exc).__name__}: {str(exc)[:200]}", charges_file=False)
    if isinstance(exc, LLMFallbackClientError):
        # The fallback refused what the primary could not serve: not the primary's verdict on this input — a charged
        # attempt, retried on the primary after the backoff (§25.4.4 N7, §25.4.6).
        logger.warning(f"[{agent_type}] fallback {exc.provider_name or '?'} refused the input "
                       f"(http={exc.http_status}): {str(exc)[:300]}")
        return DriveModelUnavailable(f"fallback refused (http={exc.http_status})", charges_file=True)
    if isinstance(exc, LLMClientError) and exc.http_status == 413:
        logger.warning(f"[{agent_type}] provider refused the input as too large (http=413): {str(exc)[:300]}")
        return DriveInputTooLarge("refused (http=413)")
    if _timed_out(exc):
        logger.warning(f"[{agent_type}] model call timed out ({type(exc).__name__}): {str(exc)[:300]}")
        return DriveModelTimedOut(f"{type(exc).__name__}: {str(exc)[:200]}", charges_file=True)
    if isinstance(exc, LLMClientError) and exc.http_status in _REFUSED_STATUSES:
        logger.warning(f"[{agent_type}] provider refused the input (http={exc.http_status}): {str(exc)[:300]}")
        return DriveInputRefused(f"refused (http={exc.http_status})")
    if isinstance(exc, LLMClientError) and exc.http_status in _PER_INPUT_STATUSES:
        logger.warning(f"[{agent_type}] provider refused the input (http={exc.http_status}): {str(exc)[:300]}")
        return DriveModelCallFailed(f"refused (http={exc.http_status})")
    if isinstance(exc, LLMClientError):
        logger.error(f"[{agent_type}] provider rejected the account or model (http={exc.http_status}): "
                     f"{str(exc)[:300]}")
    elif isinstance(exc, (LLMError, asyncio.TimeoutError)):
        logger.warning(f"[{agent_type}] model unavailable ({type(exc).__name__}): {str(exc)[:300]}")
    else:
        logger.error(f"[{agent_type}] unexpected failure ({type(exc).__name__}): {exc}", exc_info=exc)
    return DriveModelUnavailable(f"{type(exc).__name__}: {str(exc)[:200]}", charges_file=_charges_file(exc))


def _charges_file(exc: BaseException) -> bool:
    """Whether an outage may be the file's (final review F1-1, arbiter R3-x2-a): a provider 5xx, a timeout or an
    unexpected (non-LLM) error. A rate limit, an account-level 4xx, a network failure or an open breaker is the
    model's for everyone and never charges a file."""
    cause = _cause(exc)
    if isinstance(cause, (LLMServerError, LLMUnavailableError, LLMTimeoutError, asyncio.TimeoutError)):
        return True
    return not isinstance(cause, LLMError)


@dataclass
class CallCounter:
    """LLM calls one agent method made, parse retries and correction turns included (§24.9 tick summary)."""
    calls: int = 0


def with_usage(exc: Exception, counter: CallCounter, cost: float, *, fallback_cost: float = 0.0,
               model: str = "") -> Exception:
    """`exc` (DriveModelCallFailed or DriveModelUnavailable) with the calls made and their cost from the ledger, so
    the runner counts what a failed read or batch spent; Search v2 agents add the fallback's share and the model that
    served (`model_fallback_cost`, `model_name`)."""
    setattr(exc, "model_calls", counter.calls)
    setattr(exc, "model_cost", cost)
    setattr(exc, "model_fallback_cost", fallback_cost)
    setattr(exc, "model_name", model)
    return exc


def reply_status(exc: BaseException) -> ModelReplyStatus:
    """The §25.4.6 outcome of a Search v2 agent failure (after `classify_model_error` and `with_usage`):
    - a refusal of the input by the primary (HTTP 400/413/422, a safety stop): `refused`;
    - output still invalid after the parse retry, or another per-input failure (a refusal raised before any HTTP
      exchange): `invalid` — charged, no backoff;
    - an outage that may be this input's (a 5xx, a timeout, an unexpected error, the fallback's refusal) after a call
      was made: `failed` — charged, and a backoff for the agent type;
    - every other outage (429, network, breaker, account 4xx, missing key or prompt, nothing sent): `unavailable`."""
    if isinstance(exc, (DriveInputRefused, DriveInputTooLarge)):
        return ModelReplyStatus.REFUSED
    if isinstance(exc, DriveModelCallFailed):
        return ModelReplyStatus.INVALID
    if isinstance(exc, DriveModelUnavailable):
        return (ModelReplyStatus.FAILED if exc.charges_file and getattr(exc, "model_calls", 0) > 0
                else ModelReplyStatus.UNAVAILABLE)
    return ModelReplyStatus.FAILED if getattr(exc, "model_calls", 0) > 0 else ModelReplyStatus.UNAVAILABLE


@dataclass(frozen=True)
class ModelSpend:
    """One execution's cost: the model that served (the primary when it answered any call, else the fallback), the
    whole cost and the fallback's share, in USD as the ledger prices each model (§25.4.4, §25.4.5)."""
    model: str
    usd: float
    fallback_usd: float


def ledger_spend(ledger: TokenLedger, *, primary_model: str, fallback_model: Optional[str]) -> ModelSpend:
    fallback_usd = 0.0
    primary_served = False
    for model, leg in ledger.by_model.items():
        if fallback_model and model == fallback_model:
            fallback_usd += leg.cost(model)
        elif leg.total_tokens:
            primary_served = True
    served = primary_model if primary_served or not fallback_usd else (fallback_model or primary_model)
    return ModelSpend(model=served, usd=ledger.cost(), fallback_usd=round(fallback_usd, 6))


def _timed_out(exc: BaseException) -> bool:
    """A timeout, also when the agent's provider strategy reports it wrapped (no fallback configured: the primary's
    timeout arrives as BothProvidersUnavailableError, or TranscriptLockedError mid-transcript)."""
    return isinstance(_cause(exc), (LLMTimeoutError, asyncio.TimeoutError))


def _cause(exc: BaseException) -> BaseException:
    """The provider's own error under the provider strategy's wrappers (BothProvidersUnavailableError's primary cause,
    TranscriptLockedError's cause)."""
    if isinstance(exc, BothProvidersUnavailableError):
        return exc.primary_cause
    if isinstance(exc, TranscriptLockedError):
        return exc.cause
    return exc


async def build_drive_prompt(prompt_builder: PromptBuilderPort, agent_type: str) -> str:
    """The Firestore prompt, or DriveModelUnavailable (logged ERROR): §24.2 treats a missing prompt as an outage. A
    rejected assembly is evicted from the builder's cache (`invalidate_cache()`)."""
    try:
        prompt = await prompt_builder.build_for_agent(agent_type=agent_type, user_id=None, account_id=None,
                                                      include_biographical=False, include_directives=False)
    except Exception as exc:
        logger.error(f"[{agent_type}] prompt assembly failed: {type(exc).__name__}: {exc}")
        raise DriveModelUnavailable(f"prompt for {agent_type} unavailable") from exc
    missing = [s for s in _REQUIRED_SECTIONS if s not in (prompt or "")]
    if missing:
        logger.error(f"[{agent_type}] prompt is missing sections {missing}: profile, blueprint or tokens not uploaded")
        # The builder cached this assembly: drop it, so the prompt is assembled afresh once the missing part is
        # uploaded instead of failing until the cache expires.
        prompt_builder.invalidate_cache()
        raise DriveModelUnavailable(f"prompt for {agent_type} incomplete")
    return prompt
