"""Call tools — schemas, transcript support check, note/end state. No network, no Pipecat."""

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
    from server import call_tools as ct
except ImportError:
    import call_tools as ct  # type: ignore

FIELDS = (
    "allInPrice",
    "partPrice",
    "laborRatePerHour",
    "laborHours",
    "acceptsCustomerParts",
    "partsType",
    "warrantyMonths",
    "earliestSlot",
)


class TestSchemas(unittest.TestCase):
    def test_no_pipecat_import(self) -> None:
        self.assertNotIn("pipecat", sys.modules)

    def test_fact_fields(self) -> None:
        self.assertEqual(ct.FACT_FIELDS, FIELDS)

    def test_note_fact_schema(self) -> None:
        schema = ct.note_fact_schema()
        self.assertEqual(schema["name"], "note_fact")
        self.assertTrue(schema["description"])
        params = schema["parameters"]
        self.assertEqual(params["type"], "object")
        self.assertEqual(params["required"], ["field", "value"])
        self.assertEqual(params["properties"]["field"]["enum"], list(FIELDS))
        self.assertEqual(
            set(params["properties"]["value"]["type"]), {"string", "number", "boolean"}
        )

    def test_end_call_schema(self) -> None:
        schema = ct.end_call_schema()
        self.assertEqual(schema["name"], "end_call")
        params = schema["parameters"]
        self.assertEqual(params["required"], ["reason"])
        self.assertEqual(
            params["properties"]["reason"]["enum"], ["all_facts", "refused", "voicemail", "other"]
        )


class TestValueInTranscript(unittest.TestCase):
    def test_spoken_hundreds(self) -> None:
        self.assertTrue(ct.value_in_transcript(420, ["it's four hundred twenty all in"]))

    def test_dollar_digits(self) -> None:
        self.assertTrue(ct.value_in_transcript("420", ["that'd be $420 installed"]))
        self.assertTrue(ct.value_in_transcript("$420", ["four hundred and twenty dollars"]))
        self.assertTrue(ct.value_in_transcript(420.0, ["420.00 out the door"]))

    def test_spoken_shorthand(self) -> None:
        self.assertTrue(ct.value_in_transcript(420, ["four twenty"]))
        self.assertTrue(ct.value_in_transcript(125, ["one twenty five an hour"]))
        self.assertTrue(ct.value_in_transcript(1200, ["twelve hundred"]))

    def test_rejects_number_not_said(self) -> None:
        self.assertFalse(ct.value_in_transcript(999, ["it's 420 all in"]))
        self.assertFalse(ct.value_in_transcript(420, ["it's four hundred all in"]))
        self.assertFalse(ct.value_in_transcript(420, []))

    def test_decimals_and_fractions(self) -> None:
        self.assertTrue(ct.value_in_transcript(2.5, ["about two and a half hours"]))
        self.assertTrue(ct.value_in_transcript(1.5, ["one point five hours"]))
        self.assertTrue(ct.value_in_transcript(0.5, ["half an hour"]))
        self.assertTrue(ct.value_in_transcript(1.5, ["an hour and a half"]))

    def test_thousands(self) -> None:
        self.assertTrue(ct.value_in_transcript(2300, ["two thousand three hundred"]))
        self.assertTrue(ct.value_in_transcript(1200, ["1,200 with tax"]))

    def test_warranty_years(self) -> None:
        self.assertTrue(ct.value_in_transcript(12, ["one year on parts and labor"], field="warrantyMonths"))
        self.assertTrue(ct.value_in_transcript(24, ["two years"], field="warrantyMonths"))
        self.assertTrue(ct.value_in_transcript(12, ["a year"], field="warrantyMonths"))
        self.assertFalse(ct.value_in_transcript(36, ["two years"], field="warrantyMonths"))

    def test_booleans(self) -> None:
        self.assertTrue(ct.value_in_transcript(True, ["yeah sure, bring it in"], field="acceptsCustomerParts"))
        self.assertFalse(ct.value_in_transcript(False, ["yeah sure"], field="acceptsCustomerParts"))
        self.assertTrue(ct.value_in_transcript(False, ["no, we don't install customer parts"], field="acceptsCustomerParts"))

    def test_parts_type(self) -> None:
        self.assertTrue(ct.value_in_transcript("oem", ["we use factory parts"]))
        self.assertTrue(ct.value_in_transcript("aftermarket", ["aftermarket, usually"]))
        self.assertFalse(ct.value_in_transcript("aftermarket", ["we use factory parts"]))

    def test_earliest_slot_free_text(self) -> None:
        self.assertTrue(ct.value_in_transcript("Tuesday morning", ["we could do tuesday morning"], field="earliestSlot"))
        self.assertTrue(ct.value_in_transcript("next week", ["sometime next week probably"], field="earliestSlot"))

    def test_only_recent_lines_count(self) -> None:
        state = ct.CallToolState()
        state.record_business_line("four twenty all in")
        for i in range(ct.BUSINESS_LINE_WINDOW):
            state.record_business_line(f"filler line {i}")
        self.assertEqual(len(state.business_lines), ct.BUSINESS_LINE_WINDOW)
        self.assertFalse(state.apply_note("allInPrice", 420)["ok"])


class TestCallToolState(unittest.TestCase):
    def test_note_rejected_when_not_in_recent_lines(self) -> None:
        state = ct.CallToolState()
        state.record_business_line("it's four hundred twenty all in")
        result = state.apply_note("allInPrice", 999)
        self.assertFalse(result["ok"])
        self.assertIn("reason", result)
        self.assertEqual(state.noted, {})

    def test_note_accepted_when_said(self) -> None:
        state = ct.CallToolState()
        state.record_business_line("it's four hundred twenty all in")
        result = state.apply_note("allInPrice", "$420")
        self.assertTrue(result["ok"])
        self.assertEqual(state.noted, {"allInPrice": 420.0})

    def test_unknown_field_rejected(self) -> None:
        state = ct.CallToolState()
        state.record_business_line("420")
        self.assertFalse(state.apply_note("totalPrice", 420)["ok"])
        self.assertFalse(state.apply_note("allInPrice", "lots")["ok"])
        self.assertEqual(state.noted, {})

    def test_coercion(self) -> None:
        state = ct.CallToolState()
        state.record_business_line("yes, factory parts, one year, thursday afternoon")
        self.assertTrue(state.apply_note("acceptsCustomerParts", "yes")["ok"])
        self.assertIs(state.noted["acceptsCustomerParts"], True)
        self.assertTrue(state.apply_note("partsType", "OEM")["ok"])
        self.assertEqual(state.noted["partsType"], "oem")
        self.assertTrue(state.apply_note("warrantyMonths", "12")["ok"])
        self.assertEqual(state.noted["warrantyMonths"], 12)
        self.assertTrue(state.apply_note("earliestSlot", "Thursday afternoon")["ok"])
        self.assertFalse(state.apply_note("partsType", "used")["ok"])

    def test_should_end_after_eight(self) -> None:
        state = ct.CallToolState()
        lines = {
            "allInPrice": ("four hundred twenty all in", 420),
            "partPrice": ("the pads and rotors are one eighty", 180),
            "laborRatePerHour": ("one twenty an hour", 120),
            "laborHours": ("about two hours", 2),
            "acceptsCustomerParts": ("sure, we can do that", True),
            "partsType": ("aftermarket", "aftermarket"),
            "warrantyMonths": ("twelve months", 12),
            "earliestSlot": ("tomorrow morning", "tomorrow morning"),
        }
        for field, (line, value) in lines.items():
            self.assertFalse(state.should_end())
            state.record_business_line(line)
            self.assertTrue(state.apply_note(field, value)["ok"], field)
        self.assertTrue(state.should_end())
        self.assertEqual(state.remaining(), [])

    def test_apply_end(self) -> None:
        state = ct.CallToolState()
        self.assertFalse(state.ended)
        result = state.apply_end("all_facts")
        self.assertTrue(result["ok"])
        self.assertTrue(state.ended)
        self.assertEqual(state.end_reason, "all_facts")
        self.assertEqual(ct.CallToolState().apply_end("bogus")["reason"], "other")


if __name__ == "__main__":
    unittest.main()
