"""A fake supervised agent: heartbeats a few times, then goes silent (hangs).

Reads its state dir + thresholds from REAPER_* env (set by the demo). Prints its
unit id on the first stdout line so the demo can follow it."""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from devnote.config import Config
from devnote.heartbeat import Heartbeat


def main():
    cfg = Config.load(None)  # picks up REAPER_STATE_DIR / REAPER_LEASE_TTL_S from env
    beats = int(os.environ.get("WORKER_BEATS", "3"))
    hb = Heartbeat.register_self(cfg, label=os.environ.get("WORKER_LABEL", "crawler"))
    print(hb.unit_id, flush=True)
    for _ in range(beats):
        hb.beat()
        time.sleep(0.6)
    # The hang: stop heartbeating but keep the process running. The in-band world
    # sees nothing wrong (no error, no exit). dev-note's wall clock does.
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
