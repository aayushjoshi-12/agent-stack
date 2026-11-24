"""Stop the benchmark jobs when the OpenRouter key's lifetime usage reaches a ceiling.

    OPENROUTER_API_KEY=... python bench/spend_guard.py 2.00 [poll-seconds]

Polls ``GET /api/v1/key`` (the provider's own counter, so it also covers calls no local ledger
sees) and sends SIGTERM to every Harbor process when ``usage >= ceiling``. Prints one line per
change in usage; never prints the key.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request


def usage(key: str) -> float:
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {key}"}
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return float(json.load(response)["data"]["usage"])


def stop_harbor() -> list[int]:
    """SIGTERM every Harbor process; returns the pids signalled."""
    out = subprocess.run(["pgrep", "-f", "venv/bin/harbor"], capture_output=True, text=True)
    pids = [int(pid) for pid in out.stdout.split()]
    for pid in pids:
        os.kill(pid, signal.SIGTERM)
    return pids


def main() -> int:
    ceiling = float(sys.argv[1])
    poll = float(sys.argv[2]) if len(sys.argv) > 2 else 15.0
    key = os.environ["OPENROUTER_API_KEY"]
    last = -1.0
    while True:
        try:
            now = usage(key)
        except Exception as exc:  # a failed poll must not end the guard
            print(f"guard: poll failed ({type(exc).__name__})", flush=True)
            time.sleep(poll)
            continue
        if now != last:
            print(f"guard: key usage ${now:.4f} / ceiling ${ceiling:.2f}", flush=True)
            last = now
        if now >= ceiling:
            print("guard: ceiling reached, stopping harbor", flush=True)
            stop_harbor()
            return 3
        time.sleep(poll)


if __name__ == "__main__":
    raise SystemExit(main())
