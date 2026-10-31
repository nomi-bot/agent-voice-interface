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

    async def receive_json_object(self) -> dict:
        """Return one JSON object from a text frame.

        Binary frames and text that is not a JSON object raise TransportFrameError.
        The frame contents are not attached to that exception.
        """
        try:
            message = await self.websocket.receive()
        except (WebSocketDisconnect, WebSocketDisconnected) as exc:
            raise TransportDisconnected from exc
        if message["type"] == "websocket.disconnect":
            raise TransportDisconnected
        text = message.get("text")
        if not isinstance(text, str):
            raise TransportFrameError(binary=True)
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            raise TransportFrameError(binary=False) from None
        if not isinstance(value, dict):
            raise TransportFrameError(binary=False)
        return value

    async def send(self, event: dict) -> None:
        try:
            await self.websocket.send_json(event)
        except (WebSocketDisconnect, OSError) as exc:
            raise TransportDisconnected from exc

    async def close(self, code: int, reason: str) -> None:
        try:
            await self.websocket.close(code=code, reason=reason)
        except (WebSocketDisconnect, OSError) as exc:
            raise TransportDisconnected from exc
