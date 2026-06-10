#!/usr/bin/env bash
#
# Build, sign, notarize, staple MeetingNotifier.app and produce a release zip.
#
# Outputs:
#   dist/MeetingNotifier.app                  - signed + notarized + stapled
#   dist/MeetingNotifier-X.Y.Z-notarize.zip   - the zip submitted to Apple
#   dist/MeetingNotifier-X.Y.Z.zip            - the release zip (attach this
#                                                to GitHub Releases)
#
# Env vars (override if your setup differs):
#   VENV     - virtualenv with py2app + PyObjC installed.
#              Default: ~/Library/Application Support/meeting-notifier/venv
#   IDENTITY - codesign Developer ID Application identity.
#              Default: "Developer ID Application: Mark Ryland (Q5A8FF5XXR)"
#   PROFILE  - notarytool keychain profile name (see `xcrun notarytool
#              store-credentials` to set one up).
#              Default: MeetingNotifierNotary
#
# Flags:
#   --no-notarize  Skip the Apple notary round-trip. Produces a signed but
#                  unnotarized bundle; the release zip is just a copy of
#                  the notarize zip. Useful for fast local iteration.
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VENV="${VENV:-$HOME/Library/Application Support/meeting-notifier/venv}"
IDENTITY="${IDENTITY:-Developer ID Application: Mark Ryland (Q5A8FF5XXR)}"
PROFILE="${PROFILE:-MeetingNotifierNotary}"

NO_NOTARIZE=0
for arg in "$@"; do
    case "$arg" in
        --no-notarize) NO_NOTARIZE=1 ;;
        -h|--help)
            sed -n '2,/^set/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "unknown arg: $arg" >&2
            echo "see --help" >&2
            exit 1
            ;;
    esac
done

# Derive version from setup.py so the zip filenames stay in sync with the
# bundle's CFBundleShortVersionString.
VERSION="$(grep -oE '"CFBundleShortVersionString":[^,]+' setup.py \
            | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
if [ -z "$VERSION" ]; then
    echo "Could not parse CFBundleShortVersionString from setup.py" >&2
    exit 1
fi

NOTARIZE_ZIP="dist/MeetingNotifier-${VERSION}-notarize.zip"
RELEASE_ZIP="dist/MeetingNotifier-${VERSION}.zip"
APP="dist/MeetingNotifier.app"

echo "=== building MeetingNotifier ${VERSION} ==="

# Kill any running MeetingNotifier so we don't fight launchd or a previous
# Save & Start process for /Applications/MeetingNotifier.app handle.
pkill -f MeetingNotifier 2>/dev/null || true

# Aggressive clean. Without this, py2app has cached __pycache__ contents that
# can sneak stale .pyc files into the next bundle's lib/python314.zip - that
# bit us moving from 0.2.4 to 0.2.5 with the missing --daemon flag.
rm -rf build dist
find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
find . -name "*.pyc" -delete 2>/dev/null || true

echo "=== py2app ==="
"$VENV/bin/python3" setup.py py2app 2>&1 | tail -1

# py2app sometimes leaves a dangling symlink that trips --deep --strict verify.
rm -f "$APP/Contents/Resources/lib/python3.14/site.pyo"

echo "=== signing nested binaries ==="
# All .so / .dylib first. py2app's --deep doesn't catch these reliably.
find "$APP" \( -name "*.so" -o -name "*.dylib" \) -print0 \
    | xargs -0 -n 50 codesign --force --options runtime --timestamp --sign "$IDENTITY" \
    2>&1 | tail -1
# Embedded Python framework binary.
find "$APP/Contents/Frameworks/Python.framework" -type f -name "Python" \
    -exec codesign --force --options runtime --timestamp --sign "$IDENTITY" {} \;
# Secondary python launcher (py2app creates two binaries in MacOS/, our main
# MeetingNotifier launcher plus a python framework launcher). Both need
# Hardened Runtime or notarization will fail.
codesign --force --options runtime --timestamp --sign "$IDENTITY" "$APP/Contents/MacOS/python"
# Outer bundle, this time with entitlements (Hardened Runtime + Calendar
# data-class). Has to be the LAST signing step.
codesign --force --options runtime --timestamp --entitlements entitlements.plist \
    --sign "$IDENTITY" "$APP"

echo "=== verify ==="
codesign --verify --deep --strict "$APP"

echo "=== ditto-zip for notary ==="
ditto -c -k --keepParent "$APP" "$NOTARIZE_ZIP"

if [ "$NO_NOTARIZE" = "1" ]; then
    echo "=== --no-notarize: skipping Apple notary round-trip ==="
    cp "$NOTARIZE_ZIP" "$RELEASE_ZIP"
    echo "=== DONE (unnotarized) ==="
    echo "Release zip: $RELEASE_ZIP"
    echo "Bundle: $APP"
    exit 0
fi

echo "=== Apple notary submit (waits for completion) ==="
xcrun notarytool submit "$NOTARIZE_ZIP" --keychain-profile "$PROFILE" --wait

echo "=== stapling ==="
xcrun stapler staple "$APP"

echo "=== ditto-zip release ==="
ditto -c -k --keepParent "$APP" "$RELEASE_ZIP"

echo "=== DONE ==="
echo "Release zip: $RELEASE_ZIP"
echo "Stapled bundle: $APP"
