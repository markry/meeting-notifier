#!/usr/bin/env python3
"""Pure "find the join link" logic — no EventKit/PyObjC, so it's unit-testable.

Security note: a calendar event can come from an untrusted sender, and the alert
turns a recognized provider URL into a one-click "Join" button. So a link is
only treated as a known provider when BOTH hold:

  1. It matches that provider's URL shape (path included), and
  2. its parsed **hostname** is exactly the provider's domain or a true subdomain.

The hostname check is what defeats look-alikes: `evilzoom.us`, `fakewebex.com`,
`zoom.us.evil.com`, and the `https://zoom.us@evil.com` userinfo trick all fail
it, because `urlparse(...).hostname` yields the real host and a suffix match on a
dot boundary can't be fooled by a shared substring. (Homograph/IDN hosts fail
too — their code points simply aren't the ASCII trusted domain.)
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

# Each entry: (URL-shape pattern, trusted registrable host). The pattern picks a
# plausible candidate (and its path) out of free text; the host is then verified
# with `host_matches`. `(?:[a-z0-9-]+\.)*` allows real subdomains
# (us02web.zoom.us) but requires a dot boundary — so it can't match `evilzoom.us`.
PREFERRED_MEETING_PATTERNS = [
    # Google Meet: https://meet.google.com/abc-defg-hij
    (re.compile(r"https?://meet\.google\.com/[a-z0-9?=&-]+", re.IGNORECASE),
     "meet.google.com"),
    # Zoom: https://zoom.us/j/1234567890 or https://us02web.zoom.us/j/...?pwd=...
    (re.compile(r"https?://(?:[a-z0-9-]+\.)*zoom\.us/j/\d+(?:\?[^\s<>\"'\)]*)?", re.IGNORECASE),
     "zoom.us"),
    # Zoom personal meeting room: https://us02web.zoom.us/my/yourname
    (re.compile(r"https?://(?:[a-z0-9-]+\.)*zoom\.us/my/[\w/.-]+(?:\?[^\s<>\"'\)]*)?", re.IGNORECASE),
     "zoom.us"),
    # Microsoft Teams: https://teams.microsoft.com/l/meetup-join/...
    (re.compile(r"https?://teams\.microsoft\.com/l/meetup-join/[^\s<>\"'\)]+", re.IGNORECASE),
     "teams.microsoft.com"),
    # Webex: https://company.webex.com/meet/user or .../wbxmjs/...
    (re.compile(r"https?://(?:[a-z0-9-]+\.)*webex\.com/(?:meet|wbxmjs|join|j\.php)[^\s<>\"'\)]+", re.IGNORECASE),
     "webex.com"),
    # GoToMeeting
    (re.compile(r"https?://(?:[a-z0-9-]+\.)*gotomeeting\.com/join/\d+", re.IGNORECASE),
     "gotomeeting.com"),
    # BlueJeans
    (re.compile(r"https?://(?:[a-z0-9-]+\.)*bluejeans\.com/\d+(?:[?/][^\s<>\"'\)]*)?", re.IGNORECASE),
     "bluejeans.com"),
    # Whereby
    (re.compile(r"https?://whereby\.com/[\w-]+", re.IGNORECASE),
     "whereby.com"),
    # Jitsi
    (re.compile(r"https?://meet\.jit\.si/[^\s<>\"'\)]+", re.IGNORECASE),
     "meet.jit.si"),
]

# Fallback for "any URL" — used only when no known-provider pattern matched.
_URL_RE_GENERIC = re.compile(r"https?://[^\s<>\"'\)]+")

# Characters that can visually disguise the true target of a URL: ASCII control
# chars (C0 + DEL) and Unicode bidi / zero-width formatting chars. Stripped from
# URLs before storing or displaying so a malicious notes field can't include
# "https://goodco.com‮/evilco.com" that right-to-left-overrides in the eye.
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


def trim_url(url: str) -> str:
    """Strip trailing punctuation the URL regex commonly over-captures, plus any
    bidi / zero-width / control characters that could disguise the URL's true
    target when rendered."""
    url = "".join(c for c in url if c not in _URL_SAFE_STRIP)
    while url and url[-1] in ".,;:!?>)]}'\"":
        url = url[:-1]
    return url


def host_matches(url: str, domain: str) -> bool:
    """Whether `url`'s parsed hostname is exactly `domain` or a true subdomain of
    it (dot boundary). Case-insensitive; tolerant of a trailing FQDN dot; safe
    against userinfo (`user@host`), ports, and shared substrings."""
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return host == domain or host.endswith("." + domain)


def find_join_link(text: str, known_only: bool = True) -> str | None:
    """The best "join meeting" URL in `text`, or None.

    Tries each provider pattern in priority order; a candidate is only returned
    once its hostname is verified against that provider's trusted domain. With
    `known_only` False, falls back to the first http(s) URL when nothing known
    matched (that URL is NOT presented as a trusted provider).
    """
    for pattern, domain in PREFERRED_MEETING_PATTERNS:
        m = pattern.search(text)
        if m:
            url = trim_url(m.group(0))
            if host_matches(url, domain):
                return url
    if known_only:
        return None
    m = _URL_RE_GENERIC.search(text)
    return trim_url(m.group(0)) if m else None
