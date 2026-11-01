"""Async headless session on ``/ws/avip``.

One client opens one socket, sends one ``session.start``, then sends uplink
audio. It does not reconnect, and it does not start an agent run.
"""

from collections.abc import Mapping

from websockets.exceptions import ConnectionClosed

from avip_client.errors import AvipError
from avip_client.framing import UplinkClock, unpack_frame
from avip_client.messages import parse_server_text, session_start

# Unread messages held by the library before it pauses the socket. This is
# transport backpressure, not an application reject. The browser client uses
# the same depth and then fails the session.
_RECEIVE_QUEUE = 8


class AvipClient:
    """Headless AVIP client.

    ``origin`` is sent on the WebSocket handshake. The development gateway
    allows ``http://127.0.0.1:8000`` and ``http://localhost:8000``. Compression
    is off so each binary message is one raw AVIP frame.

    Use it as an async context manager::

        async with AvipClient(
            "ws://127.0.0.1:8000/ws/avip",
            request_id="req-python-001",
            device_id="headless-sim-001",
            capabilities={"audio_input": True, "audio_output": True},
        ) as client:
            ready = await client.start()
            await client.send_uplink(pcm)
            downlink = await client.recv()
    """

    def __init__(
        self,
        url: str,
        *,
        request_id: str,
        device_id: str,
        capabilities: Mapping[str, bool],
        origin: str = "http://127.0.0.1:8000",
    ):
        self.url = url
        self.origin = origin
        self._start_text = session_start(
            request_id=request_id,
            device_id=device_id,
            capabilities=capabilities,
        )
        self._clock = UplinkClock()
        self._ws = None
        self._started = False
        self._closed = False

    async def __aenter__(self):
        from websockets.asyncio.client import connect

        self._ws = await connect(
            self.url,
            origin=self.origin,
            compression=None,
            proxy=None,
            max_size=16 * 1024,
            max_queue=_RECEIVE_QUEUE,
            user_agent_header=None,
        )
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._closed = True
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        return False

    @property
    def closed(self) -> bool:
        """True after the socket has ended. The client does not reconnect."""
        return self._closed

    async def start(self) -> dict:
        """Send ``session.start`` and return the ``session.ready`` object."""
        if self._started:
            raise AvipError("session_already_started")
        self._require_open()
        try:
            await self._ws.send(self._start_text)
        except ConnectionClosed:
            self._closed = True
            raise
        message = await self.recv()
        if not isinstance(message, dict) or message.get("type") != "session.ready":
            raise AvipError("invalid_message", "type")
        self._started = True
        return message

    async def send_uplink(self, payload: bytes) -> None:
        """Send one uplink frame.

        The clock advances only after ``send`` returns. A raised error means
        the socket did not accept the frame, so the sequence is unchanged and
        may be retried on this socket if it is still open. A successful return
        means the bytes were queued locally, not that the peer acknowledged
        them. If the socket later closes, that frame may or may not have been
        delivered; do not send the same sequence again here. This client does
        not reconnect. Open a new ``AvipClient`` for a new session, which
        starts at sequence 0.
        """
        self._require_open()
        if not self._started:
            raise AvipError("invalid_message")
        raw = self._clock.encode(payload)
        try:
            await self._ws.send(raw)
        except ConnectionClosed:
            self._closed = True
            raise
        self._clock.commit()

    async def recv(self):
        """Return a ``session.ready`` dict or one decoded downlink frame.

        A server ``error`` envelope raises ``AvipError`` with that code and
        field. Other JSON objects are returned as dicts. The library holds at
        most 8 unread messages and then pauses the socket. Peer disconnect
        raises ``ConnectionClosed`` and marks this client closed.
        """
        self._require_open()
        try:
            raw = await self._ws.recv()
        except ConnectionClosed:
            self._closed = True
            raise
        if isinstance(raw, str):
            message = parse_server_text(raw)
            if message.get("type") == "error":
                code = message.get("code")
                field = message.get("field")
                raise AvipError(
                    code if type(code) is str else "invalid_message",
                    field if type(field) is str else None,
                )
            return message
        if isinstance(raw, bytearray):
            raw = bytes(raw)
        if type(raw) is not bytes:
            raise AvipError("invalid_message")
        return unpack_frame(raw)

    def _require_open(self) -> None:
        if self._closed or self._ws is None:
            raise AvipError("socket_closed")
