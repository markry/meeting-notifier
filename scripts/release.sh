#!/usr/bin/env bash
#
# release.sh - cut the GitHub release for the current version, with BOTH the
# notarized app zip AND upgrade.sh attached as assets.
#
# Why this exists: build.sh stops at producing dist/MeetingNotifier-X.Y.Z.zip;
# the GitHub release was cut by hand, which made it easy to attach the zip and
# forget upgrade.sh. This script always attaches both, so the Releases page is
# self-contained (zip + upgrade script in one place).
#
# Run build.sh first (it must have produced the release zip). Then:
#   bash scripts/release.sh --notes-file NOTES.md
#   bash scripts/release.sh --notes "Privacy: hide alert from screen sharing."
#   bash scripts/release.sh --generate-notes        # autogenerate from commits
#
# Any extra args are forwarded to `gh release create`. The version is derived
# from setup.py (same source build.sh uses), so the tag/title stay in sync.
#
# If the release already exists, the assets are (re)uploaded with --clobber
# instead of failing.
#
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    sed -n '2,/^set/p' "$0" | sed '$d' | sed 's/^# \{0,1\}//'
    exit 0
fi

VERSION="$(grep -oE '"CFBundleShortVersionString":[^,]+' setup.py \
            | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)"
if [ -z "$VERSION" ]; then
    echo "Could not parse CFBundleShortVersionString from setup.py" >&2
    exit 1
fi

TAG="v${VERSION}"
RELEASE_ZIP="dist/MeetingNotifier-${VERSION}.zip"
UPGRADE="upgrade.sh"

if [ ! -f "$RELEASE_ZIP" ]; then
    echo "ERROR: $RELEASE_ZIP not found. Run scripts/build.sh first." >&2
    exit 1
fi
if [ ! -f "$UPGRADE" ]; then
    echo "ERROR: $UPGRADE not found at repo root." >&2
    exit 1
fi

if gh release view "$TAG" >/dev/null 2>&1; then
    echo "==> Release $TAG already exists - (re)uploading both assets (--clobber)"
    gh release upload "$TAG" "$RELEASE_ZIP" "$UPGRADE" --clobber
else
    echo "==> Creating release $TAG with both assets"
    gh release create "$TAG" "$RELEASE_ZIP" "$UPGRADE" --title "$TAG" "$@"
fi

echo "==> Assets on $TAG:"
gh release view "$TAG" --json assets -q '.assets[] | "    \(.name)  (\(.size) bytes)"'
echo "==> Done."
