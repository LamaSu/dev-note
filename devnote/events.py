"""Append-only audit log. Every decision, refusal, and signal is recorded here.

A reaper that can kill things on a shared box must be fully accountable after the
fact: what did it see, what did it decide, what did it do. One JSON object per line.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


def log_event(path: str | os.PathLike, kind: str, **fields) -> dict:
    """Append one event. `kind` is e.g. detect|suspect|hung|reap_dryrun|reap_signal|
    reap_refused|respawn|dead_letter|heartbeat|register. Returns the row written."""
    row = {"ts": time.time(), "iso": _iso(), "kind": kind, **fields}
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    return row


def _iso() -> str:
    import datetime
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def read_events(path: str | os.PathLike, kind: str | None = None, limit: int = 200) -> list[dict]:
    p = Path(path)
    if not p.is_file():
        return []
    rows: list[dict] = []
    with open(p, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if kind is None or row.get("kind") == kind:
                rows.append(row)
    return rows[-limit:]
