"""Configuration with safe defaults.

Loaded from (lowest to highest precedence): built-in defaults -> reaper.toml ->
REAPER_* environment variables. The two safety-critical defaults — dry_run=True
and allowed_uids=={current uid} — are chosen so that a misconfigured or
zero-config reaper cannot kill anything, and cannot kill anything it doesn't own.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, asdict
from pathlib import Path


def _default_uid() -> int:
    # os.getuid is absent on Windows; -1 is a sentinel meaning "uid checks unavailable".
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid else -1


@dataclass
class Config:
    # --- timing (seconds) ---
    poll_interval_s: float = 30.0
    suspect_threshold_s: float = 300.0   # 5 min silent -> suspect (watch closely)
    hung_threshold_s: float = 600.0      # 10 min silent -> hung (reap)
    lease_ttl_s: float = 120.0           # a heartbeat extends a unit's lease this far
    grace_period_s: float = 10.0         # SIGTERM -> wait -> SIGKILL

    # --- safety rails ---
    dry_run: bool = True                 # default: log "WOULD kill", never signal
    allowed_uids: list[int] = field(default_factory=lambda: [_default_uid()])
    max_kills_per_window: int = 5
    kill_window_s: float = 3600.0
    require_uid_match: bool = True        # refuse to signal a pid we don't own
    allow_without_uid_check: bool = False # arm on a platform with no uid model (e.g. Windows)

    # --- respawn ---
    respawn_enabled: bool = False         # off by default; reaping alone is the v1 core
    respawn_max: int = 3
    respawn_backoff_s: float = 30.0

    # --- paths ---
    state_dir: str = "state"
    registry_filename: str = "registry.json"
    event_log_filename: str = "events.jsonl"

    # --- liveness sources (mtime of these signals an agent is alive) ---
    # Glob-style absolute or project-relative paths checked per unit in addition to
    # the unit's own declared sources and its heartbeat.
    global_liveness_globs: list[str] = field(default_factory=list)

    # --- integrations (all optional / graceful-degrade) ---
    redaction_enabled: bool = False       # local GLiNER pass before excerpts are logged/sent
    redaction_model: str = "fastino/gliner2-base-v1"
    phoenix_otlp_endpoint: str = ""       # e.g. http://dgx-spark:6006/v1/traces ; empty = off
    notify_cmd: str = ""                  # shell cmd run on dead-letter; {msg} substituted

    @property
    def registry_path(self) -> Path:
        return Path(self.state_dir) / self.registry_filename

    @property
    def event_log_path(self) -> Path:
        return Path(self.state_dir) / self.event_log_filename

    # ------------------------------------------------------------------ loaders
    @classmethod
    def load(cls, toml_path: str | os.PathLike | None = None) -> "Config":
        cfg = cls()
        if toml_path and Path(toml_path).is_file():
            with open(toml_path, "rb") as fh:
                data = tomllib.load(fh)
            cfg = cls._merge(cfg, data.get("reaper", data))
        cfg = cls._apply_env(cfg)
        cfg._validate()
        return cfg

    @staticmethod
    def _merge(cfg: "Config", data: dict) -> "Config":
        known = asdict(cfg)
        for k, v in data.items():
            if k in known:
                setattr(cfg, k, v)
        return cfg

    @staticmethod
    def _apply_env(cfg: "Config") -> "Config":
        # REAPER_DRY_RUN=0 is the explicit, deliberate way to arm the reaper.
        for name, typ in cfg.__annotations__.items():
            env = os.environ.get(f"REAPER_{name.upper()}")
            if env is None:
                continue
            cur = getattr(cfg, name)
            try:
                if isinstance(cur, bool):
                    setattr(cfg, name, env.strip().lower() in ("1", "true", "yes", "on"))
                elif isinstance(cur, float):
                    setattr(cfg, name, float(env))
                elif isinstance(cur, int):
                    setattr(cfg, name, int(env))
                elif isinstance(cur, list):
                    setattr(cfg, name, [p for p in env.split(",") if p])
                else:
                    setattr(cfg, name, env)
            except (TypeError, ValueError):
                pass  # keep the safe default on a malformed override
        return cfg

    def _validate(self) -> None:
        if self.hung_threshold_s < self.suspect_threshold_s:
            raise ValueError("hung_threshold_s must be >= suspect_threshold_s")
        if self.max_kills_per_window < 1:
            raise ValueError("max_kills_per_window must be >= 1")
        if self.poll_interval_s <= 0:
            raise ValueError("poll_interval_s must be > 0")
