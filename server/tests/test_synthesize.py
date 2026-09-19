"""Synthesize from provided facts only — no invented shops, no network."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_SERVER = Path(__file__).resolve().parents[1]
_REPO = Path(__file__).resolve().parents[2]
for _p in (str(_REPO), str(_SERVER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Fixture-shaped facts: RockAuto part + Sam's quote. Elite is voicemail.
PROVIDED_AGENTS = [
    {
        "id": "a_rockauto",
        "taskId": "task_test",
        "kind": "web",
        "status": "done",
        "business": {"name": "RockAuto", "type": "parts"},
        "facts": {"partPrice": 186, "partsType": "oem", "confidence": 0.94},
    },
    {
        "id": "a_sams",
        "taskId": "task_test",
        "kind": "call",
        "status": "done",
        "business": {"name": "Sam's Auto", "type": "mechanic", "phone": "+15105550122"},
        "facts": {
            "allInPrice": 610,
            "laborRatePerHour": 120,
            "laborHours": 2.5,
            "acceptsCustomerParts": True,
            "partsType": "oem",
            "warrantyMonths": 12,
            "earliestSlot": "2026-09-20T09:00:00",
            "confidence": 0.93,
        },
        "call": {"durationS": 134, "answeredBy": "Sam", "outcome": "quote"},
    },
    {
        "id": "a_elite",
        "taskId": "task_test",
        "kind": "call",
        "status": "done",
        "business": {"name": "Elite Motors", "type": "mechanic", "phone": "+15105550199"},
        "call": {"durationS": 24, "outcome": "voicemail"},
    },
]


def _blocked(*_args, **_kwargs):
    raise AssertionError("network is forbidden in synthesize tests")


class TestSynthesizeProvidedFacts(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from server import synthesize as syn
        except ImportError:
            import synthesize as syn  # type: ignore

        self.syn = syn
        patches = [
            patch.object(syn, "_llm_complete", None),
            patch("urllib.request.urlopen", _blocked),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
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

    def test_byo_and_shop_from_provided_facts(self) -> None:
        result = self.syn.synthesize(agents=PROVIDED_AGENTS, userQuote=800)
        labels = [opt["label"] for opt in result["options"]]
        self.assertIn("Bring your own part", labels)
        self.assertIn("Shop supplies part", labels)
        byo = next(o for o in result["options"] if o["label"] == "Bring your own part")
        shop = next(o for o in result["options"] if o["label"] == "Shop supplies part")
        self.assertEqual(byo["total"], 486)
        self.assertEqual(shop["total"], 610)
        self.assertIn("a_rockauto", byo["agentIds"])
        self.assertIn("a_sams", byo["agentIds"])
        self.assertEqual(shop["agentIds"], ["a_sams"])
        self.assertNotIn("a_elite", json.dumps(result))
        self.assertEqual(result["recommendedAgentId"], "a_sams")
        self.assertEqual(result["savingsVsQuote"], 314)
        self.assertEqual(len(result["why"].split(".")), 3)  # two sentences + trailing empty
        self.assertTrue(result["why"].count(".") >= 2)

    def test_ignores_agents_without_real_facts(self) -> None:
        result = self.syn.synthesize(
            agents=PROVIDED_AGENTS
            + [
                {
                    "id": "a_ghost",
                    "taskId": "task_test",
                    "kind": "call",
                    "status": "done",
                    "business": {"name": "Ghost Garage", "type": "mechanic"},
                    "summary": "should never become a price",
                }
            ],
            userQuote=800,
        )
        dumped = json.dumps(result)
        self.assertNotIn("Ghost Garage", dumped)
        self.assertNotIn("a_ghost", dumped)
        shop = next(o for o in result["options"] if o["label"] == "Shop supplies part")
        self.assertEqual(shop["total"], 610)

    def test_one_real_quote_says_so(self) -> None:
        only_sams = [a for a in PROVIDED_AGENTS if a["id"] != "a_rockauto"]
        result = self.syn.synthesize(agents=only_sams, userQuote=800)
        self.assertEqual(len(result["options"]), 1)
        self.assertEqual(result["options"][0]["total"], 610)
        self.assertIn("only one shop", result["why"].lower())
        self.assertNotIn("invent", result["why"].lower() + " ")

    def test_no_quotes_does_not_invent_prices(self) -> None:
        voicemail_only = [a for a in PROVIDED_AGENTS if a["id"] == "a_elite"]
        result = self.syn.synthesize(agents=voicemail_only, userQuote=800)
        self.assertEqual(result["options"], [])
        self.assertIn("did not invent", result["why"].lower())

    def test_task_object_same_as_agents_kwarg(self) -> None:
        try:
            from server.models import Task
        except ImportError:
            from models import Task  # type: ignore

        task = Task.model_validate(
            {
                "id": "task_test",
                "title": "Brake repair",
                "request": "2019 Camry brakes",
                "createdAt": "2026-09-19T14:02:00Z",
                "status": "running",
                "userQuote": 800,
                "agents": PROVIDED_AGENTS,
            }
        )
        from_task = self.syn.synthesize(task)
        from_kwargs = self.syn.synthesize(agents=PROVIDED_AGENTS, userQuote=800)
        self.assertEqual(from_task["options"], from_kwargs["options"])
        self.assertEqual(from_task["savingsVsQuote"], from_kwargs["savingsVsQuote"])


if __name__ == "__main__":
    unittest.main()
