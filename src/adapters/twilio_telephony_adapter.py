"""
TwilioTelephonyAdapter — TelephonyPort implementation backed by the Twilio REST API.

`twilio.rest.Client.calls.create` is synchronous (uses `requests` under the hood,
per Phase 0 spike 0.6's Charles Proxy note). It is offloaded via `asyncio.to_thread`
so a blocking HTTP call does not stall the event loop on the 1-vCPU main service
(CLAUDE.md: "All I/O — async/await. No synchronous calls to DB or LLM.").
"""

import asyncio
from typing import Callable, Optional

from twilio.rest import Client

from src.ports.telephony_port import TelephonyPort


class TwilioTelephonyAdapter(TelephonyPort):
    def __init__(self, account_sid: str, auth_token: str, client_factory: Optional[Callable] = None) -> None:
        factory = client_factory or Client
        self._client = factory(account_sid, auth_token)

    async def originate_call(self, to: str, from_: str, answer_url: str, status_callback_url: str) -> str:
        call = await asyncio.to_thread(
            self._client.calls.create,
            to=to,
            from_=from_,
            url=answer_url,
            machine_detection="DetectMessageEnd",
            # Async: the call connects at once and the verdict arrives on the status callback.
            # Sync AMD held every pickup for 4-5 s and hung up on an owner who opened with a
            # request longer than Twilio's 2.4 s "human greeting" threshold (live, 2026-09-24).
            async_amd=True,
            async_amd_status_callback=status_callback_url,
            async_amd_status_callback_method="POST",
            status_callback=status_callback_url,
            status_callback_event=["initiated", "ringing", "answered", "completed"],
        )
        return call.sid
