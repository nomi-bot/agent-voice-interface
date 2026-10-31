import json

from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketDisconnected

from avi.transports.base import TransportDisconnected, TransportFrameError


class WebSocketTransport:
    def __init__(self, websocket: WebSocket):
        self.websocket = websocket

    async def receive(self) -> dict:
        try:
            return await self.websocket.receive_json()
        except WebSocketDisconnect as exc:
            raise TransportDisconnected from exc

    async def receive_message(self) -> tuple[str, dict | bytes]:
        """Return one text JSON object or one complete binary message.

        Text that is not a JSON object raises ``TransportFrameError`` with
        ``binary=False``. A frame that is neither text nor bytes raises it
        with ``binary=True``. The payload is not attached to that exception.
        """
        try:
            message = await self.websocket.receive()
        except (WebSocketDisconnect, WebSocketDisconnected) as exc:
            raise TransportDisconnected from exc
        if message["type"] == "websocket.disconnect":
            raise TransportDisconnected
        text = message.get("text")
        if isinstance(text, str):
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                raise TransportFrameError(binary=False) from None
            if not isinstance(value, dict):
                raise TransportFrameError(binary=False)
            return ("text", value)
        data = message.get("bytes")
        if type(data) is bytes or type(data) is bytearray:
            return ("binary", bytes(data))
        raise TransportFrameError(binary=True)

    async def receive_json_object(self) -> dict:
        """Return one JSON object from a text frame.

        Binary frames and text that is not a JSON object raise TransportFrameError.
        The frame contents are not attached to that exception.
        """
        kind, payload = await self.receive_message()
        if kind != "text" or not isinstance(payload, dict):
            raise TransportFrameError(binary=True)
        return payload

    async def send(self, event: dict) -> None:
        try:
            await self.websocket.send_json(event)
        except (WebSocketDisconnect, OSError) as exc:
            raise TransportDisconnected from exc

    async def send_bytes(self, payload: bytes) -> None:
        try:
            await self.websocket.send_bytes(payload)
        except (WebSocketDisconnect, OSError) as exc:
            raise TransportDisconnected from exc

    async def close(self, code: int, reason: str) -> None:
        try:
            await self.websocket.close(code=code, reason=reason)
        except (WebSocketDisconnect, OSError) as exc:
            raise TransportDisconnected from exc
