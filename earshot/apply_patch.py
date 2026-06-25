"""Entry point for the apply-approved GitHub Action.

Reads PATCHES_JSON from the environment (the patch Brady approved in Telegram, carried by
repository_dispatch), applies it through selfcode's test-gate, and leaves the working tree modified
ONLY if the gate passed — the Action commits a non-empty tree. Prints a JSON result either way.
"""
from __future__ import annotations

import json
import os

from . import selfcode


def main() -> int:
    raw = (os.getenv("PATCHES_JSON") or "").strip()
    if not raw:
        print(json.dumps({"status": "skipped", "why": "no PATCHES_JSON"}))
        return 0
    try:
        patches = json.loads(raw)
        if isinstance(patches, str):                 # client_payload double-encodes the string
            patches = json.loads(patches)
    except Exception as e:
        print(json.dumps({"status": "error", "why": f"bad PATCHES_JSON: {e}"}))
        return 0
    if not isinstance(patches, list):
        print(json.dumps({"status": "error", "why": "patches must be a list"}))
        return 0
    print(json.dumps(selfcode.apply_external(patches), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
