# ADR-005: Local security boundary
Status: accepted for development only.

Bind to localhost; reject missing or foreign browser origins. This does not provide
remote authentication. Never expose arbitrary command execution, long-lived provider
keys or unauthenticated remote access. Treat speech as untrusted input and preserve
agent tool permissions and human approvals. Secure remote deployment is out of scope.

## Related decision

This ADR remains accepted. [009](009-agent-run-lifecycle.md): Adds future per-run authorization requirements without relaxing this local-only boundary.
