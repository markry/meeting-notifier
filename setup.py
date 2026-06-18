"""py2app build script for MeetingNotifier.

Usage (from the project root, with the venv active):

    python3 setup.py py2app

Output:  ./dist/MeetingNotifier.app

Bundle is a regular foreground app (no LSUIElement). Each role sets its own
activation policy at runtime: setup_gui → Regular (Dock + menu bar), alert
subprocess → Accessory (no Dock icon flash), poller daemon never instantiates
NSApplication at all. This pattern is required so the GUI can pop the
Calendar TCC permission dialog on first launch — which is the canonical fix
for the Sequoia launchd/TCC trap where a background-only LaunchAgent process
can never get Calendar access.
"""
from setuptools import setup


APP = ["main.py"]

OPTIONS = {
    # No argv emulation — we use real CLI args (subcommands + flags), not
    # the open-document AppleEvent path that argv_emulation is for.
    "argv_emulation": False,

    # The bundle's Info.plist.
    "plist": {
        "CFBundleName": "MeetingNotifier",
        "CFBundleDisplayName": "Meeting Notifier",
        "CFBundleIdentifier": "net.ryland.meeting-notifier",
        "CFBundleVersion": "0.3.3",
        "CFBundleShortVersionString": "0.3.3",
        "CFBundleSignature": "????",
        "LSMinimumSystemVersion": "12.0",
        "NSHumanReadableCopyright": "Copyright (c) 2026 Mark Ryland. MIT-licensed.",
        # Calendar access usage descriptions shown in the macOS permission
        # prompt. BOTH keys must be present:
        #   - NSCalendarsUsageDescription      → legacy / pre-macOS 14 API
        #   - NSCalendarsFullAccessUsageDescription → macOS 14+ Full Access API
        # which is what EKEventStore.requestFullAccessToEventsWithCompletion_
        # uses. Without the latter, the new API silently returns "denied"
        # without ever showing the system dialog — confused us into thinking
        # TCC was caching a denial when actually the prompt was never legal
        # to display in the first place.
        "NSCalendarsUsageDescription":
            "Meeting Notifier reads upcoming events from your local calendars "
            "so it can alert you a few minutes before each meeting starts.",
        "NSCalendarsFullAccessUsageDescription":
            "Meeting Notifier reads upcoming events from your local calendars "
            "so it can alert you a few minutes before each meeting starts.",
    },

    # Force-include our internal modules. py2app tries to detect imports but
    # listing them explicitly is more reliable for non-trivial PyObjC apps.
    "includes": [
        "overlay",
        "poller",
        "alert_runner",
    ],

    # PyObjC frameworks we actually use.
    "packages": [
        "objc",
    ],
    "frameworks": [],

    # Resources copied into MeetingNotifier.app/Contents/Resources/. These
    # ship alongside the executable; the user's config.toml stays outside the
    # bundle (per-machine, not bundled).
    "resources": [
        "config.example.toml",
        "README.md",
        "LICENSE",
    ],

    # Keep the bundle reasonably small.
    "optimize": 1,
}


setup(
    app=APP,
    name="MeetingNotifier",
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
