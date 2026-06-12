"""The killer. Every refusal rail lives here.

Order of checks (any failure => refuse, no signal):
  1. unit kind is one we know how to terminate
  2. kill-rate cap for the current window not exceeded
  3. (process/remote) a pid + pgid are present
  4. (process/remote) we OWN the pid — its uid is in allowed_uids
  5. dry_run => log "WOULD signal" and stop; only an explicitly armed reaper signals

The reaper signals a process GROUP (killpg), never a bare pid, so a hung agent's
children die with it. It never matches process names; the only targets are units a
caller registered.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .registry import SupervisedUnit, Registry, VALID_KIND
from .events import log_event


@dataclass
class KillDecision:
    allowed: bool
    would_signal: bool
    reason: str


def uid_enforcement_available() -> bool:
    return hasattr(os, "getuid")


def owner_uid(pid: int) -> int | None:
    """Owner uid of a running process, or None if undeterminable.

    Linux: owner of /proc/<pid>. Other platforms have no cheap equivalent, so we
    return None and let precheck decide based on policy."""
    try:
        return os.stat(f"/proc/{pid}").st_uid
    except (FileNotFoundError, PermissionError, OSError, ValueError, TypeError):
        return None


def recent_kill_count(kill_times: list[float], now: float, window_s: float) -> int:
    return sum(1 for t in kill_times if now - t <= window_s)


def precheck(unit: SupervisedUnit, cfg, kill_times: list[float], now: float) -> KillDecision:
    if unit.kind not in VALID_KIND:
        return KillDecision(False, False, f"unknown kind '{unit.kind}'")

    used = recent_kill_count(kill_times, now, cfg.kill_window_s)
    if used >= cfg.max_kills_per_window:
        return KillDecision(False, False, f"kill cap reached ({used}/{cfg.max_kills_per_window} in window)")

    if unit.kind in ("process", "remote"):
        needs_pgid = hasattr(os, "killpg")   # POSIX reaps the group; Windows reaps the pid
        if unit.pid is None or (needs_pgid and unit.pgid is None):
            return KillDecision(False, False, "no pid/pgid recorded")

        if cfg.require_uid_match:
            if not uid_enforcement_available():
                # No uid model on this platform (Windows). Dry-run is always fine;
                # arming requires the operator to explicitly accept the missing check.
                ok = cfg.dry_run or cfg.allow_without_uid_check
                return KillDecision(
                    ok, ok and not cfg.dry_run,
                    "uid checks unavailable on platform"
                    + ("" if ok else " — set allow_without_uid_check to arm"),
                )
            owner = owner_uid(unit.pid)
            if owner is None:
                return KillDecision(False, False, "cannot verify pid owner uid")
            if owner not in cfg.allowed_uids:
                return KillDecision(False, False, f"owner uid {owner} not in allowed {cfg.allowed_uids}")

        return KillDecision(True, not cfg.dry_run, "ok")

    # harness_task: can't signal from out-of-band; emit a stop-request for an in-session helper.
    if not unit.task_id:
        return KillDecision(False, False, "no task_id recorded")
    return KillDecision(True, not cfg.dry_run, "ok (stop-request)")


def reap(unit: SupervisedUnit, cfg, registry: Registry, kill_times: list[float],
         now: float | None = None, reason: str = "hung") -> KillDecision:
    now = now if now is not None else time.time()
    log_path = cfg.event_log_path

    decision = precheck(unit, cfg, kill_times, now)

    base = dict(unit_id=unit.id, unit_kind=unit.kind, pid=unit.pid, pgid=unit.pgid,
                task_id=unit.task_id, label=unit.label, trigger=reason)

    if not decision.allowed:
        log_event(log_path, "reap_refused", reason=decision.reason, **base)
        return decision

    if not decision.would_signal:
        log_event(log_path, "reap_dryrun",
                  note="dry_run active: would signal but did not", reason=decision.reason, **base)
        return decision

    # --- armed: take the action ---
    if unit.kind in ("process", "remote"):
        _signal_group(unit, cfg, log_path)
    else:
        _write_stop_request(unit, cfg)

    token = registry.next_fencing_token()
    registry.update(unit.id, status="killed", fencing_token=token)
    kill_times.append(now)
    log_event(log_path, "reap_signal", fencing_token=token, **base)
    return decision


def _signal_group(unit: SupervisedUnit, cfg, log_path) -> None:
    pgid = unit.pgid
    if unit.host != "local":
        _ssh_kill(unit, cfg, log_path)
        return
    if not hasattr(os, "killpg"):
        _windows_terminate(unit, cfg, log_path)   # Windows has no process groups
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return  # already gone
    except PermissionError as e:
        log_event(log_path, "reap_error", unit_id=unit.id, error=f"SIGTERM denied: {e}")
        return
    deadline = time.time() + cfg.grace_period_s
    while time.time() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return  # exited cleanly on SIGTERM
        except OSError:
            return
        time.sleep(0.25)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


def _windows_terminate(unit: SupervisedUnit, cfg, log_path) -> None:
    """Windows has no process groups; os.kill(pid, SIGTERM) maps to TerminateProcess.
    Terminates the single pid (no group reaping available on this platform)."""
    try:
        os.kill(unit.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except (PermissionError, OSError) as e:
        log_event(log_path, "reap_error", unit_id=unit.id, error=f"terminate denied: {e}")


def _ssh_kill(unit: SupervisedUnit, cfg, log_path) -> None:
    # Remote kill is still scoped to the recorded process group, never a name.
    pgid = unit.pgid
    grace = int(cfg.grace_period_s)
    cmd = f"kill -TERM -{pgid} 2>/dev/null; sleep {grace}; kill -KILL -{pgid} 2>/dev/null; true"
    try:
        subprocess.run(["ssh", unit.host, cmd], timeout=grace + 30, check=False)
    except (subprocess.SubprocessError, OSError) as e:
        log_event(log_path, "reap_error", unit_id=unit.id, error=f"ssh kill failed: {e}")


def _write_stop_request(unit: SupervisedUnit, cfg) -> None:
    """Out-of-band can't call the harness TaskStop tool. Drop a request file an
    in-session helper polls and actions via TaskStop(task_id)."""
    import json
    d = Path(cfg.state_dir) / "stop-requests"
    d.mkdir(parents=True, exist_ok=True)
    req = {"task_id": unit.task_id, "unit_id": unit.id,
           "requested_at": time.time(), "reason": "reaper:hung"}
    (d / f"{unit.task_id}.json").write_text(json.dumps(req, indent=2), encoding="utf-8")
