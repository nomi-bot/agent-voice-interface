# Agent Voice Interface (AVI)

Give every AI agent and robot a natural voice.

AVI is an open-source, plug-and-play voice interaction layer, independent of
agent runtimes and model vendors. It is not an agent framework.

## Current status: initialization skeleton

The current implementation supplies a Python 3.12+ FastAPI gateway, four adapter contracts,
an asynchronous mock agent, a JSON WebSocket control protocol, tests and CI.
The original Apache-2.0 LICENSE is preserved.

**There is no browser audio client, Gemini connection, or working Hermes
integration yet.** No full-duplex, latency or macOS runtime performance claim
has been validated. Provider placeholders fail explicitly rather than simulating
a successful integration. No Google SDK dependency or unverified model ID is
introduced.

## Local development (macOS / Mac Mini)

Install Python 3.12 or newer (for example, Homebrew: `brew install python@3.12`).

```sh
git clone https://github.com/nomi-bot/agent-voice-interface.git
cd agent-voice-interface
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
python -m uvicorn avi.main:app --host 127.0.0.1 --port 8000 --ws-max-size 16384 --ws-max-queue 16
```

Check `http://127.0.0.1:8000/health` or `/docs`. No credentials are needed for
the mock. `.env.example` reserves a future server-side key; this skeleton does
not load environment files. Never commit a populated `.env`.

```sh
python -m unittest discover -s tests -v
```

## WebSocket smoke test

Open `http://127.0.0.1:8000/docs` in a browser, then use its developer console:

```js
const ws = new WebSocket("ws://127.0.0.1:8000/ws");
ws.onmessage = e => console.log(JSON.parse(e.data));
ws.onopen = () => ws.send(JSON.stringify({type: "submit", request: "demo"}));
// Copy the id from the run response:
ws.send(JSON.stringify({type: "status", run_id: "<returned id>"}));
ws.send(JSON.stringify({type: "cancel_task", run_id: "<returned id>"}));
```

The mock completes after one second. Status is retrieved explicitly; no progress
push channel is implemented. `ping` returns `pong`; `stop_playback` returns a
client instruction without cancelling a task. `cancel_response` is explicitly
unsupported until a provider implements it. Runs belong to one session and are
cancelled on disconnect. Each session retains at most 32 runs and expires after
300 seconds without a message.

Agent operations execute sequentially with a 30-second deadline. Receiving stays
active during those operations, so `ping`, `stop_playback` and disconnects do not
wait for an agent response. Control replies can overtake agent replies.
There are at most 16 queued operations and 16 queued replies. Excess operations
return an error; a full reply queue or a send stalled for 5 seconds closes the
session with code 1013. Initial `ready` sends and close frames also have a
5-second deadline. Adapter cleanup has a 5-second deadline. These are control-plane
safeguards, not measured realtime audio guarantees. Adapters must cooperate with
asyncio cancellation and must not run blocking work on the event loop.

## Security and integration boundaries

Bind to loopback only. The WebSocket checks exact localhost origins on port 8000;
this is defense in depth, **not authentication**. Do not expose the development
service through a proxy or bind it remotely. Mock requests execute no tools.
Future agent integrations must retain their own authorisation and approval
controls. Treat voice transcripts and tool arguments as untrusted input.

## Documentation map

- [Current architecture](docs/architecture.md): executable skeleton behavior and limitations.
- [Target architecture](docs/architecture/overview.md): accepted device-neutral direction;
  provider integration, AVIP and durable runs are planned capabilities.
- [Canonical roadmap](docs/roadmap/milestones.md): M0–M4 scope and required exit gates,
  including the complete M2 ownership, authorization, persistence and migration gates.
- [AVIP draft](docs/protocols/avip-v1.md): proposed protocol, separate from the current socket.
- [ADR index and policy](docs/adr/README.md): one three-digit sequence, 001–010.
  ADRs 001–005 remain accepted; 006–010 extend them with explicit lineage.

The [legacy roadmap](docs/roadmap.md) redirects to the canonical plan and maps its
original milestones. v0.1 robotics scope is a simulator and optional ROS 2 bridge
with mock actions only; no physical actuation.

## License

Apache-2.0; see [LICENSE](LICENSE).
