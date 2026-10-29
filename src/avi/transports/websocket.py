from fastapi import WebSocket


class WebSocketTransport:
    def __init__(self, websocket: WebSocket):
        self.websocket = websocket

    async def receive(self) -> dict:
        return await self.websocket.receive_json()

    async def send(self, event: dict) -> None:
        await self.websocket.send_json(event)
