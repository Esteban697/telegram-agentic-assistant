"""Scheduled job entrypoints (PLAN.md §2.1, §11.3).

Each job is a **one-shot process**: it scans the database, claims with `FOR UPDATE SKIP
LOCKED`, does its work and exits. There is no long-running loop and no in-process timer
(spec §11, spec §17 "Los jobs no dependen de procesos en memoria"); the timer is Cloud
Scheduler, which arrives in Phase 5.
"""

from __future__ import annotations

import json
import logging
import os
import socket
from typing import Any

_logger = logging.getLogger(__name__)


def executor_id(role: str) -> str:
    """An identity that is unique per process, for the `claimed_by` audit column."""

    return f"{role}@{socket.gethostname()}:{os.getpid()}"


def report_line(payload: dict[str, Any]) -> str:
    """The single machine-readable line a job prints for the operator and the tests."""

    return "JOB_REPORT " + json.dumps(payload, ensure_ascii=False, sort_keys=True)


__all__ = ["executor_id", "report_line"]
