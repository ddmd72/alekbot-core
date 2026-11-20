"""
Callback reference — what rides in a Twilio callback URL instead of the call ticket.

Cloud Run request logs record each request's full URL, query string included, and anyone who can read the project's
logs can read it. The ticket is the credential that opens a voice session at the relay, so it never rides in a URL
Twilio is asked to call (`/voice/answer`, `/voice/status`). The URL carries the KMS ciphertext of the ticket
(`CALLBACK_REF_AAD` binds it to this use); only a request with a valid Twilio signature reaches the code that
decrypts it, and the relay never accepts it.

The reference sits in a Twilio-signed URL, so it uses the URL-safe base64 alphabet without padding: it survives with no
percent-encoding and the signed URL stays byte-identical on both ends.
"""
import base64
import re

CALLBACK_REF_AAD = "voice/twilio-callback-ref"

_REF_PATTERN = re.compile(r"[A-Za-z0-9_-]+")


def to_callback_ref(ciphertext: str) -> str:
    """Standard base64 ciphertext -> URL-safe alphabet, padding stripped."""
    return ciphertext.replace("+", "-").replace("/", "_").rstrip("=")


def from_callback_ref(ref: str) -> str:
    """Back to standard base64 with padding; ValueError on an empty value or any character outside [A-Za-z0-9_-]."""
    if not ref or _REF_PATTERN.fullmatch(ref) is None:
        raise ValueError("malformed callback reference")
    if len(ref) % 4 == 1:
        raise ValueError("malformed callback reference")
    standard = ref.replace("-", "+").replace("_", "/")
    return standard + "=" * (-len(standard) % 4)


__all__ = ["CALLBACK_REF_AAD", "to_callback_ref", "from_callback_ref"]
