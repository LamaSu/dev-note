"""devnote CLI — Death Note for hung agents: write the name, the agent dies.

Runs anywhere Python 3.10+ runs. No GPU, no Spark, no network required for the
core. `devnote watch` is the out-of-band loop; `devnote write <id>` is the manual
reap (the namesake).
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from .config import Config
from .registry import Registry
from .protocol import LocalProcessBackend
from .watcher import Watcher
from .liveness import classify, silence_seconds


def _load(args):
    cfg = Config.load(getattr(args, "config", None))
    if getattr(args, "arm", False):
        cfg.dry_run = False
    reg = Registry(cfg.registry_path)
    backend = LocalProcessBackend(reg, cfg)
    return cfg, reg, backend


def cmd_watch(args):
    cfg, reg, _ = _load(args)
    backend = LocalProcessBackend(reg, cfg)
    w = Watcher(backend, cfg)
    mode = "dry-run" if cfg.dry_run else "ARMED"
    print(f"devnote watch [{mode}] poll={cfg.poll_interval_s}s hung>={cfg.hung_threshold_s}s "
          f"units={len(reg)} state={cfg.state_dir}")
    if not cfg.dry_run:
        print("  ! ARMED: will signal units it owns (uid-checked). Ctrl-C to stop.")
    w.run(max_ticks=args.ticks)
    return 0


def cmd_list(args):
    cfg, reg, _ = _load(args)
    now = time.time()
    units = reg.list()
    if args.json:
        print(json.dumps([{
            "id": u.id, "kind": u.kind, "status": u.status,
            "state": classify(u, cfg, now), "silence_s": round(silence_seconds(u, now), 1),
            "fence": u.fencing_token, "label": u.label,
        } for u in units], indent=2))
        return 0
    if not units:
        print("(no units registered)")
        return 0
    print(f"{'ID':28} {'KIND':12} {'STATE':9} {'SIL(s)':>8} {'FENCE':>5}  LABEL")
    for u in units:
        print(f"{u.id:28.28} {u.kind:12} {classify(u, cfg, now):9} "
              f"{silence_seconds(u, now):8.1f} {u.fencing_token:5}  {u.label}")
    return 0


def cmd_reap(args):
    cfg, reg, backend = _load(args)
    if args.unit_id not in reg:
        print(f"no such unit: {args.unit_id}", file=sys.stderr)
        return 2
    res = backend.reap(args.unit_id, reason=args.reason or "manual")
    if res.signalled:
        verb = "REAPED"
    elif res.acted:
        verb = "would reap (dry-run — pass --arm to act)"
    else:
        verb = f"refused: {res.reason}"
    print(f"{args.unit_id}: {verb}  [fence #{res.fencing_token}]")
    return 0


def cmd_brief(args):
    cfg, reg, backend = _load(args)
    if args.unit_id not in reg:
        print(f"no such unit: {args.unit_id}", file=sys.stderr)
        return 2
    print(backend.harvest(args.unit_id).as_prompt())
    return 0


def cmd_status(args):
    cfg, reg, _ = _load(args)
    now = time.time()
    by: dict[str, int] = {}
    for u in reg.list():
        st = classify(u, cfg, now)
        by[st] = by.get(st, 0) + 1
    print(json.dumps({
        "mode": "dry-run" if cfg.dry_run else "armed",
        "units": len(reg), "by_state": by,
        "hung_threshold_s": cfg.hung_threshold_s,
        "max_kills_per_window": cfg.max_kills_per_window,
        "state_dir": cfg.state_dir,
    }, indent=2))
    return 0


def cmd_version(_args):
    import devnote
    print(devnote.__version__)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="devnote", description="Death Note for hung agents.")
    p.add_argument("--config", default="reaper.toml", help="path to reaper.toml (optional)")
    sub = p.add_subparsers(dest="cmd", required=True)

    w = sub.add_parser("watch", help="run the out-of-band watcher loop")
    w.add_argument("--arm", action="store_true", help="send real signals (default: dry-run)")
    w.add_argument("--ticks", type=int, default=None, help="stop after N ticks (default: forever)")
    w.set_defaults(func=cmd_watch)

    ls = sub.add_parser("list", help="list units and their live state")
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(func=cmd_list)

    rp = sub.add_parser("reap", aliases=["write"], help="manually reap a unit (write the name)")
    rp.add_argument("unit_id")
    rp.add_argument("--arm", action="store_true")
    rp.add_argument("--reason", default=None)
    rp.set_defaults(func=cmd_reap)

    br = sub.add_parser("brief", help="print a unit's harvested handoff brief")
    br.add_argument("unit_id")
    br.set_defaults(func=cmd_brief)

    sub.add_parser("status", help="registry summary").set_defaults(func=cmd_status)
    sub.add_parser("version", help="print version").set_defaults(func=cmd_version)

    args = p.parse_args(argv)
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
