"""ConnectorTextService: the drive connector's texts, loaded from two Firestore prompt tokens, validated, cached.

Public interface:
    ConnectorTextService(token_repo, *, tool_token_id, result_token_id, required_tool_keys, required_result_keys,
                         ttl_s=60.0, clock=time.monotonic, alert_sink=None)
    await texts() -> ConnectorTexts

The first load failing raises `ConnectorTextsUnavailable` (and posts one ops alert when a sink is given). A later
refresh failing logs an error and keeps serving the last good value. There is never an inline default text.
"""
from __future__ import annotations

import asyncio
import time
from typing import AbstractSet, Callable, Optional

from src.domain.connector_texts import ConnectorTexts
from src.domain.exceptions import ConnectorTextsUnavailable
from src.domain.prompt_v3.token import Token, TokenId
from src.ports.alert_sink import AlertSinkPort
from src.ports.prompt_v3.token_repository import TokenRepository
from src.utils.logger import logger

# The token's `metadata["validation"]["risk_level"]` values we accept. LOW is accepted because no adapter produces it
# and none rewrites text at LOW (only MEDIUM is sanitized); MEDIUM is cleaned silently, so it is refused here.
_ACCEPTED_RISK_LEVELS = frozenset({"safe", "low"})


class ConnectorTextService:
    """Loads `ConnectorTexts` from two prompt tokens and caches the result for `ttl_s` seconds."""

    def __init__(self, token_repo: TokenRepository, *, tool_token_id: str, result_token_id: str,
                 required_tool_keys: AbstractSet[str], required_result_keys: AbstractSet[str],
                 ttl_s: float = 60.0, clock: Callable[[], float] = time.monotonic,
                 alert_sink: Optional[AlertSinkPort] = None) -> None:
        self._repo = token_repo
        self._tool_token_id = tool_token_id
        self._result_token_id = result_token_id
        self._required_tool_keys = frozenset(required_tool_keys)
        self._required_result_keys = frozenset(required_result_keys)
        self._ttl_s = ttl_s
        self._clock = clock
        self._alert_sink = alert_sink
        self._lock = asyncio.Lock()
        self._value: Optional[ConnectorTexts] = None
        self._loaded_at = 0.0

    async def texts(self) -> ConnectorTexts:
        """The current texts. Raises ConnectorTextsUnavailable only while no load has ever succeeded."""
        if self._is_fresh():
            assert self._value is not None
            return self._value
        async with self._lock:
            if self._is_fresh():
                assert self._value is not None
                return self._value
            try:
                self._value = await self._load()
                self._loaded_at = self._clock()
            except Exception as exc:
                reason = str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)
                if self._value is None:
                    message = f"connector texts could not be loaded: {reason}"
                    logger.error(f"[ConnectorTexts] {message}")
                    await self._alert(message)
                    raise ConnectorTextsUnavailable(message) from exc
                logger.error(f"[ConnectorTexts] refresh failed, serving the last good texts: {reason}")
                # Retry on the next call, not after a whole TTL of failing reads per caller.
                return self._value
            return self._value

    def _is_fresh(self) -> bool:
        return self._value is not None and self._clock() - self._loaded_at < self._ttl_s

    async def _load(self) -> ConnectorTexts:
        tool_token = await self._get(self._tool_token_id)
        result_token = await self._get(self._result_token_id)
        return ConnectorTexts.from_bodies(tool_token.content, result_token.content,
                                          self._required_tool_keys, self._required_result_keys)

    async def _get(self, token_id: str) -> Token:
        try:
            token = await self._repo.get(TokenId(token_id))
        except KeyError as exc:
            raise ValueError(f"token {token_id} not found") from exc
        validation = token.metadata.get("validation")
        level = validation.get("risk_level") if isinstance(validation, dict) else None
        if level not in _ACCEPTED_RISK_LEVELS:
            raise ValueError(f"token {token_id} has risk level {level!r}, accepted: safe or low")
        return token

    async def _alert(self, message: str) -> None:
        if self._alert_sink is None:
            return
        try:
            await self._alert_sink.post(f"Drive connector texts unavailable: {message}")
        except Exception as exc:  # best-effort: the alert never replaces the real failure
            logger.warning(f"[ConnectorTexts] alert not delivered ({type(exc).__name__})")
