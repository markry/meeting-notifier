# MeetingNotifier — installation

Two install paths. **Pick one — don't mix them on the same Mac without reading the "Why not mix" section below.**

## Path A — GUI (recommended for most users)

1. Download `MeetingNotifier.app.zip` from the [latest release](../../releases) and unzip it.
2. Drag `MeetingNotifier.app` into `/Applications`.
3. **Double-click `/Applications/MeetingNotifier.app`** to launch it. If you get a Gatekeeper warning (the release is signed but not yet notarized), right-click the app and choose **Open** instead.
4. macOS will pop a Calendar permission dialog — click **Allow**.
5. The settings window appears:
   - Check the calendars you want watched
   - Adjust timing / display options as you like (defaults are sensible)
6. Click **Save & Start**. The window closes; the background notifier is now running and will pop alerts before your meetings.

To **change settings later**: double-click `/Applications/MeetingNotifier.app` again. The window re-opens with your current settings pre-loaded. Make changes, hit Save & Start, and the background process restarts with the new config.

To **uninstall**:

```bash
launchctl bootout gui/$(id -u)/net.ryland.meeting-notifier
rm ~/Library/LaunchAgents/net.ryland.meeting-notifier.plist
rm -rf /Applications/MeetingNotifier.app
rm -rf ~/.config/meeting-notifier
rm -rf ~/Library/Logs/meeting-notifier
```

## Path B — CLI (for power users / scripted setups)

This is the developer-focused install. Use it if you want to manage everything from terminal without the GUI.

### 1. Place the app
Drop the unzipped `MeetingNotifier.app` into `/Applications` (or any path you prefer).

### 2. Grant Calendar permission
The simplest way is still to double-click the app once from Finder so macOS shows the foreground TCC dialog and you click Allow. Running the binary from Terminal before any foreground grant exists can leave TCC in an inconsistent state — see "Why not mix" below.

### 3. Scaffold your config
```bash
/Applications/MeetingNotifier.app/Contents/MacOS/MeetingNotifier --init-config
```
Writes `~/.config/meeting-notifier/config.toml`. Edit it:
```bash
open -e ~/.config/meeting-notifier/config.toml
```
The file is fully commented. The most important section is `[[calendars]]` — repeat that header once per calendar you want watched, using `title` + optional `source` from the `--list` flag's output. Each calendar needs its **own** `[[calendars]]` header — DO NOT add more `title=` / `source=` lines to one block.

### 4. Install the LaunchAgent
The shipped `install_launchagent.py` writes the plist and bootstraps it:
```bash
python3 install_launchagent.py --app /Applications/MeetingNotifier.app
```

## Why not mix paths

Both paths produce the same end state (config in `~/.config/meeting-notifier/`, LaunchAgent in `~/Library/LaunchAgents/`). The risk in mixing them on the same Mac is **macOS's TCC permission system**:

- The GUI launches the .app as a foreground app via Finder. The OS issues the Calendar permission to **the .app's bundle identity** with full UI affordance.
- The CLI runs the .app's binary as a child of your shell (Terminal). The OS may bind the permission to a **mixture of the shell + the binary** depending on macOS version. The grant your shell already has can mask whether the .app's own bundle has its own grant.

When you mix:
- If you ran the CLI path first, the .app may not appear in **System Settings → Privacy & Security → Calendars** at all — because no UI-prompted grant ever happened. Then the GUI later shows the prompt, but the GUI's grant conflicts with the shell-inherited one already in TCC, and you can end up with the LaunchAgent silently denied while the foreground .app runs fine.
- If you ran the GUI path first, the CLI `--init-config` may write into a config file the GUI also writes to, and your hand edits get overwritten next time you re-launch the GUI.

**Recommendation**: pick one path per Mac. The GUI path is more reliable for "I just want it working." The CLI path is more comfortable if you're already managing everything else from terminal.

If you've already mixed them and things are weird, the cleanest reset is:
```bash
tccutil reset Calendar net.ryland.meeting-notifier
launchctl bootout gui/$(id -u)/net.ryland.meeting-notifier 2>/dev/null
rm -f ~/Library/LaunchAgents/net.ryland.meeting-notifier.plist
rm -rf ~/.config/meeting-notifier
```
Then start fresh with one path.
