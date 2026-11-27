"""Trusted, in-process credential issuer for the loopback durable-run service."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
import hashlib
from math import isfinite
import re
import secrets
from threading import Lock
from time import time


_PRINCIPAL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")


class AccessDenied(PermissionError):
    def __init__(self):
        super().__init__("Durable run access denied")


class RunPermission(StrEnum):
    SUBMIT = "submit"
    STATUS = "status"
    DISCOVER = "discover"
    RESULT = "result"
    PROGRESS = "progress"
    ATTACH = "attach"
    CANCEL = "cancel"


@dataclass(frozen=True)
class AuthenticatedConnection:
    _token_digest: bytes = field(repr=False)


@dataclass(frozen=True)
class _Grant:
    owner: str
    permissions: frozenset[RunPermission]
    expires_at: float


class LoopbackCredentials:
    """Opaque bearer grants issued by server code, with live revocation checks.

    A WebSocket handler authenticates once and holds the resulting connection
    object server side. A token is needed again after reconnect or restart.
    """

    def __init__(self, *, clock: Callable[[], float] = time):
        self._clock = clock
        self._grants: dict[bytes, _Grant] = {}
        self._lock = Lock()

    def issue(
        self, owner: str, permissions: Iterable[RunPermission], *, lifetime: float = 900,
    ) -> str:
        if not isinstance(owner, str) or _PRINCIPAL_ID.fullmatch(owner) is None:
            raise ValueError("Invalid principal ID")
        if not isfinite(lifetime) or lifetime <= 0:
            raise ValueError("Credential lifetime must be positive and finite")
        granted = frozenset(RunPermission(permission) for permission in permissions)
        token = secrets.token_urlsafe(32)
        digest = self._digest(token)
        with self._lock:
            now = self._clock()
            self._grants = {
                key: grant for key, grant in self._grants.items() if grant.expires_at > now
            }
            self._grants[digest] = _Grant(owner, granted, now + lifetime)
        return token

    def revoke(self, token: str) -> None:
        digest = self._digest(token)
        with self._lock:
            self._grants.pop(digest, None)

    def authenticate(self, token: str) -> AuthenticatedConnection:
        connection = AuthenticatedConnection(self._digest(token))
        self.require(connection)
        return connection

    def require(
        self, connection: AuthenticatedConnection | None, *permissions: RunPermission,
    ) -> str:
        if not isinstance(connection, AuthenticatedConnection):
            raise AccessDenied()
        with self._lock:
            grant = self._grants.get(connection._token_digest)
            if grant is None or grant.expires_at <= self._clock():
                raise AccessDenied()
            if any(permission not in grant.permissions for permission in permissions):
                raise AccessDenied()
            return grant.owner

    @staticmethod
    def _digest(token: str) -> bytes:
        if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
            raise AccessDenied()
        return hashlib.sha256(token.encode("ascii")).digest()
