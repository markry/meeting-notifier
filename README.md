# meeting-notifier

A small macOS background agent that pops a **large, screen-centered alert** before each of your meetings — because corner banners on a large display get missed.

- Reads meetings from any calendar Apple Calendar.app knows about (Google, iCloud, Exchange, M365, CalDAV — anything you've added to **System Settings → Internet Accounts**)
- Big borderless alert window appears on every connected display, on every macOS Space (including over full-screen apps), a configurable number of minutes before each meeting
- **Dismiss** / **Snooze** buttons; clickable join link (Zoom / Meet / Teams / Webex / GoToMeeting / BlueJeans / Whereby / Jitsi) auto-extracted from the event
- Persists across login as a launchd LaunchAgent

## Requirements

- macOS 12 or later
- Calendar access permission (you'll be prompted on first run)

## Install — GUI (recommended)

1. Download the latest `MeetingNotifier-*.zip` from the [Releases page](../../releases) and unzip it.
2. Drag `MeetingNotifier.app` into `/Applications`.
3. Double-click it. macOS will ask for Calendar access — click **Allow**.
4. The settings window appears: tick the calendars you want watched, adjust any timing / display options, and click **Save & Start**.

A brief "Installation successful" dialog confirms the LaunchAgent is running. The window quits and the background notifier is now alerting you before every meeting. To change settings later, double-click the app again.

The release is signed with a Developer ID *and* notarized, so first launch goes through the standard macOS quarantine prompt (one click to confirm) — no right-click trick needed.

See [INSTALLATION.md](INSTALLATION.md) for the CLI-driven install path and the "why not mix" warnings.

## Install — source (developers)

```bash
git clone https://github.com/markry/meeting-notifier.git
cd meeting-notifier

# Create the venv outside the project so py2app doesn't try to bundle it.
python3 -m venv "$HOME/Library/Application Support/meeting-notifier/venv"

# Install dependencies
"$HOME/Library/Application Support/meeting-notifier/venv/bin/pip" \
    install pyobjc-framework-EventKit pyobjc-framework-Cocoa py2app

# Run the GUI directly from source
"$HOME/Library/Application Support/meeting-notifier/venv/bin/python3" main.py

# Or build the .app bundle yourself
"$HOME/Library/Application Support/meeting-notifier/venv/bin/python3" setup.py py2app
# Result: dist/MeetingNotifier.app
```

## Configuration

The GUI writes `~/.config/meeting-notifier/config.toml`. You can hand-edit it for things the GUI doesn't expose. **Beware that direct edits to the TOML file can be clobbered by running or re-running the GUI** — see [INSTALLATION.md](INSTALLATION.md) for the workaround. See `config.example.toml` for a fully-commented reference. Common knobs:

| Key | Default | What it does |
|-----|---------|--------------|
| `lead_time_minutes` | `5` | Alert N minutes before the meeting starts |
| `poll_interval_seconds` | `60` | How often to check for upcoming events |
| `snooze_minutes` | `2` | Snooze button duration; alert re-fires after this many minutes |
| `alert_timeout_seconds` | `0` | Auto-dismiss the alert after N seconds with no interaction (`0` = stay up until clicked) |
| `display_mode` | `"all"` | `"all"` / `"main"` / `"focused"` — which display(s) show the alert |
| `all_spaces` | `true` | Show across every macOS Space, including over full-screen apps |
| `skip_all_day` | `true` | Skip all-day events (holidays, OOO blocks) |
| `skip_unaccepted_meetings` | `false` | Skip Tentative / Pending / Declined invitations; self-created events without an attendee list are treated as accepted |
| `notify_in_progress_meetings` | `false` | Also alert for meetings already running when the notifier first sees them |
| `skip_title_substrings` | `[]` | Skip events whose title contains any of these (case-insensitive) |
| `show_location` | `true` | Show event location below the title |
| `show_join_link` | `true` | Extract + show the first known-provider URL from the event body / location |

Each calendar to watch is its own `[[calendars]]` block:

```toml
[[calendars]]
title = "Work"
source = "iCloud"          # optional — disambiguates same-titled calendars across accounts

[[calendars]]
identifier = "AD8A50E8-FB39-4141-B44A-B1668FB6300E"   # alternative — survives renames
```

## How it works

- **EventKit** (Apple's calendar framework — the same data Calendar.app shows) is queried every `poll_interval_seconds` for events starting in the next `lookahead_seconds`.
- Within `lead_time_minutes` of an event start, a borderless centered `NSWindow` is created and brought to the front above almost everything. By default it appears on every display and joins every Space, so a Space switch or full-screen Zoom can't make you miss the next meeting.
- Each alert is shown in a short-lived subprocess. When you click Dismiss or Snooze, that subprocess exits and macOS reclaims the window — eliminating a class of "modal stayed up after click" bugs the inline approach had.
- The window has **Dismiss** (Return key) and **Snooze N min** (S key) buttons, plus a clickable join link when one was found.
- Events are deduped by `eventIdentifier` so each meeting only fires once (snooze re-arms).
- A launchd LaunchAgent (`KeepAlive` + `RunAtLoad`) keeps the poller alive across login sessions.

## Logs

```bash
tail -F "$HOME/Library/Logs/meeting-notifier/stdout.log" \
        "$HOME/Library/Logs/meeting-notifier/stderr.log"
```

## Uninstall

```bash
launchctl bootout gui/$(id -u)/net.ryland.meeting-notifier
rm ~/Library/LaunchAgents/net.ryland.meeting-notifier.plist
rm -rf /Applications/MeetingNotifier.app
tccutil reset Calendar net.ryland.meeting-notifier
rm -rf ~/.config/meeting-notifier
rm -rf ~/Library/Logs/meeting-notifier
```

## Privacy

This tool runs entirely on your local machine. It makes no network calls of its own — it only reads from Apple's local Calendar.app database via EventKit. Your event data never leaves your Mac.

## License

MIT. See [LICENSE](LICENSE).
