"""The out-of-band loop — the thing the in-band hooks structurally cannot be.

It runs as its own process with its own wall clock. On every tick it asks the
backend for each unit's liveness and acts on `hung`: harvest the partial work,
then reap. Because it talks only to the SupervisionBackend contract, the same loop
drives a laptop's local processes or a fleet backend — only the backend changes.
"""

from __future__ import annotations

import signal as _signal
import time
from pathlib import Path

from .events import log_event


class Watcher:
    def __init__(self, backend, cfg):
        self.backend = backend
        self.cfg = cfg
        self._stop = False

    def request_stop(self, *_a):
        self._stop = True

    def tick(self, now: float | None = None):
        now = now if now is not None else time.time()
        observed = []
        for unit in self.backend.list_units():
            if unit.status in ("killed", "dead_letter", "respawned", "exited"):
                continue
            sig = self.backend.liveness(unit.id, now)
            observed.append((unit.id, sig.state, round(sig.silence_s, 1)))
            if sig.state == "hung":
                self._handle_hung(unit, sig)
            elif sig.state == "suspect":
                log_event(self.cfg.event_log_path, "suspect",
                          unit_id=unit.id, silence_s=round(sig.silence_s, 1))
            elif sig.state == "exited":
                log_event(self.cfg.event_log_path, "exited", unit_id=unit.id)
        return observed

    def _handle_hung(self, unit, sig):
        log_event(self.cfg.event_log_path, "hung", unit_id=unit.id,
                  silence_s=round(sig.silence_s, 1), label=unit.label)
        brief = None
        try:
            brief = self.backend.harvest(unit.id)        # harvest BEFORE reaping
            self._persist_brief(unit.id, brief)
        except Exception as e:
            log_event(self.cfg.event_log_path, "harvest_error", unit_id=unit.id, error=str(e))
        result = self.backend.reap(unit.id, reason="hung")
        if self.cfg.respawn_enabled and result.signalled:
            try:
                from .respawn import respawn_unit
                respawn_unit(unit, brief, self.cfg, self.backend)
            except Exception as e:
                log_event(self.cfg.event_log_path, "respawn_error", unit_id=unit.id, error=str(e))

    def _persist_brief(self, unit_id, brief):
        if brief is None:
            return
        d = Path(self.cfg.state_dir) / "briefs"
        d.mkdir(parents=True, exist_ok=True)
        try:
            text = brief.as_prompt() if hasattr(brief, "as_prompt") else str(brief)
        except Exception:
            text = getattr(brief, "raw_excerpt", "") or ""
        (d / f"{unit_id}.md").write_text(text, encoding="utf-8")

    def run(self, max_ticks: int | None = None):
        for name in ("SIGTERM", "SIGINT"):
            s = getattr(_signal, name, None)
            if s is not None:
                try:
                    _signal.signal(s, self.request_stop)
                except (ValueError, OSError):
                    pass  # not main thread / unsupported on platform
        log_event(self.cfg.event_log_path, "watch_start", dry_run=self.cfg.dry_run,
                  poll_s=self.cfg.poll_interval_s, hung_threshold_s=self.cfg.hung_threshold_s)
        ticks = 0
        while not self._stop:
            try:
                self.tick()
            except Exception as e:
                log_event(self.cfg.event_log_path, "tick_error", error=str(e))
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            slept = 0.0
            while slept < self.cfg.poll_interval_s and not self._stop:
                step = min(0.5, self.cfg.poll_interval_s - slept)
                time.sleep(step)
                slept += step
        log_event(self.cfg.event_log_path, "watch_stop", ticks=ticks)
