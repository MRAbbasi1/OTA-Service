from __future__ import annotations


class DomainError(Exception):
    """Domain rule violation carrying a machine-readable code.

    Codes are part of the API contract: the HTTP layer maps them to status
    codes, and they are what an operator sees when an upload or a transition is
    rejected.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
