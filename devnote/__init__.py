"""agent-reaper — out-of-band stuck-agent supervision.

The harness's built-in stall handling is all *in-band*: hooks fire on tool calls,
retries fire on Agent completion. A silent hang produces neither, so nothing reacts.
This package is the missing out-of-band clock — a separate process that watches
elapsed time independently and acts when an agent goes quiet past a threshold.

Safety posture (this runs on a box shared with other users):
  - Acts ONLY on units in its own registry. Never pattern-matches process names.
  - Verifies UID ownership before any OS signal.
  - dry_run is the default. Arming is an explicit, logged choice.
  - Caps kills per time window. Every action is appended to an audit log.
"""

__version__ = "0.1.0"
