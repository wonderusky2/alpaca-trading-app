"""
logger.py — Thin logging wrapper.
All modules call get_logger(__name__) to get a namespaced logger.
"""
from __future__ import annotations
import logging
import sys

_configured = False


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
    _configured = True


def get_logger(name: str) -> logging.Logger:
    _configure()
    return logging.getLogger(f"rht.{name}")
