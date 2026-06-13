"""
logger.py — Thin logging wrapper with in-memory error buffer.
All modules call get_logger(__name__) to get a namespaced logger.

The error buffer captures every WARNING+ entry so /api/lab/errors can surface
them in the iOS app without needing an external log platform.
"""
from __future__ import annotations
import collections
import logging
import sys
import time

_configured = False

# ── In-memory error ring buffer ────────────────────────────────────────────────
# Holds last 100 WARNING/ERROR entries. Thread-safe reads; appends are GIL-safe.
_error_buffer: collections.deque[dict] = collections.deque(maxlen=100)

# Callback slot — server.py wires in a push function after startup
_on_new_error: "callable | None" = None


def set_error_push_callback(fn: "callable") -> None:
    """Called by server.py to register the push-notification hook."""
    global _on_new_error
    _on_new_error = fn


def get_error_log() -> list[dict]:
    """Return buffered errors newest-first."""
    return list(reversed(_error_buffer))


class _ErrorBufferHandler(logging.Handler):
    """Captures WARNING+ records into the in-memory buffer and optionally pushes."""

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING:
            return
        try:
            entry = {
                "ts":     time.strftime("%H:%M:%S", time.localtime(record.created)),
                "level":  record.levelname,
                "logger": record.name,
                "msg":    record.getMessage(),
            }
            _error_buffer.append(entry)
            # Push only for ERROR level (not WARNING) to avoid noise
            if record.levelno >= logging.ERROR and _on_new_error is not None:
                try:
                    _on_new_error(entry)
                except Exception:
                    pass   # never let the push hook crash the logger
        except Exception:
            pass   # never let the handler crash the caller


def _configure() -> None:
    global _configured
    if _configured:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    # Attach buffer handler to root so every logger feeds it
    root = logging.getLogger()
    root.addHandler(_ErrorBufferHandler())
    _configured = True


def get_logger(name: str) -> logging.Logger:
    _configure()
    return logging.getLogger(f"rht.{name}")
