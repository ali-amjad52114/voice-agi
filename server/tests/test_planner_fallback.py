"""Planner Camry fallback — no LLM, SerpAPI, or Twilio."""

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

CAMRY = (
    "One mechanic quoted me $800 for front brakes on my 2019 Camry, "
    "I'm in Fremont. Find the part price online, call mechanics and "
    "dealers near me, get their all-in price and their hourly labor "
    "rate, and tell me whether I should bring my own part or let "
    "them supply it."
)


def _blocked(*_args, **_kwargs):
    raise AssertionError("network is forbidden in planner tests")


class TestPlannerCamryFallback(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from server import planner as planner_mod
        except ImportError:
            import planner as planner_mod  # type: ignore

        self.planner = planner_mod
        self._patches = [
            patch.object(planner_mod, "complete", side_effect=RuntimeError("offline")),
            patch("urllib.request.urlopen", _blocked),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self._mock_io()

    def _mock_io(self) -> None:
        for name in ("server.discovery", "discovery"):
            try:
                mod = __import__(name, fromlist=["discover_shops"])
            except ImportError:
                continue
            p = patch.object(mod, "discover_shops", side_effect=_blocked)
            p.start()
            self.addCleanup(p.stop)
        for name in ("server.twilio_call", "twilio_call"):
            try:
                mod = __import__(name, fromlist=["start_call"])
            except ImportError:
                continue
            p = patch.object(mod, "start_call", side_effect=_blocked)
            p.start()
            self.addCleanup(p.stop)

    def test_camry_sentence_offline_fields(self) -> None:
        out = self.planner.plan(CAMRY)
        self.assertEqual(out["title"], "Brake repair")
        self.assertEqual(out["userQuote"], 800.0)
        self.assertEqual(out["vehicle"], "2019 Camry")
        self.assertEqual(out["location"], "Fremont")
        self.assertGreaterEqual(out["businessCount"], 5)
        self.assertLessEqual(out["businessCount"], 8)
        self.assertIn("allInPrice", out["factsNeeded"])
        self.assertIn("laborRatePerHour", out["factsNeeded"])
        self.assertIsInstance(out["callScript"], str)
        self.assertTrue(out["callScript"])
        self.assertIn("2019 Camry", out["callScript"])
        schema = out["extractionSchema"]
        self.assertIsInstance(schema, dict)
        self.assertIn("confidence", schema.get("properties", schema))

    def test_location_hint_used_when_city_missing(self) -> None:
        out = self.planner.plan(
            "Need front brakes on a 2018 Honda, quoted $500",
            location="Hayward",
        )
        self.assertEqual(out["location"], "Hayward")
        self.assertEqual(out["userQuote"], 500.0)
        self.assertEqual(out["vehicle"], "2018 Honda")

    def test_plan_does_not_import_or_call_discovery(self) -> None:
        self.planner.plan(CAMRY)
        # If discover_shops / start_call were invoked, setUp patches raise.


if __name__ == "__main__":
    unittest.main()
