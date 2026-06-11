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

Exit codes (interpreted by the poller via poller._EXIT_CODE_TO_RESULT; in the
100+ range so a pre-return crash using a low code isn't mistaken for a user
action):
  100 = dismiss
  101 = snooze
  102 = link (user clicked the join link)
  103 = timeout (auto-dismissed)
  anything else = treated by the poller as a dispatch failure (retry next poll)

Invocation:
    Primary (used by the poller): `alert_runner.py --json-stdin` with the alert
    parameters as a JSON document on stdin. Legacy `--title ... --start-str ...`
    argv flags remain for manual CLI testing only — never feed calendar data
    through them (a value starting with '-' is ambiguous to argparse).
"""
from __future__ import annotations

import argparse
import sys

from overlay import AlertInfo, show_alert


# Exit codes paired with poller._EXIT_CODE_TO_RESULT. 100+ range so they
# don't collide with common Python crash codes (1 = unhandled exception,
# 2 = argparse error, etc.). A crash before main() returns will use one of
# those low codes; poller.fire_alert treats anything not in this map as a
# dispatch failure rather than a user action.
EXIT_CODES = {
    "dismiss": 100,
    "snooze":  101,
    "link":    102,
    "timeout": 103,
}


_VALID_DISPLAY_MODES = {"all", "main", "focused"}
_VALID_APPEARANCES = {"auto", "glass", "solid"}


def _parse_iso(value):
    if not value:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _run_from_stdin_json() -> int:
    """Primary invocation path (poller.fire_alert). All alert parameters arrive
    as a JSON document on stdin instead of argv, so calendar-controlled PII
    never lands in the process's argument list (visible via `ps`) and a crafted
    meeting title can't be mis-parsed as a CLI option. See poller.fire_alert."""
    import json
    raw = sys.stdin.read()
    data = json.loads(raw)
    display_mode = data.get("display_mode", "all")
    if display_mode not in _VALID_DISPLAY_MODES:
        display_mode = "all"
    window_appearance = data.get("window_appearance", "auto")
    if window_appearance not in _VALID_APPEARANCES:
        window_appearance = "auto"
    info = AlertInfo(
        title=str(data.get("title", "(no title)")),
        start_str=str(data.get("start_str", "")),
        minutes_until=int(data.get("minutes_until", 0)),
        location=data.get("location"),
        join_link=data.get("join_link"),
        start_utc=_parse_iso(data.get("start_utc_iso")),
    )
    result = show_alert(info,
                        snooze_minutes=int(data.get("snooze_minutes", 2)),
                        timeout_seconds=int(data.get("timeout_seconds", 0)),
                        display_mode=display_mode,
                        all_spaces=bool(data.get("all_spaces", True)),
                        window_appearance=window_appearance,
                        hide_from_screen_sharing=bool(
                            data.get("hide_from_screen_sharing", True)))
    return EXIT_CODES.get(result, EXIT_CODES["dismiss"])


def main() -> int:
    if "--json-stdin" in sys.argv[1:]:
        return _run_from_stdin_json()

    # Legacy argv path — kept for manual CLI testing only. The poller no longer
    # uses this (it passes --json-stdin). Do not feed it untrusted/calendar data
    # via argv: a value starting with '-' is ambiguous to argparse.
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--title", required=True)
    p.add_argument("--start-str", required=True,
                   help="already-formatted local time, e.g. '9:30 AM'")
    p.add_argument("--minutes-until", type=int, required=True)
    p.add_argument("--start-utc-iso", default=None,
                   help="ISO-format datetime of the meeting start in UTC; "
                        "when present the overlay refreshes the 'Starts in "
                        "N minutes' line every 30s so it stays truthful")
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
    p.add_argument("--window-appearance", choices=["auto", "glass", "solid"],
                   default="auto",
                   help="glass card that follows the Mac's transparency "
                        "setting (auto/glass), or a forced opaque card (solid)")
    args = p.parse_args()

    start_utc = None
    if args.start_utc_iso:
        from datetime import datetime
        try:
            start_utc = datetime.fromisoformat(args.start_utc_iso)
        except ValueError:
            start_utc = None
    info = AlertInfo(
        title=args.title,
        start_str=args.start_str,
        minutes_until=args.minutes_until,
        location=args.location,
        join_link=args.join_link,
        start_utc=start_utc,
    )
    result = show_alert(info,
                        snooze_minutes=args.snooze_minutes,
                        timeout_seconds=args.timeout_seconds,
                        display_mode=args.display_mode,
                        all_spaces=not args.no_all_spaces,
                        window_appearance=args.window_appearance)
    return EXIT_CODES.get(result, EXIT_CODES["dismiss"])


if __name__ == "__main__":
    sys.exit(main())
