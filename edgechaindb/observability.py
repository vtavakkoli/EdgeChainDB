from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
import sys
import threading
from typing import Any


_STANDARD_FIELDS = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "taskName",
}


class JsonLogFormatter(logging.Formatter):
    """Render one compact JSON object per line for Docker-friendly logs."""

    def format(self, record: logging.LogRecord) -> str:
        value: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "component": getattr(record, "component", "edgechaindb"),
            "event": getattr(record, "event", record.getMessage()),
            "message": record.getMessage(),
            "pid": record.process,
            "thread": record.threadName,
        }
        for key, item in record.__dict__.items():
            if key in _STANDARD_FIELDS or key in value or key.startswith("_"):
                continue
            try:
                json.dumps(item)
                value[key] = item
            except (TypeError, ValueError):
                value[key] = str(item)
        if record.exc_info:
            value["exception"] = self.formatException(record.exc_info)
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


_config_lock = threading.Lock()


def configure_logging() -> None:
    """Configure the EdgeChainDB logger once per process."""

    with _config_lock:
        logger = logging.getLogger("edgechaindb")
        level_name = os.getenv("EDGECHAIN_LOG_LEVEL", "INFO").upper()
        level = getattr(logging, level_name, logging.INFO)
        logger.setLevel(level)
        logger.propagate = False
        if not logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(JsonLogFormatter())
            logger.addHandler(handler)
        for handler in logger.handlers:
            handler.setLevel(level)


class StructuredLogger:
    def __init__(self, component: str) -> None:
        configure_logging()
        self.component = component
        self.logger = logging.getLogger(f"edgechaindb.{component}")

    def _write(
        self,
        level: int,
        event: str,
        message: str | None = None,
        *,
        exc_info: bool = False,
        **fields: Any,
    ) -> None:
        safe_fields: dict[str, Any] = {}
        reserved = _STANDARD_FIELDS | {"message", "asctime", "component", "event"}
        for key, value in fields.items():
            target = f"field_{key}" if key in reserved else key
            safe_fields[target] = value
        self.logger.log(
            level,
            message or event,
            extra={"component": self.component, "event": event, **safe_fields},
            exc_info=exc_info,
        )

    def debug(self, event: str, message: str | None = None, **fields: Any) -> None:
        self._write(logging.DEBUG, event, message, **fields)

    def info(self, event: str, message: str | None = None, **fields: Any) -> None:
        self._write(logging.INFO, event, message, **fields)

    def warning(self, event: str, message: str | None = None, **fields: Any) -> None:
        self._write(logging.WARNING, event, message, **fields)

    def error(
        self,
        event: str,
        message: str | None = None,
        *,
        exc_info: bool = False,
        **fields: Any,
    ) -> None:
        self._write(
            logging.ERROR,
            event,
            message,
            exc_info=exc_info,
            **fields,
        )


def get_logger(component: str) -> StructuredLogger:
    return StructuredLogger(component)
