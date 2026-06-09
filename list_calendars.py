#!/usr/bin/env python3
"""Enumerate EventKit calendars to find the Google calendar's exact title/source.

RUN ON macOS (not Windows). Requires PyObjC:  pip3 install pyobjc-framework-EventKit
First run triggers the Calendar permission prompt -- grant it in
System Settings > Privacy & Security > Calendars, then re-run if needed.

Output identifies each calendar's title, source, source type, and identifier so we
can pick the exact filter for the Google calendar in the poller.
"""
import sys
import platform

from EventKit import EKEventStore, EKEntityTypeEvent
from Foundation import NSRunLoop, NSDate


def macos_major():
    try:
        return int(platform.mac_ver()[0].split(".")[0])
    except (ValueError, IndexError):
        return 0


# source-type enum -> human label (EKSourceType)
SOURCE_TYPES = {
    0: "Local",
    1: "Exchange",
    2: "CalDAV/iCloud",
    3: "MobileMe",
    4: "Subscribed",
    5: "Birthdays",
}


def request_access(store):
    """Request Calendar access, pumping the runloop until the completion handler fires."""
    state = {"done": False, "granted": False, "error": None}

    def handler(granted, error):
        state["granted"] = bool(granted)
        state["error"] = error
        state["done"] = True

    if macos_major() >= 14 and hasattr(store, "requestFullAccessToEventsWithCompletion_"):
        store.requestFullAccessToEventsWithCompletion_(handler)
    else:
        store.requestAccessToEntityType_completion_(EKEntityTypeEvent, handler)

    # Pump the runloop (the callback arrives async) with a ~60s safety timeout.
    deadline = 600  # 600 * 0.1s
    while not state["done"] and deadline > 0:
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))
        deadline -= 1
    return state


def main():
    store = EKEventStore.alloc().init()
    res = request_access(store)
    if not res["granted"]:
        print("Calendar access NOT granted.", res["error"] or "")
        print("Grant it in System Settings > Privacy & Security > Calendars, then re-run.")
        sys.exit(1)

    cals = store.calendarsForEntityType_(EKEntityTypeEvent)
    print("Found %d calendar(s):\n" % len(cals))
    for c in cals:
        src = c.source()
        src_title = src.title() if src else None
        try:
            stype = SOURCE_TYPES.get(int(src.sourceType()), src.sourceType()) if src else None
        except (ValueError, TypeError):
            stype = None
        print("- title:       %s" % c.title())
        print("  source:      %s" % src_title)
        print("  sourceType:  %s" % stype)
        print("  identifier:  %s" % c.calendarIdentifier())
        print("  allowsMod:   %s" % c.allowsContentModifications())
        print()

    print("Look for the calendar whose source is your Google account.")
    print("Tell Claude the exact `title` + `source` so we can filter to just it.")


if __name__ == "__main__":
    main()
