"""Errors raised by the headless AVIP client.

Wire failures still arrive as server envelopes. ``AvipError`` is local: it
does not include payload bytes, identifiers, or exception text from the gateway.
"""


class AvipError(ValueError):
    """A client-side AVIP failure with a stable code and optional field."""

    def __init__(self, code: str, field: str | None = None):
        self.code = code
        self.field = field
        super().__init__(code if field is None else f"{code}: {field}")
