"""The refusal rails. These are the tests that matter most: they prove the devnote
will NOT signal in every case where it shouldn't, deterministically on any platform.

uid plumbing is monkeypatched so the logic is exercised identically on Windows
(no /proc, no getuid) and Linux. The real killpg path is covered separately on a
POSIX host (test_signal_posix)."""

import time

import pytest

import devnote.kill as killmod
from devnote.kill import precheck, reap, KillDecision
from devnote.registry import Registry, SupervisedUnit
from devnote.events import read_events


def _own_uid(monkeypatch, uid=1000, available=True):
    monkeypatch.setattr(killmod, "uid_enforcement_available", lambda: available)
    monkeypatch.setattr(killmod, "owner_uid", lambda pid: uid)


def _proc(**kw):
    base = dict(id="p1", kind="process", pid=4321, pgid=4321, label="impl")
    base.update(kw)
    return SupervisedUnit(**base)


# ------------------------------------------------------------ the core invariant
def test_dry_run_never_signals(cfg, registry, monkeypatch):
    _own_uid(monkeypatch, uid=1000)            # passes uid gate
    cfg.dry_run = True
    u = registry.add(_proc())
    kills: list[float] = []

    d = reap(u, cfg, registry, kills, now=time.time())

    assert d.allowed is True
    assert d.would_signal is False             # the whole point
    assert registry.get("p1").status == "alive"  # not flipped to killed
    assert kills == []                          # nothing counted against the cap
    evts = read_events(cfg.event_log_path, kind="reap_dryrun")
    assert len(evts) == 1 and evts[0]["unit_id"] == "p1"


# ------------------------------------------------------------ refusal rails
def test_refuse_unknown_kind(cfg, registry, monkeypatch):
    _own_uid(monkeypatch)
    u = SupervisedUnit(id="x", kind="harness_task", task_id="t")
    u.kind = "weird"                            # bypass add()'s validation
    d = precheck(u, cfg, [], time.time())
    assert d.allowed is False and "unknown kind" in d.reason


def test_refuse_when_cap_reached(cfg, registry, monkeypatch):
    _own_uid(monkeypatch)
    cfg.dry_run = False
    cfg.max_kills_per_window = 2
    now = time.time()
    kills = [now - 1, now - 2]                  # already at cap within window
    d = precheck(_proc(), cfg, kills, now)
    assert d.allowed is False and "cap reached" in d.reason


def test_refuse_no_pid(cfg, monkeypatch):
    _own_uid(monkeypatch)
    cfg.dry_run = False
    u = _proc(pid=None, pgid=None)
    d = precheck(u, cfg, [], time.time())
    assert d.allowed is False and "no pid/pgid" in d.reason


def test_refuse_uid_mismatch(cfg, monkeypatch):
    _own_uid(monkeypatch, uid=9999)            # process owned by someone else
    cfg.dry_run = False
    cfg.allowed_uids = [1000]
    d = precheck(_proc(), cfg, [], time.time())
    assert d.allowed is False and "not in allowed" in d.reason


def test_refuse_when_owner_unverifiable(cfg, monkeypatch):
    monkeypatch.setattr(killmod, "uid_enforcement_available", lambda: True)
    monkeypatch.setattr(killmod, "owner_uid", lambda pid: None)  # can't read /proc
    cfg.dry_run = False
    d = precheck(_proc(), cfg, [], time.time())
    assert d.allowed is False and "cannot verify" in d.reason


def test_platform_without_uid_refuses_to_arm(cfg, monkeypatch):
    _own_uid(monkeypatch, available=False)     # e.g. Windows
    cfg.dry_run = False
    cfg.allow_without_uid_check = False
    d = precheck(_proc(), cfg, [], time.time())
    assert d.allowed is False                   # won't arm without uid check unless told to


def test_platform_without_uid_can_arm_when_opted_in(cfg, monkeypatch):
    _own_uid(monkeypatch, available=False)
    cfg.dry_run = False
    cfg.allow_without_uid_check = True
    d = precheck(_proc(), cfg, [], time.time())
    assert d.allowed is True and d.would_signal is True


# ------------------------------------------------------------ armed paths
def test_armed_process_signals_and_fences(cfg, registry, monkeypatch):
    _own_uid(monkeypatch, uid=1000)
    cfg.dry_run = False
    signalled = {}
    monkeypatch.setattr(killmod, "_signal_group",
                        lambda unit, c, log: signalled.update(id=unit.id))
    u = registry.add(_proc())
    before = u.fencing_token
    kills: list[float] = []

    d = reap(u, cfg, registry, kills, now=time.time())

    assert d.would_signal is True
    assert signalled == {"id": "p1"}
    after = registry.get("p1")
    assert after.status == "killed"
    assert after.fencing_token > before        # fence advances so a zombie can't act
    assert len(kills) == 1                       # counted against the cap
    assert read_events(cfg.event_log_path, kind="reap_signal")


def test_armed_harness_task_writes_stop_request(cfg, registry):
    cfg.dry_run = False                         # harness_task needs no uid gate
    u = registry.add(SupervisedUnit(id="h1", kind="harness_task", task_id="task-9"))
    kills: list[float] = []

    d = reap(u, cfg, registry, kills, now=time.time())

    assert d.would_signal is True
    req = (cfg.registry_path.parent / "stop-requests" / "task-9.json")
    assert req.is_file()
    assert registry.get("h1").status == "killed"


@pytest.mark.skipif(not hasattr(__import__("os"), "killpg"),
                    reason="POSIX process-group signalling only")
def test_signal_posix(cfg, registry):
    """Real end-to-end: spawn a process in its own group, arm, confirm it dies."""
    import os
    import subprocess
    _ = cfg
    proc = subprocess.Popen(["sleep", "120"], start_new_session=True)
    pgid = os.getpgid(proc.pid)
    cfg.dry_run = False
    cfg.require_uid_match = True
    cfg.allowed_uids = [os.getuid()]
    u = registry.add(SupervisedUnit(id="real", kind="process", pid=proc.pid, pgid=pgid))
    kills: list[float] = []

    reap(u, cfg, registry, kills, now=time.time())

    proc.wait(timeout=15)
    assert proc.poll() is not None              # actually terminated
