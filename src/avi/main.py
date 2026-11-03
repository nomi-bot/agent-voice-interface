"""Local-only development gateway. This skeleton has no live voice provider."""
import os
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import FileResponse

from avi.agents.mock import MockAgentAdapter
from avi.core.avip_session import AvipSession
from avi.core.session import ControlSession, SessionLimits
from avi.protocols.avip import Capabilities
from avi.transports.websocket import WebSocketTransport


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.active_sessions = 0
    yield


app = FastAPI(title="AVI development gateway", lifespan=lifespan)
ALLOWED_ORIGINS = {"http://localhost:8000", "http://127.0.0.1:8000"}
SESSION_LIMITS = SessionLimits()
# Development demo only. Not a general file server. Names below are the only
# files the route reads. A wheel install carries them in ``avi/_browser``.
# A source checkout uses ``clients/browser`` when that package directory is
# absent. The packaged directory wins when both are complete.
_VOICE_FILES = (
    "voice.html",
    "avip-client.js",
    "pcm.js",
    "voice.js",
    "telemetry.js",
    "capture-worklet.js",
)
_VOICE_ASSETS = {
    "avip-client.js": "text/javascript",
    "pcm.js": "text/javascript",
    "voice.js": "text/javascript",
    "telemetry.js": "text/javascript",
    "capture-worklet.js": "text/javascript",
}
_VOICE_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def _directory_has_voice_files(directory: Path) -> bool:
    return all((directory / name).is_file() for name in _VOICE_FILES)


def resolve_browser_dir(packaged: Path, source: Path) -> Path:
    """Pick the voice-asset directory. Packaged files win when complete."""
    if _directory_has_voice_files(packaged):
        return packaged
    if _directory_has_voice_files(source):
        return source
    raise FileNotFoundError("browser voice assets are not installed")


def _browser_dir() -> Path:
    packaged = Path(__file__).resolve().parent / "_browser"
    source = Path(__file__).resolve().parents[2] / "clients" / "browser"
    return resolve_browser_dir(packaged, source)


def _browser_file(name: str) -> Path:
    root = _browser_dir()
    path = (root / name).resolve()
    if path.parent != root.resolve() or not path.is_file():
        raise HTTPException(status_code=404)
    return path


def development_capabilities(environ: Mapping[str, str] | None = None) -> Capabilities:
    """Return the features this process can actually negotiate.

    The default is none. ``AVIP_MOCK_MEDIA=1`` enables the M0 PCM mirror for
    every client: audio input and audio output only. It does not enable a
    voice provider, camera, display, or robot actions. Any other value leaves
    every capability false.
    """
    source = os.environ if environ is None else environ
    if source.get("AVIP_MOCK_MEDIA") == "1":
        return Capabilities(audio_input=True, audio_output=True)
    return Capabilities()


AVIP_CAPABILITIES = development_capabilities()


@app.get("/health")
async def health():
    return {"status": "ok", "voice": "not_implemented", "agent": "mock"}


@app.get("/voice")
async def voice_demo():
    """Serve the local microphone and speaker page.

    The page is a development client for ``/ws/avip``. It is not a voice
    provider. ``AVIP_MOCK_MEDIA=1`` is what negotiates the PCM mirror.
    """
    return FileResponse(
        _browser_file("voice.html"),
        media_type="text/html; charset=utf-8",
        headers=_VOICE_HEADERS,
    )


@app.get("/voice/{asset}")
async def voice_asset(asset: str):
    media_type = _VOICE_ASSETS.get(asset)
    if media_type is None:
        raise HTTPException(status_code=404)
    return FileResponse(
        _browser_file(asset),
        media_type=f"{media_type}; charset=utf-8",
        headers=_VOICE_HEADERS,
    )


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


@app.websocket("/ws/avip")
async def avip_session(websocket: WebSocket):
    # Defense in depth for local development; this is not remote authentication.
    # This path speaks only the AVIP handshake. It does not run legacy control
    # messages and it does not create agent runs.
    if websocket.headers.get("origin") not in ALLOWED_ORIGINS:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    app.state.active_sessions += 1
    try:
        await AvipSession(
            WebSocketTransport(websocket), SESSION_LIMITS, AVIP_CAPABILITIES
        ).run()
    finally:
        app.state.active_sessions -= 1
