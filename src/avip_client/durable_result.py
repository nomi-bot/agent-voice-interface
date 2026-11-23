"""Read a retained result on a fresh session using the JEV-28 wire proposal."""

import asyncio
from dataclasses import dataclass
from ipaddress import ip_address
import re
from urllib.parse import urlsplit

from avip_client.errors import AvipError
from avip_client.messages import PROTOCOL
from avip_client.session import AvipClient


_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_GRANT = re.compile(r"[A-Za-z0-9_-]{43}")
_CAPABILITY = "durable_runs"


@dataclass(frozen=True)
class RetainedRunResult:
    run_id: str
    state: str
    retained: bool
    result: str | None


def _loopback_endpoint(url: str) -> tuple:
    try:
        parts = urlsplit(url)
        host = ip_address(parts.hostname or "")
        if (
            parts.scheme in {"ws", "wss"} and host.is_loopback and parts.path == "/ws/avip"
            and parts.username is None and parts.password is None and not parts.query
            and not parts.fragment and parts.port != 0
        ):
            return host, parts.port or (443 if parts.scheme == "wss" else 80)
    except (TypeError, ValueError):
        pass
    raise AvipError("invalid_run_result_endpoint")


async def lookup_run_result(
    url: str, *, run_id: str, token: str, origin: str = "http://127.0.0.1:8000",
) -> RetainedRunResult:
    """Authenticate and read once, after explicit capability negotiation."""

    host, port = _loopback_endpoint(url)
    if type(run_id) is not str or _RUN_ID.fullmatch(run_id) is None:
        raise AvipError("invalid_run_id")
    if type(token) is not str or _GRANT.fullmatch(token) is None:
        raise AvipError("invalid_run_grant")
    async with asyncio.timeout(5), AvipClient(
        url, request_id="headless-result-lookup", device_id="headless-simulator",
        capabilities={_CAPABILITY: True}, origin=origin,
    ) as client:
        peer = client.remote_address
        if not peer or ip_address(peer[0]) != host or peer[1] != port:
            raise AvipError("invalid_run_result_endpoint")
        ready = await client.start()
        capabilities = ready.get("capabilities")
        if not isinstance(capabilities, dict) or capabilities.get(_CAPABILITY) is not True:
            raise AvipError("durable_runs_not_negotiated")
        await client.send_control({
            "protocol": PROTOCOL, "type": "agent.run.authenticate", "token": token,
        })
        authenticated = await client.recv()
        if not _message_type(authenticated, "agent.run.authenticated"):
            raise AvipError("invalid_run_authentication")
        await client.send_control({
            "protocol": PROTOCOL, "type": "agent.run.result", "run_id": run_id,
        })
        return _result(await client.recv(), run_id)


def _message_type(message: object, kind: str) -> bool:
    return isinstance(message, dict) and message.get("protocol") == PROTOCOL and message.get("type") == kind


def _result(message: object, run_id: str) -> RetainedRunResult:
    if not _message_type(message, "agent.run.result"):
        raise AvipError("invalid_run_result")
    status = message.get("status")
    result = message.get("result")
    retained = message.get("retained")
    if (
        not isinstance(status, dict) or status.get("run_id") != run_id
        or type(status.get("state")) is not str or type(retained) is not bool
        or (result is not None and (type(result) is not str or not retained))
        or "result" not in message
    ):
        raise AvipError("invalid_run_result")
    return RetainedRunResult(run_id, status["state"], retained, result)
