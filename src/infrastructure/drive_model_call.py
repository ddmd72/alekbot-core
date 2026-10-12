"""
What the drive indexer's two model agents share (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §24.2, §24.8): how a failed
call is sorted, and how the Firestore prompt is loaded (`build_drive_prompt`: no biography, no directives, no user
overrides; a prompt missing a section is an outage, never a fallback prompt).

Two outcomes, decided by what the failure says about the input:
- DriveModelCallFailed: this one input cannot be read now — the provider refused it (HTTP 400/413/422, a safety
  refusal, a file part the adapter could not build) or the output stayed invalid after the parse retry. A per-file
  transient for the tick; the runner turns it into an `error` outcome. Its DriveInputTooLarge case (413) first
  halves a multi-page PDF window, down to one page.
- DriveModelUnavailable (domain): the model cannot be used now for anyone — 429, 5xx, timeout, network, an open
  breaker, an account-level 4xx (key, permission, model id), a missing prompt, or anything unexpected. The tick
  defers model work for the user and keeps parsing (§24.8), so a bug never burns a file's attempts. Its
  DriveModelTimedOut case first halves a multi-page PDF window once.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ..domain.drive_extraction import DriveModelUnavailable
from ..domain.exceptions import (
    BothProvidersUnavailableError, LLMClientError, LLMError, LLMTimeoutError, TranscriptLockedError,
)
from ..ports.prompt_builder_port import PromptBuilderPort
from ..utils.logger import logger

# A client error with one of these statuses is about the request's content, not the account (None: raised before
# any HTTP exchange — a refusal or a part the adapter could not build).
_PER_INPUT_STATUSES = frozenset({None, 400, 413, 422})
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
    a single page still too large fails the file (§24.3)."""


class DriveModelTimedOut(DriveModelUnavailable):
    """The call ran past its timeout. An outage, except that the reader first halves a multi-page window once — ten
    dense pages may simply take longer than the timeout (§24.3, §24.8)."""


def classify_model_error(exc: BaseException, agent_type: str) -> Exception:
    """The exception the agent raises for `exc`: DriveModelCallFailed or DriveModelUnavailable (or their
    DriveInputTooLarge / DriveModelTimedOut cases). Logs it."""
    if isinstance(exc, (DriveModelCallFailed, DriveModelUnavailable)):
        return exc
    if isinstance(exc, LLMClientError) and exc.http_status == 413:
        logger.warning(f"[{agent_type}] provider refused the input as too large (http=413): {str(exc)[:300]}")
        return DriveInputTooLarge("refused (http=413)")
    if _timed_out(exc):
        logger.warning(f"[{agent_type}] model call timed out ({type(exc).__name__}): {str(exc)[:300]}")
        return DriveModelTimedOut(f"{type(exc).__name__}: {str(exc)[:200]}")
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
    return DriveModelUnavailable(f"{type(exc).__name__}: {str(exc)[:200]}")


@dataclass
class CallCounter:
    """LLM calls one agent method made, parse retries and correction turns included (§24.9 tick summary)."""
    calls: int = 0


def with_usage(exc: Exception, counter: CallCounter, cost: float) -> Exception:
    """`exc` (DriveModelCallFailed or DriveModelUnavailable) with the calls made and their cost from the ledger, so
    the runner counts what a failed read or batch spent."""
    setattr(exc, "model_calls", counter.calls)
    setattr(exc, "model_cost", cost)
    return exc


def _timed_out(exc: BaseException) -> bool:
    """A timeout, also when the agent's provider strategy reports it wrapped (no fallback configured: the primary's
    timeout arrives as BothProvidersUnavailableError, or TranscriptLockedError mid-transcript)."""
    if isinstance(exc, BothProvidersUnavailableError):
        exc = exc.primary_cause
    elif isinstance(exc, TranscriptLockedError):
        exc = exc.cause
    return isinstance(exc, (LLMTimeoutError, asyncio.TimeoutError))


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
