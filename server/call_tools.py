"""Call-brain tools: ``note_fact`` and ``end_call``.

Pure Python, no Pipecat import. ``call_audio.py`` turns the schemas below into
Pipecat ``FunctionSchema`` objects and registers the handlers; the state class
here decides whether a note is accepted. A note is accepted only when the value
appears in the shop's last few transcript lines, so the model cannot write down
a dollar the shop did not say. Extraction at hangup stays canonical; the notes
are for the model's own bookkeeping and for deciding when to end the call.
"""

from __future__ import annotations

import re
from typing import Any

FACT_FIELDS: tuple[str, ...] = (
    "allInPrice",
    "partPrice",
    "laborRatePerHour",
    "laborHours",
    "acceptsCustomerParts",
    "partsType",
    "warrantyMonths",
    "earliestSlot",
)

NUMBER_FIELDS: frozenset[str] = frozenset(
    {"allInPrice", "partPrice", "laborRatePerHour", "laborHours", "warrantyMonths"}
)

END_REASONS: tuple[str, ...] = ("all_facts", "refused", "voicemail", "other")

# How many recent business lines a note is checked against.
BUSINESS_LINE_WINDOW = 3


def note_fact_schema() -> dict[str, Any]:
    """OpenAI function-tool shape for ``note_fact(field, value)``."""
    return {
        "name": "note_fact",
        "description": (
            "Record one fact the shop just stated, using exactly the number or word "
            "the shop said. Call it right after the shop answers a question. Never "
            "call it with a number the shop did not say."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "field": {
                    "type": "string",
                    "enum": list(FACT_FIELDS),
                    "description": (
                        "allInPrice: installed price in dollars. partPrice: the shop's "
                        "price for the part alone. laborRatePerHour: dollars per hour. "
                        "laborHours: hours for the job. acceptsCustomerParts: true or "
                        "false. partsType: oem or aftermarket. warrantyMonths: months. "
                        "earliestSlot: the earliest appointment in the shop's words."
                    ),
                },
                "value": {
                    "type": ["string", "number", "boolean"],
                    "description": "The value exactly as the shop said it.",
                },
            },
            "required": ["field", "value"],
        },
    }


def end_call_schema() -> dict[str, Any]:
    """OpenAI function-tool shape for ``end_call(reason)``."""
    return {
        "name": "end_call",
        "description": (
            "Hang up. Call it in the same turn as your one-sentence goodbye once all "
            "eight facts are noted, the shop refuses to quote, or you reached voicemail."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "enum": list(END_REASONS)},
            },
            "required": ["reason"],
        },
    }


# ---------------------------------------------------------------------------
# Spoken numbers
# ---------------------------------------------------------------------------

_UNITS: dict[str, int] = {
    "zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS: dict[str, int] = {
    "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_SCALES: dict[str, int] = {"hundred": 100, "thousand": 1000}
_FRACTIONS: dict[str, float] = {"half": 0.5, "quarter": 0.25}
_DIGIT_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_WORD_RE = re.compile(r"[a-z]+|\d+(?:\.\d+)?")
# Clause breaks: "one eighty, one twenty" must not read as one number run.
_CLAUSE_RE = re.compile(r"[,;:!?\n]|\.(?!\d)")


def _is_number_word(tok: str) -> bool:
    return tok in _UNITS or tok in _TENS or tok in _SCALES or tok.isdigit()


def _parse_run(tokens: list[str]) -> list[float]:
    """Interpret one run of number words.

    Returns every reading we accept: the standard one ("four hundred twenty"
    = 420, "twenty four" = 24, "twelve hundred" = 1200) and, when a unit is
    directly followed by a tens or teen word, the spoken shorthand with the
    hundred left out ("four twenty" = 420, "one twenty five" = 125).
    """
    if not tokens:
        return []
    out: set[float] = set()

    def standard(toks: list[str]) -> float | None:
        total = 0.0
        current = 0.0
        seen = False
        for tok in toks:
            if tok.isdigit():
                current += int(tok)
                seen = True
            elif tok in _UNITS or tok in _TENS:
                current += _UNITS.get(tok, _TENS.get(tok, 0))
                seen = True
            elif tok == "hundred":
                current = (current or 1) * 100
                seen = True
            elif tok == "thousand":
                total += (current or 1) * 1000
                current = 0.0
                seen = True
        return total + current if seen else None

    std = standard(tokens)
    if std is not None:
        out.add(std)
    # Shorthand: leading unit (1-9) then tens/teen words, no scale word.
    if (
        len(tokens) >= 2
        and tokens[0] in _UNITS
        and 1 <= _UNITS[tokens[0]] <= 9
        and (tokens[1] in _TENS or (tokens[1] in _UNITS and _UNITS[tokens[1]] >= 10))
        and not any(t in _SCALES for t in tokens)
    ):
        rest = standard(tokens[1:])
        if rest is not None:
            out.add(_UNITS[tokens[0]] * 100 + rest)
    return sorted(out)


def spoken_numbers(text: str) -> list[float]:
    """Every number readable from ``text``: digits and spoken number words."""
    lowered = text.casefold()
    found: set[float] = set()
    for m in _DIGIT_RE.finditer(lowered):
        try:
            found.add(float(m.group(0).replace(",", "")))
        except ValueError:
            continue
    for clause in _CLAUSE_RE.split(lowered):
        found.update(_spoken_numbers_in_clause(clause))
    return sorted(found)


def _spoken_numbers_in_clause(lowered: str) -> set[float]:
    """Number words inside one clause (no punctuation or line break inside)."""
    found: set[float] = set()
    words = _WORD_RE.findall(lowered)
    i = 0
    n = len(words)
    while i < n:
        if not _is_number_word(words[i]) or words[i].isdigit() and "." in words[i]:
            i += 1
            continue
        run: list[str] = []
        j = i
        while j < n:
            tok = words[j]
            if _is_number_word(tok) and not tok.isdigit():
                run.append(tok)
                j += 1
            elif tok == "and" and j + 1 < n and (words[j + 1] in _UNITS or words[j + 1] in _TENS) and run and run[-1] in _SCALES:
                # "four hundred and twenty"
                j += 1
            elif tok in ("a", "an") and j + 1 < n and words[j + 1] in _SCALES:
                run.append("one")
                j += 1
            else:
                break
        if not run and words[i] in ("a", "an") and i + 1 < n and words[i + 1] in _SCALES:
            run = ["one"]
            j = i + 1
        bases = _parse_run(run) if run else []
        # Decimals: "four point five", "one point twenty five" (not needed).
        k = j
        extra = 0.0
        if k < n and words[k] == "point":
            digits = []
            k += 1
            while k < n and (words[k] in _UNITS and _UNITS[words[k]] <= 9 or words[k].isdigit()):
                digits.append(str(_UNITS.get(words[k], words[k])))
                k += 1
            if digits:
                extra = float("0." + "".join(digits))
                j = k
        elif k + 2 < n and words[k] == "and" and words[k + 1] in ("a", "one") and words[k + 2] in _FRACTIONS:
            # "two and a half"
            extra = _FRACTIONS[words[k + 2]]
            j = k + 3
        for base in bases:
            found.add(base + extra)
        if not bases and extra:
            found.add(extra)
        i = max(j, i + 1)
    # "half an hour", "a half hour", "an hour and a half" on their own.
    if re.search(r"\b(?:half an? hour|an? half hour)\b", lowered):
        found.add(0.5)
    m = re.search(r"\ban hour and an? (half|quarter)\b", lowered)
    if m:
        found.add(1 + _FRACTIONS[m.group(1)])
    return found


# ---------------------------------------------------------------------------
# Transcript support check
# ---------------------------------------------------------------------------

_TRUE_WORDS = ("yes", "yeah", "yep", "sure", "absolutely", "of course", "certainly",
               "we do", "we can", "we will", "no problem", "that's fine", "thats fine")
_FALSE_WORDS = ("no", "nope", "don't", "do not", "won't", "will not", "can't",
                "cannot", "never", "not ")
_KEYWORDS: dict[str, tuple[str, ...]] = {
    "oem": ("oem", "o.e.m", "original", "factory", "genuine", "dealer"),
    "aftermarket": ("aftermarket", "after market", "after-market", "third party", "generic"),
}
_SLOT_WORDS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
               "sunday", "tomorrow", "today", "morning", "afternoon", "evening",
               "noon", "week", "am", "pm", "o'clock", "oclock")


def _coerce_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.strip().lstrip("$").replace(",", "").replace("$", "").strip()
        cleaned = re.sub(r"\s*(dollars?|bucks|hours?|hrs?|months?|mo)\s*$", "", cleaned)
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().casefold()
        if low in ("true", "yes", "y", "yeah", "yep", "sure"):
            return True
        if low in ("false", "no", "n", "nope"):
            return False
    return None


def _near(a: float, b: float) -> bool:
    return abs(a - b) < 0.005


def value_in_transcript(value: Any, lines: list[str], field: str | None = None) -> bool:
    """True if ``value`` is supported by the shop's recent ``lines``.

    Numbers must appear as digits or spoken number words; when in doubt the
    answer is False. Booleans and part types need one of their keywords.
    Free text (``earliestSlot``) is accepted; it is not a dollar.
    """
    # Lines are joined with a break so number words never run together across
    # utterances; keyword checks use the joined text.
    text = "\n".join(str(line) for line in lines if line).casefold()
    if not text.strip():
        return False

    number = _coerce_number(value)
    if number is not None and field != "earliestSlot":
        heard = spoken_numbers(text)
        if any(_near(number, h) for h in heard):
            return True
        # "one year" / "two years" for a warranty given in months.
        if "year" in text and number > 0 and _near(number % 12, 0):
            years = number / 12
            if any(_near(years, h) for h in heard) or (_near(years, 1) and re.search(r"\ba year\b", text)):
                return True
        # "half" / "an hour and a half" style hours.
        return False

    flag = _coerce_bool(value)
    if flag is not None and field in (None, "acceptsCustomerParts"):
        words = _TRUE_WORDS if flag else _FALSE_WORDS
        return any(w in text for w in words)

    if isinstance(value, str):
        low = value.strip().casefold()
        if low in _KEYWORDS:
            return any(k in text for k in _KEYWORDS[low])
        if field == "earliestSlot" or field is None:
            # Free text: accept when any slot keyword or any word of the value
            # is present; otherwise still accept (it cannot invent a dollar).
            if any(w in text for w in _SLOT_WORDS):
                return True
            return True
    return False


class CallToolState:
    """Per-call tool state: what was noted, the last business lines, end flag."""

    def __init__(self) -> None:
        self.noted: dict[str, Any] = {}
        self.business_lines: list[str] = []
        self.ended: bool = False
        self.end_reason: str | None = None

    def record_business_line(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        self.business_lines.append(text)
        del self.business_lines[:-BUSINESS_LINE_WINDOW]

    def remaining(self) -> list[str]:
        return [f for f in FACT_FIELDS if f not in self.noted]

    def _coerce(self, field: str, value: Any) -> tuple[Any, str | None]:
        if field in NUMBER_FIELDS:
            number = _coerce_number(value)
            if number is None:
                return None, f"{field} needs a number"
            if field == "warrantyMonths":
                return int(round(number)), None
            return number, None
        if field == "acceptsCustomerParts":
            flag = _coerce_bool(value)
            if flag is None:
                return None, "acceptsCustomerParts needs true or false"
            return flag, None
        if field == "partsType":
            low = str(value).strip().casefold()
            if low in ("oem", "original", "factory", "genuine"):
                return "oem", None
            if low in ("aftermarket", "after market", "after-market"):
                return "aftermarket", None
            return None, "partsType must be oem or aftermarket"
        text = str(value).strip()
        if not text:
            return None, "earliestSlot needs text"
        return text, None

    def apply_note(self, field: str, value: Any) -> dict[str, Any]:
        """Record ``field=value`` only when the shop's recent lines support it."""
        if field not in FACT_FIELDS:
            return {"ok": False, "reason": f"unknown field {field!r}"}
        coerced, problem = self._coerce(field, value)
        if problem:
            return {"ok": False, "reason": problem}
        if not value_in_transcript(coerced, self.business_lines, field=field):
            return {
                "ok": False,
                "reason": (
                    f"the shop did not say {value!r}; only note what the shop said"
                ),
            }
        self.noted[field] = coerced
        return {"ok": True, "field": field, "value": coerced, "remaining": self.remaining()}

    def should_end(self) -> bool:
        return all(f in self.noted for f in FACT_FIELDS)

    def apply_end(self, reason: str) -> dict[str, Any]:
        reason = str(reason or "other")
        if reason not in END_REASONS:
            reason = "other"
        self.ended = True
        self.end_reason = reason
        return {"ok": True, "reason": reason}


__all__ = (
    "BUSINESS_LINE_WINDOW",
    "CallToolState",
    "END_REASONS",
    "FACT_FIELDS",
    "NUMBER_FIELDS",
    "end_call_schema",
    "note_fact_schema",
    "spoken_numbers",
    "value_in_transcript",
)
