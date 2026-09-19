"""Hang-up rules — voicemail, complete facts, refusal. No Twilio."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_SERVER = Path(__file__).resolve().parents[1]
_REPO = Path(__file__).resolve().parents[2]
for _p in (str(_REPO), str(_SERVER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

FULL_FACTS = {
    "allInPrice": 610,
    "laborRatePerHour": 120,
    "laborHours": 2.5,
    "acceptsCustomerParts": True,
    "partsType": "oem",
    "warrantyMonths": 12,
    "earliestSlot": "2026-09-20T09:00:00",
    "confidence": 0.9,
}


def _blocked(*_args, **_kwargs):
    raise AssertionError("network is forbidden in call-rule tests")


class TestCallRules(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from server import call_rules as rules
        except ImportError:
            import call_rules as rules  # type: ignore

        self.rules = rules
        p = patch("urllib.request.urlopen", _blocked)
        p.start()
        self.addCleanup(p.stop)
        for name in ("server.twilio_call", "twilio_call"):
            try:
                mod = __import__(name, fromlist=["start_call"])
            except ImportError:
                continue
            tw = patch.object(mod, "start_call", side_effect=_blocked)
            tw.start()
            self.addCleanup(tw.stop)

    def test_is_voicemail_greeting(self) -> None:
        self.assertTrue(
            self.rules.is_voicemail(
                "You've reached Elite Motors. Leave a message after the beep."
            )
        )
        self.assertFalse(self.rules.is_voicemail("Service desk, how can I help?"))

    def test_is_voicemail_uses_business_transcript_only(self) -> None:
        lines = [
            {"role": "agent", "text": "Hi, leave a message if this is a mailbox.", "t": 0},
            {"role": "business", "text": "This is Sam, go ahead.", "t": 2},
        ]
        self.assertFalse(self.rules.is_voicemail(lines))
        lines[1]["text"] = "You've reached the office. Please leave a message."
        self.assertTrue(self.rules.is_voicemail(lines))

    def test_is_refusal(self) -> None:
        self.assertTrue(self.rules.is_refusal("Sorry, we don't give quotes over the phone."))
        self.assertFalse(self.rules.is_refusal("Six ten out the door with OEM parts."))

    def test_all_fields_filled(self) -> None:
        self.assertTrue(self.rules.all_fields_filled(FULL_FACTS))
        incomplete = dict(FULL_FACTS)
        incomplete.pop("earliestSlot")
        self.assertFalse(self.rules.all_fields_filled(incomplete))
        zero_price = dict(FULL_FACTS, allInPrice=0)
        self.assertFalse(self.rules.all_fields_filled(zero_price))

    def test_should_hang_up_voicemail_refusal_or_complete(self) -> None:
        self.assertTrue(
            self.rules.should_hang_up(text="Please leave a voicemail after the tone")
        )
        self.assertTrue(self.rules.should_hang_up(text="We're not interested, don't call."))
        self.assertTrue(self.rules.should_hang_up(facts=FULL_FACTS))
        self.assertFalse(
            self.rules.should_hang_up(
                text="What year is the Camry?",
                facts={"allInPrice": 610, "confidence": 0.4},
            )
        )

    def test_disclosure_mentions_assistant(self) -> None:
        line = self.rules.disclosure_line()
        self.assertIn("assistant", line.lower())
        self.assertIn("customer", line.lower())


if __name__ == "__main__":
    unittest.main()
