"""Local-only development gateway.

``/ws/avip`` mounts the configured voice provider when credentials are
complete. Otherwise ``AVIP_MOCK_MEDIA=1`` keeps the PCM mirror. A missing
model does not guess one. The provider key stays in the process environment.
"""
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import FileResponse

from avi.agents.mock import MockAgentAdapter
from avi.core.avip_session import AvipSession
from avi.core.session import ControlSession, SessionLimits
from avi.protocols.avip import Capabilities
from avi.providers.mount import attach_voice, resolve_voice_mount
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
    """Return the audio features this process can honestly advertise.

    Live credentials and ``AVIP_MOCK_MEDIA=1`` both advertise audio input and
    audio output. Neither enables camera, display, or robot actions. Any
    other environment leaves every capability false. Live wins when the key
    and the model are both set, including when the mock flag is also set.
    """
    return resolve_voice_mount(environ).capabilities


# Tests patch this to drive a partial capability set. It applies only when
# the resolved mode is not configured. Mock and Live ignore it and use
# ``resolve_voice_mount`` instead, so an import-time snapshot cannot hide a
# later credential or force Live during a mock-media test.
AVIP_CAPABILITIES = Capabilities()


@app.get("/health")
async def health():
    """Report the voice path this process will mount. No key and no model id."""
    mount = resolve_voice_mount()
    return {
        "status": "ok",
        "voice": mount.mode,
        "voice_reason": mount.reason,
        "agent": "mock",
    }


@app.get("/voice")
async def voice_demo():
    """Serve the local microphone and speaker page.

    The page is a development client for ``/ws/avip``. It does not hold a
    provider key. Live audio is negotiated when the process is configured;
    ``AVIP_MOCK_MEDIA=1`` negotiates the PCM mirror when Live is not.
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
        voice = attach_voice(AVIP_CAPABILITIES)
        await AvipSession(
            WebSocketTransport(websocket),
            SESSION_LIMITS,
            voice.capabilities,
            provider=voice.provider,
            telemetry_path=voice.telemetry_path,
            telemetry_scenario=voice.telemetry_scenario,
            elicit=voice.elicit,
        ).run()
    finally:
        app.state.active_sessions -= 1
