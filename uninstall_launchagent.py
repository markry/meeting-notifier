#!/usr/bin/env python3
"""Uninstall the meeting-notifier LaunchAgent.

Bootouts the running agent (if any), then deletes the plist file from
~/Library/LaunchAgents/.

Usage:
    python3 uninstall_launchagent.py [--label LABEL]

Default label: net.ryland.meeting-notifier
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


DEFAULT_LABEL = "net.ryland.meeting-notifier"
LAUNCHAGENTS_DIR = Path.home() / "Library" / "LaunchAgents"


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    print("  $ " + " ".join(repr(c) if " " in c else c for c in cmd))
    return subprocess.run(cmd, check=check, text=True, capture_output=True)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--label", default=DEFAULT_LABEL)
    args = p.parse_args()

    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{args.label}"
    plist_path = LAUNCHAGENTS_DIR / f"{args.label}.plist"

    print(f"=== Uninstalling LaunchAgent {args.label} ===")

    # Bootout (ignore failure if not loaded).
    r = run(["launchctl", "print", target], check=False)
    if r.returncode == 0:
        run(["launchctl", "bootout", target], check=False)
        print("  unloaded")
    else:
        print("  (was not loaded)")

    # Remove plist file.
    if plist_path.exists():
        plist_path.unlink()
        print(f"  removed {plist_path}")
    else:
        print(f"  (no plist at {plist_path})")

    print("Done.")


if __name__ == "__main__":
    main()
