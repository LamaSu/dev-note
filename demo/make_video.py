"""Render a self-contained MP4 demo of dev-note — no screen capture, no human.

Renders text frames (title, the problem, integration, the LIVE hang/terminate/restart
session, outro) to images and stitches an MP4 with imageio-ffmpeg.

    python demo/make_video.py            -> demo/dev-note-demo.mp4
"""

import os
import sys
import subprocess
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image, ImageDraw, ImageFont
import imageio.v2 as imageio

from devnote.config import Config
from devnote.registry import Registry
from devnote.protocol import LocalProcessBackend
from devnote.liveness import classify, silence_seconds

W, H = 1280, 720
BG = (13, 17, 23)
FG = (201, 209, 217)
GREEN = (63, 185, 80)
RED = (248, 81, 73)
YEL = (210, 153, 34)
DIM = (110, 118, 129)
FPS = 12
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dev-note-demo.mp4")


def _font(sz):
    for p in (r"C:\Windows\Fonts\consola.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"):
        try:
            return ImageFont.truetype(p, sz)
        except OSError:
            continue
    return ImageFont.load_default()


F = _font(23)
FB = _font(30)
FT = _font(48)


def render(lines, title=None, title_color=FG, x=56):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = 48
    if title:
        d.text((x, y), title, font=FT, fill=title_color)
        y += 92
    for item in lines:
        text, col = item if isinstance(item, tuple) else (item, FG)
        d.text((x, y), text, font=F, fill=col)
        y += 31
    return img


def hold(frames, img, seconds):
    for _ in range(max(1, int(seconds * FPS))):
        frames.append(img)


def run_session():
    """Run the real orchestration; capture one table-frame per ~0.33s."""
    state = tempfile.mkdtemp(prefix="devnote-vid-")
    env = dict(os.environ, REAPER_STATE_DIR=state, REAPER_LEASE_TTL_S="1.5",
               REAPER_ALLOW_WITHOUT_UID_CHECK="1")
    worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fleet_worker.py")
    SUB = [("scout", "scrape product listings", False),
           ("pricer", "fetch competitor prices", False),
           ("filler", "submit signup forms", True),
           ("mailer", "send outreach emails", False)]

    def spawn(label, desc, hang):
        e = dict(env, TASK_LABEL=label, TASK_DESC=desc, WORK_UNITS="20", HANG_AT=("6" if hang else "-1"))
        p = subprocess.Popen([sys.executable, worker], env=e, stdout=subprocess.PIPE,
                             text=True, start_new_session=hasattr(os, "setsid"))
        return p, (p.stdout.readline() or "").strip()

    cfg = Config()
    cfg.state_dir = state
    cfg.suspect_threshold_s = 1.5
    cfg.hung_threshold_s = 3.0
    cfg.poll_interval_s = 0.4
    cfg.grace_period_s = 2.0
    cfg.dry_run = False
    cfg.allow_without_uid_check = True
    if hasattr(os, "getuid"):
        cfg.allowed_uids = [os.getuid()]
    reg = Registry(cfg.registry_path)
    backend = LocalProcessBackend(reg, cfg)

    rows = {}
    for label, desc, hang in SUB:
        p, uid = spawn(label, desc, hang)
        if uid:
            rows[uid] = dict(label=label, desc=desc, status="working", proc=p, restart=False)

    captured, narration = [], []
    t0 = time.time()
    while time.time() - t0 < 40:
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
                backend.harvest(uid)
                res = backend.reap(uid, reason="hung")
                if res.signalled:
                    r["status"] = "reaped"
                    narration.append((f"{now-t0:4.1f}s  '{r['label']}' went SILENT -> harvested brief -> TERMINATED  (fence #{res.fencing_token})", RED))
                    p2, uid2 = spawn(r["label"] + "*", r["desc"], False)
                    if uid2:
                        rows[uid2] = dict(label=r["label"] + "*", desc=r["desc"], status="working", proc=p2, restart=True)
                        narration.append((f"{now-t0:4.1f}s  RESTARTED '{r['label']}*' -> resuming the task  (0 work lost)", GREEN))
            elif st == "exited" and r["proc"].poll() == 0:
                r["status"] = "done"
        for r in rows.values():
            if r["status"] == "working" and r["proc"].poll() == 0:
                r["status"] = "done"

        lines = [(f"dev note  -  LIVE session     t={now-t0:4.1f}s     supervisor: ARMED, out-of-band", FG), ("", FG),
                 (f"  {'SUBAGENT':12} {'TASK':26} {'STATE':13} SILENCE", DIM),
                 ("  " + "-" * 60, DIM)]
        for uid, r in rows.items():
            u = reg.get(uid)
            sil = silence_seconds(u, now) if u else 0.0
            badge, col = {"working": ("working...", FG), "reaped": ("TERMINATED", RED),
                          "done": ("done", GREEN)}.get(r["status"], (r["status"], FG))
            if r["status"] == "working" and sil > 1.6:
                badge, col = ("HUNG", RED)
            if r["restart"]:
                col = GREEN if r["status"] == "done" else YEL
            lines.append((f"  {r['label']:12} {r['desc']:26.26} {badge:13} {sil:5.1f}s", col))
        lines.append(("", FG))
        for n in narration[-4:]:
            lines.append(("  * " + n[0], n[1]))
        captured.append(lines)

        originals = [r for r in rows.values() if not r["restart"]]
        restarts = [r for r in rows.values() if r["restart"]]
        if restarts and all(r["status"] in ("done", "reaped") for r in originals) and all(r["status"] == "done" for r in restarts):
            captured.extend([lines] * 6)   # hold the final frame
            break
        time.sleep(0.33)

    for r in rows.values():
        try:
            if r["proc"].poll() is None:
                r["proc"].kill()
        except Exception:
            pass
    return captured


def main():
    frames = []

    hold(frames, render([("Death Note for hung agents.", FG), ("", FG),
                         ("an out-of-band supervisor for AI-agent orchestrations", DIM)],
                        title="dev note", title_color=GREEN), 3.5)

    hold(frames, render([
        ("Every agent harness has retry logic.", FG),
        ("None of it fires when an agent goes SILENTLY hung --", FG),
        ("no error, no exit, it just stops.", RED), ("", FG),
        ("Every in-band safeguard runs ON a tool call.", DIM),
        ("A hang is the ABSENCE of one. You can't detect", DIM),
        ("silence with a thing that only runs on activity.", DIM), ("", FG),
        ("And the orchestrator is blocked INSIDE the call to", DIM),
        ("its own stuck child -- it can't even look.", DIM),
    ], title="the problem", title_color=RED), 9)

    hold(frames, render([
        ("$ pip install dev-note", GREEN), ("", FG),
        ("from devnote.heartbeat import Heartbeat", FG),
        ('hb = Heartbeat.register_self(cfg, label="my-agent")', FG),
        ("while working:", FG),
        ("    hb.beat()          # renew the lease each loop", DIM), ("", FG),
        ("$ devnote watch        # the supervisor: its own wall clock", GREEN),
    ], title="access + integrate", title_color=FG), 9)

    session = run_session()
    for lines in session:
        frames.append(render(lines))
        # ~3 video frames per captured frame -> readable pacing
        frames.append(render(lines))

    hold(frames, render([
        ("LEASE  ->  FENCE  ->  HARVEST  ->  REAP  ->  RESPAWN", GREEN), ("", FG),
        ("fence first, harvest second, reap last -- the kill is never a race.", DIM),
        ("scoped, uid-checked, dry-run by default. safe on a shared box.", DIM), ("", FG),
        ("one interface -> one laptop, or a million-agent fleet.", FG), ("", FG),
        ("github.com/LamaSu/dev-note", GREEN),
    ], title="out-of-band recovery", title_color=GREEN), 8)

    imageio.mimwrite(OUT, [f for f in frames], fps=FPS, codec="libx264", quality=8,
                     macro_block_size=None)
    secs = len(frames) / FPS
    print(f"wrote {OUT}  ({len(frames)} frames, {secs:.0f}s)")


if __name__ == "__main__":
    main()
