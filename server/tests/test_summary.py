"""Card summaries come only from extracted facts — every branch, always < 60 chars."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SERVER = Path(__file__).resolve().parents[1]
_REPO = Path(__file__).resolve().parents[2]
for _p in (str(_REPO), str(_SERVER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from server.models import Agent, Business, CallInfo, Facts
    from server.summary import MAX_LEN, summary_from_facts
except ImportError:  # pragma: no cover
    from models import Agent, Business, CallInfo, Facts  # type: ignore
    from summary import MAX_LEN, summary_from_facts  # type: ignore

# What extraction produced on the shared fixture (Fremont Auto Tech, 81 s).
FIXTURE_FACTS = {
    "allInPrice": 450,
    "laborRatePerHour": 150,
    "laborHours": None,
    "acceptsCustomerParts": None,
    "partsType": None,
    "warrantyMonths": 1,
    "earliestSlot": "Tomorrow",
    "partPrice": None,
    "confidence": 0.8,
}
FIXTURE_SUMMARY = "$450 all-in · $150/h · 1-mo warranty"


def _call(facts: dict | None = None, outcome: str | None = "quote", **kw) -> dict:
    agent: dict = {
        "id": "a_call",
        "taskId": "task_test",
        "kind": "call",
        "status": "done",
        "business": {"name": "Fremont Auto Tech", "type": "mechanic"},
    }
    if facts is not None:
        agent["facts"] = facts
    if outcome is not None:
        agent["call"] = {"durationS": 81, "outcome": outcome}
    agent.update(kw)
    return agent


def _web(facts: dict | None = None) -> dict:
    agent: dict = {
        "id": "a_web",
        "taskId": "task_test",
        "kind": "web",
        "status": "done",
        "business": {"name": "RockAuto", "type": "parts", "url": "https://example.test"},
    }
    if facts is not None:
        agent["facts"] = facts
    return agent


class SummaryCase(unittest.TestCase):
    """Base with a helper that asserts the text and the length bound together."""

    def check(self, agent, expected: str) -> None:
        got = summary_from_facts(agent)
        self.assertEqual(got, expected)
        self.assertLess(len(got), 60)
        self.assertLess(len(got), MAX_LEN)


class TestSharedFixture(SummaryCase):
    def test_fixture_facts_as_dict(self) -> None:
        self.check(_call(FIXTURE_FACTS), FIXTURE_SUMMARY)

    def test_fixture_facts_as_agent_model(self) -> None:
        agent = Agent(
            id="a_fremont",
            taskId="task_test",
            kind="call",
            status="done",
            business=Business(name="Fremont Auto Tech", type="mechanic"),
            facts=Facts(**FIXTURE_FACTS),
            call=CallInfo(durationS=81, outcome="quote"),
        )
        self.check(agent, FIXTURE_SUMMARY)

    def test_fixture_with_customer_parts_inferred(self) -> None:
        # What Session 2 should produce: "Yeah, I can do" → acceptsCustomerParts true.
        facts = dict(FIXTURE_FACTS, acceptsCustomerParts=True)
        self.check(
            _call(facts),
            "$450 all-in · $150/h · takes your parts · 1-mo warranty",
        )


class TestCallFactOrderAndFormat(SummaryCase):
    def test_all_in_only(self) -> None:
        self.check(_call({"allInPrice": 450, "confidence": 0.5}), "$450 all-in")

    def test_part_price_is_the_shops_own_part(self) -> None:
        self.check(
            _call({"allInPrice": 450, "partPrice": 500, "confidence": 0.5}),
            "$450 all-in · part $500",
        )

    def test_rate_and_hours(self) -> None:
        self.check(
            _call({"laborRatePerHour": 150, "laborHours": 2.5, "confidence": 0.5}),
            "$150/h · 2.5 h",
        )

    def test_whole_hours_have_no_decimal(self) -> None:
        self.check(_call({"laborHours": 2.0, "confidence": 0.5}), "2 h")

    def test_no_customer_parts_and_aftermarket(self) -> None:
        self.check(
            _call(
                {
                    "acceptsCustomerParts": False,
                    "partsType": "aftermarket",
                    "confidence": 0.5,
                }
            ),
            "no customer parts · aftermarket",
        )

    def test_oem_label_is_uppercase(self) -> None:
        self.check(_call({"partsType": "oem", "confidence": 0.5}), "OEM")

    def test_warranty_months_not_divisible_by_twelve(self) -> None:
        self.check(_call({"warrantyMonths": 18, "confidence": 0.5}), "18-mo warranty")

    def test_warranty_divisible_by_twelve_shows_years(self) -> None:
        self.check(_call({"warrantyMonths": 24, "confidence": 0.5}), "2-yr warranty")
        self.check(_call({"warrantyMonths": 12, "confidence": 0.5}), "1-yr warranty")

    def test_dollars_with_cents_use_two_decimals(self) -> None:
        self.check(
            _call({"allInPrice": 612.5, "laborRatePerHour": 99.99, "confidence": 0.5}),
            "$612.50 all-in · $99.99/h",
        )

    def test_full_fact_order(self) -> None:
        # Every field present; the join is longer than the limit so the trailing
        # items (OEM, warranty) are dropped and the numbers survive.
        facts = {
            "allInPrice": 610,
            "partPrice": 200,
            "laborRatePerHour": 120,
            "laborHours": 2.5,
            "acceptsCustomerParts": True,
            "partsType": "oem",
            "warrantyMonths": 12,
            "earliestSlot": "2026-09-20T09:00:00",
            "confidence": 0.93,
        }
        got = summary_from_facts(_call(facts))
        self.assertEqual(got, "$610 all-in · part $200 · $120/h · 2.5 h · takes your parts")
        self.assertLess(len(got), 60)
        self.assertTrue(got.startswith("$610 all-in · part $200 · $120/h"))
        self.assertNotIn("warranty", got)

    def test_trimming_keeps_prefix_intact(self) -> None:
        facts = {
            "allInPrice": 1234.5,
            "partPrice": 999.99,
            "laborRatePerHour": 175,
            "laborHours": 3.25,
            "acceptsCustomerParts": False,
            "partsType": "aftermarket",
            "warrantyMonths": 36,
            "confidence": 0.9,
        }
        got = summary_from_facts(_call(facts))
        full = (
            "$1234.50 all-in · part $999.99 · $175/h · 3.25 h"
            " · no customer parts · aftermarket · 3-yr warranty"
        )
        self.assertGreaterEqual(len(full), 60)
        self.assertTrue(full.startswith(got))
        self.assertLess(len(got), 60)
        self.assertEqual(got, "$1234.50 all-in · part $999.99 · $175/h · 3.25 h")

    def test_earliest_slot_and_confidence_are_not_shown(self) -> None:
        got = summary_from_facts(
            _call({"allInPrice": 450, "earliestSlot": "Tomorrow", "confidence": 0.99})
        )
        self.assertEqual(got, "$450 all-in")
        self.assertNotIn("Tomorrow", got)
        self.assertLess(len(got), 60)


class TestCallOutcomes(SummaryCase):
    def test_voicemail(self) -> None:
        self.check(_call(outcome="voicemail"), "Voicemail · no quote")

    def test_refused(self) -> None:
        self.check(_call(outcome="refused"), "Declined to quote")

    def test_answered_without_facts(self) -> None:
        self.check(_call(outcome="quote"), "Call answered · no quote")

    def test_answered_with_empty_facts(self) -> None:
        # Offline extraction: confidence 0 and nothing else. Still no invented price.
        self.check(_call({"confidence": 0.0}, outcome="quote"), "Call answered · no quote")

    def test_answered_with_facts_model_confidence_only(self) -> None:
        agent = Agent(
            id="a_x",
            taskId="task_test",
            kind="call",
            status="done",
            business=Business(name="Shop", type="mechanic"),
            facts=Facts(confidence=0.0),
            call=CallInfo(durationS=40, outcome="quote"),
        )
        self.check(agent, "Call answered · no quote")

    def test_no_call_info_and_no_facts(self) -> None:
        self.check(_call(outcome=None), "Call answered · no quote")

    def test_voicemail_wins_over_stray_facts(self) -> None:
        self.check(
            _call({"allInPrice": 450, "confidence": 0.1}, outcome="voicemail"),
            "Voicemail · no quote",
        )


class TestWebAgents(SummaryCase):
    def test_oem_part(self) -> None:
        self.check(
            _web({"partPrice": 186, "partsType": "oem", "confidence": 0.94}),
            "OEM pads + rotors $186",
        )

    def test_aftermarket_part(self) -> None:
        self.check(
            _web({"partPrice": 129.99, "partsType": "aftermarket", "confidence": 0.9}),
            "aftermarket pads + rotors $129.99",
        )

    def test_unknown_parts_type(self) -> None:
        self.check(_web({"partPrice": 186, "confidence": 0.9}), "part $186")

    def test_web_agent_without_price(self) -> None:
        got = summary_from_facts(_web({"confidence": 0.0}))
        self.assertNotIn("$", got)
        self.assertLess(len(got), 60)
        self.assertEqual(got, summary_from_facts(_web(None)))

    def test_web_agent_model(self) -> None:
        agent = Agent(
            id="a_rockauto",
            taskId="task_test",
            kind="web",
            status="done",
            business=Business(name="RockAuto", type="parts"),
            facts=Facts(partPrice=186, partsType="oem", confidence=0.94),
        )
        self.check(agent, "OEM pads + rotors $186")


class TestEveryBranchUnderSixty(unittest.TestCase):
    """One sweep over every branch, dict and model, asserting the length bound."""

    def test_all_branches(self) -> None:
        cases = [
            _call(FIXTURE_FACTS),
            _call({"allInPrice": 450, "confidence": 0.5}),
            _call({"partPrice": 500, "confidence": 0.5}),
            _call({"laborRatePerHour": 150, "confidence": 0.5}),
            _call({"laborHours": 2.5, "confidence": 0.5}),
            _call({"acceptsCustomerParts": True, "confidence": 0.5}),
            _call({"acceptsCustomerParts": False, "confidence": 0.5}),
            _call({"partsType": "oem", "confidence": 0.5}),
            _call({"partsType": "aftermarket", "confidence": 0.5}),
            _call({"warrantyMonths": 1, "confidence": 0.5}),
            _call({"warrantyMonths": 24, "confidence": 0.5}),
            _call(
                {
                    "allInPrice": 99999.99,
                    "partPrice": 99999.99,
                    "laborRatePerHour": 9999.99,
                    "laborHours": 99.75,
                    "acceptsCustomerParts": False,
                    "partsType": "aftermarket",
                    "warrantyMonths": 120,
                    "confidence": 1.0,
                }
            ),
            _call(outcome="voicemail"),
            _call(outcome="refused"),
            _call(outcome="quote"),
            _call(outcome="error"),
            _call(outcome=None),
            _call({"confidence": 0.0}),
            _web({"partPrice": 186, "partsType": "oem", "confidence": 0.9}),
            _web({"partPrice": 186, "partsType": "aftermarket", "confidence": 0.9}),
            _web({"partPrice": 186, "confidence": 0.9}),
            _web({"partPrice": 123456.78, "partsType": "aftermarket", "confidence": 0.9}),
            _web({"confidence": 0.0}),
            _web(None),
        ]
        for case in cases:
            with self.subTest(case=case):
                got = summary_from_facts(case)
                self.assertIsInstance(got, str)
                self.assertTrue(got)
                self.assertLess(len(got), 60)
                # And the same agent as a pydantic model.
                model_got = summary_from_facts(Agent.model_validate(case))
                self.assertEqual(model_got, got)


if __name__ == "__main__":
    unittest.main()
