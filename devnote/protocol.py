"""The supervision PROTOCOL — the seam that lets the same primitives scale.

dev-note's local core (registry + liveness + killpg) is the right tool for an
individual or a small orchestrator: a handful to a few hundred agents on one or a
few boxes. It does NOT scale to a million agents, and it shouldn't try — at that
size you cannot centrally watch every agent, and you must not "kill" an actor you
can't prove is dead.

This module defines the contract that survives both regimes. A backend implements
eight methods; the watcher, the policy, and the report layer stay identical whether
the backend is `LocalProcessBackend` (this repo) or a company's million-agent
orchestrator, or PCC's gateway.

The two primitives that make it scale-safe — and that a large system plugs in at
its PROTOCOL layer, not as a sidecar:

  * LEASE   — holding anything (a task, a resource, an escrow) requires a heartbeat.
              Silence => the lease lapses and the claim is reclaimable. Liveness
              stops being something you watch for and becomes something that expires.
  * FENCE   — every reassignment mints a strictly higher token. The gateway rejects
              any action carrying a superseded token, so a hung agent that wakes up
              is harmless by construction. You never have to win a race to kill it.

`FencingGate.check()` is the single call a high-scale gateway makes on every action.
That one check is the "protection at the protocol layer" — it is O(1), needs no
central watcher, and is what keeps a billion-agent system from double-acting when
agents wedge.
"""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field

from .registry import Registry, SupervisedUnit
from .liveness import classify, silence_seconds, process_alive
from .kill import reap as _local_reap, KillDecision


# --------------------------------------------------------------------- value types
@dataclass
class LivenessSignal:
    unit_id: str
    alive: bool
    silence_s: float
    lease_expired: bool
    state: str                       # alive | suspect | hung | exited


@dataclass
class HandoffBrief:
    """What a fresh agent needs to continue. Deterministic where possible.

    In the local backend, files/commands/errors are parsed from the transcript and
    git diff; completed/pending are an optional LLM step. In a PCC-style backend the
    attested step-completeness trail fills these exactly — no inference needed."""
    unit_id: str
    label: str
    files_touched: list[str] = field(default_factory=list)
    commands_run: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    completed_steps: list[str] = field(default_factory=list)
    pending_steps: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    redacted: bool = False
    fencing_token: int = 0
    raw_excerpt: str = ""

    def as_prompt(self) -> str:
        L = [f"# Handoff brief for {self.label or self.unit_id} (fence #{self.fencing_token})"]
        if self.completed_steps:
            L.append("## Already done (do NOT redo):\n" + "\n".join(f"- {s}" for s in self.completed_steps))
        if self.pending_steps:
            L.append("## Remaining:\n" + "\n".join(f"- {s}" for s in self.pending_steps))
        if self.blockers:
            L.append("## Blockers hit:\n" + "\n".join(f"- {s}" for s in self.blockers))
        if self.files_touched:
            L.append("## Files in flight:\n" + "\n".join(f"- {p}" for p in self.files_touched))
        if self.errors:
            L.append("## Last errors:\n" + "\n".join(f"- {e}" for e in self.errors[:5]))
        return "\n\n".join(L)


@dataclass
class ReapResult:
    unit_id: str
    acted: bool                      # did anything actually happen
    signalled: bool                  # did we send a real signal / release a real lease
    fencing_token: int
    reason: str


# --------------------------------------------------------------------- the contract
class SupervisionBackend(abc.ABC):
    """Implement these eight methods and the rest of dev-note works unchanged.

    A scale backend (orchestrator / PCC) typically implements register/heartbeat/
    fence/is_current as cheap gateway operations and makes reap a lease-reclaim
    rather than an OS kill."""

    @abc.abstractmethod
    def register(self, unit: SupervisedUnit) -> str: ...

    @abc.abstractmethod
    def heartbeat(self, unit_id: str, token: int) -> float:
        """Renew the lease. MUST reject (raise) a stale token. Returns new expiry epoch."""

    @abc.abstractmethod
    def liveness(self, unit_id: str, now: float | None = None) -> LivenessSignal: ...

    @abc.abstractmethod
    def list_units(self, status: str | None = None) -> list[SupervisedUnit]: ...

    @abc.abstractmethod
    def fence(self, unit_id: str) -> int:
        """Mint a strictly higher token; the previous holder is now superseded."""

    @abc.abstractmethod
    def is_current(self, unit_id: str, token: int) -> bool:
        """The gateway hot path. True iff `token` is the live fence for the unit."""

    @abc.abstractmethod
    def harvest(self, unit_id: str) -> HandoffBrief: ...

    @abc.abstractmethod
    def reap(self, unit_id: str, reason: str = "hung") -> ReapResult: ...


# --------------------------------------------------------------------- gateway hot path
class FencingGate:
    """The million/billion-scale protection. A gateway wraps every state-changing
    action with `check()`. A superseded (hung-then-respawned) agent's calls are
    refused here — no central watcher, no kill race, O(1)."""

    def __init__(self, backend: SupervisionBackend):
        self.backend = backend

    def check(self, unit_id: str, token: int) -> bool:
        return self.backend.is_current(unit_id, token)

    def guard(self, unit_id: str, token: int) -> None:
        if not self.check(unit_id, token):
            raise StaleFenceError(f"unit {unit_id} token {token} is superseded")


class StaleFenceError(RuntimeError):
    pass


# --------------------------------------------------------------------- reference backend
class LocalProcessBackend(SupervisionBackend):
    """The individual / small-orchestrator implementation, over the local registry
    and killpg. Fencing is enforced in-process; reaping is a real OS signal."""

    def __init__(self, registry: Registry, cfg, kill_times: list[float] | None = None):
        self.registry = registry
        self.cfg = cfg
        self.kill_times = kill_times if kill_times is not None else []

    def register(self, unit: SupervisedUnit) -> str:
        return self.registry.add(unit).id

    def heartbeat(self, unit_id: str, token: int) -> float:
        unit = self.registry.get(unit_id)
        if unit is None:
            raise KeyError(unit_id)
        if token != unit.fencing_token:
            raise StaleFenceError(f"stale token {token} != {unit.fencing_token} for {unit_id}")
        self.registry.heartbeat(unit_id)
        return self.registry.get(unit_id).lease_expires_at()

    def liveness(self, unit_id: str, now: float | None = None) -> LivenessSignal:
        now = now if now is not None else time.time()
        unit = self.registry.get(unit_id)
        if unit is None:
            raise KeyError(unit_id)
        return LivenessSignal(
            unit_id=unit_id,
            alive=process_alive(unit),
            silence_s=silence_seconds(unit, now),
            lease_expired=now > unit.lease_expires_at(),
            state=classify(unit, self.cfg, now),
        )

    def list_units(self, status: str | None = None) -> list[SupervisedUnit]:
        return self.registry.list(status)

    def fence(self, unit_id: str) -> int:
        token = self.registry.next_fencing_token()
        self.registry.update(unit_id, fencing_token=token)
        return token

    def is_current(self, unit_id: str, token: int) -> bool:
        unit = self.registry.get(unit_id)
        return unit is not None and unit.fencing_token == token

    def harvest(self, unit_id: str) -> HandoffBrief:
        unit = self.registry.get(unit_id)
        if unit is None:
            raise KeyError(unit_id)
        try:
            from .harvest import harvest_unit  # richer, optional
            return harvest_unit(unit, self.cfg)
        except Exception:
            # minimal fallback: tails of the declared liveness sources
            excerpt = _tail_sources(unit.liveness_sources)
            return HandoffBrief(unit_id=unit_id, label=unit.label,
                                fencing_token=unit.fencing_token, raw_excerpt=excerpt)

    def reap(self, unit_id: str, reason: str = "hung") -> ReapResult:
        unit = self.registry.get(unit_id)
        if unit is None:
            raise KeyError(unit_id)
        decision: KillDecision = _local_reap(unit, self.cfg, self.registry, self.kill_times, reason=reason)
        after = self.registry.get(unit_id)
        return ReapResult(unit_id=unit_id, acted=decision.allowed,
                          signalled=decision.would_signal, fencing_token=after.fencing_token,
                          reason=decision.reason)


def _tail_sources(paths: list[str], n: int = 60) -> str:
    out = []
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()[-n:]
            out.append(f"--- {p} ---\n" + "".join(lines))
        except OSError:
            continue
    return "\n".join(out)
