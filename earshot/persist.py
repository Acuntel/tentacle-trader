"""Crash-proof persistence — an update, a redeploy, or a kill mid-write must NEVER lose the son's
progress. Every save is ATOMIC (write a temp file, fsync, then os.replace — which is atomic on the
Railway volume) and keeps a .bak of the last-good copy. Every load falls back to the .bak if the main
file is ever truncated or corrupt. State lives on the persistent volume (/app/state), which the image
build never touches, so the brain, the ladder, the sims, the patterns, every tentacle's learning, and
the whole evolution history carry over every single update — only ever growing, never reset.
"""
from __future__ import annotations

import json
import os


def save_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    if os.path.exists(path):
        try:
            os.replace(path, path + ".bak")        # preserve the previous good copy
        except OSError:
            pass  # ignore if .bak already gone or unwritable
    os.replace(tmp, path)                            # atomic swap-in — readers never see a half file


def _scrub(obj, stats: dict):
    """AUTOPSY FIX 2026-09-01: JSON round-trips carry NaN/Infinity, and one poisoned accumulator
    (sim.sum_ret, a strategy fold's u, a source's sum_ret) silently turned the benchmark, the luck
    floor and the honesty gauge into NaN for weeks. Every load replaces non-finite floats with 0.0
    and counts what it healed."""
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):
            stats["healed"] = stats.get("healed", 0) + 1
            return 0.0
        return obj
    if isinstance(obj, dict):
        return {k: _scrub(v, stats) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v, stats) for v in obj]
    return obj


def load_json(path: str, default):
    for p in (path, path + ".bak"):                 # main first, then the backup if it's corrupt
        try:
            with open(p) as f:
                obj = json.load(f)
            stats: dict = {}
            obj = _scrub(obj, stats)
            if stats.get("healed"):
                print(f"  [persist] healed {stats['healed']} non-finite value(s) in {os.path.basename(p)}")
            return obj
        except FileNotFoundError:
            continue
        except Exception:
            continue
    return default


def backup(path: str, tag: str) -> str | None:
    """Keep a dated snapshot of a critical file (e.g. the brain) so even a bad logical change is
    recoverable, not just a crash. Returns the backup path."""
    if not os.path.exists(path):
        return None
    bdir = "state/backups"
    os.makedirs(bdir, exist_ok=True)
    dst = os.path.join(bdir, f"{os.path.basename(path)}.{tag}")
    try:
        with open(path) as src, open(dst, "w") as out:
            out.write(src.read())
        # keep only the most recent ~30 snapshots per file
        snaps = sorted(f for f in os.listdir(bdir) if f.startswith(os.path.basename(path)))
        for old in snaps[:-30]:
            try:
                os.remove(os.path.join(bdir, old))
            except OSError:
                pass
        return dst
    except OSError:
        return None
