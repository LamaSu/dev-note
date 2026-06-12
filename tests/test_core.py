"""Registry persistence/fencing + liveness classification."""

import time

from devnote.registry import Registry, SupervisedUnit
from devnote import liveness


def _unit(**kw):
    base = dict(id="u1", kind="harness_task", task_id="t1", label="x")
    base.update(kw)
    return SupervisedUnit(**base)


# ----------------------------------------------------------------- registry
def test_add_get_roundtrip(cfg):
    r = Registry(cfg.registry_path)
    r.add(_unit())
    r2 = Registry(cfg.registry_path)  # reload from disk
    got = r2.get("u1")
    assert got is not None and got.task_id == "t1"


def test_fencing_is_monotonic(cfg):
    r = Registry(cfg.registry_path)
    a = r.add(_unit(id="a", task_id="a"))
    b = r.add(_unit(id="b", task_id="b"))
    assert a.fencing_token == 1
    assert b.fencing_token == 2
    assert r.next_fencing_token() == 3


def test_duplicate_id_rejected(cfg):
    r = Registry(cfg.registry_path)
    r.add(_unit())
    try:
        r.add(_unit())
    except ValueError:
        return
    assert False, "expected ValueError on duplicate id"


def test_heartbeat_revives_suspect(cfg):
    r = Registry(cfg.registry_path)
    r.add(_unit(status="suspect"))
    r.heartbeat("u1", ts=time.time())
    assert r.get("u1").status == "alive"


def test_atomic_save_leaves_no_tmp(cfg):
    r = Registry(cfg.registry_path)
    r.add(_unit())
    tmp = cfg.registry_path.with_suffix(cfg.registry_path.suffix + ".tmp")
    assert not tmp.exists()
    assert cfg.registry_path.exists()


# ----------------------------------------------------------------- liveness
def test_silence_uses_most_recent_activity(cfg):
    now = 1000.0
    u = _unit(started_at=900.0, last_heartbeat_at=950.0)
    assert liveness.silence_seconds(u, now=now) == 50.0


def test_classify_alive_suspect_hung(cfg):
    now = 1000.0
    u = _unit(started_at=now - 100, last_heartbeat_at=now)  # old start; heartbeat drives silence
    assert liveness.classify(u, cfg, now=now) == "alive"

    u.last_heartbeat_at = now - 6  # > suspect (5), < hung (10)
    assert liveness.classify(u, cfg, now=now) == "suspect"

    u.last_heartbeat_at = now - 11  # > hung (10)
    assert liveness.classify(u, cfg, now=now) == "hung"


def test_classify_lease_expiry_escalates(cfg):
    now = 1000.0
    # lease_ttl 8: silent 9s => lease expired AND past suspect(5) => hung even though < hung(10)
    u = _unit(started_at=now - 100, last_heartbeat_at=now - 9, lease_ttl_s=8.0)
    assert liveness.classify(u, cfg, now=now) == "hung"


def test_classify_exited_for_dead_process(cfg):
    now = 1000.0
    # An almost-certainly-dead pid -> process_alive False -> exited
    u = SupervisedUnit(id="p", kind="process", pid=2_147_483_000, pgid=2_147_483_000,
                       started_at=now, last_heartbeat_at=now)
    assert liveness.classify(u, cfg, now=now) == "exited"


def test_terminal_status_passes_through(cfg):
    now = 1000.0
    u = _unit(status="killed", last_heartbeat_at=now - 9999)
    assert liveness.classify(u, cfg, now=now) == "killed"
