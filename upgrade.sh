#!/usr/bin/env bash
#
# upgrade.sh - upgrade an installed MeetingNotifier.app in place.
#
# Replacing the app by hand is fiddly: a launchd daemon (the background poller)
# runs from /Applications and is auto-restarted by launchd's KeepAlive, so a
# plain drag-and-replace fights the running process. This script does it
# cleanly - stop the daemon, verify and swap in the new build, restart the
# daemon - while preserving your settings and your Calendar permission.
#
# Usage:
#   1. From the project's Releases page, download both this script and the
#      latest MeetingNotifier-X.Y.Z.zip into the same folder (no need to
#      unzip the .zip), e.g. ~/Downloads.
#   2. Run one of:
#        bash upgrade.sh                                   # uses newest zip in ~/Downloads
#        bash upgrade.sh ~/Downloads/MeetingNotifier-X.Y.Z.zip   # explicit path
#
# It does NOT touch your config (~/.config/meeting-notifier/) or reset the
# Calendar permission. To change settings, open the app after upgrading.
#
set -euo pipefail

LABEL="net.ryland.meeting-notifier"
APP="/Applications/MeetingNotifier.app"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
EXPECTED_TEAM="Q5A8FF5XXR"      # Developer ID: Mark Ryland
DOMAIN="gui/$(id -u)"

# ---- locate the release zip -------------------------------------------------
if [[ $# -ge 1 ]]; then
    ZIP="$1"
else
    ZIP="$(ls -t "$HOME"/Downloads/MeetingNotifier-*.zip 2>/dev/null | head -1 || true)"
fi
if [[ -z "${ZIP:-}" || ! -f "$ZIP" ]]; then
    echo "ERROR: couldn't find a MeetingNotifier-*.zip." >&2
    echo "Download the latest release, then either drop it in ~/Downloads or pass its path:" >&2
    echo "  bash upgrade.sh /path/to/MeetingNotifier-X.Y.Z.zip" >&2
    exit 1
fi
echo "==> Upgrading from: $(basename "$ZIP")"

# ---- unzip to a temp dir ----------------------------------------------------
TMP="$(mktemp -d /tmp/meetingnotifier-upgrade.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT
ditto -x -k "$ZIP" "$TMP"
NEW_APP="$TMP/MeetingNotifier.app"
if [[ ! -d "$NEW_APP" ]]; then
    echo "ERROR: MeetingNotifier.app not found inside the zip." >&2
    exit 1
fi
NEW_VER="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' \
            "$NEW_APP/Contents/Info.plist" 2>/dev/null || echo '?')"

# ---- verify the new build BEFORE installing it ------------------------------
# Releases are signed with a Developer ID and notarized; refuse anything that
# isn't, so a tampered or mystery zip can't get installed as the background app.
echo "==> Verifying code signature (v$NEW_VER)"
if ! codesign --verify --deep --strict "$NEW_APP"; then
    echo "ERROR: code-signature verification failed - refusing to install." >&2
    exit 1
fi
TEAM="$(codesign -dv "$NEW_APP" 2>&1 | sed -n 's/^TeamIdentifier=//p')"
if [[ "$TEAM" != "$EXPECTED_TEAM" ]]; then
    echo "ERROR: unexpected signing team '$TEAM' (expected '$EXPECTED_TEAM')." >&2
    exit 1
fi
if ! spctl --assess --type exec "$NEW_APP" 2>/dev/null; then
    echo "ERROR: Gatekeeper rejected the app (not notarized?) - refusing to install." >&2
    exit 1
fi

# ---- stop the running daemon so KeepAlive doesn't fight the swap ------------
echo "==> Stopping the background notifier"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
# Match the app binary specifically (not this script) so we never kill ourselves.
pkill -f "MeetingNotifier.app/Contents/MacOS/MeetingNotifier" 2>/dev/null || true
sleep 1

# ---- swap the app -----------------------------------------------------------
echo "==> Installing into /Applications"
rm -rf "$APP"
ditto "$NEW_APP" "$APP"
# We already validated signature + notarization above, so clear any download
# quarantine flag to ensure the launchd-started daemon launches without a snag.
xattr -dr com.apple.quarantine "$APP" 2>/dev/null || true

# ---- restart the daemon -----------------------------------------------------
if [[ -f "$PLIST" ]]; then
    echo "==> Restarting the background notifier"
    launchctl bootstrap "$DOMAIN" "$PLIST"
    launchctl kickstart -k "$DOMAIN/$LABEL"
    echo
    echo "==> Done. Upgraded to v$NEW_VER and the background notifier is running."
    echo "    Your settings and Calendar permission were preserved."
else
    # No LaunchAgent yet (first-time setup, or installed but never started).
    echo "==> No background agent found - opening the app so you can finish setup."
    open "$APP"
    echo
    echo "==> Upgraded to v$NEW_VER. In the window that opened, click \"Save & Start\"."
fi
