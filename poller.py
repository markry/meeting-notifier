#!/usr/bin/env python3
"""meeting-notifier poller core.

Polls Apple Calendar.app (via EventKit) for upcoming events in user-configured
calendars and fires an alert `lead_time_minutes` before each event starts.

Current state: alert dispatch is a STUB that prints to stdout. The real overlay
window comes in the next phase. This file is structured so that swapping the
print-stub for the real overlay is a one-function change.

Usage:
    python3 poller.py [--config PATH] [--once] [--list]

Flags:
    --config PATH   Use a specific config file (default: ./config.toml, then
                    ~/.config/meeting-notifier/config.toml)
    --once          Run one poll cycle and exit (good for cron/launchd diagnostics)
    --list          Print the discovered calendars (like list_calendars.py) and exit
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
import platform
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore

import subprocess

from EventKit import EKEventStore, EKEntityTypeEvent
from Foundation import NSRunLoop, NSDate

from overlay import (AlertInfo, minutes_until_display, effective_snooze_minutes,
                     POST_START_SNOOZE_MINUTES)
                                # AlertInfo: the data class passed to the alert
                                # subprocess; minutes_until_display: shared
                                # rounding so the first popup and the overlay's
                                # 30s refresh agree on the minute shown;
                                # effective_snooze_minutes: shared normal-vs-final
                                # snooze decision so the button label and the
                                # re-fire timing agree.


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class CalendarMatch:
    """One entry under [[calendars]] in config.toml."""
    title: str | None = None
    identifier: str | None = None
    source: str | None = None

    def matches(self, cal) -> bool:
        """True if the given EKCalendar matches this config entry."""
        if self.identifier:
            return str(cal.calendarIdentifier()) == self.identifier
        if self.title and str(cal.title()) != self.title:
            return False
        if self.source:
            src = cal.source()
            return bool(src and str(src.title()) == self.source)
        # title-only and matched: yes
        return self.title is not None


@dataclass
class Config:
    lead_time_minutes: int = 5
    poll_interval_seconds: int = 20
    lookahead_seconds: int = 900
    snooze_minutes: int = 2
    # Final snooze offered once the meeting is closer than snooze_minutes, so
    # the alert's Snooze button doesn't propose a delay that overshoots the
    # start (the "meeting in 2 min, snooze for 3" nonsense). Instead of a fixed
    # delay it re-fires this many minutes BEFORE the start; shown as "Snooze
    # to M min before". Set <= 0 or >= snooze_minutes to disable (the normal
    # snooze is used then). See overlay.effective_snooze_minutes for the rule.
    final_snooze_minutes: int = 1
    # Auto-dismiss after N seconds if the user doesn't interact. 0 = never
    # auto-dismiss (alert stays up until clicked — the default, so you can
    # still see the alert if you were away from your desk when it fired).
    alert_timeout_seconds: int = 0
    # Which displays the alert appears on:
    #   "all"     — every connected display (default; can't miss an alert
    #               because you were looking at a different monitor)
    #   "main"    — primary display only
    #   "focused" — the display showing the currently-focused app
    display_mode: str = "all"
    # Show the alert on every macOS Space simultaneously, including overlaying
    # full-screen apps. Default True so you don't miss alerts when switched
    # away from your normal Space.
    all_spaces: bool = True
    # When True, fire one alert for any meeting that's currently in progress
    # (started in the past, hasn't ended yet) the first time the poller sees
    # it. Useful for the "notifier just started up mid-day and there's already
    # a meeting running" case. Default False preserves classic pre-meeting-only
    # behavior. Snoozed re-fires already ignore the lead-time window, so this
    # only changes the initial-fire path.
    notify_in_progress_meetings: bool = False
    # When True, skip events the user hasn't accepted (Tentative / Pending /
    # Declined invitations). Events with no attendees array — typically things
    # the user put on their own calendar without an invite flow — are treated
    # as accepted and NOT skipped. Default False preserves current "alert on
    # everything that lives on the calendar" behavior.
    skip_unaccepted_meetings: bool = False
    use_overlay: bool = True   # False = print-stub (for headless / testing)
    calendars: list[CalendarMatch] = field(default_factory=list)
    skip_title_substrings: list[str] = field(default_factory=list)
    skip_all_day: bool = True
    show_location: bool = True
    show_join_link: bool = True
    # When True (default), only URLs matching a known meeting-provider pattern
    # (Zoom/Meet/Teams/Webex/...) become the clickable "Join" button. When
    # False, the first arbitrary http(s) URL found anywhere in the event's
    # notes/location is used as a fallback. The fallback is convenient for
    # in-house video systems but is a phishing vector: anyone who can land an
    # invite on a watched calendar could surface `Join (evil.example)` in a
    # trusted, reflex-click context. Leave True unless you rely on a custom
    # provider. (L2)
    join_link_known_providers_only: bool = True
    # When True (default), the alert window is excluded from screen capture /
    # sharing / recording (Zoom, Teams, Meet, screenshots). It still shows on
    # the local display; viewers see whatever is behind it. Keeps your
    # next-meeting details off a shared screen. Not foolproof across every
    # capture path, but covers the common screen-sharing tools.
    hide_from_screen_sharing: bool = True


def _safe_write_text(path: Path, content: str, mode: int = 0o600) -> None:
    """Write text to `path` atomically, refusing if the existing path is a
    symlink. Defends against a TOCTOU class of attack where another local
    process plants a symlink in a writable parent directory between our
    existence check and our open, redirecting our write somewhere we don't
    intend. Also writes via tempfile+rename so a crash mid-write can't leave
    a half-written file."""
    import os, tempfile
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    # lstat raises FileNotFoundError if absent — that's fine, we'll create it.
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        st = None
    if st is not None and (st.st_mode & 0o170000) == 0o120000:  # S_IFLNK
        raise OSError(
            f"refusing to write through symlink at {path}; "
            "remove the symlink and re-run if this was intentional")
    fd, tmp_path = tempfile.mkstemp(dir=str(parent), prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def _clamp(value: int, lo: int, default: int) -> int:
    """Clamp an integer config value to at least `lo`; substitute default if
    the value is below lo (preventing a `time.sleep(0)` daemon spin from a
    typo or malicious config edit)."""
    return max(lo, value) if value >= lo else default


def load_config(path: Path) -> Config:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    cfg = Config(
        lead_time_minutes=max(0, int(data.get("lead_time_minutes", 5))),
        poll_interval_seconds=_clamp(int(data.get("poll_interval_seconds", 20)), 5, 20),
        lookahead_seconds=_clamp(int(data.get("lookahead_seconds", 900)), 60, 900),
        snooze_minutes=max(1, int(data.get("snooze_minutes", 2))),
        final_snooze_minutes=max(0, int(data.get("final_snooze_minutes", 1))),
        alert_timeout_seconds=max(0, int(data.get("alert_timeout_seconds", 0))),
        display_mode=str(data.get("display_mode", "all")),
        all_spaces=bool(data.get("all_spaces", True)),
        notify_in_progress_meetings=bool(data.get("notify_in_progress_meetings", False)),
        skip_unaccepted_meetings=bool(data.get("skip_unaccepted_meetings", False)),
        use_overlay=bool(data.get("use_overlay", True)),
        skip_title_substrings=list(data.get("skip_title_substrings", [])),
        skip_all_day=bool(data.get("skip_all_day", True)),
        show_location=bool(data.get("show_location", True)),
        show_join_link=bool(data.get("show_join_link", True)),
        join_link_known_providers_only=bool(
            data.get("join_link_known_providers_only", True)),
        hide_from_screen_sharing=bool(data.get("hide_from_screen_sharing", True)),
    )
    for entry in data.get("calendars", []):
        cfg.calendars.append(CalendarMatch(
            title=entry.get("title"),
            identifier=entry.get("identifier"),
            source=entry.get("source"),
        ))
    if not cfg.calendars:
        raise SystemExit(
            f"config at {path} defines no [[calendars]] entries; nothing to watch")
    return cfg


def find_config(explicit: Path | None) -> Path:
    if explicit:
        if not explicit.exists():
            raise SystemExit(f"--config path does not exist: {explicit}")
        return explicit
    here = Path(__file__).resolve().parent / "config.toml"
    if here.exists():
        return here
    xdg = Path(os.environ.get(
        "XDG_CONFIG_HOME",
        Path.home() / ".config")) / "meeting-notifier" / "config.toml"
    if xdg.exists():
        return xdg
    raise SystemExit(
        "No config.toml found. Copy config.example.toml to config.toml "
        "and edit it. Searched: " + ", ".join(str(p) for p in [here, xdg]))


# ---------------------------------------------------------------------------
# EventKit helpers
# ---------------------------------------------------------------------------


def macos_major() -> int:
    try:
        return int(platform.mac_ver()[0].split(".")[0])
    except (ValueError, IndexError):
        return 0


def request_access(store: EKEventStore) -> bool:
    """Request Calendar access, pumping the runloop until the completion handler fires.

    Note: The Hardened Runtime + Developer ID bundle MUST carry the
    `com.apple.security.personal-information.calendars` entitlement on macOS
    26 (Tahoe) and later, otherwise tccd silently rejects this request before
    showing the system dialog. The entitlement is wired in entitlements.plist.
    """
    state = {"done": False, "granted": False, "error": None}

    def handler(granted, error):
        state["granted"] = bool(granted)
        state["error"] = error
        state["done"] = True

    if macos_major() >= 14 and hasattr(store, "requestFullAccessToEventsWithCompletion_"):
        store.requestFullAccessToEventsWithCompletion_(handler)
    else:
        store.requestAccessToEntityType_completion_(EKEntityTypeEvent, handler)

    deadline = 600  # ~60s
    while not state["done"] and deadline > 0:
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.1))
        deadline -= 1
    if not state["granted"]:
        sys.stderr.write(
            "Calendar access denied. Grant it in System Settings > "
            "Privacy & Security > Calendars, then re-run.\n")
    return state["granted"]


def resolve_calendars(store: EKEventStore, cfg: Config):
    """Resolve config entries to actual EKCalendar objects. Warn on unmatched entries."""
    all_cals = list(store.calendarsForEntityType_(EKEntityTypeEvent))
    resolved = []
    available_titles = [str(c.title()) for c in all_cals]
    for entry in cfg.calendars:
        matches = [c for c in all_cals if entry.matches(c)]
        if not matches:
            # Promote the warning to stdout too — stderr-only is too easy to
            # miss in launchd setups where stderr goes to a separate log file.
            msg = (f"WARNING: config entry {entry!r} matched 0 calendars. "
                   f"Available calendar titles on this Mac: {available_titles}")
            print(msg, flush=True)
            sys.stderr.write(msg + "\n")
            continue
        for c in matches:
            resolved.append(c)
    if not resolved:
        raise SystemExit("No calendars resolved from config — nothing to watch.")
    return resolved


def upcoming_events(store: EKEventStore, calendars, lookahead_seconds: int):
    """Return EKEvents starting within the next `lookahead_seconds`."""
    now = NSDate.date()
    end = NSDate.dateWithTimeIntervalSinceNow_(lookahead_seconds)
    predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
        now, end, calendars)
    return list(store.eventsMatchingPredicate_(predicate))


# ---------------------------------------------------------------------------
# Alert filtering + dispatch
# ---------------------------------------------------------------------------


# Known meeting-service URL patterns, in priority order. The extractor checks
# these first so that — for example — a Google Meet link in event.location wins
# over an unrelated tracking URL pasted into event.notes. Add more here as
# needed; ordering within the list is "first match wins."
PREFERRED_MEETING_PATTERNS = [
    # Google Meet: https://meet.google.com/abc-defg-hij
    re.compile(r"https?://meet\.google\.com/[a-z0-9?=&-]+", re.IGNORECASE),
    # Zoom: https://zoom.us/j/1234567890 or https://us02web.zoom.us/j/...?pwd=...
    re.compile(r"https?://[a-z0-9.-]*zoom\.us/j/\d+(?:\?[^\s<>\"'\)]*)?", re.IGNORECASE),
    # Zoom personal meeting room: https://us02web.zoom.us/my/yourname
    re.compile(r"https?://[a-z0-9.-]*zoom\.us/my/[\w/.-]+(?:\?[^\s<>\"'\)]*)?", re.IGNORECASE),
    # Microsoft Teams: https://teams.microsoft.com/l/meetup-join/...
    re.compile(r"https?://teams\.microsoft\.com/l/meetup-join/[^\s<>\"'\)]+", re.IGNORECASE),
    # Webex: https://company.webex.com/meet/user or .../wbxmjs/...
    re.compile(r"https?://[a-z0-9.-]*webex\.com/(?:meet|wbxmjs|join|j\.php)[^\s<>\"'\)]+", re.IGNORECASE),
    # GoToMeeting
    re.compile(r"https?://[a-z0-9.-]*gotomeeting\.com/join/\d+", re.IGNORECASE),
    # BlueJeans
    re.compile(r"https?://[a-z0-9.-]*bluejeans\.com/\d+(?:[?/][^\s<>\"'\)]*)?", re.IGNORECASE),
    # Whereby
    re.compile(r"https?://whereby\.com/[\w-]+", re.IGNORECASE),
    # Jitsi
    re.compile(r"https?://meet\.jit\.si/[^\s<>\"'\)]+", re.IGNORECASE),
]

# Fallback for "any URL" — used only when no known-provider pattern matched.
_URL_RE_GENERIC = re.compile(r"https?://[^\s<>\"'\)]+")


def should_skip(event, cfg: Config) -> bool:
    if cfg.skip_all_day and event.isAllDay():
        return True
    title = str(event.title() or "")
    lo = title.lower()
    for needle in cfg.skip_title_substrings:
        if needle.lower() in lo:
            return True
    if cfg.skip_unaccepted_meetings and _user_response_not_accepted(event):
        return True
    return False


# EKParticipantStatus values we care about.
# 0=Unknown 1=Pending 2=Accepted 3=Declined 4=Tentative
# 5=Delegated 6=Completed 7=InProcess
_EK_STATUS_ACCEPTED = 2


def _user_response_not_accepted(event) -> bool:
    """True iff the user IS an attendee on this event and their response
    is anything other than Accepted (Tentative, Pending, Declined, etc.).

    Returns False (don't skip) when:
      - The event has no attendees array (self-created, calendar-feed item)
      - The current user isn't in the attendees list
      - The user's status is Accepted
    """
    attendees = event.attendees()
    if not attendees:
        return False
    for participant in attendees:
        is_me = False
        try:
            is_me = bool(participant.isCurrentUser())
        except AttributeError:
            pass
        if is_me:
            try:
                status = int(participant.participantStatus())
            except (AttributeError, TypeError):
                return False
            return status != _EK_STATUS_ACCEPTED
    return False


def extract_join_link(event, known_only: bool = True) -> str | None:
    """Pull the best candidate "join meeting" URL out of an event.

    Strategy:
      1. Concatenate all text-bearing fields (notes/location/URL).
      2. Try each PREFERRED_MEETING_PATTERNS in order — a Google Meet link
         beats an unrelated tracking URL, a Zoom link beats a calendar
         invitation URL, etc.
      3. If `known_only` is False, fall back to the first http(s) URL found
         anywhere when no preferred pattern matched. When True (the default),
         no generic fallback is used — an unrecognized URL is NOT turned into
         a clickable Join button (anti-phishing; see Config.
         join_link_known_providers_only).
    """
    parts = []
    for field_name in ("notes", "location", "URL"):
        getter = getattr(event, field_name, None)
        if not getter:
            continue
        val = getter()
        if not val:
            continue
        if hasattr(val, "absoluteString"):
            val = val.absoluteString()
        parts.append(str(val))
    text = "\n".join(parts)

    for pattern in PREFERRED_MEETING_PATTERNS:
        m = pattern.search(text)
        if m:
            return _trim_url(m.group(0))

    if known_only:
        return None
    m = _URL_RE_GENERIC.search(text)
    return _trim_url(m.group(0)) if m else None


# Characters that can visually disguise the true target of a URL: ASCII
# control chars (C0 + DEL) and Unicode bidi / zero-width formatting chars.
# We strip these from URLs before storing or displaying so a malicious event
# notes field can't include "https://goodco.com‮/evilco.com" that
# right-to-left-overrides into the user's eye as the good domain.
_URL_SAFE_STRIP = (
    set(chr(c) for c in range(0x00, 0x20)) | {chr(0x7F)} |
    {chr(c) for c in (
        0x200B, 0x200C, 0x200D,                       # zero-width spaces / joiners
        0x200E, 0x200F,                               # LRM/RLM
        0x202A, 0x202B, 0x202C, 0x202D, 0x202E,       # bidi embeddings / overrides
        0x2066, 0x2067, 0x2068, 0x2069,               # bidi isolates
        0xFEFF,                                       # BOM / zero-width nbsp
    )}
)


def _trim_url(url: str) -> str:
    """Strip trailing punctuation the URL regex commonly over-captures, plus
    any bidi / zero-width / control characters that could disguise the URL's
    true target when rendered."""
    url = "".join(c for c in url if c not in _URL_SAFE_STRIP)
    while url and url[-1] in ".,;:!?>)]}'\"":
        url = url[:-1]
    return url


def _build_alert_info(event, cfg: Config, now_utc: datetime) -> AlertInfo:
    """Marshal event fields into an AlertInfo for the overlay."""
    start_utc = datetime.fromtimestamp(
        event.startDate().timeIntervalSince1970(), tz=timezone.utc)
    start_local = start_utc.astimezone()
    # Rounded to nearest minute (and negative when already started); see
    # minutes_until_display. Negative means the meeting has already started
    # (only reached when notify_in_progress_meetings=True) and the overlay
    # renders "Already started" rather than "Starts in 0 minutes".
    minutes_until = minutes_until_display(start_utc, now_utc)
    location = None
    if cfg.show_location:
        loc = event.location()
        if loc:
            location = str(loc)
    join = None
    if cfg.show_join_link:
        join = extract_join_link(event, known_only=cfg.join_link_known_providers_only)
    return AlertInfo(
        title=str(event.title() or "(no title)"),
        start_str=start_local.strftime("%-I:%M %p"),
        minutes_until=minutes_until,
        location=location,
        join_link=join,
        start_utc=start_utc,
    )


# Exit codes for the alert subprocess. Chosen in the 100+ range so they don't
# collide with common crash codes (Python uses 1 for unhandled exceptions and
# 2 for argparse errors, etc.). If we see a non-recognized code we treat it
# as a dispatch failure rather than silently mapping an unrelated crash code
# to a user action (the 0.2.5 bug where exit code 2 from a broken alert-
# subprocess invocation was mis-interpreted as "user clicked link").
_EXIT_CODE_TO_RESULT = {
    100: "dismiss",
    101: "snooze",
    102: "link",
    103: "timeout",
    104: "until_start",
}


def fire_alert(event, cfg: Config, now_utc: datetime) -> str:
    """Spawn the alert subprocess. Returns the action: 'dismiss' | 'snooze' | 'link' | 'timeout'.

    The alert subprocess (`alert_runner.py`) handles the entire NSWindow
    lifecycle in its own process and exits with a code that maps back to the
    result string. Running the modal session in a separate process eliminates
    a class of "window stayed up after click" bugs we hit when the modal ran
    inline inside the long-running poller daemon — process termination
    guarantees the window is gone.
    """
    import sys
    info = _build_alert_info(event, cfg, now_utc)
    # Log an opaque event identifier, never the meeting title — titles are PII
    # and launchd appends these logs forever (L4). The identifier is enough to
    # correlate a fire with a calendar event during debugging.
    print(f"[poller] fire_alert: launching alert subprocess for event "
          f"{str(event.eventIdentifier())}", flush=True, file=sys.stderr)

    if not cfg.use_overlay:
        # Headless print fallback (also useful for tests / launchd debugging).
        lines = [
            "=" * 56,
            "  ALERT:",
            "  " + info.title,
            f"  starts in {info.minutes_until} min ({info.start_str})",
        ]
        if info.location:
            lines.append("  location: " + info.location)
        if info.join_link:
            lines.append("  join: " + info.join_link)
        lines.append("=" * 56)
        print("\n".join(lines), flush=True)
        return "dismiss"

    # Build the subprocess command. Two modes:
    #   - source install: spawn `python3 alert_runner.py --title ...`
    #   - py2app .app bundle ("frozen"): re-invoke the bundle's LAUNCHER binary
    #     (Contents/MacOS/MeetingNotifier) with `alert` as the first arg, so
    #     main.py's dispatcher routes to alert_runner.main(). sys.executable
    #     in py2app is Contents/MacOS/python (the framework binary, not the
    #     launcher) — invoking THAT with "alert" makes Python interpret "alert"
    #     as a script path, which fails. The 0.2.5 bug. Find the launcher via
    #     siblings of sys.executable.
    if getattr(sys, "frozen", False):
        launcher = Path(sys.executable).resolve().parent / "MeetingNotifier"
        cmd = [str(launcher), "alert", "--json-stdin"]
    else:
        here = Path(__file__).resolve().parent
        cmd = [sys.executable, str(here / "alert_runner.py"), "--json-stdin"]

    # Pass all alert parameters as a JSON document on the subprocess's stdin
    # rather than as argv. Two reasons:
    #   1. Meeting fields (title, location, join link incl. Zoom ?pwd= tokens)
    #      are calendar-controlled PII. argv is world-readable via `ps` for the
    #      life of the alert; stdin is not. (L1)
    #   2. A crafted title like "--once" or "-Standup" (leading dash, no space)
    #      would be mis-parsed by argparse as an option, crashing the alert
    #      subprocess every poll cycle and silently suppressing the alert — a
    #      DoS triggerable by anyone who can send a calendar invite. JSON values
    #      have no such ambiguity. (M1)
    payload = {
        "title": info.title,
        "start_str": info.start_str,
        "minutes_until": info.minutes_until,
        "start_utc_iso": info.start_utc.isoformat(),
        "snooze_minutes": cfg.snooze_minutes,
        "final_snooze_minutes": cfg.final_snooze_minutes,
        "timeout_seconds": cfg.alert_timeout_seconds,
        "display_mode": cfg.display_mode,
        "all_spaces": cfg.all_spaces,
        "hide_from_screen_sharing": cfg.hide_from_screen_sharing,
        "location": info.location,
        "join_link": info.join_link,
    }

    try:
        # Inherit stdout/stderr so the alert subprocess's diagnostic prints
        # land in our LaunchAgent log files alongside the poller's. stdin is
        # fed the JSON payload (subprocess.run sets stdin=PIPE when input= is
        # given; stdout/stderr stay inherited).
        import json
        proc = subprocess.run(cmd, input=json.dumps(payload), text=True)
    except Exception as exc:
        print(f"[poller] fire_alert: subprocess failed: {exc!r}",
              flush=True, file=sys.stderr)
        return "dismiss"

    result = _EXIT_CODE_TO_RESULT.get(proc.returncode)
    if result is None:
        # Unrecognized exit code = the subprocess didn't reach our own exit
        # path. Most likely a crash before alert_runner.main() returned.
        # Log loudly and signal failure to the caller so it doesn't mark the
        # event as fired (and so on next poll we'll try again).
        print(f"[poller] fire_alert: alert subprocess crashed (returncode={proc.returncode}); "
              f"event will NOT be marked fired", flush=True, file=sys.stderr)
        return "failed"
    print(f"[poller] fire_alert: subprocess exited code={proc.returncode}, "
          f"result={result!r}", flush=True, file=sys.stderr)
    return result


# ---------------------------------------------------------------------------
# Poll loop
# ---------------------------------------------------------------------------


def run_once(store, calendars, cfg: Config, fired: dict,
             snoozed_until: dict, now_utc: datetime) -> int:
    """One poll cycle. Returns the number of alerts fired this cycle.

    Two fire paths:
      1. Snoozed re-fire: if `snoozed_until[id]` has elapsed, fire regardless
         of whether the meeting has started. (User explicitly asked to be
         reminded — honor that even if the meeting is now in progress.)
      2. Initial fire: not snoozed; standard "now is within lead_time minutes
         before start" check.
    """
    events = upcoming_events(store, calendars, cfg.lookahead_seconds)
    fire_threshold = now_utc + timedelta(minutes=cfg.lead_time_minutes)
    n_fired = 0
    for event in events:
        if should_skip(event, cfg):
            continue
        ident = str(event.eventIdentifier())
        if ident in fired:
            continue

        snooze_time = snoozed_until.get(ident)
        if snooze_time is not None:
            # Snoozed: re-fire the moment the snooze elapses; don't filter on
            # lead-time anymore. If the meeting is already in progress, that's
            # fine — the user asked to be reminded.
            if now_utc < snooze_time:
                continue
        else:
            # No snooze pending. Two fire conditions on the initial-fire path:
            #  (a) Standard pre-meeting alert window: now <= start <= now+lead
            #  (b) In-progress catch-up (only if user opted in): start already
            #      past, end still future, and we've never fired for this event
            #      before (the `fired in ident` check above already enforces
            #      "first time we see it").
            start_utc = datetime.fromtimestamp(
                event.startDate().timeIntervalSince1970(), tz=timezone.utc)
            in_window = now_utc <= start_utc <= fire_threshold
            in_progress = False
            if (not in_window
                    and cfg.notify_in_progress_meetings
                    and now_utc > start_utc):
                end_utc = datetime.fromtimestamp(
                    event.endDate().timeIntervalSince1970(), tz=timezone.utc)
                in_progress = now_utc <= end_utc
            if not (in_window or in_progress):
                continue

        import sys
        result = fire_alert(event, cfg, now_utc)
        print(f"[poller] run_once: fire_alert returned {result!r}, updating state",
              flush=True, file=sys.stderr)
        if result == "failed":
            # Don't mark fired — next poll cycle will retry. Loud log lines
            # in fire_alert have already explained the crash.
            continue
        if result == "snooze":
            # Mirror what the alert's button actually offered. Recompute against
            # a FRESH now (the alert may have sat on screen a while before the
            # click) so the re-fire lands where the clicked label promised, and
            # so a long-sitting alert that crossed into final-snooze territory
            # is treated as final rather than using the stale fire-time decision.
            #
            # Normal snooze: re-fire snooze_minutes from now.
            # Final snooze ("Snooze to M min before", offered once the meeting
            # is within snooze_minutes of starting): re-fire M minutes *before*
            # the start, so the last reminder lands just ahead of the meeting.
            # If that target is already past — we're within M minutes of start,
            # or the meeting is underway (in-progress catch-up) — fall back to a
            # fixed short re-nudge rather than re-firing instantly in a loop.
            snooze_now = datetime.now(timezone.utc)
            start_utc = datetime.fromtimestamp(
                event.startDate().timeIntervalSince1970(), tz=timezone.utc)
            snooze_mins, is_final = effective_snooze_minutes(
                minutes_until_display(start_utc, snooze_now),
                cfg.snooze_minutes, cfg.final_snooze_minutes)
            target = start_utc - timedelta(minutes=snooze_mins) if is_final else None
            if target is not None and target > snooze_now:
                snoozed_until[ident] = target
            else:
                snoozed_until[ident] = snooze_now + timedelta(minutes=snooze_mins)
            # Do NOT add to fired — we want to fire again after the snooze
        elif result == "until_start":
            # Secondary button. Before the meeting it read "Snooze until start"
            # → re-fire at the start itself. Once the meeting has begun the
            # button no longer greys out — it reads "Snooze for N minutes" → a
            # fixed re-nudge from now, so it stays useful for last-minute work.
            # Recompute against a fresh now (the alert may have sat on screen,
            # crossing the start) so the re-fire matches the clicked label.
            # Don't mark fired either way, so a future alert still happens.
            until_now = datetime.now(timezone.utc)
            start_utc = datetime.fromtimestamp(
                event.startDate().timeIntervalSince1970(), tz=timezone.utc)
            if start_utc > until_now:
                snoozed_until[ident] = start_utc
            else:
                snoozed_until[ident] = until_now + timedelta(
                    minutes=POST_START_SNOOZE_MINUTES)
            # Do NOT add to fired — we want to fire again later
        else:
            fired[ident] = datetime.fromtimestamp(
                event.startDate().timeIntervalSince1970(), tz=timezone.utc)
            snoozed_until.pop(ident, None)
        n_fired += 1
        print(f"[poller] run_once: state updated, n_fired={n_fired}",
              flush=True, file=sys.stderr)
    return n_fired


def prune_fired(fired: dict, snoozed_until: dict, now_utc: datetime,
                retention_seconds: int = 3600) -> None:
    """Remove old entries from the dedup dicts.

    `fired`: drop entries whose start was >retention_seconds ago.
    `snoozed_until`: drop entries whose snooze elapsed >24h ago. This handles
    the case where a user snoozes an alert and then the event is deleted from
    the calendar before the snooze elapses - without this prune the entry
    sits forever, an unbounded slow memory leak on a long-running daemon.
    """
    cutoff = now_utc - timedelta(seconds=retention_seconds)
    for k in [k for k, ts in fired.items() if ts < cutoff]:
        del fired[k]
    snooze_cutoff = now_utc - timedelta(hours=24)
    for k in [k for k, ts in snoozed_until.items() if ts < snooze_cutoff]:
        del snoozed_until[k]


def _find_example_config() -> Path | None:
    """Locate config.example.toml in either the py2app bundle or the source tree."""
    candidates = []
    # py2app bundle: <App>.app/Contents/Resources/config.example.toml. The frozen
    # executable lives in Contents/MacOS/, so two parents up + Resources is right.
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent.parent / "Resources" / "config.example.toml")
    # Source layout: alongside poller.py
    candidates.append(Path(__file__).resolve().parent / "config.example.toml")
    for c in candidates:
        if c.exists():
            return c
    return None


def init_config() -> int:
    """Scaffold ~/.config/meeting-notifier/config.toml from the bundled example.

    Returns a process exit code (0 = success).
    """
    target = Path.home() / ".config" / "meeting-notifier" / "config.toml"
    if target.exists():
        print(f"Config already exists at {target}", file=sys.stderr)
        print("Open it in your editor to change calendars or other options.",
              file=sys.stderr)
        return 0
    src = _find_example_config()
    if src is None:
        print("ERROR: config.example.toml not found in bundle or source tree.",
              file=sys.stderr)
        return 1
    _safe_write_text(target, src.read_text(encoding="utf-8"), mode=0o600)
    print(f"Wrote {target}")
    print()
    print("Next steps:")
    print("  1. Run `--list` to see your available calendars.")
    print(f"  2. Edit {target} — the [[calendars]] entries decide which")
    print("     calendars are watched. The file is fully commented; every option")
    print("     has an explanation right above it.")
    print("  3. Install the LaunchAgent so the poller runs in the background.")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=None,
                        help="Path to config file (default: search ./config.toml then ~/.config/meeting-notifier/)")
    parser.add_argument("--once", action="store_true",
                        help="Run one poll cycle and exit")
    parser.add_argument("--list", action="store_true",
                        help="List discovered calendars and exit")
    parser.add_argument("--init-config", action="store_true",
                        help="Write a starter config.toml to ~/.config/meeting-notifier/ and exit")
    parser.add_argument("--daemon", action="store_true",
                        help="Run in LaunchAgent mode (no-op flag; signals launchd-managed invocation)")
    args = parser.parse_args()

    # --init-config doesn't need calendar access; handle it first.
    if args.init_config:
        sys.exit(init_config())

    store = EKEventStore.alloc().init()
    if not request_access(store):
        sys.exit(1)

    if args.list:
        for c in store.calendarsForEntityType_(EKEntityTypeEvent):
            src = c.source()
            print(f"title={str(c.title())!r}  source={str(src.title()) if src else None!r}  "
                  f"identifier={str(c.calendarIdentifier())}")
        return

    cfg_path = find_config(args.config)
    cfg = load_config(cfg_path)
    print(f"Loaded config from {cfg_path}", flush=True)

    calendars = resolve_calendars(store, cfg)
    print(f"Watching {len(calendars)} calendar(s): "
          + ", ".join(repr(str(c.title())) for c in calendars), flush=True)

    fired: dict[str, datetime] = {}
    snoozed_until: dict[str, datetime] = {}
    last_prune = time.time()

    while True:
        now_utc = datetime.now(timezone.utc)
        n = run_once(store, calendars, cfg, fired, snoozed_until, now_utc)
        if time.time() - last_prune > 3600:
            prune_fired(fired, snoozed_until, now_utc)
            last_prune = time.time()
        if args.once:
            print(f"--once: {n} alert(s) fired this cycle.", flush=True)
            return
        time.sleep(cfg.poll_interval_seconds)


if __name__ == "__main__":
    main()
