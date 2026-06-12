"""A subagent for the dev-note session demo: claims a task, heartbeats while it
works, and (if HANG_AT is set) silently hangs at that step to show recovery."""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from devnote.config import Config
from devnote.heartbeat import Heartbeat


def main():
    cfg = Config.load(None)
    label = os.environ.get("TASK_LABEL", "agent")
    units = int(os.environ.get("WORK_UNITS", "20"))
    hang_at = int(os.environ.get("HANG_AT", "-1"))
    hb = Heartbeat.register_self(cfg, label=label, kind="process",
                                 meta={"task": os.environ.get("TASK_DESC", "")})
    print(hb.unit_id, flush=True)
    for i in range(units):
        if i == hang_at:
            while True:           # silent hang: no heartbeat, no error, no exit
                time.sleep(3600)
        hb.beat()
        time.sleep(0.6)           # one unit of "web work"
    hb.done()


if __name__ == "__main__":
    main()
