"""Service layer — thin, dependency-light wrappers around the external APIs.

Each service mirrors the logic of the original Claude Desktop skill scripts
(notion_query.py, intray.py) but reads its config from environment variables
and returns plain dicts instead of printing.
"""
from __future__ import annotations


class ServiceError(Exception):
    """Raised by services on a recoverable, reportable failure.

    Routers (and a global handler) translate this into a clean JSON HTTP error
    instead of a 500 stack trace.
    """

    def __init__(self, message: str, status_code: int = 502, detail=None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.detail = detail
