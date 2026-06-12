# dev note — Devpost submission

> **Death Note for hung agents.** Write the name in the note, the agent dies.

## Elevator pitch

Every agent harness has retry logic. None of it fires when an agent goes *silently*
hung — no error, no exit, just stops. **dev-note** is the out-of-band supervisor that
catches exactly that failure, harvests the stuck agent's work, and reaps it safely —
on a laptop or across a million-agent fleet, behind one interface.

## Inspiration

Every safeguard in a modern agent harness is **in-band**: a hook fires on a tool call,
a retry fires when an Agent returns. But a hang is the *absence* of activity — so the
machinery meant to catch it never runs. You cannot detect silence with a mechanism that
only runs on activity. The orchestrator is also blocked *inside* the call to its own
stuck child, so it can't even look. The fix has to be a separate process with its own
wall clock. That's dev-note.

## What it does

- **Detects silent hangs out-of-band** — a watcher with its own clock notices an agent
  that stopped heartbeating while still running. (In our demo it catches a hang in
  seconds that the harness's own hooks never see.)
- **Harvests the partial work** into a handoff brief *before* killing anything, so the
  replacement resumes instead of restarting.
- **Reaps safely** — kills only registered units, only ones it owns (uid-checked), capped
  per window, **dry-run by default**, signalling the process group not a bare pid. Safe
  to run on a shared machine.
- **Fences zombies** — every respawn mints a higher token; if the "dead" agent wakes up,
  the gateway rejects its actions. No kill race.

## How we built it

Pure-Python stdlib core — **no GPU, no cloud, runs on any laptop**. Five primitives:
**LEASE** (heartbeat or your claim lapses) → **FENCE** (a superseded token is rejected at
the gateway) → **HARVEST** (deterministic partial-work capture) → **REAP** (scoped,
uid-checked, dry-run-default) → **RESPAWN** (fenced, capped, dead-lettered). The correct
ordering — **fence first, harvest second, reap last** — makes the kill provably race-free.

The same five primitives scale because of one seam: a `SupervisionBackend` interface (8
methods). The local backend uses `killpg`; a fleet backend implements `heartbeat`/`fence`/
`is_current` as cheap gateway ops and makes `reap` a lease-reclaim. The one call a
high-scale gateway makes on **every action** is `FencingGate.check(unit, token)` — O(1),
no central watcher. That's the protection that scales to a billion agents.

**Sponsor tools** snap into the enterprise tier behind that interface:
- **Composio** — supervised agents reach SaaS with managed auth (removes the whole class of hangs where an agent wedges behind an OAuth screen).
- **Langfuse / Arize Phoenix** — Claude Code emits OpenTelemetry natively; we use the last span timestamp as the liveness signal and the trace UI to show *why* an agent stalled.
- **ClickHouse** — reap/lease analytics at fleet scale.
- **guild.ai** — governance, audit, and a second-opinion overseer.
- **TrueFoundry** — the agent gateway where the fencing check lives for fleet traffic.
- **OpenUI Lang (thesys)** — renders the reap/handoff reports as live UI.

## Challenges

- **Silent hangs are invisible by construction** to anything in-band — the core design
  insight, and why a separate-process wall clock is non-negotiable.
- **The zombie / kill-race**: you can't prove a silent agent is dead, so killing-then-
  reassigning corrupts state. Fencing tokens make the stale actor inert instead.
- **Killing safely on a shared box**: never `pkill`, never a pid you don't own — every
  refusal is audited, and arming is an explicit, logged choice.

## Accomplishments

- **25 tests green on Linux** (24 on Windows + 1 POSIX-only), including a real end-to-end
  reap of a live process.
- One interface that runs identically for an individual and a fleet.
- Ships Spark-free; the heavy build (and two design docs) ran in parallel on a DGX, but
  the product needs none of it.

## What's next

- Native integration into a protocol substrate so large orchestrations never wedge
  (leases + fencing enforced at the gateway, safe-state handoff for physical actuators).
- Richer harvest (git-diff + transcript parse + optional local GLiNER PII redaction) and
  the respawn loop wired to the trace backend.

## Built with

`python` · `opentelemetry` · Composio · Langfuse · Arize Phoenix · ClickHouse · guild.ai ·
TrueFoundry · OpenUI Lang · `git`

## Links

- **Repo:** https://github.com/LamaSu/dev-note
- **Demo:** `python demo/demo_hang.py` (a worker hangs; dev-note detects, harvests, reaps)
