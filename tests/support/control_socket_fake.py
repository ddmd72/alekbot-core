"""A page's control socket as the relay sees it (a `websockets` server connection): `recv`, async iteration, `send`,
`close(code)`. The page side is driven with `page_says`; what the relay sent is read back with `frames()`."""
import asyncio
import json

from websockets.exceptions import ConnectionClosedOK

_CLOSED = object()


class FakeControlSocket:
    def __init__(self, *, stall_sends: bool = False):
        self._incoming: asyncio.Queue = asyncio.Queue()
        self.sent: list = []
        self.close_code = None
        self.stall_sends = stall_sends

    @property
    def closed(self) -> bool:
        return self.close_code is not None

    # ---------------------------------------------------------------- the page

    def page_says(self, message) -> None:
        """Queue a frame from the page: a dict is sent as JSON text, a str or bytes as is."""
        self._incoming.put_nowait(json.dumps(message) if isinstance(message, dict) else message)

    def page_leaves(self) -> None:
        self._incoming.put_nowait(_CLOSED)

    def frames(self) -> list:
        return [json.loads(text) for text in self.sent]

    def types(self) -> list:
        return [frame["type"] for frame in self.frames()]

    # ---------------------------------------------------------------- the connection API the relay uses

    async def recv(self):
        item = await self._incoming.get()
        if item is _CLOSED:
            raise ConnectionClosedOK(None, None)
        return item

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._incoming.get()
        if item is _CLOSED:
            raise StopAsyncIteration
        return item

    async def send(self, message: str) -> None:
        if self.closed:
            raise ConnectionClosedOK(None, None)
        if self.stall_sends:
            await asyncio.sleep(3600)
        self.sent.append(message)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.close_code is None:
            self.close_code = code
        self._incoming.put_nowait(_CLOSED)
