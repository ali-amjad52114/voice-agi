"""Extraction: mocked model, shared fixture, spoken-money guard. No network."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_SERVER = Path(__file__).resolve().parents[1]
_REPO = Path(__file__).resolve().parents[2]
for _p in (str(_REPO), str(_SERVER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from server import extract as ext
    from server.models import Facts, TranscriptLine
except ImportError:  # pragma: no cover
    import extract as ext  # type: ignore
    from models import Facts, TranscriptLine  # type: ignore

# The first call that worked (Fremont Auto Tech, 81 s). Shared across sessions.
FIXTURE = [
    {"role": "agent", "t": 3, "text": "I'm an assistant calling for a customer with a 2019 Camry for front pads and rotors. What is the all-in installed price?"},
    {"role": "business", "t": 15, "text": "Well, $450."},
    {"role": "agent", "t": 16, "text": "Got it. What is your hourly labor rate?"},
    {"role": "business", "t": 28, "text": "$150. $150."},
    {"role": "agent", "t": 29, "text": "Okay, thanks. Will you install parts that the customer brings in?"},
    {"role": "business", "t": 39, "text": "Yeah, I can do for $150."},
    {"role": "agent", "t": 40, "text": "Got it. Do you use OEM or aftermarket parts?"},
    {"role": "business", "t": 48, "text": "We can use either depending on"},
    {"role": "agent", "t": 50, "text": "Okay. How long is the warranty on the work?"},
    {"role": "business", "t": 56, "text": "one month."},
    {"role": "agent", "t": 57, "text": "Got it. When is the earliest appointment you have available?"},
    {"role": "business", "t": 64, "text": "Tomorrow."},
    {"role": "agent", "t": 66, "text": "Thanks for your help, goodbye."},
]

# What the model should say for the fixture (from the plan's "Should be" column).
GOOD_REPLY = {
    "allInPrice": 450,
    "partPrice": None,
    "laborRatePerHour": 150,
    "laborHours": None,
    "acceptsCustomerParts": True,
    "partsType": None,
    "warrantyMonths": 1,
    "earliestSlot": "Tomorrow",
    "confidence": 0.85,
}

SPOKEN_WORDS = [
    {"role": "agent", "t": 1, "text": "What is the all-in installed price for front pads and rotors?"},
    {"role": "business", "t": 5, "text": "You're looking at six ten out the door."},
    {"role": "agent", "t": 6, "text": "And your price for the pads and rotors alone, if you supply them?"},
    {"role": "business", "t": 10, "text": "Parts would run about two forty."},
    {"role": "agent", "t": 11, "text": "What is your hourly labor rate?"},
    {"role": "business", "t": 14, "text": "One twenty an hour."},
    {"role": "agent", "t": 15, "text": "Roughly how many hours is the job?"},
    {"role": "business", "t": 18, "text": "About two and a half."},
    {"role": "agent", "t": 19, "text": "Will you install a part the customer brings in?"},
    {"role": "business", "t": 22, "text": "No, we only use our parts."},
    {"role": "agent", "t": 23, "text": "How long is the warranty?"},
    {"role": "business", "t": 25, "text": "A year."},
]

_KEY_ENV = {"GENERAL_COMPUTE_API_KEY": "test-key-not-real"}


def _mock_llm(reply):
    """Patch ``extract._llm_complete`` with a stub returning canned JSON."""
    calls: list[dict] = []

    def fake_complete(system, user, json_schema=None, **kwargs):
        calls.append({"system": system, "user": user, "json_schema": json_schema})
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply)

    fake_complete.calls = calls  # type: ignore[attr-defined]
    return patch.object(ext, "_llm_complete", fake_complete)


class TestSharedFixture(unittest.TestCase):
    def test_good_reply_keeps_every_price_and_infers_customer_parts(self) -> None:
        with patch.dict(os.environ, _KEY_ENV), _mock_llm(GOOD_REPLY) as fake:
            facts = ext.extract_facts(FIXTURE)
        self.assertIsInstance(facts, Facts)
        self.assertTrue(facts.acceptsCustomerParts)
        self.assertEqual(facts.allInPrice, 450)
        self.assertEqual(facts.laborRatePerHour, 150)
        self.assertIsNone(facts.partPrice)
        self.assertIsNone(facts.laborHours)
        self.assertIsNone(facts.partsType)
        self.assertEqual(facts.warrantyMonths, 1)
        self.assertEqual(facts.earliestSlot, "Tomorrow")
        self.assertAlmostEqual(facts.confidence, 0.85)
        self.assertEqual(len(fake.calls), 1)

    def test_unspoken_all_in_price_is_rejected(self) -> None:
        reply = dict(GOOD_REPLY, allInPrice=999)
        with patch.dict(os.environ, _KEY_ENV), _mock_llm(reply):
            facts = ext.extract_facts(FIXTURE)
        self.assertIsNone(facts.allInPrice, "999 was never spoken on this call")
        self.assertEqual(facts.laborRatePerHour, 150, "spoken prices survive")
        self.assertTrue(facts.acceptsCustomerParts)
        self.assertLess(facts.confidence, 0.85)
        self.assertAlmostEqual(facts.confidence, 0.85 - ext._PRICE_REJECT_PENALTY)

    def test_two_unspoken_prices_lower_confidence_twice(self) -> None:
        reply = dict(GOOD_REPLY, allInPrice=999, partPrice=75)
        with patch.dict(os.environ, _KEY_ENV), _mock_llm(reply):
            facts = ext.extract_facts(FIXTURE)
        self.assertIsNone(facts.allInPrice)
        self.assertIsNone(facts.partPrice)
        self.assertAlmostEqual(facts.confidence, 0.85 - 2 * ext._PRICE_REJECT_PENALTY)

    def test_transcript_line_models_work_too(self) -> None:
        lines = [TranscriptLine.model_validate(l) for l in FIXTURE]
        with patch.dict(os.environ, _KEY_ENV), _mock_llm(GOOD_REPLY):
            facts = ext.extract_facts(lines)
        self.assertEqual(facts.allInPrice, 450)
        self.assertTrue(facts.acceptsCustomerParts)


class TestOffline(unittest.TestCase):
    def test_no_key_returns_confidence_zero_and_no_prices(self) -> None:
        env = dict(os.environ)
        env.pop("GENERAL_COMPUTE_API_KEY", None)
        env.pop("GENERALCOMPUTE_API_KEY", None)
        with patch.dict(os.environ, env, clear=True), _mock_llm(GOOD_REPLY) as fake:
            facts = ext.extract_facts(FIXTURE)
        self.assertEqual(facts.confidence, 0.0)
        self.assertIsNone(facts.allInPrice)
        self.assertIsNone(facts.laborRatePerHour)
        self.assertIsNone(facts.partPrice)
        self.assertEqual(fake.calls, [], "offline must not call the model")

    def test_llm_missing_returns_offline_facts(self) -> None:
        with patch.dict(os.environ, _KEY_ENV), patch.object(ext, "_llm_complete", None):
            facts = ext.extract_facts(FIXTURE)
        self.assertEqual(facts.confidence, 0.0)
        self.assertIsNone(facts.allInPrice)

    def test_llm_failure_returns_offline_facts(self) -> None:
        with patch.dict(os.environ, _KEY_ENV), _mock_llm(RuntimeError("boom")):
            facts = ext.extract_facts(FIXTURE)
        self.assertEqual(facts.confidence, 0.0)
        self.assertIsNone(facts.allInPrice)

    def test_garbage_reply_returns_offline_facts(self) -> None:
        with patch.dict(os.environ, _KEY_ENV), _mock_llm("sorry, no json here"):
            facts = ext.extract_facts(FIXTURE)
        self.assertEqual(facts.confidence, 0.0)
        self.assertIsNone(facts.allInPrice)


class TestSpokenMoneyGuard(unittest.TestCase):
    def test_prices_spoken_as_words_are_kept(self) -> None:
        reply = {
            "allInPrice": 610,
            "partPrice": 240,
            "laborRatePerHour": 120,
            "laborHours": 2.5,
            "acceptsCustomerParts": False,
            "partsType": None,
            "warrantyMonths": 12,
            "earliestSlot": None,
            "confidence": 0.9,
        }
        with patch.dict(os.environ, _KEY_ENV), _mock_llm(reply):
            facts = ext.extract_facts(SPOKEN_WORDS)
        self.assertEqual(facts.allInPrice, 610)
        self.assertEqual(facts.partPrice, 240)
        self.assertEqual(facts.laborRatePerHour, 120)
        self.assertEqual(facts.laborHours, 2.5)
        self.assertIs(facts.acceptsCustomerParts, False)
        self.assertEqual(facts.warrantyMonths, 12)
        self.assertAlmostEqual(facts.confidence, 0.9)

    def test_number_only_in_agent_line_does_not_count(self) -> None:
        transcript = [
            {"role": "agent", "t": 1, "text": "The customer was quoted $800 elsewhere. What is your all-in price?"},
            {"role": "business", "t": 5, "text": "We don't give quotes over the phone."},
        ]
        reply = {"allInPrice": 800, "confidence": 0.5}
        with patch.dict(os.environ, _KEY_ENV), _mock_llm(reply):
            facts = ext.extract_facts(transcript)
        self.assertIsNone(facts.allInPrice)
        self.assertAlmostEqual(facts.confidence, 0.5 - ext._PRICE_REJECT_PENALTY)

    def test_plain_string_transcript_uses_all_text(self) -> None:
        reply = {"allInPrice": 450, "confidence": 0.7}
        with patch.dict(os.environ, _KEY_ENV), _mock_llm(reply):
            facts = ext.extract_facts(["all-in?", "four fifty"])
        self.assertEqual(facts.allInPrice, 450)

    def test_spoken_amounts_parser(self) -> None:
        cases = {
            "four fifty": 450,
            "six ten out the door": 610,
            "one twenty an hour": 120,
            "a hundred": 100,
            "hundred and fifty": 150,
            "one hundred and fifty": 150,
            "twelve hundred": 1200,
            "$450": 450,
            "450": 450,
            "1,200 bucks": 1200,
            "$150. $150.": 150,
            "two forty": 240,
            "twenty five": 25,
            "ninety-nine": 99,
            "a thousand": 1000,
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertIn(float(want), ext._spoken_amounts(text))

    def test_parser_splits_on_punctuation_and_glued_stt(self) -> None:
        punctuated = ext._spoken_amounts("Parts would run about two forty.\nOne twenty an hour.")
        self.assertIn(240.0, punctuated)
        self.assertIn(120.0, punctuated)
        glued = ext._spoken_amounts("parts would run about two forty one twenty an hour")
        self.assertIn(240.0, glued)
        self.assertIn(120.0, glued)

    def test_parser_does_not_manufacture_numbers(self) -> None:
        self.assertNotIn(54.0, ext._spoken_amounts("four fifty"))
        self.assertNotIn(2500.0, ext._spoken_amounts("twenty five"))
        self.assertEqual(ext._spoken_amounts("Tomorrow."), set())
        self.assertEqual(ext._spoken_amounts("a year"), set())


class TestNullHandling(unittest.TestCase):
    def test_nulls_zero_and_negatives_become_none(self) -> None:
        reply = {
            "allInPrice": None,
            "partPrice": 0,
            "laborRatePerHour": -5,
            "laborHours": 0,
            "acceptsCustomerParts": None,
            "partsType": "either",
            "warrantyMonths": None,
            "earliestSlot": "",
            "confidence": 0.4,
        }
        with patch.dict(os.environ, _KEY_ENV), _mock_llm(reply):
            facts = ext.extract_facts(FIXTURE)
        self.assertIsNone(facts.allInPrice)
        self.assertIsNone(facts.partPrice)
        self.assertIsNone(facts.laborRatePerHour)
        self.assertIsNone(facts.laborHours)
        self.assertIsNone(facts.acceptsCustomerParts)
        self.assertIsNone(facts.partsType)
        self.assertIsNone(facts.warrantyMonths)
        self.assertIsNone(facts.earliestSlot)
        self.assertAlmostEqual(facts.confidence, 0.4)

    def test_confidence_is_clamped(self) -> None:
        with patch.dict(os.environ, _KEY_ENV), _mock_llm({"confidence": 7}):
            self.assertEqual(ext.extract_facts(FIXTURE).confidence, 1.0)
        with patch.dict(os.environ, _KEY_ENV), _mock_llm({"confidence": "nope"}):
            self.assertEqual(ext.extract_facts(FIXTURE).confidence, 0.0)


class TestSchemaPassThrough(unittest.TestCase):
    PLANNER_SCHEMA = {
        "type": "object",
        "required": ["allInPrice"],
        "properties": {
            "allInPrice": {"type": ["number", "null"]},
            "shopNotes": {"type": ["string", "null"]},
        },
    }

    def test_planner_schema_is_merged_into_request(self) -> None:
        with patch.dict(os.environ, _KEY_ENV), _mock_llm(GOOD_REPLY) as fake:
            ext.extract_facts(FIXTURE, schema=self.PLANNER_SCHEMA)
        sent = fake.calls[0]["json_schema"]
        self.assertIn("shopNotes", sent["properties"])
        self.assertIn("laborRatePerHour", sent["properties"])
        self.assertIn("allInPrice", sent["required"])
        self.assertIn("confidence", sent["required"])

    def test_no_schema_sends_default(self) -> None:
        with patch.dict(os.environ, _KEY_ENV), _mock_llm(GOOD_REPLY) as fake:
            ext.extract_facts(FIXTURE)
        sent = fake.calls[0]["json_schema"]
        self.assertEqual(sent["required"], ["confidence"])
        self.assertNotIn("shopNotes", sent["properties"])

    def test_orchestrator_passes_remembered_schema(self) -> None:
        """The extraction loop hands the planner schema to extract_facts."""
        try:
            from server import orchestrator as orch
            from server.models import Agent, Business, Task
        except ImportError:  # pragma: no cover
            self.skipTest("orchestrator not importable as a package")

        agent = Agent(
            id="a_call_x",
            taskId="task_s2",
            kind="call",
            status="done",
            business=Business(name="Fremont Auto Tech", type="mechanic", phone="+10000000000"),
            transcript=[TranscriptLine.model_validate(l) for l in FIXTURE],
        )
        task = Task(
            id="task_s2",
            title="Brake repair",
            request="front brakes on my 2019 Camry",
            createdAt="2026-09-19T00:00:00Z",
            status="running",
            agents=[agent],
        )
        seen: list[dict] = []

        def fake_extract(transcript, schema=None):
            seen.append({"n": len(transcript), "schema": schema})
            return Facts(confidence=0.0)

        def no_synth(_task):
            raise RuntimeError("synthesize disabled in this test")

        orch._EXTRACTION_SCHEMAS["task_s2"] = self.PLANNER_SCHEMA
        with (
            patch.object(orch, "_db", None),
            patch.object(orch, "_load_task", lambda _tid: task),
            patch.object(orch, "_persist_agent", lambda _a: None),
            patch.object(orch, "_persist_task_fields", lambda _t: None),
            patch("server.dialer.run_dialer", lambda _tid, _agents: []),
            patch("server.synthesize.synthesize", no_synth),
            patch("server.extract.extract_facts", fake_extract),
        ):
            orch._run_calls_then_complete(task, None)

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["n"], len(FIXTURE))
        self.assertIs(seen[0]["schema"], self.PLANNER_SCHEMA)
        self.assertNotIn("task_s2", orch._EXTRACTION_SCHEMAS, "schema is released after use")
        self.assertEqual(agent.facts.confidence, 0.0)

    def test_load_task_keeps_richer_in_memory_transcript(self) -> None:
        """Session 6: a DB row shorter than ``api._memory`` must not lose lines."""
        try:
            from server import api as api_mod
            from server import orchestrator as orch
            from server.models import Agent, Business, Task
        except ImportError:  # pragma: no cover
            self.skipTest("orchestrator not importable as a package")

        def make(lines):
            return Task(
                id="task_s6",
                title="Brake repair",
                request="front brakes on my 2019 Camry",
                createdAt="2026-09-19T00:00:00Z",
                status="running",
                agents=[
                    Agent(
                        id="a_call_y",
                        taskId="task_s6",
                        kind="call",
                        status="active",
                        business=Business(name="Sam's Auto", type="mechanic", phone="+10000000000"),
                        transcript=[TranscriptLine.model_validate(l) for l in lines],
                    )
                ],
            )

        db_task = make(FIXTURE[:2])  # DB has the first two lines only
        memory_task = make(FIXTURE)  # memory has the full call
        fake_db = MagicMock()
        fake_db.get_task = lambda _tid: db_task
        with (
            patch.object(orch, "_db", fake_db),
            patch.dict(api_mod._memory, {"task_s6": memory_task}, clear=False),
        ):
            loaded = orch._load_task("task_s6")
        self.assertEqual(len(loaded.agents[0].transcript), len(FIXTURE))

        # A DB row that is already complete is left alone.
        full_db = make(FIXTURE)
        fake_db.get_task = lambda _tid: full_db
        with (
            patch.object(orch, "_db", fake_db),
            patch.dict(api_mod._memory, {"task_s6": make(FIXTURE[:1])}, clear=False),
        ):
            loaded = orch._load_task("task_s6")
        self.assertEqual(len(loaded.agents[0].transcript), len(FIXTURE))


if __name__ == "__main__":
    unittest.main()
