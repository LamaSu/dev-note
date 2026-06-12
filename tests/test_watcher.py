"""Watcher loop: detects hung units, harvests, dry-run-reaps. Cross-platform
(uses harness_task units so it runs identically on Windows and Linux)."""

import os
import time

from devnote.registry import Registry, SupervisedUnit
from devnote.protocol import LocalProcessBackend
from devnote.watcher import Watcher
from devnote.events import read_events


def _stale_source(tmp_path, name="hb"):
    p = tmp_path / name
    p.write_text("x")
    old = time.time() - 9999
    os.utime(p, (old, old))
    return p, old


def test_watcher_detects_and_dryrun_reaps_hung(cfg, tmp_path):
    cfg.dry_run = True
    reg = Registry(cfg.registry_path)
    hb, old = _stale_source(tmp_path)
    reg.add(SupervisedUnit(id="w1", kind="harness_task", task_id="t1", label="worker",
                           started_at=old, last_heartbeat_at=old, liveness_sources=[str(hb)]))
    w = Watcher(LocalProcessBackend(reg, cfg), cfg)

    obs = {uid: st for uid, st, _ in w.tick(now=time.time())}

    assert obs["w1"] == "hung"
    assert read_events(cfg.event_log_path, kind="hung")
    assert read_events(cfg.event_log_path, kind="reap_dryrun")
    assert reg.get("w1").status == "alive"          # dry-run never flips to killed


def test_watcher_ignores_fresh_unit(cfg, tmp_path):
    reg = Registry(cfg.registry_path)
    hb = tmp_path / "fresh"
    hb.write_text("x")                                # mtime == now
    reg.add(SupervisedUnit(id="w2", kind="harness_task", task_id="t2",
                           label="fresh", liveness_sources=[str(hb)]))
    w = Watcher(LocalProcessBackend(reg, cfg), cfg)

    obs = {uid: st for uid, st, _ in w.tick(now=time.time())}

    assert obs["w2"] == "alive"
    assert not read_events(cfg.event_log_path, kind="hung")


def test_brief_written_on_hang(cfg, tmp_path):
    cfg.dry_run = True
    reg = Registry(cfg.registry_path)
    src = tmp_path / "agent.log"
    src.write_text("ran command X\nopened file Y\nerror: boom\n")
    old = time.time() - 9999
    os.utime(src, (old, old))
    reg.add(SupervisedUnit(id="w3", kind="harness_task", task_id="t3", label="logger",
                           started_at=old, last_heartbeat_at=old, liveness_sources=[str(src)]))
    w = Watcher(LocalProcessBackend(reg, cfg), cfg)

    w.tick(now=time.time())

    bpath = os.path.join(cfg.state_dir, "briefs", "w3.md")
    assert os.path.exists(bpath)


def test_armed_harness_reap_via_watcher(cfg, tmp_path):
    cfg.dry_run = False                               # harness_task needs no uid gate
    reg = Registry(cfg.registry_path)
    hb, old = _stale_source(tmp_path, "hb2")
    reg.add(SupervisedUnit(id="w4", kind="harness_task", task_id="task-42", label="w",
                           started_at=old, last_heartbeat_at=old, liveness_sources=[str(hb)]))
    w = Watcher(LocalProcessBackend(reg, cfg), cfg)

    w.tick(now=time.time())

    assert reg.get("w4").status == "killed"
    assert os.path.exists(os.path.join(cfg.state_dir, "stop-requests", "task-42.json"))
