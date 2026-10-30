"""Local-only development gateway. This skeleton has no live voice provider."""
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket

from avi.agents.mock import MockAgentAdapter
from avi.core.session import ControlSession, SessionLimits
from avi.transports.websocket import WebSocketTransport


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.active_sessions = 0
    yield


app = FastAPI(title="AVI development gateway", lifespan=lifespan)
ALLOWED_ORIGINS = {"http://localhost:8000", "http://127.0.0.1:8000"}
SESSION_LIMITS = SessionLimits()


@app.get("/health")
async def health():
    return {"status": "ok", "voice": "not_implemented", "agent": "mock"}


@app.websocket("/ws")
async def session(websocket: WebSocket):
    # Defense in depth for local development; this is not remote authentication.
    if websocket.headers.get("origin") not in ALLOWED_ORIGINS:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    agent = MockAgentAdapter()
    transport = WebSocketTransport(websocket)
    app.state.active_sessions += 1
    try:
        await ControlSession(agent, transport, SESSION_LIMITS).run(
            {"type": "ready", "agent": "mock", "voice": False}
        )
    finally:
        app.state.active_sessions -= 1
