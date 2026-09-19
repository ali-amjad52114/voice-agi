"""Hang-up rules — voicemail, complete facts, refusal. No Twilio."""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_SERVER = Path(__file__).resolve().parents[1]
_REPO = Path(__file__).resolve().parents[2]
for _p in (str(_REPO), str(_SERVER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# A complete call quote: all-in, the shop's own part price, rate, hours,
# customer-parts policy, parts type, warranty, earliest slot.
FULL_FACTS = {
    "allInPrice": 610,
    "partPrice": 220,
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


def _without(facts: dict, *keys: str) -> dict:
    out = dict(facts)
    for key in keys:
        out.pop(key, None)
    return out


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

    def test_required_fields_include_hours_and_part_price(self) -> None:
        self.assertIn("laborHours", self.rules.REQUIRED_CALL_FIELDS)
        self.assertIn("partPrice", self.rules.REQUIRED_CALL_FIELDS)

    def test_all_fields_filled(self) -> None:
        self.assertTrue(self.rules.all_fields_filled(FULL_FACTS))
        self.assertFalse(self.rules.all_fields_filled(_without(FULL_FACTS, "earliestSlot")))
        self.assertFalse(self.rules.all_fields_filled(dict(FULL_FACTS, allInPrice=0)))

    def test_labor_hours_required_and_positive(self) -> None:
        self.assertFalse(self.rules.all_fields_filled(_without(FULL_FACTS, "laborHours")))
        self.assertFalse(self.rules.all_fields_filled(dict(FULL_FACTS, laborHours=None)))
        self.assertFalse(self.rules.all_fields_filled(dict(FULL_FACTS, laborHours=0)))
        self.assertFalse(self.rules.all_fields_filled(dict(FULL_FACTS, laborHours="soon")))
        self.assertTrue(self.rules.all_fields_filled(dict(FULL_FACTS, laborHours=1)))

    def test_part_price_required_when_shop_takes_customer_parts(self) -> None:
        self.assertFalse(self.rules.all_fields_filled(_without(FULL_FACTS, "partPrice")))
        self.assertFalse(self.rules.all_fields_filled(dict(FULL_FACTS, partPrice=None)))
        self.assertFalse(self.rules.all_fields_filled(dict(FULL_FACTS, partPrice=0)))
        self.assertFalse(self.rules.all_fields_filled(dict(FULL_FACTS, partPrice="n/a")))

    def test_part_price_optional_when_shop_refuses_customer_parts(self) -> None:
        refuses = dict(FULL_FACTS, acceptsCustomerParts=False)
        self.assertTrue(self.rules.all_fields_filled(_without(refuses, "partPrice")))
        self.assertTrue(self.rules.all_fields_filled(dict(refuses, partPrice=None)))
        # Still complete if they happened to quote the part anyway.
        self.assertTrue(self.rules.all_fields_filled(refuses))
        # The exception only waives partPrice, nothing else.
        self.assertFalse(self.rules.all_fields_filled(_without(refuses, "partPrice", "laborHours")))

    def test_part_price_not_waived_when_customer_parts_unknown(self) -> None:
        unknown = _without(FULL_FACTS, "partPrice", "acceptsCustomerParts")
        self.assertFalse(self.rules.all_fields_filled(unknown))
        self.assertFalse(
            self.rules.all_fields_filled(
                dict(_without(FULL_FACTS, "partPrice"), acceptsCustomerParts=None)
            )
        )

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

    def test_should_hang_up_waits_for_hours_and_part_price(self) -> None:
        # The six-question quote from the first working call is no longer enough.
        old_six = _without(FULL_FACTS, "partPrice", "laborHours")
        self.assertFalse(self.rules.should_hang_up(text="Tomorrow.", facts=old_six))
        self.assertFalse(
            self.rules.should_hang_up(text="Tomorrow.", facts=_without(FULL_FACTS, "partPrice"))
        )
        # A shop that refuses customer parts is complete without a part price.
        refuses = dict(_without(FULL_FACTS, "partPrice"), acceptsCustomerParts=False)
        self.assertTrue(self.rules.should_hang_up(text="Tomorrow.", facts=refuses))

    def test_disclosure_mentions_assistant(self) -> None:
        line = self.rules.disclosure_line()
        self.assertIn("assistant", line.lower())
        self.assertIn("customer", line.lower())

    def test_caller_prompt_lists_eight_short_questions_in_order(self) -> None:
        prompt = self.rules.load_caller_prompt()
        numbered = re.findall(r"^(\d+)\.\s+(.+?)\s*$", prompt, flags=re.MULTILINE)
        self.assertEqual([int(n) for n, _ in numbered], list(range(1, 9)))
        questions = [q for _, q in numbered]
        for q in questions:
            self.assertLess(len(q.split()), 20, q)
        expected_order = (
            "all-in",
            "part alone",
            "labor rate",
            "hours",
            "customer",
            "oem",
            "warranty",
            "earliest",
        )
        for q, needle in zip(questions, expected_order):
            self.assertIn(needle, q.lower(), q)
        # Implied-labor rule: confirmation only, never a new number.
        self.assertIn("so labor is about x, is that right?", prompt.lower())
        self.assertIn("never a new number", prompt.lower())


if __name__ == "__main__":
    unittest.main()
