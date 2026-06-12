"""The registry: the set of units the reaper is allowed to act on.

This is the safety boundary. The reaper NEVER discovers targets by scanning the
process table or matching names — it only ever touches units that were explicitly
registered here. On a shared box that is the line between "kills my hung agent"
and "kills someone else's work".

Persistence is a single JSON file written atomically (temp + os.replace) so a
crash mid-write can't corrupt the registry.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

SCHEMA_VERSION = 1

# status lifecycle:
#   alive -> suspect -> hung -> (killed | dead_letter) ; or alive -> exited
VALID_STATUS = {"alive", "suspect", "hung", "killed", "respawned", "dead_letter", "exited"}
VALID_KIND = {"process", "harness_task", "remote"}


@dataclass
class SupervisedUnit:
    id: str
    kind: str                      # one of VALID_KIND
    label: str = ""
    # identity by kind:
    pid: int | None = None         # process / remote
    pgid: int | None = None        # process / remote (kill the whole group)
    task_id: str | None = None     # harness_task (TaskStop target)
    host: str = "local"            # "local" or an ssh host alias (remote)
    uid: int | None = None         # owner uid recorded at registration (advisory)
    # timing (epoch seconds):
    started_at: float = field(default_factory=time.time)
    last_heartbeat_at: float = field(default_factory=time.time)
    lease_ttl_s: float = 120.0
    # liveness:
    liveness_sources: list[str] = field(default_factory=list)  # files whose mtime == activity
    # supervision state:
    fencing_token: int = 0
    respawn_count: int = 0
    status: str = "alive"
    brief_path: str | None = None  # handoff brief produced at harvest
    meta: dict = field(default_factory=dict)

    def lease_expires_at(self) -> float:
        return self.last_heartbeat_at + self.lease_ttl_s


class Registry:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self._units: dict[str, SupervisedUnit] = {}
        self._fencing_counter: int = 0
        if self.path.is_file():
            self.load()

    # ---------------------------------------------------------------- persistence
    def load(self) -> None:
        with open(self.path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        self._fencing_counter = int(data.get("fencing_counter", 0))
        self._units = {
            uid: SupervisedUnit(**u) for uid, u in data.get("units", {}).items()
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "fencing_counter": self._fencing_counter,
            "units": {uid: asdict(u) for uid, u in self._units.items()},
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        os.replace(tmp, self.path)  # atomic on POSIX and Windows

    # ---------------------------------------------------------------- mutations
    def add(self, unit: SupervisedUnit) -> SupervisedUnit:
        if unit.kind not in VALID_KIND:
            raise ValueError(f"invalid kind: {unit.kind}")
        if unit.id in self._units:
            raise ValueError(f"duplicate unit id: {unit.id}")
        if unit.fencing_token == 0:
            unit.fencing_token = self.next_fencing_token()
        self._units[unit.id] = unit
        self.save()
        return unit

    def update(self, unit_id: str, **fields) -> SupervisedUnit:
        unit = self._units[unit_id]
        for k, v in fields.items():
            if k == "status" and v not in VALID_STATUS:
                raise ValueError(f"invalid status: {v}")
            setattr(unit, k, v)
        self.save()
        return unit

    def heartbeat(self, unit_id: str, ts: float | None = None) -> SupervisedUnit:
        unit = self._units[unit_id]
        unit.last_heartbeat_at = ts if ts is not None else time.time()
        if unit.status in ("suspect", "hung"):
            unit.status = "alive"  # it spoke; it's back
        self.save()
        return unit

    def next_fencing_token(self) -> int:
        self._fencing_counter += 1
        return self._fencing_counter

    def remove(self, unit_id: str) -> None:
        self._units.pop(unit_id, None)
        self.save()

    # ---------------------------------------------------------------- queries
    def get(self, unit_id: str) -> SupervisedUnit | None:
        return self._units.get(unit_id)

    def list(self, status: str | None = None) -> list[SupervisedUnit]:
        units = list(self._units.values())
        return [u for u in units if status is None or u.status == status]

    def __len__(self) -> int:
        return len(self._units)

    def __contains__(self, unit_id: str) -> bool:
        return unit_id in self._units
