# MeetingNotifier - installation

Two install paths. Pick one - don't mix them on the same Mac without reading the "Why not mix" section below.

## Path A - GUI (recommended for most users)

1. Download the latest `MeetingNotifier-*.zip` from the [Releases page](../../releases) and unzip it.
2. Drag `MeetingNotifier.app` into `/Applications`.
3. Double-click `/Applications/MeetingNotifier.app`. (The release is notarized + stapled, so Gatekeeper just launches it - no right-click trick needed.)
4. macOS will pop a Calendar permission dialog - click **Allow**.
5. The settings window appears:
   - Check the calendars you want watched.
   - Adjust timing / display options as you like (defaults are sensible).
6. Click **Save & Start**. The status line animates for ~15 seconds while the LaunchAgent gets installed, then a "Installation successful" dialog confirms it's running. Click OK and the window quits; the background notifier is now polling.

To change settings later: double-click `/Applications/MeetingNotifier.app` again. The window re-opens with your current settings pre-loaded. Make changes, hit Save & Start, and the background process restarts with the new config.

To uninstall:

```bash
launchctl bootout gui/$(id -u)/net.ryland.meeting-notifier
rm ~/Library/LaunchAgents/net.ryland.meeting-notifier.plist
rm -rf /Applications/MeetingNotifier.app
tccutil reset Calendar net.ryland.meeting-notifier
rm -rf ~/.config/meeting-notifier
rm -rf ~/Library/Logs/meeting-notifier
```

## Path B - CLI (for power users / scripted setups)

This is the developer-focused install. Use it if you want to manage everything from terminal without the GUI.

### 1. Place the app
Drop the unzipped `MeetingNotifier.app` into `/Applications` (or any path you prefer).

### 2. Grant Calendar permission
Easiest is to still double-click the .app once from Finder so macOS shows the foreground TCC dialog and you click Allow - that grants the *bundle* itself (not Terminal) Calendar access, which is what the LaunchAgent-spawned daemon will use later. The CLI helpers below don't trigger their own TCC prompt; they rely on the grant the bundle already has.

### 3. Scaffold your config
```bash
/Applications/MeetingNotifier.app/Contents/MacOS/MeetingNotifier --init-config
```
Writes `~/.config/meeting-notifier/config.toml`. Edit it:
```bash
open -e ~/.config/meeting-notifier/config.toml
```
The file is fully commented. The most important section is `[[calendars]]` - repeat that header once per calendar you want watched, using `title` + optional `source` from the `--list` flag's output. Each calendar needs its own `[[calendars]]` header - DO NOT add more `title=` / `source=` lines to one block (TOML treats duplicate keys as overwrites, not multi-value).

```bash
/Applications/MeetingNotifier.app/Contents/MacOS/MeetingNotifier --list
```
That prints every Calendar.app calendar this Mac knows about, with `title` / `source` / `identifier`.

### 4. Install the LaunchAgent
The shipped `install_launchagent.py` writes the plist at `~/Library/LaunchAgents/net.ryland.meeting-notifier.plist` and bootstraps it:
```bash
python3 install_launchagent.py --app /Applications/MeetingNotifier.app
```

## Why not mix paths

Both paths produce the same end state (config at `~/.config/meeting-notifier/config.toml`, LaunchAgent at `~/Library/LaunchAgents/net.ryland.meeting-notifier.plist`). The risks of mixing:

- **TCC attribution.** When you double-click the .app from Finder, macOS attributes the Calendar permission to the bundle identity `net.ryland.meeting-notifier`. When you instead run the binary as a child of your shell (Terminal), macOS may attribute the request to *Terminal's* identity - which means Terminal gets the grant, not the bundle. Subsequent launchd-spawned daemons run as the bundle and won't see Terminal's grant.
- **Config rewrites.** If you ran the GUI path, hand-edits to `config.toml` survive only until you re-launch the .app - because the GUI rewrites the file from its form fields on Save & Start and drops keys it doesn't manage (notably `use_overlay` and `identifier`-based `[[calendars]]` entries, which the GUI re-writes by `title` + `source`). The TOML header that the GUI writes spells out this gotcha.

**Recommendation**: pick one path per Mac. GUI for "I just want it working." CLI for "I'm managing everything from terminal anyway."

If you've already mixed them and things are weird, the cleanest reset is:
```bash
tccutil reset Calendar net.ryland.meeting-notifier
launchctl bootout gui/$(id -u)/net.ryland.meeting-notifier 2>/dev/null
rm -f ~/Library/LaunchAgents/net.ryland.meeting-notifier.plist
rm -rf ~/.config/meeting-notifier
```
Then start fresh with one path.

## Requirements

- macOS 12 or later (developed against macOS 26).
- Calendar access permission (the app prompts on first run).
- For macOS 14+ / non-sandboxed apps requesting Calendar via EventKit, the bundle must carry `com.apple.security.personal-information.calendars` in its signed entitlements. The release zip ships with that entitlement signed in; you don't have to do anything. If you build from source, see `entitlements.plist`.

## Logs

```bash
tail -F ~/Library/Logs/meeting-notifier/stdout.log ~/Library/Logs/meeting-notifier/stderr.log
```
