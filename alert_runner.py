#!/usr/bin/env python3
"""Show ONE meeting alert and exit.

This script is spawned as a subprocess by `poller.py` for each alert. Running
the AppKit modal in a short-lived subprocess (rather than inline inside the
long-running poller daemon) means that when the user clicks a button:

  - the action handler sets a result code
  - the modal session ends
  - main() returns
  - the process terminates
  - macOS reclaims all the process's resources, including the NSWindow

The window can't possibly persist past process termination — which fixes a
class of "the modal window stayed up after I clicked" bugs we hit with the
inline approach.

Exit codes (interpreted by the poller):
  0 = dismiss
  1 = snooze
  2 = link (user clicked the join link)
  3 = timeout (auto-dismissed)
  >127 = killed by signal — treated as dismiss

Usage:
    python3 alert_runner.py --title "Standup" --start-str "9:30 AM" \\
        --minutes-until 5 [--location "Room 4B"] [--join-link https://...] \\
        [--snooze-minutes 2] [--timeout-seconds 0]
"""
from __future__ import annotations

import argparse
import sys

from overlay import AlertInfo, show_alert


EXIT_CODES = {
    "dismiss": 0,
    "snooze":  1,
    "link":    2,
    "timeout": 3,
}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--title", required=True)
    p.add_argument("--start-str", required=True,
                   help="already-formatted local time, e.g. '9:30 AM'")
    p.add_argument("--minutes-until", type=int, required=True)
    p.add_argument("--location", default=None)
    p.add_argument("--join-link", default=None)
    p.add_argument("--snooze-minutes", type=int, default=2)
    p.add_argument("--timeout-seconds", type=int, default=0,
                   help="0 = no auto-dismiss (the default)")
    p.add_argument("--display-mode", choices=["all", "main", "focused"],
                   default="all",
                   help="which display(s) to show the alert on")
    p.add_argument("--no-all-spaces", action="store_true",
                   help="if set, alert appears only on the current Space")
    args = p.parse_args()

    info = AlertInfo(
        title=args.title,
        start_str=args.start_str,
        minutes_until=args.minutes_until,
        location=args.location,
        join_link=args.join_link,
    )
    result = show_alert(info,
                        snooze_minutes=args.snooze_minutes,
                        timeout_seconds=args.timeout_seconds,
                        display_mode=args.display_mode,
                        all_spaces=not args.no_all_spaces)
    return EXIT_CODES.get(result, EXIT_CODES["dismiss"])


if __name__ == "__main__":
    sys.exit(main())
