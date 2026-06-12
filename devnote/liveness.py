"""Liveness: deciding when a unit has gone silent long enough to be hung.

An agent is "alive" if it is EITHER heartbeating OR touching one of its declared
liveness sources (a telemetry JSONL, a thread file, a log). "Silence" is the time
since the most recent of those. A hang is silence past a threshold while the
process is still running — the in-band hooks can't see that; this can.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from .registry import SupervisedUnit


def newest_source_mtime(paths: list[str]) -> float | None:
    newest: float | None = None
    for raw in paths:
        p = Path(raw)
        try:
            m = p.stat().st_mtime
        except (FileNotFoundError, PermissionError, OSError):
            continue
        if newest is None or m > newest:
            newest = m
    return newest


def process_alive(unit: SupervisedUnit) -> bool:
    """True if the unit appears to still be running.

    For `process`, a real OS check. For `harness_task`/`remote` we cannot probe
    cheaply from out-of-band, so we report alive and let staleness drive the
    decision (a dead remote task simply stops updating its sources)."""
    if unit.kind != "process" or unit.pid is None:
        return True
    try:
        os.kill(unit.pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but not ours to signal — still "alive"
    except OSError:
        # Windows: os.kill(pid, 0) raises OSError if the pid is gone.
        return False


def silence_seconds(unit: SupervisedUnit, now: float | None = None) -> float:
    now = now if now is not None else time.time()
    last_activity = max(unit.last_heartbeat_at, unit.started_at)
    src = newest_source_mtime(unit.liveness_sources)
    if src is not None and src > last_activity:
        last_activity = src
    return max(0.0, now - last_activity)


def classify(unit: SupervisedUnit, cfg, now: float | None = None) -> str:
    """Return alive | suspect | hung | exited. Terminal states pass through."""
    if unit.status in ("killed", "dead_letter", "respawned", "exited"):
        return unit.status

    now = now if now is not None else time.time()

    if not process_alive(unit):
        return "exited"

    sil = silence_seconds(unit, now)
    lease_expired = now > unit.lease_expires_at()

    # Hung = silent past the hard threshold, OR lease lapsed while still running
    # and already past the suspect mark (a unit that promised to heartbeat and didn't).
    if sil >= cfg.hung_threshold_s or (lease_expired and sil >= cfg.suspect_threshold_s):
        return "hung"
    if sil >= cfg.suspect_threshold_s:
        return "suspect"
    return "alive"
