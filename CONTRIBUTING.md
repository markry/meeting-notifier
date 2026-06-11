# Contributing to meeting-notifier

Thanks for your interest! This is a small, single-maintainer macOS app. All changes go through **fork → branch → pull request**, and the maintainer reviews and merges everything — no PR is merged without maintainer sign-off.

## Workflow (fork → branch → PR)

You don't have write access to this repository (normal for a public project), so you **cannot push branches to it directly**. Use the standard fork flow:

1. **Fork** this repo to your own account (the *Fork* button, top-right).
2. **Clone your fork** and make a branch:
   ```bash
   git clone https://github.com/<your-username>/meeting-notifier.git
   cd meeting-notifier
   git checkout -b my-change
   ```
3. Commit your work and **push to your fork**:
   ```bash
   git push origin my-change
   ```
4. Open a **Pull Request** from your fork's branch into `markry/meeting-notifier:main`.

> Got a "permission denied" or "couldn't create the branch" error? That's from trying to push to *this* repo instead of your fork. Step 1 (fork first) fixes it — you push to *your* fork, then open the PR from there.

## Building & testing locally

There is **no CI** — please build and run your change before opening a PR.

```bash
# One-time: create the venv OUTSIDE the project so py2app doesn't bundle it.
python3 -m venv "$HOME/Library/Application Support/meeting-notifier/venv"
"$HOME/Library/Application Support/meeting-notifier/venv/bin/pip" \
    install pyobjc-framework-EventKit pyobjc-framework-Cocoa py2app

VENV_PY="$HOME/Library/Application Support/meeting-notifier/venv/bin/python3"

# Run the setup GUI from source
"$VENV_PY" main.py

# Run one poll cycle and exit (test the poller without waiting for a meeting)
"$VENV_PY" poller.py --once

# Build the .app bundle
"$VENV_PY" setup.py py2app    # -> dist/MeetingNotifier.app
```

At minimum, confirm your change imports/launches cleanly, the poller loads config (`--once`), and — if you touched the alert UI — that an alert still renders (you can drive one directly through `alert_runner.py`). If you touched packaging or the LaunchAgent, build the `.app` and launch it.

Note: release builds are **signed + notarized** with the maintainer's Developer ID. Contributors can't (and don't need to) produce notarized builds — the maintainer handles releases.

## Architecture invariants — please don't break these

These were hard-won; a reasonable-looking change can quietly undo one. If your change genuinely needs to touch one, say so in the PR description.

- **Subprocess-per-alert.** Each alert runs in a short-lived subprocess (`alert_runner.py`, or `MeetingNotifier alert`) that owns the window and exits on click. Don't move the modal back inline into the long-running daemon.
- **`@objc.python_method` on internal (non-selector) methods** of `AlertController` and the GUI controller. Without it, PyObjC tries to bridge them to Cocoa selectors and crashes at class-definition time.
- **`--daemon` in the LaunchAgent.** The bundled binary routes on `argv`: no args → setup GUI, `alert` → alert subprocess, anything else → poller daemon. The LaunchAgent **must** pass `--daemon`, or launchd launches the GUI on a `KeepAlive` loop instead of the poller.
- **Alert parameters travel as JSON over stdin**, not argv, from the poller to the alert subprocess. This keeps calendar PII (titles, join links with tokens) out of `ps`, and stops a crafted meeting title from being parsed as a CLI flag. Don't move meeting fields back into argv.
- **Entitlements.** Keep `com.apple.security.cs.allow-unsigned-executable-memory` and `com.apple.security.personal-information.calendars`. Do **not** re-add `disable-library-validation` — the nested libs are signed with the same Developer ID, so library validation accepts them, and the entitlement only weakens a Calendar-privileged process.
- **Window behavior** (`overlay.py`): `_KeyableBorderlessWindow` (so a borderless window can become key and receive clicks), `setReleasedWhenClosed_(False)`, action handlers call `orderOut` on their windows *before* `stopModal`, and the all-Spaces / full-screen-auxiliary collection behavior.
- **Signing order** (`scripts/build.sh`): sign nested `.so`/`.dylib` and the embedded Python framework first, then the outer `.app` with entitlements **last**.

The README's "How it works" section and the inline comments explain the *why* behind each.

## Adding a config option

Wire it through all of these so it stays consistent and discoverable:

1. `Config` dataclass + `load_config()` in `poller.py`
2. the alert JSON payload (`poller.fire_alert`) and `alert_runner.py`, if it affects the alert window
3. `config.example.toml` (with a comment)
4. `setup_gui.py`: add to `DEFAULTS`, the `save_settings` writer, and a field/toggle in the panel
5. the README config table

## Style

- Match the surrounding code — naming, comment density, and idioms. This codebase favors short comments explaining *why* for the non-obvious macOS / PyObjC bits.
- Keep PRs focused: one logical change each.

## Reporting bugs / ideas

Open an issue with what you saw vs. expected, your macOS version, and any relevant lines from `~/Library/Logs/meeting-notifier/stdout.log` and `stderr.log`.

## License

By contributing, you agree that your contributions are licensed under the project's [MIT License](LICENSE).
