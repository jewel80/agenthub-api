"""Logging setup: request-scoped IDs + optional JSON formatting.

Every log line carries the current request's id (from the Request-ID
middleware) so a request can be traced across log entries. `LOG_FORMAT=json`
emits structured lines ready for log shippers (roadmap §9 builds on this).
"""
from __future__ import annotations

import json
import logging
from contextvars import ContextVar

# Current request id ("-" outside a request, e.g. startup or background jobs).
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class _RequestLogFormatter(logging.Formatter):
    """Format records as `ts level [request_id] logger :: message`."""

    def format(self, record: logging.LogRecord) -> str:
        record.request_id = request_id_var.get()
        return super().format(record)


class _JsonLogFormatter(logging.Formatter):
    """One JSON object per line: ts, level, logger, request_id, message."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": request_id_var.get(),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(log_format: str = "text") -> None:
    """Configure root logging once at startup. `json` | `text`."""
    formatter = (
        _JsonLogFormatter()
        if log_format == "json"
        else _RequestLogFormatter(
            "%(asctime)s %(levelname)s [%(request_id)s] %(name)s :: %(message)s"
        )
    )
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
