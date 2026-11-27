"""Trusted local composition shared by legacy and AVIP sessions."""

from dataclasses import dataclass

from avi.core.durable_auth import AuthenticatedConnection, LoopbackCredentials, RunPermission
from avi.core.durable_runs import DurableRunService
from avi.core.legacy_cutover import LegacyCutover
from avi.core.voice_tool_journal import VoiceToolJournal


@dataclass(frozen=True)
class DurableRuntime:
    credentials: LoopbackCredentials
    runs: DurableRunService
    journal: VoiceToolJournal
    cutover: LegacyCutover
    approved_queries: frozenset[str]

    def approve_query(self, connection: AuthenticatedConnection, query: str) -> bool:
        self.credentials.require(connection, RunPermission.SUBMIT)
        return query in self.approved_queries
