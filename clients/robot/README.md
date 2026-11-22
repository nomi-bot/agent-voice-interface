# Headless AVIP simulator

The simulator uses the installed Python SDK and the browser's `/ws/avip` endpoint. Its fake microphone emits one deterministic 640-byte frame of 16 kHz mono PCM every 20 ms, plus network time. The SDK assigns each frame's sequence and timestamp. The fake speaker retains at most eight frames. It stops locally and flushes only the active generation on a matching `response.interrupted` event.

Install the checkout and start the shared mock gateway from the repository root.

```sh
python -m pip install -e .
AVIP_MOCK_MEDIA=1 python -m uvicorn avi.main:app --host 127.0.0.1 --port 8000 --ws-max-size 16384 --ws-max-queue 16
```

Run a finite session in a second terminal.

```sh
python -m avip_client.simulator ws://127.0.0.1:8000/ws/avip --frames 24 --scenario normal
```

| Scenario | Behavior |
| --- | --- |
| `normal` | Play every mirrored frame and validate its PCM, sequence, and timestamp. |
| `slow-speaker` | Consume one queued frame per four replies, drop arrivals when full, then drain the remaining queue. |
| `disconnect` | Open a fresh session halfway through. SDK framing starts again at sequence and timestamp zero. |
| `stop-playback` | Stop the local speaker halfway through and continue sending microphone frames until the session ends. |

The CLI prints one JSON object with session, uplink, downlink, playback, drop, flush, and peak queue counts. Exit codes are 0 for success, 1 for a gateway or media failure, and 2 for invalid arguments. The input limit is 3,000 frames. The disconnect and local stop scenarios require at least two frames.

The default SDK install supports the simulator without ROS. The existing browser CLI and this simulator use the same mock gateway in the conformance test. Real provider and agent parity, including a mounted response-interruption scenario, depend on JEV-29, JEV-30 and JEV-32.

## Retained run result lookup

The SDK's `avip_client.durable_result.lookup_run_result` coroutine accepts the numeric loopback AVIP URL, AVI run ID, and an operator-issued grant. It opens a fresh session, requires negotiated `durable_runs`, then authenticates and reads the result. The server owns authorization. Pass the grant directly from trusted local code. The coroutine reports `durable_runs_not_negotiated` on the current mock route.

The client follows the [JEV-28 protocol proposal](https://github.com/nomi-bot/agent-voice-interface/blob/dd5b285991aa4d631fbf4027347e1807bfdaa789/docs/protocols/avip-durable-runs-v1.md) and checks it against a loopback fixture. Gateway support and real owner-authorized result retrieval require the corresponding integration.

## Local mock device

The [mock device fixture](../../tests/fixtures/jev31-mock-device-v1.json) defines a synthetic sensor observation and the allowlisted `record_sample` action. The action records a number in memory. An admission checks the capability, local authorization, action name, and validated payload. The device returns the existing events when an admitted request repeats under the same event ID. Conflicting payloads are refused. The device retains up to 32 IDs per instance and refuses new IDs when full.

A simulator clock advances the action from accepted to running and then to completed after 50 ms of mock work. At its 250 ms deadline an unfinished action times out instead. A caller can cancel an active local action. The stock gateway does not negotiate `robot_actions` and this fixture travels only on the local mock device interface.

See the [optional ROS 2 bridge](ros2/README.md) for the same mock lifecycle on a ROS graph.
