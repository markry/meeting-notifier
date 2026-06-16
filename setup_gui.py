#!/usr/bin/env python3
"""Setup / settings window for MeetingNotifier.

Shown when the .app is launched as a foreground app (no CLI args or just from
Finder). On Save: writes the user's choices to
`~/.config/meeting-notifier/config.toml`, installs / restarts the LaunchAgent,
then quits the GUI process. The background LaunchAgent keeps running.

Distinct from the alert overlay: this is a NORMAL window — not always-on-top,
not on all screens, not multi-Space. The user opens it, makes changes, hits
Save. The "always on top across all screens / all Spaces" treatment is reserved
for the meeting alerts themselves.

Re-launching the .app shows the same window with current settings pre-loaded,
so it doubles as the settings panel.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore

import objc
from AppKit import (
    NSApp, NSApplication, NSApplicationActivationPolicyRegular,
    NSWindow, NSWindowStyleMaskTitled, NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable, NSBackingStoreBuffered,
    NSColor, NSFont, NSTextField, NSButton, NSButtonTypeSwitch,
    NSBezelStyleRounded, NSScrollView, NSScrollerStyleLegacy,
    NSView, NSPopUpButton, NSTextAlignmentCenter,
    NSScreen, NSAlert, NSAlertStyleInformational,
)
from Foundation import NSObject, NSMakeRect, NSMakeSize, NSMakePoint, NSTimer
from EventKit import EKEventStore, EKEntityTypeEvent

# Use the existing request_access helper from poller.py so we get the same
# macOS-version-aware logic (full-access vs. legacy entity-type call).
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from poller import request_access, _safe_write_text  # noqa: E402


# ---------------------------------------------------------------------------
# Paths + constants
# ---------------------------------------------------------------------------


CONFIG_DIR = Path.home() / ".config" / "meeting-notifier"
CONFIG_PATH = CONFIG_DIR / "config.toml"
LAUNCHAGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
LOG_DIR = Path.home() / "Library" / "Logs" / "meeting-notifier"
LABEL = "net.ryland.meeting-notifier"

# Window dimensions
WIN_W = 628
WIN_H = 712   # tightened the calendars-header gap and trimmed bottom dead
              # space, but kept ~10px clearance below the lowest Filtering
              # checkbox so the bottom-anchored install/status label (at y =
              # PAD-4) doesn't overlap it during "Installing background agent…".
              # Nets ~same height as 0.2.21's 708 despite the new Timing row.
PAD = 20

# Form column geometry: [ label | value field/popup | help text ], all rows
# anchored at the left margin (PAD). Kept as constants so the int-field, popup,
# and switch helpers stay in sync when the window width changes.
LABEL_W = 250
FIELD_W = 56
COL_GAP = 10
FIELD_X = PAD + LABEL_W + COL_GAP        # left edge of the value field / popup
HELP_X = FIELD_X + FIELD_W + COL_GAP     # left edge of the help text

# Bottom-right action buttons (Cancel stacked above Save & Start), tucked into
# the empty space beside the Filtering checkboxes instead of a full-width row.
BTN_W = 115
BTN_H = 32
# Filtering checkboxes are capped to this width so their (full-frame) click
# target doesn't slip under the button column to their right.
FILTER_SW_W = WIN_W - 2 * PAD - BTN_W - 12


# ---------------------------------------------------------------------------
# Settings model (loaded from disk; saved by the GUI)
# ---------------------------------------------------------------------------


DEFAULTS = {
    "lead_time_minutes": 5,
    "poll_interval_seconds": 20,
    "lookahead_seconds": 900,
    "snooze_minutes": 2,
    "final_snooze_minutes": 1,
    "alert_timeout_seconds": 0,
    "display_mode": "all",
    "all_spaces": True,
    "hide_from_screen_sharing": True,
    "show_location": True,
    "show_join_link": True,
    "join_link_known_providers_only": True,
    "skip_all_day": True,
    "skip_unaccepted_meetings": False,
    "notify_in_progress_meetings": False,
    "skip_title_substrings": ["OOO", "Out of Office", "Birthday"],
}


def load_settings() -> dict:
    """Read the existing config.toml, falling back to defaults."""
    settings = dict(DEFAULTS)
    settings["calendars"] = []  # list of {"title": ..., "source": ...} dicts
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "rb") as f:
                data = tomllib.load(f)
        except Exception:
            return settings
        for k in DEFAULTS:
            if k in data:
                settings[k] = data[k]
        for entry in data.get("calendars", []):
            settings["calendars"].append({
                "title":  entry.get("title"),
                "source": entry.get("source"),
            })
    return settings


def save_settings(settings: dict, watched_calendars: list[dict]) -> None:
    """Write the config to ~/.config/meeting-notifier/config.toml in our schema.

    `watched_calendars` is a list of {"title": str, "source": str|None} dicts.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Meeting Notifier configuration - written by the setup GUI.",
        "#",
        "# Editing this file by hand:",
        "#",
        "#   1. Make your edits.",
        "#   2. Restart the background poller so it re-reads the file:",
        "#        launchctl kickstart -k gui/$(id -u)/net.ryland.meeting-notifier",
        "#   3. DO NOT double-click /Applications/MeetingNotifier.app afterward.",
        "#      Re-launching the .app runs the GUI, which rewrites this file",
        "#      from the form fields and DROPS any keys the GUI doesn't manage -",
        "#      notably `use_overlay` and identifier-based [[calendars]] entries",
        "#      (the GUI re-writes calendars by title + source, not identifier).",
        "#",
        "# To recover from an accidental GUI overwrite, restore from your backup",
        "# and restart the poller as above.",
        "#",
        "# See config.example.toml in the source repo for every documented option.",
        "",
        f"lead_time_minutes = {int(settings['lead_time_minutes'])}",
        f"poll_interval_seconds = {int(settings['poll_interval_seconds'])}",
        f"lookahead_seconds = {int(settings['lookahead_seconds'])}",
        f"snooze_minutes = {int(settings['snooze_minutes'])}",
        f"final_snooze_minutes = {int(settings['final_snooze_minutes'])}",
        f"alert_timeout_seconds = {int(settings['alert_timeout_seconds'])}",
        f'display_mode = "{settings["display_mode"]}"',
        f"all_spaces = {'true' if settings['all_spaces'] else 'false'}",
        f"hide_from_screen_sharing = {'true' if settings['hide_from_screen_sharing'] else 'false'}",
        f"show_location = {'true' if settings['show_location'] else 'false'}",
        f"show_join_link = {'true' if settings['show_join_link'] else 'false'}",
        f"join_link_known_providers_only = {'true' if settings['join_link_known_providers_only'] else 'false'}",
        f"skip_all_day = {'true' if settings['skip_all_day'] else 'false'}",
        f"skip_unaccepted_meetings = {'true' if settings['skip_unaccepted_meetings'] else 'false'}",
        f"notify_in_progress_meetings = {'true' if settings['notify_in_progress_meetings'] else 'false'}",
        "skip_title_substrings = " + _toml_string_list(settings["skip_title_substrings"]),
        "",
    ]
    for cal in watched_calendars:
        lines.append("[[calendars]]")
        lines.append(f"title = {_toml_value(cal['title'])}")
        if cal.get("source"):
            lines.append(f"source = {_toml_value(cal['source'])}")
        lines.append("")
    # Atomic write + reject symlink, plus encoding="utf-8" in case the bundle
    # was launched from a context that inherited an ASCII locale (Finder).
    _safe_write_text(CONFIG_PATH, "\n".join(lines), mode=0o600)


def _toml_string_list(items: list[str]) -> str:
    inner = ", ".join(_toml_value(s) for s in items)
    return f"[{inner}]"


def _toml_value(s: str) -> str:
    """Render a Python string as a TOML string value, choosing the safest
    representation.

    Calendar titles arrive from EventKit (ultimately CalDAV / Exchange / etc.)
    so they can contain arbitrary characters including newlines, tabs, and
    embedded quotes. The original `_escape` only escaped `\\` and `"`, which
    silently broke the file on the next read if a calendar name had a
    newline - and worse, a carefully crafted name with embedded TOML syntax
    could overwrite other keys when re-parsed.

    Strategy:
      - Prefer TOML literal strings (single-quoted, no escaping) when the
        value contains no single quotes and no control characters.
      - Otherwise emit a TOML basic string with the seven standard escape
        sequences plus \\uXXXX for any other C0 / DEL control character.
    """
    has_squote = "'" in s
    has_ctrl = any(ord(c) < 0x20 or ord(c) == 0x7F for c in s)
    if not has_squote and not has_ctrl:
        return f"'{s}'"
    out = ['"']
    for c in s:
        cp = ord(c)
        if c == "\\":
            out.append("\\\\")
        elif c == '"':
            out.append('\\"')
        elif c == "\b":
            out.append("\\b")
        elif c == "\t":
            out.append("\\t")
        elif c == "\n":
            out.append("\\n")
        elif c == "\f":
            out.append("\\f")
        elif c == "\r":
            out.append("\\r")
        elif cp < 0x20 or cp == 0x7F:
            out.append(f"\\u{cp:04X}")
        else:
            out.append(c)
    out.append('"')
    return "".join(out)


# ---------------------------------------------------------------------------
# LaunchAgent management (embedded so the .app is self-contained)
# ---------------------------------------------------------------------------


def _plist_contents(app_path: Path) -> str:
    binary = app_path / "Contents" / "MacOS" / "MeetingNotifier"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{binary}</string>
        <string>--daemon</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>30</integer>
    <key>StandardOutPath</key>
    <string>{LOG_DIR / 'stdout.log'}</string>
    <key>StandardErrorPath</key>
    <string>{LOG_DIR / 'stderr.log'}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PYTHONUNBUFFERED</key>
        <string>1</string>
    </dict>
</dict>
</plist>
"""


def install_or_restart_launchagent(app_path: Path) -> None:
    """Write the plist, bootout if loaded, then bootstrap + kickstart."""
    LAUNCHAGENTS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    # Tighten log dir to owner-only so meeting titles + Zoom links with
    # embedded ?pwd= tokens aren't world-readable on a shared Mac.
    try:
        os.chmod(LOG_DIR, 0o700)
    except OSError:
        pass

    plist_path = LAUNCHAGENTS_DIR / f"{LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    target = f"{domain}/{LABEL}"

    # bootout if already loaded
    r = subprocess.run(["launchctl", "print", target],
                       capture_output=True, text=True)
    if r.returncode == 0:
        subprocess.run(["launchctl", "bootout", target], capture_output=True)
        time.sleep(1)  # launchd needs a moment to release the label

    # Atomic write that refuses to follow a symlink at plist_path - blocks a
    # local malware vector where another user-context process plants a symlink
    # in ~/Library/LaunchAgents/ to redirect our plist write, then waits for
    # the resulting launchd-loaded plist to run code at every login.
    _safe_write_text(plist_path, _plist_contents(app_path), mode=0o600)
    subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)],
                   check=True)
    subprocess.run(["launchctl", "kickstart", "-k", target], check=False)


def detect_app_path() -> Path:
    """When the GUI runs inside a py2app bundle, sys.executable is
    <App>.app/Contents/MacOS/<binary>. Walking two parents up gives the .app
    directory itself, which is what the LaunchAgent ProgramArguments needs."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent.parent.parent
    # Source mode (running poller/setup_gui directly via venv) — point at the
    # source project as a stand-in. The user would manually adjust the
    # ProgramArguments if they really want to run the source via launchd.
    return HERE


# ---------------------------------------------------------------------------
# The window
# ---------------------------------------------------------------------------


class SettingsWindow(NSObject):
    """Owns the settings NSWindow + tracks control state."""

    def init(self):
        self = objc.super(SettingsWindow, self).init()
        if self is None:
            return None
        self._settings = load_settings()
        self._calendar_rows = []  # list of (NSButton checkbox, EKCalendar)
        self._field_controls = {}  # name → NSTextField
        self._switch_controls = {}  # name → NSButton (switch style)
        self._display_popup = None
        self._save_button = None
        self._cancel_button = None
        self._status_label = None
        return self

    # ----- public entry -----

    def startSetup_(self, sender):
        """Entry point called once NSApp.run() is active.

        Calling request_access() before the main runloop has started leaves
        the app in a half-activated state that Sequoia's tccd refuses to show
        a permission dialog into. By deferring to a performSelectorAfterDelay,
        we land here from inside the running NSApp loop with the app properly
        in the foreground — at which point requestFullAccessToEventsWithCompletion_
        can actually surface the system Allow/Don't Allow prompt.
        """
        NSApp.activateIgnoringOtherApps_(True)
        self._build()

    @objc.python_method
    def _build(self):
        store = EKEventStore.alloc().init()
        if not request_access(store):
            self._show_modal_alert(
                "Calendar permission needed",
                "MeetingNotifier needs access to your local calendars to "
                "alert you before meetings.\n\n"
                "If macOS didn't show a permission prompt, your system may be "
                "caching a denial. Run this in Terminal to reset and try again:\n\n"
                "    tccutil reset Calendar net.ryland.meeting-notifier\n\n"
                "Otherwise, grant access in System Settings → Privacy & "
                "Security → Calendars and relaunch."
            )
            # Exit cleanly so the user doesn't end up with a phantom Dock icon
            # and no window after dismissing the alert.
            NSApp.terminate_(None)
            return
        calendars = list(store.calendarsForEntityType_(EKEntityTypeEvent))
        # Sort: first all writable + non-system, alphabetical
        calendars.sort(key=lambda c: (
            0 if c.allowsContentModifications() else 1,
            str(c.title()).lower(),
        ))
        self._build_window(calendars)
        self._window.makeKeyAndOrderFront_(None)

    # ----- layout -----

    @objc.python_method
    def _build_window(self, calendars):
        # Centered on the main screen, with a normal title bar (NOT always-on-top).
        screen = NSScreen.mainScreen()
        sf = screen.visibleFrame()
        win_x = sf.origin.x + (sf.size.width  - WIN_W) / 2
        win_y = sf.origin.y + (sf.size.height - WIN_H) / 2

        mask = (NSWindowStyleMaskTitled |
                NSWindowStyleMaskClosable |
                NSWindowStyleMaskMiniaturizable)
        win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(win_x, win_y, WIN_W, WIN_H),
            mask, NSBackingStoreBuffered, False,
        )
        win.setTitle_("Meeting Notifier — Setup")
        win.setReleasedWhenClosed_(False)
        content = win.contentView()
        self._window = win

        y = WIN_H - PAD - 30
        # Title
        title = NSTextField.alloc().initWithFrame_(
            NSMakeRect(PAD, y, WIN_W - 2 * PAD, 30))
        title.setStringValue_("Meeting Notifier")
        title.setFont_(NSFont.boldSystemFontOfSize_(22))
        title.setBezeled_(False)
        title.setDrawsBackground_(False)
        title.setEditable_(False)
        title.setSelectable_(False)
        content.addSubview_(title)
        y -= 30

        intro = NSTextField.alloc().initWithFrame_(
            NSMakeRect(PAD, y, WIN_W - 2 * PAD, 20))
        intro.setStringValue_(
            "Choose which calendars to watch and tune how alerts behave. "
            "Click Save & Start to apply.")
        intro.setFont_(NSFont.systemFontOfSize_(13))
        intro.setTextColor_(NSColor.secondaryLabelColor())
        intro.setBezeled_(False)
        intro.setDrawsBackground_(False)
        intro.setEditable_(False)
        intro.setSelectable_(False)
        content.addSubview_(intro)
        y -= 30

        # Calendars section header
        header = self._make_section_header(
            NSMakeRect(PAD, y, WIN_W - 2 * PAD, 18),
            "Calendars to watch")
        content.addSubview_(header)
        # 6px tail to match the other section headers (they return y - 24 from
        # an 18px header) — was 22, which left an oddly large gap above the list.
        y -= 6

        # Scrollable list of calendar checkboxes. Show ~4 rows by default;
        # scroll vertically when the user has more calendars. Keeps the window
        # short enough to fit even when the user has cranked up macOS Display
        # → "Larger Text" mode (which shrinks the screen's effective point count).
        row_h = 24
        visible_rows = 4
        list_h = visible_rows * row_h + 8
        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(PAD, y - list_h, WIN_W - 2 * PAD, list_h))
        scroll.setHasVerticalScroller_(True)
        # Force "legacy" non-overlay scrollers so the bar is visible at a glance
        # even when the user's system pref is "Show scroll bars: only when
        # scrolling" (the trackpad default). Otherwise a clipped list looks
        # incomplete instead of scrollable.
        scroll.setScrollerStyle_(NSScrollerStyleLegacy)
        scroll.setAutohidesScrollers_(False)
        scroll.setBorderType_(2)  # NSBezelBorder
        inner_h = max(list_h, len(calendars) * row_h + 4)
        inner = NSView.alloc().initWithFrame_(
            NSMakeRect(0, 0, WIN_W - 2 * PAD - 20, inner_h))

        # Build a set of (title, source) currently watched, for pre-checking
        watched_keys = {
            (c["title"], c.get("source"))
            for c in self._settings.get("calendars", [])
        }

        for i, cal in enumerate(calendars):
            cb = NSButton.alloc().initWithFrame_(
                NSMakeRect(6, inner_h - (i + 1) * row_h, WIN_W - 2 * PAD - 40, row_h - 2))
            cb.setButtonType_(NSButtonTypeSwitch)
            src_title = str(cal.source().title()) if cal.source() else ""
            label = str(cal.title())
            if src_title:
                label = f"{label}   ·   {src_title}"
            cb.setTitle_(label)
            # Pre-check if this (title, source) is already watched
            key = (str(cal.title()), src_title or None)
            if key in watched_keys or (str(cal.title()), None) in watched_keys:
                cb.setState_(1)
            inner.addSubview_(cb)
            self._calendar_rows.append((cb, cal))
        scroll.setDocumentView_(inner)
        content.addSubview_(scroll)
        # Cocoa scroll views default to showing the BOTTOM of the document
        # view (because the unflipped coordinate system puts (0,0) at the
        # bottom-left). Our rows are laid out top-down, so without this the
        # window opens looking at calendars 3-4 instead of 0-1. Scroll the
        # clip view to put the top of the document in view.
        if inner_h > list_h:
            clip = scroll.contentView()
            clip.scrollToPoint_(NSMakePoint(0, inner_h - list_h))
            scroll.reflectScrolledClipView_(clip)
        y -= list_h + PAD

        # Timing section
        y = self._add_section_header(content, y, "Timing")
        y = self._add_int_field(content, y, "lead_time_minutes",
                                "Lead time (minutes before meeting)",
                                "Minutes before the meeting to alert.")
        y = self._add_int_field(content, y, "poll_interval_seconds",
                                "Polling interval (seconds)",
                                "Lower = alerts fire nearer the lead time.")
        y = self._add_int_field(content, y, "snooze_minutes",
                                "Snooze button duration (minutes)",
                                "Minutes Snooze defers the alert.")
        y = self._add_int_field(content, y, "final_snooze_minutes",
                                "Final snooze lead (minutes before start)",
                                "When the meeting is close, snooze re-fires "
                                "this many minutes before it starts.")
        y = self._add_int_field(content, y, "alert_timeout_seconds",
                                "Auto-dismiss after (seconds, 0 = never)",
                                "0 = stays up until you click it.")

        # Display section
        y = self._add_section_header(content, y, "Display")
        y = self._add_popup(content, y, "display_mode",
                            "Show alert on",
                            [("all", "All connected displays"),
                             ("main", "Main display only"),
                             ("focused", "Display with the focused app")])
        y = self._add_switch(content, y, "all_spaces",
                             "Show across all Spaces (including full-screen apps)")
        y = self._add_switch(content, y, "hide_from_screen_sharing",
                             "Hide the alert from screen sharing / recording (still shows on your screen)")

        # Filtering section. A little extra gap above the header (the switch
        # rows pack tighter than the Timing field rows, so this matches the
        # roomier Timing→Display spacing). These checkboxes are width-capped so
        # their click targets stay clear of the stacked action buttons.
        y -= 8
        y = self._add_section_header(content, y, "Filtering")
        y = self._add_switch(content, y, "skip_all_day",
                             "Skip all-day events", width=FILTER_SW_W)
        y = self._add_switch(content, y, "skip_unaccepted_meetings",
                             "Skip tentative / pending invitations (alert only for accepted meetings)",
                             width=FILTER_SW_W)
        y = self._add_switch(content, y, "notify_in_progress_meetings",
                             "Also alert for meetings already in progress when first discovered",
                             width=FILTER_SW_W)
        y = self._add_switch(content, y, "join_link_known_providers_only",
                             "Only show join links from known providers (Zoom, Meet, Teams, …)",
                             width=FILTER_SW_W)

        # Action buttons, stacked (Cancel on top, Save & Start below) in the
        # empty space to the right of the Filtering checkboxes — no separate
        # full-width row at the very bottom. `y` here is the cursor just below
        # the last checkbox.
        btn_x = WIN_W - PAD - BTN_W
        save_y = y + 2
        cancel_y = save_y + BTN_H + 10

        save_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(btn_x, save_y, BTN_W, BTN_H))
        save_btn.setTitle_("Save & Start")
        save_btn.setBezelStyle_(NSBezelStyleRounded)
        save_btn.setKeyEquivalent_("\r")
        save_btn.setTarget_(self)
        save_btn.setAction_("save:")
        content.addSubview_(save_btn)
        self._save_button = save_btn

        cancel_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(btn_x, cancel_y, BTN_W, BTN_H))
        cancel_btn.setTitle_("Cancel")
        cancel_btn.setBezelStyle_(NSBezelStyleRounded)
        cancel_btn.setTarget_(self)
        cancel_btn.setAction_("cancel:")
        content.addSubview_(cancel_btn)
        self._cancel_button = cancel_btn

        # Save/install progress text along the bottom edge (full width, left of
        # the button column's footprint), shown while the agent installs.
        status = NSTextField.alloc().initWithFrame_(
            NSMakeRect(PAD, PAD - 4, btn_x - PAD - 12, 22))
        status.setBezeled_(False)
        status.setDrawsBackground_(False)
        status.setEditable_(False)
        status.setSelectable_(False)
        status.setFont_(NSFont.systemFontOfSize_(13))
        status.setTextColor_(NSColor.secondaryLabelColor())
        content.addSubview_(status)
        self._status_label = status

    # ----- helpers for laying out form rows -----

    @objc.python_method
    def _make_section_header(self, rect, text):
        lbl = NSTextField.alloc().initWithFrame_(rect)
        lbl.setStringValue_(text)
        lbl.setFont_(NSFont.boldSystemFontOfSize_(13))
        lbl.setBezeled_(False)
        lbl.setDrawsBackground_(False)
        lbl.setEditable_(False)
        lbl.setSelectable_(False)
        return lbl

    @objc.python_method
    def _add_section_header(self, content, y, text):
        header = self._make_section_header(
            NSMakeRect(PAD, y - 18, WIN_W - 2 * PAD, 18), text)
        content.addSubview_(header)
        return y - 24

    @objc.python_method
    def _add_int_field(self, content, y, key, label, helptext):
        lbl = NSTextField.alloc().initWithFrame_(
            NSMakeRect(PAD, y - 20, LABEL_W, 20))
        lbl.setStringValue_(label)
        lbl.setBezeled_(False)
        lbl.setDrawsBackground_(False)
        lbl.setEditable_(False)
        lbl.setSelectable_(False)
        lbl.setFont_(NSFont.systemFontOfSize_(13))
        content.addSubview_(lbl)

        field = NSTextField.alloc().initWithFrame_(
            NSMakeRect(FIELD_X, y - 22, FIELD_W, 22))
        field.setStringValue_(str(self._settings.get(key, DEFAULTS[key])))
        field.setAlignment_(NSTextAlignmentCenter)
        content.addSubview_(field)
        self._field_controls[key] = field

        help_lbl = NSTextField.alloc().initWithFrame_(
            NSMakeRect(HELP_X, y - 20, WIN_W - PAD - HELP_X, 18))
        help_lbl.setStringValue_(helptext)
        help_lbl.setBezeled_(False)
        help_lbl.setDrawsBackground_(False)
        help_lbl.setEditable_(False)
        help_lbl.setSelectable_(False)
        help_lbl.setFont_(NSFont.systemFontOfSize_(11))
        help_lbl.setTextColor_(NSColor.secondaryLabelColor())
        content.addSubview_(help_lbl)
        return y - 30

    @objc.python_method
    def _add_popup(self, content, y, key, label, options):
        # A popup renders its title lower in the bezel than a text field does,
        # so nudge this label down 1px to meet the popup's text (the Timing
        # rows don't need this because their value control is a field).
        lbl = NSTextField.alloc().initWithFrame_(
            NSMakeRect(PAD, y - 21, LABEL_W, 20))
        lbl.setStringValue_(label)
        lbl.setBezeled_(False)
        lbl.setDrawsBackground_(False)
        lbl.setEditable_(False)
        lbl.setSelectable_(False)
        lbl.setFont_(NSFont.systemFontOfSize_(13))
        content.addSubview_(lbl)

        # This row's label ("Show alert on") is short, so place the popup just
        # after it rather than out in the numeric-field column (FIELD_X), where
        # it would float in empty space.
        popup = NSPopUpButton.alloc().initWithFrame_(
            NSMakeRect(PAD + 100, y - 19, 210, 22))
        current = self._settings.get(key, DEFAULTS[key])
        for (val, title) in options:
            popup.addItemWithTitle_(title)
            item = popup.lastItem()
            item.setRepresentedObject_(val)
            if val == current:
                popup.selectItem_(item)
        content.addSubview_(popup)
        self._display_popup = popup
        return y - 32

    @objc.python_method
    def _add_switch(self, content, y, key, label, width=None):
        if width is None:
            width = WIN_W - 2 * PAD
        cb = NSButton.alloc().initWithFrame_(
            NSMakeRect(PAD, y - 22, width, 22))
        cb.setButtonType_(NSButtonTypeSwitch)
        cb.setTitle_(label)
        if self._settings.get(key, DEFAULTS[key]):
            cb.setState_(1)
        content.addSubview_(cb)
        self._switch_controls[key] = cb
        return y - 28

    @objc.python_method
    def _show_modal_alert(self, title, body):
        alert = NSAlert.alloc().init()
        alert.setMessageText_(title)
        alert.setInformativeText_(body)
        alert.setAlertStyle_(NSAlertStyleInformational)
        alert.runModal()

    # ----- save / cancel actions -----

    @objc.python_method
    def _collect(self) -> tuple[dict, list[dict]]:
        settings = dict(self._settings)
        for key, field in self._field_controls.items():
            raw = str(field.stringValue() or "0").strip()
            try:
                settings[key] = int(raw)
            except ValueError:
                settings[key] = DEFAULTS[key]
        for key, switch in self._switch_controls.items():
            settings[key] = bool(switch.state())
        if self._display_popup is not None:
            sel = self._display_popup.selectedItem()
            if sel is not None and sel.representedObject() is not None:
                settings["display_mode"] = str(sel.representedObject())
        watched = []
        for cb, cal in self._calendar_rows:
            if cb.state():
                src = cal.source()
                watched.append({
                    "title":  str(cal.title()),
                    "source": str(src.title()) if src else None,
                })
        return settings, watched

    def save_(self, sender):
        settings, watched = self._collect()
        if not watched:
            self._show_modal_alert(
                "Pick at least one calendar",
                "Check the box next to one or more calendars you want "
                "MeetingNotifier to watch.")
            return
        try:
            save_settings(settings, watched)
        except Exception as exc:
            self._show_modal_alert("Couldn't save config", str(exc))
            return
        # Disable buttons so a frustrated double-click can't re-enter.
        if self._save_button:
            self._save_button.setEnabled_(False)
        if self._cancel_button:
            self._cancel_button.setEnabled_(False)
        if getattr(sys, "frozen", False):
            # Bundled .app: install/restart the LaunchAgent. Defer to the next
            # runloop tick so the status update paints before we block on
            # launchctl. Also start a 1Hz tick timer that animates dots after
            # the status label so the user has visible feedback the work is
            # progressing during the ~15s install window.
            if self._status_label:
                self._status_label.setStringValue_("Installing background agent.")
            self._install_dots = 1
            self._install_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                1.0, self, "tickStatus:", None, True)
            self.performSelector_withObject_afterDelay_("doInstall:", None, 0.1)
        else:
            # Source mode (running setup_gui.py via venv python). The detected
            # "app path" points at the source dir which doesn't have a real
            # binary, so writing a LaunchAgent plist here would be broken.
            # Just confirm the config write and quit.
            if self._status_label:
                self._status_label.setStringValue_(
                    "Config saved. Restart your background poller to apply.")
            self.performSelector_withObject_afterDelay_("doQuit:", None, 0.7)

    def doInstall_(self, sender):
        # Run the launchctl waterfall on a worker thread so the main runloop
        # keeps painting / responding to clicks. Otherwise the user gets a
        # ~15s SBOD while we shell out to launchctl print/bootout/sleep/
        # bootstrap/kickstart in sequence.
        import threading
        self._install_error = None
        threading.Thread(target=self._installWorker, daemon=True).start()

    @objc.python_method
    def _installWorker(self):
        try:
            install_or_restart_launchagent(detect_app_path())
        except Exception as exc:
            self._install_error = exc
        # Bounce back to the main thread to finish (modal alert + terminate
        # both require the main runloop).
        self.performSelectorOnMainThread_withObject_waitUntilDone_(
            "installFinished:", None, False)

    def tickStatus_(self, timer):
        # Flash the status on/off each tick AND extend the dots on the
        # visible-phase ticks. The growing dots show monotonic progress
        # for the user who happens to look during a visible phase; the
        # flash forces the eye to register motion at the periphery.
        self._install_visible = not getattr(self, "_install_visible", False)
        if self._status_label is None:
            return
        if self._install_visible:
            self._install_dots += 1
            self._status_label.setStringValue_(
                "Installing background agent" + ("." * self._install_dots))
        else:
            self._status_label.setStringValue_("")

    def installFinished_(self, sender):
        # Stop the dot-tick timer before showing the result alert or quitting.
        timer = getattr(self, "_install_timer", None)
        if timer is not None:
            timer.invalidate()
            self._install_timer = None
        err = getattr(self, "_install_error", None)
        if err is not None:
            self._show_modal_alert(
                "Couldn't (re)start the background agent",
                f"Config saved to {CONFIG_PATH}, but the LaunchAgent "
                f"install/restart failed:\n\n{err}\n\nYou can install it "
                "manually with the CLI tools described in the README.")
            if self._save_button:
                self._save_button.setEnabled_(True)
            if self._cancel_button:
                self._cancel_button.setEnabled_(True)
            return
        # Hide the settings window BEFORE showing the success alert so the
        # modal isn't visually offset above a now-irrelevant settings window.
        # With nothing behind it, the modal sits cleanly on a blank screen at
        # macOS's standard dialog position (~1/3 from the top, centered
        # horizontally) - same placement Apple uses for system dialogs.
        if self._window is not None:
            self._window.orderOut_(None)
        self._show_modal_alert(
            "Installation successful",
            "MeetingNotifier is now running in the background. "
            "You will get a large centered alert before each meeting "
            "starts. Click OK to close this window.")
        NSApp.terminate_(None)

    def doQuit_(self, sender):
        NSApp.terminate_(None)

    def cancel_(self, sender):
        # Defer terminate by one runloop tick so the button click visibly
        # registers before the app exits — avoids the impression of an SBOD.
        if self._save_button:
            self._save_button.setEnabled_(False)
        if self._cancel_button:
            self._cancel_button.setEnabled_(False)
        self.performSelector_withObject_afterDelay_("doQuit:", None, 0.05)


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def main() -> int:
    app = NSApplication.sharedApplication()
    # Regular activation policy = dock icon + menu bar, like any normal app.
    # We're not running as background-only here.
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    controller = SettingsWindow.alloc().init()
    # Defer the permission request + window build until AFTER NSApp.run()
    # is pumping the main runloop and the app is fully foreground. macOS
    # Sequoia's tccd will not surface the Calendar permission dialog if the
    # app hasn't reached this state by the time of the request.
    controller.performSelector_withObject_afterDelay_("startSetup:", None, 0.1)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
