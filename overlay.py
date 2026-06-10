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
    minutes_until: int       # e.g. 5
    location: str | None     # may be None
    join_link: str | None    # may be None


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
        self._build_windows()
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
        if self._info.minutes_until < 0:
            when_text = f"Already started at {self._info.start_str}"
        else:
            minutes_word = "minute" if self._info.minutes_until == 1 else "minutes"
            when_text = (
                f"Starts in {self._info.minutes_until} {minutes_word}"
                f"  ·  {self._info.start_str}"
            )
        when_rect = NSMakeRect(20, cursor_y - when_h, _WIN_W - 40, when_h)
        when_lbl = self._make_label(when_rect, when_text,
                                    NSFont.systemFontOfSize_(22))
        when_lbl.setTextColor_(NSColor.secondaryLabelColor())
        content.addSubview_(when_lbl)
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

        # Optional join link
        if self._info.join_link:
            link_h = 30
            link_rect = NSMakeRect(20, cursor_y - link_h, _WIN_W - 40, link_h)
            link_btn = NSButton.alloc().initWithFrame_(link_rect)
            link_btn.setTitle_("🔗  " + self._info.join_link)
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

    # ----- private: button actions (called by AppKit on button click) -----

    @objc.python_method
    def _hide_all_windows(self):
        for w in self._windows:
            w.orderOut_(None)

    def dismiss_(self, sender):
        self._hide_all_windows()
        self._result = "dismiss"
        _stop_app()

    def snooze_(self, sender):
        self._hide_all_windows()
        self._result = "snooze"
        _stop_app()

    def openLink_(self, sender):
        if self._info.join_link:
            self._hide_all_windows()
            webbrowser.open(self._info.join_link)
            self._result = "link"
            _stop_app()

    def timeoutFired_(self, timer):
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
