"""
TelephonyPort — abstract interface for outbound call origination.

Port justification: Twilio today; a future provider swap (e.g. another SIP
trunk / telephony vendor) is a real substitution need per RFC §4.13.
"""

from abc import ABC, abstractmethod


class TelephonyPort(ABC):
    """Outbound call origination. Twilio connecting inbound is a handler;
    us telling Twilio to place a call is an outbound adapter call (RFC §4.13)."""

    @abstractmethod
    async def originate_call(self, to: str, from_: str, answer_url: str, status_callback_url: str) -> str:
        """Place an outbound call with answering-machine detection mandatory
        (RFC §4.6). Returns the provider call SID. answer_url must be a
        distinct route from the auth webhook - pointing it back at auth is
        a dial loop (RFC §4.6)."""
