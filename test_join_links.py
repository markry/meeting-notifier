#!/usr/bin/env python3
"""Tests for join-link extraction + hostname validation (join_links.py).

Run:  python3 -m unittest test_join_links      (no PyObjC / venv needed)
"""
import unittest

from join_links import find_join_link, host_matches, trim_url


class HostMatches(unittest.TestCase):
    def test_exact_and_subdomain_accepted(self):
        self.assertTrue(host_matches("https://zoom.us/j/1", "zoom.us"))
        self.assertTrue(host_matches("https://us02web.zoom.us/j/1", "zoom.us"))
        self.assertTrue(host_matches("https://a.b.zoom.us/j/1", "zoom.us"))

    def test_case_and_trailing_dot(self):
        self.assertTrue(host_matches("https://ZOOM.US/j/1", "zoom.us"))
        self.assertTrue(host_matches("https://us02web.Zoom.Us./j/1", "zoom.us"))

    def test_lookalikes_rejected(self):
        self.assertFalse(host_matches("https://evilzoom.us/j/1", "zoom.us"))       # prefix, no dot
        self.assertFalse(host_matches("https://zoom.us.evil.com/j/1", "zoom.us"))  # suffix
        self.assertFalse(host_matches("https://zoom.us@evil.com/j/1", "zoom.us"))  # userinfo trick
        self.assertFalse(host_matches("https://notzoom.us/j/1", "zoom.us"))
        self.assertFalse(host_matches("https://fakewebex.com/meet/x", "webex.com"))

    def test_garbage(self):
        self.assertFalse(host_matches("not a url", "zoom.us"))
        self.assertFalse(host_matches("", "zoom.us"))


class FindJoinLink(unittest.TestCase):
    def test_real_providers_accepted(self):
        cases = {
            "https://zoom.us/j/1234567890": "https://zoom.us/j/1234567890",
            "https://us02web.zoom.us/j/1234567890?pwd=aBcD": "https://us02web.zoom.us/j/1234567890?pwd=aBcD",
            "https://us02web.zoom.us/my/mark.ryland": "https://us02web.zoom.us/my/mark.ryland",
            "https://meet.google.com/abc-defg-hij": "https://meet.google.com/abc-defg-hij",
            "https://teams.microsoft.com/l/meetup-join/19%3ameeting": "https://teams.microsoft.com/l/meetup-join/19%3ameeting",
            "https://acme.webex.com/meet/mark": "https://acme.webex.com/meet/mark",
            "https://whereby.com/mark-room": "https://whereby.com/mark-room",
            "https://meet.jit.si/MarkRoom": "https://meet.jit.si/MarkRoom",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(find_join_link(text), expected)

    def test_lookalikes_not_treated_as_known(self):
        # Default known_only=True must return None for spoofed provider domains,
        # so they never become a one-click "Join" button.
        for text in [
            "Join here: https://evilzoom.us/j/1234567890",
            "https://zoom.us.evil.com/j/1234567890",
            "https://zoom.us@evil.com/j/1234567890",
            "https://fakewebex.com/meet/mark",
            "https://evilgotomeeting.com/join/12345",
            "https://notbluejeans.com/12345",
            "https://zoоm.us/j/1234567890",   # Cyrillic 'о' homograph
        ]:
            with self.subTest(text=text):
                self.assertIsNone(find_join_link(text, known_only=True))

    def test_priority_picks_provider_over_tracking_url(self):
        text = ("Agenda: https://tracking.example.com/click?u=123\n"
                "Join Zoom Meeting https://us02web.zoom.us/j/999")
        self.assertEqual(find_join_link(text), "https://us02web.zoom.us/j/999")

    def test_generic_fallback_only_when_allowed(self):
        text = "See https://example.com/room123 for details"
        self.assertIsNone(find_join_link(text, known_only=True))
        self.assertEqual(find_join_link(text, known_only=False), "https://example.com/room123")

    def test_none_when_no_url(self):
        self.assertIsNone(find_join_link("no links in this text", known_only=False))


class TrimUrl(unittest.TestCase):
    def test_trailing_punctuation(self):
        self.assertEqual(trim_url("https://zoom.us/j/1)."), "https://zoom.us/j/1")

    def test_strips_bidi_and_zero_width(self):
        # A right-to-left override embedded in a URL must be removed.
        dirty = "https://good.com‮/evil.com"
        self.assertNotIn("‮", trim_url(dirty))


if __name__ == "__main__":
    unittest.main()
