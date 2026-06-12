"""Heartbeat / lease client — what a supervised agent uses to prove it is alive.

The lease is a file mtime, not a row in the shared registry. An agent renews its
lease by touching `state/heartbeats/<unit_id>`; the watcher reads that file's mtime.
This avoids every process contending to write one registry JSON, and it means the
lease primitive is trivial to implement in any language (touch a file) — which is
exactly what a larger system needs when thousands of agents heartbeat at once.

Usage inside a worker:

    from devnote.config import Config
    from devnote.heartbeat import Heartbeat
    hb = Heartbeat.register_self(Config.load("reaper.toml"), label="crawler")
    while working:
        hb.beat()        # cheap: one touch
        ... do a chunk ...
    hb.done()            # deregister cleanly
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from .registry import Registry, SupervisedUnit


def _slug(s: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", s).strip("-")[:48] or "unit"


class Heartbeat:
    def __init__(self, state_dir: str | os.PathLike, unit_id: str):
        self.state_dir = str(state_dir)
        self.unit_id = unit_id
        self.hb_path = Path(state_dir) / "heartbeats" / unit_id
        self.hb_path.parent.mkdir(parents=True, exist_ok=True)
        self.beat()

    def beat(self) -> None:
        # touch == renew the lease; the watcher reads this file's mtime as liveness
        now = time.time()
        self.hb_path.touch()
        try:
            os.utime(self.hb_path, (now, now))
        except OSError:
            pass

    def done(self) -> None:
        reg = Registry(Path(self.state_dir) / "registry.json")
        if self.unit_id in reg:
            reg.update(self.unit_id, status="exited")

    @classmethod
    def register_self(cls, cfg, label: str, kind: str = "process",
                      lease_ttl_s: float | None = None,
                      liveness_sources: list[str] | None = None,
                      task_id: str | None = None, meta: dict | None = None) -> "Heartbeat":
        reg = Registry(cfg.registry_path)
        pid = os.getpid()
        pgid = os.getpgrp() if hasattr(os, "getpgrp") else None
        uid = os.getuid() if hasattr(os, "getuid") else None

        # unique id even if two agents share a label
        n = 0
        while True:
            candidate = f"{_slug(label)}-{pid}" + (f"-{n}" if n else "")
            if candidate not in reg:
                break
            n += 1
        unit_id = candidate

        hb_path = Path(cfg.state_dir) / "heartbeats" / unit_id
        sources = [str(hb_path)] + list(liveness_sources or [])
        unit = SupervisedUnit(
            id=unit_id, kind=kind, label=label,
            pid=pid if kind == "process" else None,
            pgid=pgid if kind == "process" else None,
            task_id=task_id, uid=uid,
            lease_ttl_s=lease_ttl_s if lease_ttl_s is not None else cfg.lease_ttl_s,
            liveness_sources=sources, meta=meta or {},
        )
        reg.add(unit)
        return cls(cfg.state_dir, unit_id)
