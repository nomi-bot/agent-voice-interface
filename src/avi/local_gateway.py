"""Single-owner, single-process loopback gateway with durable Hermes runs."""
import argparse
import asyncio
from contextlib import asynccontextmanager, contextmanager, suppress
import logging
import os
from pathlib import Path
import stat
import tempfile

from avi.agents.hermes import HermesAgentAdapter
from avi.agents.hermes_durable import HermesDurableBackend
from avi.core.durable_auth import LoopbackCredentials, RunPermission
from avi.core.durable_runs import DurableRunService
from avi.core.durable_runtime import DurableRuntime
from avi.core.durable_store import SQLiteRunStore
from avi.core.legacy_cutover import LegacyCutover
from avi.core.voice_delegation import valid_read_only_query
from avi.core.voice_tool_journal import VoiceToolJournal


@contextmanager
def _exclusive_state(directory: Path):
    import fcntl

    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("State directory must be private (0700) and owned by this user")
    with (directory / "gateway.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("State directory already has an active gateway") from None
        yield


def _write_grant(path: Path, token: str):
    with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", delete=False) as tmp:
        try:
            tmp.write(token + "\n")
            tmp.flush()
            os.replace(tmp.name, path)
        finally:
            Path(tmp.name).unlink(missing_ok=True)


async def _maintain(runtime: DurableRuntime):
    while True:
        await asyncio.sleep(1)
        try:
            await runtime.runs.recover(startup=False)
            await runtime.runs.maintain()
            await asyncio.to_thread(runtime.journal.prune)
        except Exception:
            # Retain reads and recovery, but stop new admissions after a storage fault.
            await runtime.cutover.rollback()
            logging.getLogger(__name__).error("Durable maintenance failed; admissions closed")


@asynccontextmanager
async def open_runtime(directory, *, backend, owner, scope, queries, read_only, grant_seconds):
    try:
        with _exclusive_state(directory):
            grant_file = directory / "client.grant"
            grant_file.unlink(missing_ok=True)
            if any(not valid_read_only_query(query) for query in queries):
                raise ValueError("Approved queries must fit the read-only task schema")
            grants = LoopbackCredentials()
            store = SQLiteRunStore(directory / "runs.sqlite3")
            cutover = LegacyCutover(store)
            runs = DurableRunService(store, grants, backend, backend_scope=scope,
                                     admissions_open=True, legacy_cutover=cutover)
            runtime = DurableRuntime(grants, runs, VoiceToolJournal(directory / "voice.sqlite3"),
                                     cutover, frozenset(queries))
            await asyncio.to_thread(runtime.journal.expire_abandoned_sessions)
            await asyncio.to_thread(runtime.journal.prune)
            cutover.begin()
            await runs.recover()
            await runs.maintain()
            if not read_only:
                await cutover.enable_durable()
            permissions = set(RunPermission)
            if read_only:
                permissions.difference_update({RunPermission.SUBMIT, RunPermission.CANCEL})
            token = grants.issue(owner, permissions, lifetime=grant_seconds)
            _write_grant(grant_file, token)
            worker = asyncio.create_task(_maintain(runtime))
            try:
                yield runtime
            finally:
                grants.revoke(token)
                grant_file.unlink(missing_ok=True)
                worker.cancel()
                with suppress(asyncio.CancelledError):
                    await worker
                await cutover.rollback()
                await runs.settle_submissions()
    finally:
        await backend.close()


def main():
    import uvicorn
    from avi.agents.hermes_http import HermesHttpTransport
    from avi.main import app

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--hermes-origin", required=True)
    parser.add_argument("--backend-scope", required=True, help="Stable origin/profile/credential-epoch label")
    parser.add_argument("--owner", required=True)
    parser.add_argument("--allow-query", action="append", default=[], help="Exact read-only query approved for dispatch")
    parser.add_argument("--read-only", action="store_true", help="Retrieve existing runs with new submissions disabled")
    parser.add_argument("--grant-seconds", type=int, default=900)
    args = parser.parse_args()
    if not args.read_only and not args.allow_query:
        parser.error("Supply an approved query or use --read-only")
    if not os.environ.get("HERMES_API_KEY"):
        parser.error("Set HERMES_API_KEY in the server environment")
    default_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        transport = HermesHttpTransport(args.hermes_origin, os.environ["HERMES_API_KEY"])
        backend = HermesDurableBackend(HermesAgentAdapter(transport))
        async with default_lifespan(application), open_runtime(
            args.state_dir, backend=backend, owner=args.owner, scope=args.backend_scope,
            queries=args.allow_query, read_only=args.read_only, grant_seconds=args.grant_seconds,
        ) as runtime:
            application.state.durable_runtime = runtime
            try:
                yield
            finally:
                del application.state.durable_runtime

    app.router.lifespan_context = lifespan
    try:
        uvicorn.run(app, host="127.0.0.1", port=8000, proxy_headers=False, access_log=False,
                    ws_max_size=16384, ws_max_queue=16)
    finally:
        app.router.lifespan_context = default_lifespan


if __name__ == "__main__":
    main()
