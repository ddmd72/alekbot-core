"""The raw body of a request Slack signs, read with a size cap: the one reader for both Slack routes (events,
interactivity). The signature is checked on these bytes, so the body is read whole, but never beyond `MAX_BODY_BYTES`:
a declared `Content-Length` over the cap is refused before a byte is read, and a body without one (or one that lies) is
refused as soon as the bytes read pass the cap."""
from typing import Any

# Slack's payloads are small (an event, a button press); 1 MiB leaves a wide margin and bounds what an unsigned caller can
# make the service hold in memory before the signature is checked.
MAX_BODY_BYTES = 1024 * 1024


class BodyTooLarge(Exception):
    """The request body is over `MAX_BODY_BYTES`."""


async def read_capped_body(request: Any, limit: int = MAX_BODY_BYTES) -> bytes:
    """The raw body of `request` (a Quart request), or BodyTooLarge. Nothing is parsed."""
    declared = request.content_length
    if declared is not None and declared > limit:
        raise BodyTooLarge(f"declared {declared} bytes")
    chunks = []
    total = 0
    async for chunk in request.body:
        total += len(chunk)
        if total > limit:
            raise BodyTooLarge(f"more than {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)
