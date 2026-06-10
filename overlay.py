#!/usr/bin/env python3
"""Borderless centered NSWindow overlay for meeting-notifier.

`show_alert()` is the public entry point. It builds one or more windows
(depending on `display_mode`) containing the meeting details + Dismiss /
Snooze / Join Link buttons, brings them to the front, and pumps a modal
session until the user acts (or the auto-dismiss timeout fires, if enabled).

Designed to run inside a short-lived alert subprocess (see `alert_runner.py`):
when the user clicks a button, the modal ends, show_alert returns, and the
process exits — at which point macOS reclaims the window(s) regardless of
the cleanliness of our NSWindow.close() calls.
"""
from __future__ import annotations

import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone

import objc
from AppKit import (
    NSApp, NSApplication, NSApplicationActivationPolicyAccessory,
    NSWindow, NSScreen, NSColor, NSFont, NSTextField,
    NSButton, NSWindowStyleMaskBorderless, NSBackingStoreBuffered,
    NSModalPanelWindowLevel, NSBezelStyleRounded,
    NSCenterTextAlignment,
    NSEvent, NSEventTypeApplicationDefined,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSWindowCollectionBehaviorFullScreenAuxiliary,
)
from Foundation import (
    NSObject, NSMakeRect, NSMakePoint, NSTimer,
)


# ---------------------------------------------------------------------------
# Borderless window subclass
# ---------------------------------------------------------------------------


class _KeyableBorderlessWindow(NSWindow):
    """Borderless NSWindow that can still become the key (and main) window.

    By default, NSWindow returns False for `canBecomeKeyWindow` when the style
    mask is borderless — which means buttons inside such a window don't reliably
    receive mouse clicks if the host process isn't already the active app.
    Overriding these methods fixes it.
    """

    def canBecomeKeyWindow(self):
        return True

    def canBecomeMainWindow(self):
        return True


# ---------------------------------------------------------------------------
# Data the overlay needs
# ---------------------------------------------------------------------------


@dataclass
class AlertInfo:
    title: str
    start_str: str           # already-formatted local time (e.g. "9:32 PM")
    minutes_until: int       # e.g. 5; negative when the meeting has already started
    location: str | None     # may be None
    join_link: str | None    # may be None
    # The actual start time as a timezone-aware datetime. When provided, the
    # overlay schedules a timer that recomputes the "Starts in N minutes"
    # line every 30s so an un-dismissed alert sitting on screen stays truthful
    # as time passes (and eventually flips to "Already started at HH:MM"
    # once the meeting begins). When None, the display is static.
    start_utc: datetime | None = None


def minutes_until_display(start_utc: datetime, now_utc: datetime) -> int:
    """Whole minutes from `now_utc` until `start_utc`, for the "Starts in N
    minutes" line.

    Upcoming meetings are rounded UP to the next whole minute. The poller only
    checks for fire-eligible events once per poll cycle, so the alert typically
    fires a little after the lead-time threshold (e.g. with a 5-minute lead the
    meeting is ~4:4x away by the time we fire). Truncating showed "4 minutes"
    for a lead of 5; rounding UP restores "5" no matter how late within the
    cycle the poll caught the threshold — so it reads correctly whether the
    poll interval is the default 20s or a legacy 60s config.

    Past-start meetings (the notify_in_progress_meetings catch-up path) return
    a negative value so the overlay renders "Already started" — the sign of the
    real delta decides that independent of rounding, so a meeting that began a
    few seconds ago isn't shown as "Starts in 0 minutes".
    """
    secs = (start_utc - now_utc).total_seconds()
    if secs < 0:
        return int(secs // 60)          # floor keeps already-started negative
    return -(-int(secs) // 60)          # ceil: round up to the next whole minute


# Window dimensions, used for layout math in multiple methods.
_WIN_W = 720
_WIN_H = 360


# ---------------------------------------------------------------------------
# Window controller
# ---------------------------------------------------------------------------


class AlertController(NSObject):
    """Owns one or more overlay windows (one per selected screen) and tracks
    the user's action."""

    def initWithInfo_snoozeMinutes_displayMode_allSpaces_(
            self, info, snooze_minutes, display_mode, all_spaces):
        self = objc.super(AlertController, self).init()
        if self is None:
            return None
        self._info = info
        self._snooze_minutes = snooze_minutes
        self._display_mode = display_mode
        self._all_spaces = bool(all_spaces)
        self._result = None
        self._windows = []
        # Track every "Starts in N minutes" label across all satellite windows
        # so we can refresh them on a timer tick.
        self._when_labels = []
        self._when_timer = None
        self._build_windows()
        if self._info.start_utc is not None:
            # 30s cadence catches each minute boundary within 30s and is light
            # enough not to be noticed even if the alert sits for an hour.
            self._when_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                30.0, self, "refreshWhen:", None, True)
        return self

    # ----- public (Python-callable only; not Cocoa selectors) -----

    @objc.python_method
    def result(self):
        return self._result

    @objc.python_method
    def modal_window(self):
        """The window the modal session is run on. We pick the first window;
        button clicks on any of the satellite windows still route to this
        controller's action methods because they all share target/action."""
        return self._windows[0] if self._windows else None

    @objc.python_method
    def show(self):
        NSApp.activateIgnoringOtherApps_(True)
        # orderFrontRegardless brings each window to the front of its app's
        # window stack without changing key status. Then we make ONLY the first
        # window key, so keyboard equivalents work without the chain of
        # makeKeyAndOrderFront_ calls causing the OS to hide earlier windows.
        for w in self._windows:
            w.orderFrontRegardless()
        if self._windows:
            self._windows[0].makeKeyWindow()

    @objc.python_method
    def close(self):
        for w in self._windows:
            w.orderOut_(None)
            w.close()

    # ----- private: screen selection + layout (Python-only) -----

    @objc.python_method
    def _select_screens(self):
        all_screens = list(NSScreen.screens())
        if not all_screens:
            return []
        mode = self._display_mode
        if mode == "all":
            return all_screens
        if mode == "focused":
            s = NSScreen.mainScreen()
            return [s] if s else all_screens[:1]
        # default fallback: "main"
        return all_screens[:1]

    @objc.python_method
    def _build_windows(self):
        for screen in self._select_screens():
            win = self._build_one_window(screen)
            self._populate(win)
            self._windows.append(win)

    @objc.python_method
    def _build_one_window(self, screen):
        sf = screen.visibleFrame()  # excludes the menu bar
        win_x = sf.origin.x + (sf.size.width  - _WIN_W) / 2
        win_y = sf.origin.y + (sf.size.height - _WIN_H) / 2

        win = _KeyableBorderlessWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(win_x, win_y, _WIN_W, _WIN_H),
            NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered,
            False,
        )
        win.setLevel_(NSModalPanelWindowLevel)
        win.setReleasedWhenClosed_(False)
        win.setOpaque_(True)
        win.setBackgroundColor_(NSColor.windowBackgroundColor())
        win.setHasShadow_(True)
        win.setMovableByWindowBackground_(True)

        if self._all_spaces:
            behavior = win.collectionBehavior()
            behavior |= NSWindowCollectionBehaviorCanJoinAllSpaces
            behavior |= NSWindowCollectionBehaviorFullScreenAuxiliary
            win.setCollectionBehavior_(behavior)

        return win

    @objc.python_method
    def _populate(self, win):
        """Add labels, buttons, and link to one window's content view. All
        buttons share self as target so clicks on any window route to the same
        action methods."""
        content = win.contentView()
        cursor_y = _WIN_H - 40

        # Title
        title_h = 100
        title_rect = NSMakeRect(20, cursor_y - title_h, _WIN_W - 40, title_h)
        title_lbl = self._make_label(
            title_rect,
            self._info.title or "(no title)",
            NSFont.boldSystemFontOfSize_(36),
        )
        title_lbl.setMaximumNumberOfLines_(2)
        content.addSubview_(title_lbl)
        cursor_y -= title_h + 10

        # "Starts in N minutes · at HH:MM" line, or "Already started · at HH:MM"
        # for an in-progress meeting we're catching after the fact (the
        # notify_in_progress_meetings code path uses a negative minutes_until
        # to flag this).
        when_h = 32
        when_text = self._compute_when_text(self._info.minutes_until)
        when_rect = NSMakeRect(20, cursor_y - when_h, _WIN_W - 40, when_h)
        when_lbl = self._make_label(when_rect, when_text,
                                    NSFont.systemFontOfSize_(22))
        when_lbl.setTextColor_(NSColor.secondaryLabelColor())
        content.addSubview_(when_lbl)
        # Save for the timer refresh path.
        self._when_labels.append(when_lbl)
        cursor_y -= when_h + 8

        # Optional location
        if self._info.location:
            loc_h = 26
            loc_rect = NSMakeRect(20, cursor_y - loc_h, _WIN_W - 40, loc_h)
            loc_lbl = self._make_label(
                loc_rect, "📍  " + self._info.location,
                NSFont.systemFontOfSize_(18),
            )
            loc_lbl.setTextColor_(NSColor.secondaryLabelColor())
            content.addSubview_(loc_lbl)
            cursor_y -= loc_h + 4

        # Optional join link. Display only the URL's hostname in the button
        # label, not the full URL: the alert conditions the user to click
        # "Join meeting" without reading, so showing the full URL gives an
        # attacker who can land a malicious URL in event notes a one-click
        # phishing surface. Hostname-only makes the destination scannable.
        # The full original URL is still what gets opened on click.
        if self._info.join_link:
            from urllib.parse import urlparse as _urlparse
            try:
                _host = _urlparse(self._info.join_link).hostname or self._info.join_link
            except Exception:
                _host = self._info.join_link
            link_h = 30
            link_rect = NSMakeRect(20, cursor_y - link_h, _WIN_W - 40, link_h)
            link_btn = NSButton.alloc().initWithFrame_(link_rect)
            link_btn.setTitle_(f"🔗  Join ({_host})")
            link_btn.setBordered_(False)
            link_btn.setFont_(NSFont.systemFontOfSize_(15))
            link_btn.setTarget_(self)
            link_btn.setAction_("openLink:")
            link_btn.setContentTintColor_(NSColor.linkColor())
            link_btn.setAlignment_(NSCenterTextAlignment)
            content.addSubview_(link_btn)
            cursor_y -= link_h + 4

        # Dismiss + Snooze buttons
        btn_h = 44
        btn_w = 200
        gap = 20
        total_w = btn_w * 2 + gap
        btn_y = 24
        first_x = (_WIN_W - total_w) / 2

        dismiss = NSButton.alloc().initWithFrame_(
            NSMakeRect(first_x, btn_y, btn_w, btn_h))
        dismiss.setTitle_("Dismiss")
        dismiss.setBezelStyle_(NSBezelStyleRounded)
        dismiss.setFont_(NSFont.systemFontOfSize_(16))
        dismiss.setTarget_(self)
        dismiss.setAction_("dismiss:")
        dismiss.setKeyEquivalent_("\r")  # return key
        content.addSubview_(dismiss)

        snooze = NSButton.alloc().initWithFrame_(
            NSMakeRect(first_x + btn_w + gap, btn_y, btn_w, btn_h))
        snooze.setTitle_(f"Snooze {self._snooze_minutes} min")
        snooze.setBezelStyle_(NSBezelStyleRounded)
        snooze.setFont_(NSFont.systemFontOfSize_(16))
        snooze.setTarget_(self)
        snooze.setAction_("snooze:")
        snooze.setKeyEquivalent_("s")  # 's' key
        content.addSubview_(snooze)

    @staticmethod
    def _make_label(rect, text, font):
        lbl = NSTextField.alloc().initWithFrame_(rect)
        lbl.setStringValue_(str(text))
        lbl.setFont_(font)
        lbl.setBezeled_(False)
        lbl.setDrawsBackground_(False)
        lbl.setEditable_(False)
        lbl.setSelectable_(False)
        lbl.setAlignment_(NSCenterTextAlignment)
        return lbl

    # ----- timer: refresh the "Starts in N minutes" line -----

    @objc.python_method
    def _compute_when_text(self, minutes_until: int) -> str:
        if minutes_until < 0:
            return f"Already started at {self._info.start_str}"
        minutes_word = "minute" if minutes_until == 1 else "minutes"
        return f"Starts in {minutes_until} {minutes_word}  ·  {self._info.start_str}"

    def refreshWhen_(self, timer):
        """Recompute minutes_until from start_utc vs now and update every
        when-label across our satellite windows so an un-dismissed alert
        stays truthful as time passes."""
        if self._info.start_utc is None:
            return
        now_utc = datetime.now(timezone.utc)
        minutes_until = minutes_until_display(self._info.start_utc, now_utc)
        text = self._compute_when_text(minutes_until)
        for lbl in self._when_labels:
            lbl.setStringValue_(text)

    @objc.python_method
    def _stop_when_timer(self):
        if self._when_timer is not None:
            self._when_timer.invalidate()
            self._when_timer = None

    # ----- private: button actions (called by AppKit on button click) -----

    @objc.python_method
    def _hide_all_windows(self):
        for w in self._windows:
            w.orderOut_(None)

    def dismiss_(self, sender):
        self._stop_when_timer()
        self._hide_all_windows()
        self._result = "dismiss"
        _stop_app()

    def snooze_(self, sender):
        self._stop_when_timer()
        self._hide_all_windows()
        self._result = "snooze"
        _stop_app()

    def openLink_(self, sender):
        if self._info.join_link:
            self._stop_when_timer()
            self._hide_all_windows()
            webbrowser.open(self._info.join_link)
            self._result = "link"
            _stop_app()

    def timeoutFired_(self, timer):
        self._stop_when_timer()
        self._hide_all_windows()
        if self._result is None:
            self._result = "timeout"
        _stop_app()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def show_alert(info: AlertInfo,
               snooze_minutes: int = 2,
               timeout_seconds: int = 0,
               display_mode: str = "all",
               all_spaces: bool = True) -> str:
    """Display the overlay; block until user acts (or timeout, if enabled).

    Args:
        info: meeting details to render.
        snooze_minutes: drives the Snooze button label + later re-fire timing
            (the poller handles the re-fire timing, not this code).
        timeout_seconds: 0 = never auto-dismiss (the default — alert stays up
            indefinitely). >0 = auto-dismiss after that many seconds.
        display_mode: "all" (show on every connected display, the default),
            "main" (primary display only), or "focused" (the display containing
            the focused app).
        all_spaces: if True (the default), the alert appears on every macOS
            Space simultaneously, including overlaying full-screen apps.

    Returns one of: "dismiss", "snooze", "link", "timeout".
    """
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

    controller = AlertController.alloc().initWithInfo_snoozeMinutes_displayMode_allSpaces_(
        info, snooze_minutes, display_mode, all_spaces)
    controller.show()

    timer = None
    if timeout_seconds and timeout_seconds > 0:
        timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            float(timeout_seconds), controller, "timeoutFired:", None, False,
        )

    # NSApp.run() (not runModalForWindow_): a modal session restricts mouse
    # events to the modal window only, which means our satellite windows on
    # other displays/Spaces look clickable but aren't. With NSApp.run() every
    # window is treated equally — a click on any of them routes to the same
    # target/action. Borderless windows accept key status thanks to
    # _KeyableBorderlessWindow above. Action handlers call _stop_app() (which
    # calls NSApp.stop_(None) + posts a dummy event) to break out.
    app.run()

    if timer is not None:
        timer.invalidate()
    controller.close()
    return controller.result() or "dismiss"


# ---------------------------------------------------------------------------
# App lifecycle helper
# ---------------------------------------------------------------------------


def _stop_app():
    """Stop the NSApp event loop and wake it with a dummy event.

    `NSApp.stop_` is latent — it sets a flag checked at the next processed
    event. Posting a dummy NSEventTypeApplicationDefined event guarantees the
    loop wakes immediately and processes the stop request.
    """
    NSApp.stop_(None)
    evt = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
        NSEventTypeApplicationDefined,
        NSMakePoint(0, 0), 0, 0, 0, None, 0, 0, 0,
    )
    NSApp.postEvent_atStart_(evt, True)
