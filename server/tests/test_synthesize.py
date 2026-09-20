"""Synthesize from provided facts only — no invented shops, no network."""

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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


# --------------------------------------------------------------------------- #
# Session 4: General Compute decides, Python verifies every dollar.
# --------------------------------------------------------------------------- #

# Three shops + two web parts. Cheapest web part is PartsGeek at $140.
#   Sam's Auto       : cheapest all-in ($480), labor 2 h x $140 = $280 -> BYO $420
#   Fremont Auto Tech: cheapest labor (2.5 h x $100 = $250)           -> BYO $390
#   Elite Motors     : refuses customer parts, all-in $550
DECISION_AGENTS = [
    {
        "id": "w_rockauto",
        "taskId": "task_test",
        "kind": "web",
        "status": "done",
        "business": {"name": "RockAuto", "type": "parts"},
        "facts": {"partPrice": 186, "partsType": "oem", "confidence": 0.94},
    },
    {
        "id": "w_partsgeek",
        "taskId": "task_test",
        "kind": "web",
        "status": "done",
        "business": {"name": "PartsGeek", "type": "parts"},
        "facts": {"partPrice": 140, "partsType": "aftermarket", "confidence": 0.9},
    },
    {
        "id": "s_sams",
        "taskId": "task_test",
        "kind": "call",
        "status": "done",
        "business": {"name": "Sam's Auto", "type": "mechanic", "phone": "+15105550122"},
        "facts": {
            "allInPrice": 480,
            "partPrice": 200,
            "laborRatePerHour": 140,
            "laborHours": 2,
            "acceptsCustomerParts": True,
            "partsType": "oem",
            "warrantyMonths": 12,
            "earliestSlot": "Tomorrow",
            "confidence": 0.93,
        },
        "call": {"durationS": 120, "answeredBy": "Sam", "outcome": "quote"},
    },
    {
        "id": "s_fremont",
        "taskId": "task_test",
        "kind": "call",
        "status": "done",
        "business": {"name": "Fremont Auto Tech", "type": "mechanic", "phone": "+15105550133"},
        "facts": {
            "allInPrice": 610,
            "partPrice": 260,
            "laborRatePerHour": 100,
            "laborHours": 2.5,
            "acceptsCustomerParts": True,
            "partsType": None,
            "warrantyMonths": 1,
            "earliestSlot": "Tomorrow",
            "confidence": 0.9,
        },
        "call": {"durationS": 81, "outcome": "quote"},
    },
    {
        "id": "s_elite",
        "taskId": "task_test",
        "kind": "call",
        "status": "done",
        "business": {"name": "Elite Motors", "type": "mechanic", "phone": "+15105550199"},
        "facts": {
            "allInPrice": 550,
            "laborRatePerHour": 150,
            "laborHours": 2,
            "acceptsCustomerParts": False,
            "partsType": "oem",
            "warrantyMonths": 24,
            "confidence": 0.88,
        },
        "call": {"durationS": 95, "outcome": "quote"},
    },
]

VALID_DECISION = {
    "options": [
        {
            "label": "Bring your own part",
            "total": 390,
            "breakdown": "$140 part from PartsGeek + 2.5 h × $100 labor at Fremont Auto Tech",
            "agentIds": ["w_partsgeek", "s_fremont"],
            "hassle": "Order the part, wait for delivery, then one visit",
        },
        {
            "label": "Shop supplies part",
            "total": 480,
            "breakdown": "All-in at Sam's Auto, oem parts, 1-year warranty",
            "agentIds": ["s_sams"],
            "hassle": "One visit, shop handles the part",
        },
    ],
    "recommendedOptionIndex": 1,
    "why": (
        "Shop-supplied at Sam's Auto is $480 against $390 bringing your own part to Fremont Auto Tech. "
        "The $90 saving is under the $150 threshold, so the 1-year warranty and single visit win."
    ),
    "tradeoffs": [
        "Sam's Auto includes a 1-year warranty; Fremont's is one month",
        "Bring-your-own needs the part ordered and delivered first",
        "Shop-supplied is $90 more",
    ],
}


class TestSynthesizeDecision(unittest.TestCase):
    """The model returns the decision; the verifier keeps only real dollars."""

    def setUp(self) -> None:
        try:
            from server import synthesize as syn
        except ImportError:
            import synthesize as syn  # type: ignore

        self.syn = syn
        p = patch("urllib.request.urlopen", _blocked)
        p.start()
        self.addCleanup(p.stop)

    def _run(self, reply, agents=DECISION_AGENTS, userQuote=800):
        """Run synthesize with ``llm.complete`` mocked to ``reply``."""
        if isinstance(reply, Exception):
            mock = MagicMock(side_effect=reply)
        elif isinstance(reply, str):
            mock = MagicMock(return_value=reply)
        else:
            mock = MagicMock(return_value=json.dumps(reply))
        with patch.object(self.syn, "_llm_complete", mock):
            result = self.syn.synthesize(agents=agents, userQuote=userQuote)
        return result, mock

    def _sentences(self, text: str) -> int:
        return len([s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s])

    def test_valid_decision_passes_verification(self) -> None:
        result, mock = self._run(VALID_DECISION)
        self.assertEqual(mock.call_count, 1)
        # llm.complete(system, user, json_schema=...) with the decision schema
        _system, user = mock.call_args.args[:2]
        self.assertIs(mock.call_args.kwargs["json_schema"], self.syn._DECISION_JSON_SCHEMA)
        sent = json.loads(user)
        self.assertEqual({s["agentId"] for s in sent["shops"]}, {"s_sams", "s_fremont", "s_elite"})
        self.assertEqual({p["agentId"] for p in sent["webParts"]}, {"w_partsgeek", "w_rockauto"})
        self.assertEqual(sent["userQuote"], 800)
        self.assertIn("$150", sent["preferences"])

        self.assertEqual(len(result["options"]), 2)
        byo, shop = result["options"]
        self.assertEqual(byo["label"], "Bring your own part")
        self.assertEqual(byo["total"], 390)
        self.assertEqual(byo["agentIds"], ["w_partsgeek", "s_fremont"])
        self.assertEqual(shop["label"], "Shop supplies part")
        self.assertEqual(shop["total"], 480)
        self.assertEqual(shop["agentIds"], ["s_sams"])
        # hassle folded into breakdown (ResultOption has no hassle field)
        self.assertIn("hassle:", byo["breakdown"])
        self.assertIn("one visit", byo["breakdown"].lower())
        self.assertIn("shop handles the part", shop["breakdown"])
        self.assertEqual(result["recommendedOptionIndex"], 1)
        self.assertEqual(result["recommendedAgentId"], "s_sams")
        self.assertEqual(result["savingsVsQuote"], 320)
        self.assertEqual(result["why"], VALID_DECISION["why"])
        self.assertEqual(self._sentences(result["why"]), 2)
        self.assertEqual(result["tradeoffs"], VALID_DECISION["tradeoffs"])
        self.assertTrue(2 <= len(result["tradeoffs"]) <= 4)
        self.assertNotIn("s_elite", json.dumps(result["options"]))

    def test_wrong_total_option_is_dropped(self) -> None:
        reply = json.loads(json.dumps(VALID_DECISION))
        reply["options"][1]["total"] = 430  # Sam's all-in is 480
        reply["recommendedOptionIndex"] = 0
        reply["why"] = (
            "Bringing your own part to Fremont Auto Tech is $390. "
            "That is the cheapest verified option from the shops that quoted."
        )
        reply["tradeoffs"] = ["Fremont's warranty is one month", "You must order the part first"]
        result, _ = self._run(reply)
        self.assertEqual(len(result["options"]), 1)
        self.assertEqual(result["options"][0]["label"], "Bring your own part")
        self.assertEqual(result["options"][0]["total"], 390)
        self.assertNotIn("430", json.dumps(result))
        self.assertEqual(result["recommendedOptionIndex"], 0)
        self.assertEqual(result["recommendedAgentId"], "s_fremont")
        self.assertEqual(result["savingsVsQuote"], 410)

    def test_recommended_option_with_wrong_total_falls_back(self) -> None:
        reply = json.loads(json.dumps(VALID_DECISION))
        reply["options"][1]["total"] = 430  # recommended option is the bad one
        result, _ = self._run(reply)
        self.assertNotIn("430", json.dumps(result))
        totals = sorted(o["total"] for o in result["options"])
        self.assertEqual(totals, [390, 480])  # deterministic path

    def test_byo_at_shop_refusing_customer_parts_is_dropped(self) -> None:
        reply = json.loads(json.dumps(VALID_DECISION))
        reply["options"][0] = {
            "label": "Bring your own part",
            "total": 440,  # 140 + 2 x 150, arithmetic fine but Elite refuses
            "breakdown": "$140 part from PartsGeek + 2 h × $150 labor at Elite Motors",
            "agentIds": ["w_partsgeek", "s_elite"],
            "hassle": "order the part first",
        }
        result, _ = self._run(reply)
        self.assertEqual([o["total"] for o in result["options"]], [480])
        self.assertNotIn("440", json.dumps(result))
        self.assertNotIn("s_elite", json.dumps(result["options"]))

    def test_unknown_agent_id_is_dropped(self) -> None:
        reply = json.loads(json.dumps(VALID_DECISION))
        reply["options"][0]["agentIds"] = ["w_partsgeek", "s_ghost"]
        result, _ = self._run(reply)
        self.assertEqual([o["total"] for o in result["options"]], [480])
        self.assertNotIn("s_ghost", json.dumps(result))

    def test_model_total_within_a_dollar_is_replaced_by_recomputation(self) -> None:
        reply = json.loads(json.dumps(VALID_DECISION))
        reply["options"][0]["total"] = 390.6
        result, _ = self._run(reply)
        self.assertEqual(result["options"][0]["total"], 390)

    def test_invented_dollar_in_prose_is_rejected(self) -> None:
        reply = json.loads(json.dumps(VALID_DECISION))
        reply["options"][0]["breakdown"] = "$140 part + $275 labor at Fremont Auto Tech"
        reply["why"] = "Sam's Auto is $480 and a typical shop charges $999. Pick Sam's."
        reply["tradeoffs"] = [
            "Sam's warranty is 12 months",
            "Fremont charges $175 an hour",
            "One visit at Sam's",
        ]
        result, _ = self._run(reply)
        dumped = json.dumps(result)
        for bad in ("275", "999", "175"):
            self.assertNotIn(bad, dumped)
        self.assertEqual(len(result["options"]), 2)
        self.assertEqual(self._sentences(result["why"]), 2)  # deterministic why
        self.assertEqual(result["tradeoffs"], ["Sam's warranty is 12 months", "One visit at Sam's"])

    def test_model_error_uses_deterministic_fallback(self) -> None:
        result, mock = self._run(RuntimeError("General Compute offline"))
        self.assertEqual(mock.call_count, 1)
        labels = [o["label"] for o in result["options"]]
        self.assertEqual(labels, ["Bring your own part", "Shop supplies part"])
        self.assertEqual(result["options"][0]["total"], 390)  # cheapest labor shop
        self.assertEqual(result["options"][0]["agentIds"], ["w_partsgeek", "s_fremont"])
        self.assertEqual(result["options"][1]["total"], 480)  # cheapest all-in
        self.assertEqual(result["options"][1]["agentIds"], ["s_sams"])
        self.assertEqual(self._sentences(result["why"]), 2)
        self.assertTrue(2 <= len(result["tradeoffs"]) <= 4)
        self.assertEqual(result["savingsVsQuote"], 410)

    def test_garbage_reply_uses_deterministic_fallback(self) -> None:
        result, _ = self._run("not json at all")
        self.assertEqual(sorted(o["total"] for o in result["options"]), [390, 480])

    def test_one_shop_why_says_only_one_shop_answered(self) -> None:
        agents = [a for a in DECISION_AGENTS if a["id"] in ("w_partsgeek", "w_rockauto", "s_sams")]
        reply = {
            "options": [
                {
                    "label": "Bring your own part",
                    "total": 420,
                    "breakdown": "$140 part from PartsGeek + 2 h × $140 labor at Sam's Auto",
                    "agentIds": ["w_partsgeek", "s_sams"],
                    "hassle": "order the part, then one visit",
                },
                {
                    "label": "Shop supplies part",
                    "total": 480,
                    "breakdown": "All-in at Sam's Auto with 1-year warranty",
                    "agentIds": ["s_sams"],
                    "hassle": "one visit",
                },
            ],
            "recommendedOptionIndex": 1,
            "why": (
                "Only one shop answered with a quote, so this compares Sam's Auto against itself. "
                "The $60 saving does not beat the $150 threshold, so shop-supplied with the warranty wins."
            ),
            "tradeoffs": ["1-year warranty with shop part", "Bring-your-own saves $60"],
        }
        result, _ = self._run(reply, agents=agents)
        self.assertEqual(len(result["options"]), 2)
        self.assertIn("only one shop", result["why"].lower())
        self.assertEqual(result["why"], reply["why"])

        # If the model forgets to say so, the deterministic why is used instead.
        reply["why"] = "Sam's Auto is $480 shop-supplied. Bring-your-own is $420."
        result, _ = self._run(reply, agents=agents)
        self.assertIn("only one shop", result["why"].lower())
        self.assertEqual(self._sentences(result["why"]), 2)

    def test_zero_shops_gives_no_options_and_no_model_call(self) -> None:
        agents = [a for a in DECISION_AGENTS if a["kind"] == "web"] + [
            {
                "id": "s_voicemail",
                "taskId": "task_test",
                "kind": "call",
                "status": "done",
                "business": {"name": "Silent Garage", "type": "mechanic"},
                "call": {"durationS": 20, "outcome": "voicemail"},
            }
        ]
        result, mock = self._run(VALID_DECISION, agents=agents)
        self.assertEqual(mock.call_count, 0)
        self.assertEqual(result["options"], [])
        self.assertEqual(result["recommendedAgentId"], "")
        self.assertNotIn("savingsVsQuote", result)
        self.assertNotIn("tradeoffs", result)
        self.assertIn("no shop returned a real quote", result["why"].lower())

    def test_derived_labor_from_all_in_minus_part_price(self) -> None:
        # Shop said all-in and its own part price but not hours: labor = 450 - 200.
        agents = [
            DECISION_AGENTS[0],  # RockAuto $186
            {
                "id": "s_derived",
                "taskId": "task_test",
                "kind": "call",
                "status": "done",
                "business": {"name": "Derived Motors", "type": "mechanic"},
                "facts": {
                    "allInPrice": 450,
                    "partPrice": 200,
                    "laborRatePerHour": 150,
                    "laborHours": None,
                    "acceptsCustomerParts": True,
                    "warrantyMonths": 1,
                    "confidence": 0.8,
                },
                "call": {"durationS": 81, "outcome": "quote"},
            },
        ]
        reply = {
            "options": [
                {
                    "label": "Bring your own part",
                    "total": 436,
                    "breakdown": "$186 part from RockAuto + $250 labor derived from $450 all-in minus $200 part",
                    "agentIds": ["w_rockauto", "s_derived"],
                    "hassle": "order the part, then one visit",
                },
                {
                    "label": "Shop supplies part",
                    "total": 450,
                    "breakdown": "All-in at Derived Motors",
                    "agentIds": ["s_derived"],
                    "hassle": "one visit",
                },
            ],
            "recommendedOptionIndex": 1,
            "why": "Only one shop answered with a quote. The $14 gap is far below $150, so shop-supplied wins.",
            "tradeoffs": ["One-month warranty either way", "Bring-your-own saves only $14"],
        }
        result, _ = self._run(reply, agents=agents, userQuote=None)
        self.assertEqual([o["total"] for o in result["options"]], [436, 450])
        self.assertIn("derived", result["options"][0]["breakdown"].lower())
        self.assertNotIn("savingsVsQuote", result)

        # Deterministic path derives the same labor and says so.
        with patch.object(self.syn, "_llm_complete", None):
            det = self.syn.synthesize(agents=agents)
        self.assertEqual(det["options"][0]["total"], 436)
        self.assertIn("derived", det["options"][0]["breakdown"].lower())
        self.assertIn("$450 all-in", det["options"][0]["breakdown"])


# --------------------------------------------------------------------------- #
# Session 6: transcripts on the sheet, one verifier retry, json_object fallback.
# --------------------------------------------------------------------------- #


def _with_transcript(agent: dict, lines: list) -> dict:
    copy = json.loads(json.dumps(agent))
    copy["transcript"] = lines
    return copy


class TestSynthesizeTranscriptSheet(unittest.TestCase):
    """Each shop's transcript rides along on the sheet, capped, facts-only money."""

    def setUp(self) -> None:
        try:
            from server import synthesize as syn
        except ImportError:
            import synthesize as syn  # type: ignore

        self.syn = syn
        p = patch("urllib.request.urlopen", _blocked)
        p.start()
        self.addCleanup(p.stop)

    def _sent(self, agents):
        mock = MagicMock(return_value=json.dumps(VALID_DECISION))
        with patch.object(self.syn, "_llm_complete", mock):
            self.syn.synthesize(agents=agents, userQuote=800)
        return json.loads(mock.call_args_list[0].args[1])

    def test_user_json_contains_each_shop_transcript(self) -> None:
        try:
            from server.models import TranscriptLine
        except ImportError:
            from models import TranscriptLine  # type: ignore

        sams_lines = [
            {"role": "agent", "text": "Hi, calling about front brakes on a 2019 Camry.", "t": 0.5},
            {"role": "business", "text": "Sure, that's $480 all in with a one year warranty.", "t": 4.1},
        ]
        fremont_lines = [
            TranscriptLine(role="agent", text="Do you fit customer parts?", t=1.0),
            TranscriptLine(role="business", text="We do, labor is a hundred an hour.", t=3.0),
        ]
        agents = []
        for a in DECISION_AGENTS:
            if a["id"] == "s_sams":
                agents.append(_with_transcript(a, sams_lines))
            elif a["id"] == "s_fremont":
                agents.append(_with_transcript(a, fremont_lines))
            else:
                agents.append(a)  # s_elite keeps transcript=None
        sent = self._sent(agents)
        by_id = {s["agentId"]: s for s in sent["shops"]}
        self.assertEqual(
            by_id["s_sams"]["transcript"],
            [
                "agent: Hi, calling about front brakes on a 2019 Camry.",
                "business: Sure, that's $480 all in with a one year warranty.",
            ],
        )
        self.assertEqual(
            by_id["s_fremont"]["transcript"],
            ["agent: Do you fit customer parts?", "business: We do, labor is a hundred an hour."],
        )
        self.assertEqual(by_id["s_elite"]["transcript"], [])
        # Facts are still the only money on the sheet.
        self.assertEqual(by_id["s_sams"]["allInPrice"], 480)
        self.assertNotIn("transcript", sent["webParts"][0])

    def test_transcript_is_capped_at_forty_lines(self) -> None:
        lines = [
            {"role": "agent" if i % 2 == 0 else "business", "text": f"line {i}", "t": float(i)}
            for i in range(55)
        ]
        agents = [_with_transcript(a, lines) if a["id"] == "s_sams" else a for a in DECISION_AGENTS]
        sent = self._sent(agents)
        sams = next(s for s in sent["shops"] if s["agentId"] == "s_sams")
        self.assertEqual(len(sams["transcript"]), 40)
        self.assertEqual(sams["transcript"][0], "business: line 15")  # oldest lines dropped
        self.assertEqual(sams["transcript"][-1], "agent: line 54")

    def test_transcript_is_capped_at_two_thousand_chars(self) -> None:
        lines = [{"role": "business", "text": "x" * 150, "t": float(i)} for i in range(30)]
        agents = [_with_transcript(a, lines) if a["id"] == "s_sams" else a for a in DECISION_AGENTS]
        sent = self._sent(agents)
        sams = next(s for s in sent["shops"] if s["agentId"] == "s_sams")
        self.assertLessEqual(sum(len(line) for line in sams["transcript"]), 2000)
        self.assertLess(len(sams["transcript"]), 30)
        self.assertTrue(all(line.startswith("business: ") for line in sams["transcript"]))

    def test_prompt_describes_transcript_and_forbids_its_dollars(self) -> None:
        prompt = self.syn._load_prompt()
        self.assertIn("`transcript`", prompt)
        self.assertIn("must not be used", prompt)


BAD_DECISION = {
    "options": [
        {
            "label": "Shop supplies part",
            "total": 999,
            "breakdown": "All-in at Ninth Street Garage",
            "agentIds": ["s_ninth"],  # a shop that is not in the input
            "hassle": "one visit",
        },
        {
            "label": "Shop supplies part",
            "total": 480,
            "breakdown": "All-in at Sam's Auto",
            "agentIds": ["s_sams"],
            "hassle": "one visit",
        },
    ],
    "recommendedOptionIndex": 0,  # points at the hallucinated shop
    "why": "Ninth Street Garage is $999 all in. That beats Sam's Auto at $480 on warranty.",
    "tradeoffs": ["Ninth Street includes a lifetime warranty", "Sam's is $480"],
}


class TestSynthesizeVerifierRetry(unittest.TestCase):
    """A rejected answer gets exactly one follow-up that names the reasons."""

    def setUp(self) -> None:
        try:
            from server import synthesize as syn
        except ImportError:
            import synthesize as syn  # type: ignore

        self.syn = syn
        p = patch("urllib.request.urlopen", _blocked)
        p.start()
        self.addCleanup(p.stop)

    def _run(self, replies):
        mock = MagicMock(side_effect=[json.dumps(r) if not isinstance(r, str) else r for r in replies])
        with patch.object(self.syn, "_llm_complete", mock):
            result = self.syn.synthesize(agents=DECISION_AGENTS, userQuote=800)
        return result, mock

    def test_hallucinated_shop_triggers_one_retry_and_valid_answer_is_kept(self) -> None:
        result, mock = self._run([BAD_DECISION, VALID_DECISION])
        self.assertEqual(mock.call_count, 2)

        first_system, first_user = mock.call_args_list[0].args[:2]
        second_system, second_user = mock.call_args_list[1].args[:2]
        self.assertEqual(first_system, second_system)
        self.assertNotIn("rejected", first_user)
        self.assertIn("rejected", second_user)
        self.assertTrue(second_user.startswith(first_user))  # same sheet, plus reasons
        self.assertIn("s_ninth", second_user)
        self.assertIn("$999", second_user)
        self.assertIn("recommendedOptionIndex", second_user)
        self.assertEqual(mock.call_args_list[1].kwargs["json_schema"], self.syn._DECISION_JSON_SCHEMA)

        # The second (valid) answer is accepted; totals match Python within $1.
        byo, shop = result["options"]
        self.assertEqual(byo["total"], 390)
        self.assertEqual(byo["agentIds"], ["w_partsgeek", "s_fremont"])
        self.assertEqual(shop["total"], 480)
        self.assertEqual(shop["agentIds"], ["s_sams"])
        self.assertEqual(result["recommendedOptionIndex"], 1)
        self.assertEqual(result["why"], VALID_DECISION["why"])
        self.assertEqual(result["tradeoffs"], VALID_DECISION["tradeoffs"])
        dumped = json.dumps(result)
        self.assertNotIn("999", dumped)
        self.assertNotIn("s_ninth", dumped)
        self.assertNotIn("Ninth", dumped)

    def test_rejection_reasons_name_each_verifier_rule(self) -> None:
        shops = self.syn._shops(DECISION_AGENTS)
        parts = self.syn._web_parts(DECISION_AGENTS)
        parsed = json.loads(json.dumps(VALID_DECISION))
        parsed["options"][0]["agentIds"] = ["w_partsgeek", "s_elite"]  # refuses customer parts
        parsed["options"][1]["total"] = 430  # Sam's all-in is 480
        parsed["recommendedOptionIndex"] = 5
        parsed["why"] = "One sentence only with $777."
        reasons = self.syn._rejection_reasons(parsed, shops, parts, 800.0)
        text = "\n".join(reasons)
        self.assertIn("does not accept customer parts", text)
        self.assertIn("$430", text)
        self.assertIn("$480", text)
        self.assertIn("recommendedOptionIndex 5", text)
        self.assertIn("two sentences", text)
        self.assertIn("$777", text)

    def test_two_bad_answers_fall_back_to_deterministic(self) -> None:
        result, mock = self._run([BAD_DECISION, BAD_DECISION])
        self.assertEqual(mock.call_count, 2)
        self.assertEqual(sorted(o["total"] for o in result["options"]), [390, 480])
        self.assertEqual(result["options"][0]["agentIds"], ["w_partsgeek", "s_fremont"])
        self.assertEqual(result["options"][1]["agentIds"], ["s_sams"])
        self.assertEqual(result["savingsVsQuote"], 410)
        dumped = json.dumps(result)
        self.assertNotIn("999", dumped)
        self.assertNotIn("s_ninth", dumped)

    def test_model_error_on_retry_falls_back(self) -> None:
        result, mock = self._run([json.dumps(BAD_DECISION), "not json"])
        self.assertEqual(mock.call_count, 2)
        self.assertEqual(sorted(o["total"] for o in result["options"]), [390, 480])


class _FakeCompletions:
    """Stands in for ``client.chat.completions``; the first call may raise."""

    def __init__(self, first_error: Exception | None, reply: str) -> None:
        self.first_error = first_error
        self.reply = reply
        self.calls: list[dict] = []

    def create(self, **params):
        self.calls.append(params)
        if self.first_error is not None and len(self.calls) == 1:
            raise self.first_error
        return _fake_response(self.reply)


def _fake_response(text: str):
    message = MagicMock()
    message.content = text
    choice = MagicMock()
    choice.message = message
    usage = MagicMock()
    usage.prompt_tokens = 321
    usage.completion_tokens = 45
    response = MagicMock()
    response.choices = [choice]
    response.usage = usage
    response.model = "gemma-4-31B-it"
    return response


class _FakeClient:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.chat = MagicMock()
        self.chat.completions = completions


class _SchemaRejected(Exception):
    """Looks like ``openai.BadRequestError``: carries ``status_code == 400``."""

    status_code = 400


class TestLlmJsonObjectFallback(unittest.TestCase):
    """``json_schema`` refused with a 400 → retried once as ``json_object``."""

    def setUp(self) -> None:
        try:
            from server import gc_usage, llm
        except ImportError:
            import gc_usage  # type: ignore
            import llm  # type: ignore

        self.llm = llm
        self.gc_usage = gc_usage
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        log_path = Path(self.tmp.name) / "gc_usage.log"
        for p in (
            patch.object(gc_usage, "_LOG_PATH", log_path),
            patch("urllib.request.urlopen", _blocked),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.log_path = log_path
        gc_usage.clear()

    def _fake(self, first_error, reply='{"ok": true}'):
        completions = _FakeCompletions(first_error, reply)
        p = patch.object(self.llm, "_client", lambda: _FakeClient(completions))
        p.start()
        self.addCleanup(p.stop)
        return completions

    def test_400_on_json_schema_retries_as_json_object_with_schema_in_system(self) -> None:
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
        completions = self._fake(_SchemaRejected("400 response_format not supported"))
        text, usage = self.llm.complete_with_usage("SYSTEM PROMPT", "USER", schema, stage="synthesize")

        self.assertEqual(text, '{"ok": true}')
        self.assertEqual(len(completions.calls), 2)
        first, second = completions.calls
        self.assertEqual(first["response_format"]["type"], "json_schema")
        self.assertEqual(first["response_format"]["json_schema"]["schema"], schema)
        self.assertEqual(second["response_format"], {"type": "json_object"})
        second_system = second["messages"][0]["content"]
        self.assertTrue(second_system.startswith("SYSTEM PROMPT"))
        self.assertIn("Return only a JSON object matching this schema:", second_system)
        self.assertIn(json.dumps(schema), second_system)
        self.assertEqual(second["messages"][1], {"role": "user", "content": "USER"})
        self.assertEqual(first["messages"][0]["content"], "SYSTEM PROMPT")

        self.assertEqual(usage["json_mode"], "json_object")
        self.assertEqual(usage["prompt_tokens"], 321)
        self.assertEqual(usage["completion_tokens"], 45)
        self.assertEqual(usage["model"], "gemma-4-31B-it")
        self.assertIsInstance(usage["latency_s"], float)

        recent = self.gc_usage.recent()
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["stage"], "synthesize")
        self.assertEqual(recent[0]["json_mode"], "json_object")
        logged = [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(logged[0]["stage"], "synthesize")
        self.assertEqual(logged[0]["completion_tokens"], 45)

    def test_message_naming_json_schema_also_falls_back(self) -> None:
        completions = self._fake(RuntimeError("unsupported json_schema in response_format"))
        text = self.llm.complete("S", "U", {"type": "object"})
        self.assertEqual(text, '{"ok": true}')
        self.assertEqual(len(completions.calls), 2)
        self.assertEqual(completions.calls[1]["response_format"], {"type": "json_object"})

    def test_other_errors_are_not_retried(self) -> None:
        completions = self._fake(RuntimeError("connection reset"))
        with self.assertRaises(RuntimeError):
            self.llm.complete("S", "U", {"type": "object"})
        self.assertEqual(len(completions.calls), 1)

    def test_text_mode_records_usage_without_response_format(self) -> None:
        completions = self._fake(None, reply="plain text")
        text, usage = self.llm.complete_with_usage("S", "U", stage="planner")
        self.assertEqual(text, "plain text")
        self.assertNotIn("response_format", completions.calls[0])
        self.assertEqual(usage["json_mode"], "text")
        self.assertEqual(self.gc_usage.recent()[-1]["stage"], "planner")


# --------------------------------------------------------------------------- #
# Session 8: the first attempt streams its why through on_why_delta.
# --------------------------------------------------------------------------- #


def _chunked(text: str, n: int) -> list[str]:
    return [text[i : i + n] for i in range(0, len(text), n)]


class TestSynthesizeStreamsWhy(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from server import synthesize as syn
        except ImportError:
            import synthesize as syn  # type: ignore

        self.syn = syn
        p = patch("urllib.request.urlopen", _blocked)
        p.start()
        self.addCleanup(p.stop)

    def _fake_stream(self, replies: list[str], width: int = 7):
        """``complete_stream`` stand-in: feeds ``on_delta`` in ``width``-char pieces."""
        calls: list[dict] = []

        def fake(system, user, json_schema=None, *, stage="", on_delta=None, **kw):
            text = replies[len(calls)]
            calls.append({"system": system, "user": user, "json_schema": json_schema, "stage": stage})
            for piece in _chunked(text, width):
                if on_delta is not None:
                    on_delta(piece)
            return text, {"json_mode": "json_schema", "ttfb_s": 0.1}

        fake.calls = calls  # type: ignore[attr-defined]
        return fake

    def test_why_arrives_in_pieces_then_matches_verified_result(self) -> None:
        stream = self._fake_stream([json.dumps(VALID_DECISION)], width=5)
        plain = MagicMock(side_effect=AssertionError("non-stream path must not run"))
        seen: list[str] = []
        with patch.object(self.syn, "_llm_complete_stream", stream), patch.object(self.syn, "_llm_complete", plain):
            result = self.syn.synthesize(agents=DECISION_AGENTS, userQuote=800, on_why_delta=seen.append)

        self.assertGreater(len(seen), 1)
        self.assertEqual("".join(seen), VALID_DECISION["why"])
        self.assertEqual(result["why"], VALID_DECISION["why"])
        self.assertEqual(result["options"][1]["total"], 480)
        self.assertEqual(len(stream.calls), 1)
        self.assertIs(stream.calls[0]["json_schema"], self.syn._DECISION_JSON_SCHEMA)
        self.assertEqual(stream.calls[0]["stage"], self.syn._LLM_STAGE)
        # ``why`` pieces never contain the JSON quoting around the value
        self.assertNotIn('"', "".join(seen))

    def test_without_callback_the_non_streaming_path_is_used(self) -> None:
        stream = MagicMock(side_effect=AssertionError("stream path must not run"))
        plain = MagicMock(return_value=json.dumps(VALID_DECISION))
        with patch.object(self.syn, "_llm_complete_stream", stream), patch.object(self.syn, "_llm_complete", plain):
            result = self.syn.synthesize(agents=DECISION_AGENTS, userQuote=800)
        self.assertEqual(plain.call_count, 1)
        self.assertEqual(result["why"], VALID_DECISION["why"])

    def test_verifier_retry_does_not_stream_and_final_why_replaces_streamed(self) -> None:
        stream = self._fake_stream([json.dumps(BAD_DECISION)])
        plain = MagicMock(return_value=json.dumps(VALID_DECISION))
        seen: list[str] = []
        with patch.object(self.syn, "_llm_complete_stream", stream), patch.object(self.syn, "_llm_complete", plain):
            result = self.syn.synthesize(agents=DECISION_AGENTS, userQuote=800, on_why_delta=seen.append)

        self.assertEqual(len(stream.calls), 1)  # only the first attempt streams
        self.assertEqual(plain.call_count, 1)  # the retry is the plain call
        self.assertIn("rejected", plain.call_args.args[1])
        self.assertEqual("".join(seen), BAD_DECISION["why"])  # streamed, then discarded
        self.assertEqual(result["why"], VALID_DECISION["why"])  # task.result wins
        self.assertNotIn("999", json.dumps(result))

    def test_callback_error_does_not_lose_the_decision(self) -> None:
        stream = self._fake_stream([json.dumps(VALID_DECISION)])

        def boom(_piece: str) -> None:
            raise RuntimeError("socket gone")

        with patch.object(self.syn, "_llm_complete_stream", stream), patch.object(self.syn, "_llm_complete", MagicMock()):
            result = self.syn.synthesize(agents=DECISION_AGENTS, userQuote=800, on_why_delta=boom)
        self.assertEqual(result["why"], VALID_DECISION["why"])

    def test_stream_error_falls_back_to_deterministic(self) -> None:
        stream = MagicMock(side_effect=RuntimeError("stream reset"))
        plain = MagicMock(side_effect=AssertionError("no retry after a transport error"))
        seen: list[str] = []
        with patch.object(self.syn, "_llm_complete_stream", stream), patch.object(self.syn, "_llm_complete", plain):
            result = self.syn.synthesize(agents=DECISION_AGENTS, userQuote=800, on_why_delta=seen.append)
        self.assertEqual(seen, [])
        self.assertEqual(len(result["options"]), 2)
        self.assertEqual(result["options"][1]["total"], 480)

    def test_model_disabled_ignores_callback(self) -> None:
        seen: list[str] = []
        with patch.object(self.syn, "_llm_complete", None):
            result = self.syn.synthesize(agents=DECISION_AGENTS, userQuote=800, on_why_delta=seen.append)
        self.assertEqual(seen, [])
        self.assertTrue(result["why"])


if __name__ == "__main__":
    unittest.main()
