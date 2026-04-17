"""
Structured JSON logger for the TTS pipeline.

Every log line is a JSON object so it can be ingested directly by
Datadog, CloudWatch, ELK, or any log aggregator without a parsing step.

Usage:
    from .logger import get_logger
    log = get_logger(__name__)
    log.info("cache_hit", chunk_id=cid, voice=voice, chars=len(text))
"""

import json
import logging
import os
import time
from typing import Any


class _JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        # Any extra kwargs passed via log.info("event", key=val) land in record.__dict__
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in {
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "message",
                "taskName",
            }
        }
        payload.update(extras)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


class _ContextLogger(logging.LoggerAdapter):
    """Wraps stdlib logger so you can call log.info("event", key=val, ...)."""

    def process(self, msg: str, kwargs: dict) -> tuple:
        extra = kwargs.pop("extra", {})
        # Pull any unknown kwargs into extra so they appear in the JSON output
        for key in list(kwargs.keys()):
            if key not in ("exc_info", "stack_info", "stacklevel"):
                extra[key] = kwargs.pop(key)
        kwargs["extra"] = extra
        return msg, kwargs


def get_logger(name: str) -> _ContextLogger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(_JSONFormatter())
        logger.addHandler(handler)
        logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
        logger.propagate = False
    return _ContextLogger(logger, {})
