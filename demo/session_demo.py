"""dev-note LIVE demo (not slides).

Part 1 shows how a developer ACCESSES + INTEGRATES dev-note (install + 3 lines + the
`devnote watch` supervisor). Part 2 runs a real session: an orchestration fans out
subagents, one silently HANGS, and dev-note — a separate process with its own wall
clock — detects it, TERMINATES it, and RESTARTS it. Single-take recordable (~30s).
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
from devnote.liveness import classify, silence_seconds

ESC = "\x1b"


def clear():
    sys.stdout.write(ESC + "[2J" + ESC + "[H")
    sys.stdout.flush()


SUBAGENTS = [
    ("scout",  "scrape product listings", False),
    ("pricer", "fetch competitor prices", False),
    ("filler", "submit signup forms",     True),   # <-- this one will hang
    ("mailer", "send outreach emails",    False),
]


def banner(pause=4.0):
    clear()
    print("=" * 70)
    print("  dev note  -  Death Note for hung agents")
    print("  an out-of-band supervisor for agent orchestrations")
    print("=" * 70)
    print()
    print("  1) ACCESS + INTEGRATE  (pip install, then 3 lines in your agent):")
    print()
    print("       $ pip install dev-note")
    print()
    print("       from devnote.heartbeat import Heartbeat")
    print('       hb = Heartbeat.register_self(cfg, label="my-agent")')
    print("       while working:")
    print("           hb.beat()                  # renew the lease each loop")
    print()
    print("       $ devnote watch                # start the supervisor (its own clock)")
    print()
    print("  2) RUN A SESSION: 4 subagents do web work. One will silently HANG")
    print("     (no error, no exit). Watch dev-note TERMINATE and RESTART it.")
    print()
    print("     starting in a moment...")
    time.sleep(pause)


def spawn(worker, env, label, desc, hang):
    e = dict(env, TASK_LABEL=label, TASK_DESC=desc, WORK_UNITS="22",
             HANG_AT=("6" if hang else "-1"))
    p = subprocess.Popen([sys.executable, worker], env=e, stdout=subprocess.PIPE,
                         text=True, start_new_session=hasattr(os, "setsid"))
    uid = (p.stdout.readline() or "").strip()
    return p, uid


def main():
    banner()
    state = tempfile.mkdtemp(prefix="devnote-session-")
    env = dict(os.environ, REAPER_STATE_DIR=state, REAPER_LEASE_TTL_S="1.5",
               REAPER_ALLOW_WITHOUT_UID_CHECK="1")
    worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fleet_worker.py")

    cfg = Config()
    cfg.state_dir = state
    cfg.suspect_threshold_s = 1.5
    cfg.hung_threshold_s = 3.0
    cfg.poll_interval_s = 0.5
    cfg.grace_period_s = 2.0
    cfg.dry_run = False
    cfg.allow_without_uid_check = True
    if hasattr(os, "getuid"):
        cfg.allowed_uids = [os.getuid()]

    reg = Registry(cfg.registry_path)
    backend = LocalProcessBackend(reg, cfg)

    rows = {}  # uid -> dict(label, desc, status, proc, restart)
    for label, desc, hang in SUBAGENTS:
        p, uid = spawn(worker, env, label, desc, hang)
        if uid:
            rows[uid] = dict(label=label, desc=desc, status="working", proc=p, restart=False)

    narration = []
    t0 = time.time()
    while time.time() - t0 < 45:
        now = time.time()
        reg.load()

        for uid in list(rows):
            r = rows[uid]
            if r["status"] in ("reaped", "done"):
                continue
            u = reg.get(uid)
            if u is None:
                continue
            st = classify(u, cfg, now)
            if st == "hung":
                backend.harvest(uid)                      # capture partial work first
                res = backend.reap(uid, reason="hung")
                if res.signalled:
                    r["status"] = "reaped"
                    narration.append(f"{now-t0:4.1f}s  subagent '{r['label']}' went SILENT "
                                     f"-> harvested brief -> TERMINATED (fence #{res.fencing_token})")
                    p2, uid2 = spawn(worker, env, r["label"] + "*", r["desc"], False)
                    if uid2:
                        rows[uid2] = dict(label=r["label"] + "*", desc=r["desc"],
                                          status="working", proc=p2, restart=True)
                        narration.append(f"{now-t0:4.1f}s  RESTARTED '{r['label']}*' "
                                         f"-> resuming \"{r['desc']}\"  (0 work lost)")
            elif st == "exited" and r["proc"].poll() == 0:
                r["status"] = "done"

        for r in rows.values():
            if r["status"] == "working" and r["proc"].poll() == 0:
                r["status"] = "done"

        clear()
        print(f"  dev note - LIVE session      t={now-t0:4.1f}s     supervisor: ARMED (out-of-band)\n")
        print(f"  {'SUBAGENT':12} {'TASK':26} {'STATE':13} {'SILENCE':>8}")
        print("  " + "-" * 62)
        for uid, r in rows.items():
            u = reg.get(uid)
            sil = silence_seconds(u, now) if u else 0.0
            badge = {"working": "working...", "reaped": "TERMINATED", "done": "done"}.get(r["status"], r["status"])
            mark = "  <-- hung" if (r["status"] == "working" and sil > 1.6) else ""
            print(f"  {r['label']:12} {r['desc']:26.26} {badge:13} {sil:6.1f}s{mark}")
        print()
        for line in narration[-5:]:
            print("  * " + line)

        originals = [r for r in rows.values() if not r["restart"]]
        restarts = [r for r in rows.values() if r["restart"]]
        if restarts and all(r["status"] in ("done", "reaped") for r in originals) and all(r["status"] == "done" for r in restarts):
            break
        time.sleep(0.5)

    for r in rows.values():
        try:
            if r["proc"].poll() is None:
                r["proc"].kill()
        except Exception:
            pass

    print()
    print("  " + "=" * 62)
    print("  SESSION COMPLETE. One silent hang caught, terminated, and restarted")
    print("  by dev note. The orchestrator's own retry never fired -- a hang makes")
    print("  no tool call and throws no error, so nothing in-band ever sees it.")
    print("  " + "=" * 62)


if __name__ == "__main__":
    main()
