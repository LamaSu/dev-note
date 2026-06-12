# dev note

**Death Note for hung agents.** Write the name in the note, the agent dies.

dev-note is an **out-of-band supervisor** for AI-agent orchestrations. When an agent
goes *silently* hung — no error, no exit, just stops — the orchestration's own
machinery can't see it, because every in-band safeguard (a hook on a tool call, a
retry on an Agent return) only fires when the agent is still *doing* something. A
hang is the absence of activity. You cannot detect silence with a mechanism that
only runs on activity.

dev-note is the missing piece: a separate process with its own wall clock that
notices the silence, harvests the agent's partial work, reaps it with a **scoped,
uid-checked** signal, and respawns a fresh agent guarded by a **fencing token** so
the original — if it ever wakes up — can do no harm.

It runs on a plain laptop. **No GPU, no cloud, no Spark.** Pure-Python stdlib core.

---

## The five primitives

| Primitive | What it does | Why it scales |
|-----------|--------------|---------------|
| **LEASE** | Holding any claim (a task, a resource) requires a heartbeat. Silence ⇒ the lease lapses. | Liveness becomes *expiry*, not watching. No central watcher needed. |
| **FENCE** | Every respawn mints a strictly higher token; a superseded agent's actions are rejected at the gateway. | A hung-then-revived "zombie" is harmless **by construction** — no kill race. |
| **HARVEST** | Capture the silent agent's files / commands / errors into a handoff brief *before* reaping. | Deterministic where the trail is structured; the next agent resumes, not restarts. |
| **REAP** | Terminate the agent's process group — but only units in the registry, only ones we own, capped per window, dry-run by default. | Safe on a shared box: never `pkill`, never a process you don't own. |
| **RESPAWN** | Start a fresh agent with the brief, fenced, capped at N attempts, then dead-letter. | Bounded recovery; no infinite respawn storms. |

The same five primitives drive an individual's laptop and, behind a `SupervisionBackend`
interface, a company's million-agent fleet — see [the scaling story](#how-it-scales).

---

## Quickstart

```bash
pip install -e .

# watch (dry-run by default — logs what it WOULD reap, signals nothing)
devnote watch

# see the registry and each unit's live state
devnote list

# manually reap a unit — "write the name"
devnote write <unit-id> --arm

# see what would be handed to a replacement agent
devnote brief <unit-id>
```

Inside a worker, take a lease and keep it alive:

```python
from devnote.config import Config
from devnote.heartbeat import Heartbeat

hb = Heartbeat.register_self(Config.load("reaper.toml"), label="crawler")
while working:
    hb.beat()          # renew the lease (one file touch)
    ...
hb.done()
```

### See it happen

```bash
python demo/demo_hang.py
```

A worker heartbeats three times, then hangs. dev-note detects the silence, writes a
handoff brief, and reaps it (real SIGTERM on Linux/macOS; detection-only on Windows,
which has no process groups).

---

## Safety (it kills things, so this matters)

dev-note signals processes. On a shared machine that is a serious responsibility, so
the killer refuses unless **every** rail passes:

1. The unit is in **its own registry** — it never scans the process table or matches names.
2. It **owns** the process — the pid's uid is in `allowed_uids`.
3. The per-window **kill cap** is not exceeded.
4. `dry_run` is **the default** — arming is an explicit, logged choice (`--arm` / `REAPER_DRY_RUN=0`).

It signals the **process group** (so children die with the agent), never a bare pid,
and every decision — including every refusal — is appended to an audit log.

---

## How it scales

dev-note's local core is right for an individual or a small orchestrator. It does not
try to scale to a million agents by watching harder — at that size you *can't* watch
everything, and you *mustn't* "kill" an actor you can't prove is dead.

The contract that survives both regimes is `SupervisionBackend` (8 methods). A fleet
backend implements `heartbeat`/`fence`/`is_current` as cheap gateway operations and
makes `reap` a lease-reclaim instead of an OS kill. The one call a high-scale gateway
makes on **every action** is `FencingGate.check(unit, token)` — O(1), no central
watcher, and the thing that keeps a billion-agent system from double-acting when
agents wedge.

- **Individual:** pure dev-note on one box.
- **Small orchestrator:** + Arize Phoenix (liveness from OTel + a trace UI) + Composio (agents reach SaaS without OAuth churn).
- **Enterprise:** + guild.ai (governance/audit), TrueFoundry (gateway/fencing at fleet traffic), ClickHouse (reap analytics), OpenUI Lang (live reports).

See `docs/enterprise-design.md` and `docs/pcc-native-design.md` for the full layering
and the protocol-native PCC integration.

---

## Status

Core is built and tested: **25 tests** green on Linux (24 on Windows + 1 POSIX-only
skip), including the real end-to-end reap of a live process. Pure stdlib — the only
dependencies are *optional* integrations (GLiNER redaction, Phoenix/OTel, OpenUI Lang).

Built at a hackathon, 2026-06-12.
