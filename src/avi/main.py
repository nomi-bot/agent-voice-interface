"""Local-only development gateway. This skeleton has no live voice provider."""
import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from avi.agents.mock import MockAgentAdapter
from avi.core.orchestrator import VoiceOrchestrator
from avi.transports.websocket import WebSocketTransport


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.active_sessions = 0
    yield


app = FastAPI(title="AVI development gateway", lifespan=lifespan)
ALLOWED_ORIGINS = {"http://localhost:8000", "http://127.0.0.1:8000"}


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
    orchestrator = VoiceOrchestrator(agent)
    transport = WebSocketTransport(websocket)
    app.state.active_sessions += 1
    try:
        await transport.send({"type": "ready", "agent": "mock", "voice": False})
        while True:
            try:
                async with asyncio.timeout(300):
                    message = await transport.receive()
                reply = await orchestrator.handle(message)
            except (ValueError, KeyError, json.JSONDecodeError):
                reply = {"type": "error", "message": "Invalid message or unknown run"}
            await transport.send(reply)
    except TimeoutError:
        await websocket.close(code=1001, reason="Idle timeout")
    except WebSocketDisconnect:
        pass
    finally:
        await agent.close()
        app.state.active_sessions -= 1
