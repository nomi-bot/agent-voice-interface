"""Opt-in synthetic speech probe for an already running local Live + Hermes gateway."""
import argparse
import asyncio
from contextlib import closing
from hashlib import sha256
from http.client import HTTPConnection
import json
import os
import re
from pathlib import Path
import sqlite3
import stat
import wave

from avi.core.durable_store import canonical_task
from avi.core.voice_delegation import valid_read_only_query
from avip_client.framing import AUDIO_DOWNLINK, AudioFrame, NOMINAL_PAYLOAD_BYTES
from avip_client.session import AvipClient


URL = "ws://127.0.0.1:8000/ws/avip"
RATE = 16_000


def speech(path):
    with wave.open(str(path), "rb") as wav:
        n = wav.getnframes()
        if (wav.getframerate(), wav.getsampwidth(), wav.getnchannels(), wav.getcomptype()) != (RATE, 2, 1, "NONE"):
            raise ValueError("unsupported_wave")
        if not 0 < n <= RATE * 30:
            raise ValueError("invalid_duration")
        pcm = wav.readframes(n)
        if len(pcm) != 2 * n or not any(pcm):
            raise ValueError("empty_or_truncated_speech")
        return pcm


def live_runtime():
    with closing(HTTPConnection("127.0.0.1", 8000, timeout=3)) as http:
        http.request("GET", "/health")
        response = http.getresponse()
        if response.status != 200:
            return False
        health = json.loads(response.read(4096))
        return health.get("voice") == "live" and health.get("agent") == "durable"


def evidence(state, session_id, digest):
    # Read the launcher's private databases as the operator. Bind the run to
    # this provider call and the approved task; concurrent owners/runs do not
    # qualify. Keep raw results, fingerprints and identifiers out of the report.
    with closing(sqlite3.connect((state / "voice.sqlite3").as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("ATTACH DATABASE ? AS runs", ((state / "runs.sqlite3").as_uri() + "?mode=ro",))
        rows = db.execute("""
            SELECT r.run_id, r.state, r.dispatch, r.handle IS NOT NULL, length(r.result) > 0,
                   r.payload_digest = ?, v.delivery, r.request_id
            FROM voice_tool_deliveries v JOIN runs.durable_runs r
              ON r.request_id = v.request_id AND r.run_id = v.run_id
            WHERE v.session_id = ?
        """, (digest, session_id)).fetchall()
        matched = [r for r in rows if r[5]]
        result_reply = False
        if len(rows) == len(matched) == 1:
            run_id, _, _, _, _, _, _, request_id = matched[0]
            # Of the two AVIP tools that bind a run ID, the submitting call
            # owns its request ID. A subsequent result read has a different ID.
            result_reply = bool(db.execute("""
                SELECT 1 FROM voice_tool_deliveries
                WHERE session_id = ? AND run_id = ? AND request_id != ? AND delivery = 'acknowledged'
            """, (session_id, run_id, request_id)).fetchone())
    return {
        "session_submissions": len(rows),
        "exact_query_submissions": len(matched),
        "backend_completed_with_result": any(
            r[1] == "completed" and r[2] == "accepted" and r[3] and r[4] for r in matched
        ),
        "delegation_reply_acknowledged": any(r[6] == "acknowledged" for r in matched),
        "result_tool_reply_acknowledged": result_reply,
    }


async def send_turn(client, pcm):
    size = NOMINAL_PAYLOAD_BYTES
    # Pacing follows the AVIP input clock. Trailing silence lets automatic
    # activity detection close a turn without a microphone or synthetic text.
    pcm = pcm + bytes((-len(pcm)) % size + 50 * size)
    for offset in range(0, len(pcm), size):
        await client.send_uplink(pcm[offset:offset + size])
        await asyncio.sleep(0.020)


async def exercise(state, token, query, command, followup, report, review):
    digest = sha256(canonical_task("read_only", {"query": query}).encode("ascii")).hexdigest()
    loop = asyncio.get_running_loop()
    latest_audio = 0.
    followup_task = None
    async with AvipClient(URL, request_id="spoken-qualification", device_id="qualification",
                          capabilities={"audio_input": True, "audio_output": True, "durable_runs": True}) as client:
        if client.remote_address[:2] != ("127.0.0.1", 8000):
            raise ValueError("unexpected_peer")
        ready = await client.start()
        if not all(ready["capabilities"].get(k) is True for k in ("durable_runs", "audio_input", "audio_output")):
            raise ValueError("not_negotiated")
        await client.send_control({"protocol": "avip/1", "type": "agent.run.authenticate", "token": token})
        while True:
            message = await client.recv()
            if isinstance(message, AudioFrame):
                continue
            if message.get("type") == "agent.run.authenticated":
                break
            if message.get("type") != "response.interrupted":
                raise ValueError("unexpected_auth_reply")

        async def receive():
            nonlocal latest_audio
            while True:
                frame = await client.recv()
                if isinstance(frame, AudioFrame) and frame.direction == AUDIO_DOWNLINK:
                    latest_audio = loop.time()
                    report["downlink_frames"] += 1
                    if report.get("result_tool_reply_acknowledged"):
                        report["pcm_bytes_after_observed_result_ack"] += len(frame.payload)
                    if followup_task is not None:
                        report["followup_pcm_bytes"] += len(frame.payload)
                        if report["followup_pcm_bytes"] > 2 * RATE * 180:
                            raise ValueError("output_limit")
                        if review is not None:
                            review.writeframesraw(frame.payload)

        reader = asyncio.create_task(receive())
        try:
            await send_turn(client, command)
            while True:
                if reader.done():
                    reader.result()
                if followup_task is not None and followup_task.done():
                    followup_task.result()
                report.update(await asyncio.to_thread(evidence, state, ready["session_id"], digest))
                if report["session_submissions"] > 1:
                    return
                if report["backend_completed_with_result"] and report["delegation_reply_acknowledged"]:
                    if followup_task is None:
                        if loop.time() - latest_audio < 1.0:
                            await asyncio.sleep(.25)
                            continue
                        followup_task = asyncio.create_task(send_turn(client, followup))
                    elif (followup_task.done() and report["result_tool_reply_acknowledged"] and report["pcm_bytes_after_observed_result_ack"]
                          and loop.time() - latest_audio >= 2):
                        report["status"] = "transport_and_run_evidence_observed"
                        return
                await asyncio.sleep(.25)
        finally:
            tasks = [reader] if followup_task is None else [reader, followup_task]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Allow this probe to initiate one billable Live session")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--query-file", type=Path, help="Exact approved synthetic read-only query")
    parser.add_argument("--command-wav", type=Path)
    parser.add_argument("--followup-wav", type=Path)
    parser.add_argument("--review-wav", type=Path, help="New private local file for optional human listening")
    args = parser.parse_args(argv)
    report = {"status": "skipped", "human_spoken_accuracy": "unreviewed", "downlink_frames": 0,
              "followup_pcm_bytes": 0, "pcm_bytes_after_observed_result_ack": 0}
    if not args.live:
        report["reason"] = "live_opt_in_required"
    elif not all(path and path.is_file() for path in (args.query_file, args.command_wav, args.followup_wav)) or not args.state_dir:
        report["reason"] = "fixtures_required"
    else:
        review = None
        file = None
        try:
            state = args.state_dir.resolve()
            grant = state / "client.grant"
            if not grant.is_file():
                report["reason"] = "local_grant_required"
            elif not live_runtime():
                report["reason"] = "configured_live_durable_launcher_required"
            else:
                info = grant.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise ValueError("invalid_private_grant")
                command, followup = speech(args.command_wav), speech(args.followup_wav)
                query = args.query_file.read_text().rstrip("\r\n")
                if not valid_read_only_query(query):
                    raise ValueError("invalid_query")
                token = grant.read_text().strip()
                if re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None:
                    raise ValueError("invalid_grant_format")
                # Validate access and schema before opening a paid connection.
                evidence(state, "", "")
                if args.review_wav:
                    fd = os.open(args.review_wav, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    file = os.fdopen(fd, "wb")
                    review = wave.open(file, "wb")
                    review.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
                report["status"] = "incomplete"
                async def run():
                    async with asyncio.timeout(150):
                        await exercise(state, token, query, command, followup, report, review)
                asyncio.run(run())
        except Exception as error:
            report.update(status="incomplete", error_class=type(error).__name__)
        finally:
            if review:
                review.close()
            if file:
                file.close()
    print(json.dumps(report, sort_keys=True))
    return int(report["status"] == "incomplete")


if __name__ == "__main__":
    raise SystemExit(main())
