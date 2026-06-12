"""End-to-end demo (Spark-free, runs on any laptop).

A worker heartbeats, then silently hangs. dev-note — a separate process with its
own wall clock — notices the silence the in-band hooks can't, harvests a handoff
brief, and reaps. On Linux/macOS the reap is real (the worker dies); on Windows
process-group signalling is unavailable, so it logs WOULD-reap (detection still works).
"""

import os
import sys
import subprocess
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from devnote.config import Config
from devnote.registry import Registry
from devnote.protocol import LocalProcessBackend
from devnote.watcher import Watcher
from devnote.liveness import classify, silence_seconds
from devnote.events import read_events

POSIX = hasattr(os, "killpg") and hasattr(os, "getuid")


def main():
    state = tempfile.mkdtemp(prefix="devnote-demo-")
    print(f"== dev-note demo ==  state={state}")
    print(f"   mode: {'ARMED (real reap)' if POSIX else 'dry-run (Windows: detection only, no killpg)'}\n")

    env = dict(os.environ, REAPER_STATE_DIR=state, REAPER_LEASE_TTL_S="1.5", WORKER_BEATS="3")
    worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker.py")
    proc = subprocess.Popen(
        [sys.executable, worker], env=env, stdout=subprocess.PIPE, text=True,
        start_new_session=hasattr(os, "setsid"),
    )
    unit_id = (proc.stdout.readline() or "").strip()
    if not unit_id:
        print("worker failed to start"); proc.kill(); return 1
    print(f"[spawn] worker pid={proc.pid} unit={unit_id} — will heartbeat 3x then hang\n")

    cfg = Config()
    cfg.state_dir = state
    cfg.suspect_threshold_s = 1.0
    cfg.hung_threshold_s = 2.0
    cfg.poll_interval_s = 0.5
    cfg.dry_run = not POSIX
    if hasattr(os, "getuid"):
        cfg.allowed_uids = [os.getuid()]
    reg = Registry(cfg.registry_path)
    backend = LocalProcessBackend(reg, cfg)
    watcher = Watcher(backend, cfg)

    t0 = time.time()
    for _ in range(16):
        reg.load()
        u = reg.get(unit_id)
        if u is not None:
            st = classify(u, cfg, time.time())
            flag = "  <-- reaping" if st == "hung" else ""
            print(f"[t={time.time()-t0:4.1f}s] {st:8} silence={silence_seconds(u, time.time()):4.1f}s "
                  f"status={u.status}{flag}")
        watcher.tick()
        reg.load()
        if reg.get(unit_id) and reg.get(unit_id).status == "killed":
            break
        time.sleep(0.6)

    print()
    try:
        proc.wait(timeout=6)
        print(f"[worker] exited code={proc.returncode}  <- dev-note reaped it")
    except subprocess.TimeoutExpired:
        print("[worker] still running (dry-run: dev-note logged WOULD-reap; no signal sent)")
        proc.kill()

    print("\n-- event log --")
    for e in read_events(cfg.event_log_path):
        detail = e.get("reason") or e.get("note") or ""
        print(f"  {e['iso']}  {e['kind']:14} {e.get('unit_id',''):20} {detail}")

    brief = os.path.join(state, "briefs", f"{unit_id}.md")
    if os.path.exists(brief):
        print(f"\n-- handoff brief: {brief} --")
        print(open(brief, encoding="utf-8").read()[:500])
    return 0


if __name__ == "__main__":
    sys.exit(main())
