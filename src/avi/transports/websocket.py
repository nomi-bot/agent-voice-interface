from fastapi import WebSocket, WebSocketDisconnect

from avi.transports.base import TransportDisconnected


class WebSocketTransport:
    def __init__(self, websocket: WebSocket):
        self.websocket = websocket

    async def receive(self) -> dict:
        try:
            return await self.websocket.receive_json()
        except WebSocketDisconnect as exc:
            raise TransportDisconnected from exc

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
