"""Transcript → Facts JSON (frontend ``Facts`` + required ``confidence``).

Uses ``llm.complete`` when that helper is importable. Offline / LLM failure
returns confidence 0 and no price fields — never invent a dollar amount.

Price guard: every price the model returns (``allInPrice``,
``laborRatePerHour``, ``partPrice``) must appear in the transcript, either as
digits ("$450", "150") or as spoken money words ("four fifty", "six ten",
"one twenty", "a hundred", "hundred and fifty"). A price that was never spoken
is dropped and ``confidence`` is lowered. When transcript lines carry roles,
only the business side counts; the agent is not allowed to introduce numbers.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .models import Facts, TranscriptLine
except ImportError:  # pragma: no cover
    try:
        from models import Facts, TranscriptLine
    except ImportError:
        from server.models import Facts, TranscriptLine

try:
    from .llm import complete as _llm_complete
except ImportError:  # pragma: no cover
    try:
        from llm import complete as _llm_complete
    except ImportError:
        try:
            from server.llm import complete as _llm_complete
        except ImportError:
            _llm_complete = None

_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "extract.md"

_PRICE_KEYS = ("allInPrice", "laborRatePerHour", "partPrice")

_FACTS_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["confidence"],
    "properties": {
        "allInPrice": {"type": ["number", "null"]},
        "laborRatePerHour": {"type": ["number", "null"]},
        "laborHours": {"type": ["number", "null"]},
        "acceptsCustomerParts": {"type": ["boolean", "null"]},
        "partsType": {
            "anyOf": [
                {"type": "string", "enum": ["oem", "aftermarket"]},
                {"type": "null"},
            ]
        },
        "warrantyMonths": {"type": ["integer", "null"]},
        "earliestSlot": {"type": ["string", "null"]},
        "partPrice": {"type": ["number", "null"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
}


def extract_facts(
    transcript: Sequence[TranscriptLine | Mapping[str, Any] | str],
    schema: dict[str, Any] | None = None,
) -> Facts:
    """Turn a call/web transcript into ``Facts``. Never invents a dollar amount.

    If ``llm.complete`` is present, asks the model for Facts JSON. If it is
    missing or the call fails, returns ``confidence=0`` with empty prices.
    ``schema`` is an optional extra JSON Schema merged into the default Facts
    schema (planner extraction schema).
    """
    if _llm_complete is None or not _llm_key_present():
        return _offline_facts()

    system = _load_prompt()
    user = _format_transcript(transcript)
    json_schema = _merge_schema(schema)
    try:
        raw = _llm_complete(system, user, json_schema=json_schema)
    except Exception:
        if schema:
            # The planner-supplied schema may be the reason; retry once with
            # the canonical Facts schema before giving up.
            try:
                raw = _llm_complete(system, user, json_schema=_merge_schema(None))
            except Exception:
                return _offline_facts()
        else:
            return _offline_facts()

    parsed = _parse_json_object(raw)
    if parsed is None:
        return _offline_facts()
    spoken = _spoken_amounts(_price_source_text(transcript))
    return _facts_from_payload(parsed, spoken=spoken)


def _llm_key_present() -> bool:
    return bool(
        os.getenv("GENERAL_COMPUTE_API_KEY") or os.getenv("GENERALCOMPUTE_API_KEY")
    )


def _offline_facts() -> Facts:
    return Facts(confidence=0.0)


def _load_prompt() -> str:
    if _PROMPT_PATH.is_file():
        return _PROMPT_PATH.read_text(encoding="utf-8").strip()
    return (
        "Extract Facts JSON from the transcript. Required key: confidence. "
        "Omit price fields unless the shop stated a dollar amount. JSON only."
    )


def _format_transcript(
    transcript: Sequence[TranscriptLine | Mapping[str, Any] | str],
) -> str:
    lines: list[str] = []
    for item in transcript or []:
        if isinstance(item, str):
            text = item.strip()
            if text:
                lines.append(text)
            continue
        if isinstance(item, TranscriptLine):
            role, text = item.role, item.text
        elif isinstance(item, Mapping):
            role = str(item.get("role") or "unknown")
            text = str(item.get("text") or "").strip()
        else:
            role = str(getattr(item, "role", "unknown"))
            text = str(getattr(item, "text", "")).strip()
        if not text:
            continue
        lines.append(f"{role}: {text}")
    return "\n".join(lines) if lines else "(empty transcript)"


def _price_source_text(
    transcript: Sequence[TranscriptLine | Mapping[str, Any] | str],
) -> str:
    """Text a price may be verified against.

    Business lines only when the transcript carries roles (the agent never
    introduces a number); every line when it does not (plain strings).
    """
    business: list[str] = []
    everything: list[str] = []
    saw_role = False
    for item in transcript or []:
        if isinstance(item, str):
            text, role = item.strip(), None
        elif isinstance(item, TranscriptLine):
            text, role = item.text, item.role
        elif isinstance(item, Mapping):
            text = str(item.get("text") or "").strip()
            role = item.get("role")
        else:
            text = str(getattr(item, "text", "")).strip()
            role = getattr(item, "role", None)
        if not text:
            continue
        everything.append(text)
        if role:
            saw_role = True
            if str(role) != "agent":
                business.append(text)
    return "\n".join(business if saw_role else everything)


# --- spoken money ---------------------------------------------------------

_UNITS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9,
}
_TEENS = {
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_MULTIPLIERS = {"hundred": 100, "thousand": 1000, "grand": 1000}
_ARTICLES = {"a", "an"}
_SMALL_WORDS = set(_UNITS) | set(_TEENS) | set(_TENS)
_NUMBER_WORDS = _SMALL_WORDS | set(_MULTIPLIERS)

_DIGIT_AMOUNT = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
# Words, digit groups, or punctuation. Punctuation is a token so "two forty."
# followed by "One twenty" on the next line never merges into one run.
_WORD = re.compile(r"[a-z]+|\d[\d,.]*|[^\sa-z\d]+")


def _spoken_amounts(text: str) -> set[float]:
    """Every dollar-like amount present in ``text`` as digits or number words.

    Digits: "$450", "150", "1,200", "150.50". Words: "four fifty" → 450,
    "six ten" → 610, "one twenty" → 120, "a hundred" → 100,
    "hundred and fifty" → 150, "twelve hundred" → 1200, "twenty five" → 25.
    """
    amounts: set[float] = set()
    if not text:
        return amounts
    for match in _DIGIT_AMOUNT.finditer(text):
        try:
            amounts.add(float(match.group(0).replace(",", "")))
        except ValueError:
            continue

    tokens = _WORD.findall(text.lower().replace("-", " "))
    run: list[str] = []
    for index, tok in enumerate(tokens):
        nxt = tokens[index + 1] if index + 1 < len(tokens) else ""
        if tok in _NUMBER_WORDS:
            run.append(tok)
        elif tok in _ARTICLES and nxt in _MULTIPLIERS and not run:
            run.append(tok)
        elif tok == "and" and run and run[-1] in _MULTIPLIERS and nxt in _SMALL_WORDS:
            run.append(tok)
        else:
            amounts.update(_parse_spoken_run(run))
            run = []
    amounts.update(_parse_spoken_run(run))
    return amounts


def _parse_spoken_run(run: list[str], windows: bool = True) -> set[float]:
    """Candidate values for one run of number words."""
    out: set[float] = set()
    if not run:
        return out
    standard = _standard_value(run)
    if standard is not None and standard > 0:
        out.add(float(standard))
    # Colloquial hundreds: "four fifty" = 450, "six ten" = 610, "one twenty five" = 125.
    for split in range(1, len(run)):
        left = _small_value(run[:split])
        right = _small_value(run[split:])
        if left is not None and right is not None and right >= 10:
            out.add(float(left * 100 + right))
    # Unpunctuated STT can glue two amounts together ("two forty one twenty an
    # hour"). For a run of four or more small words, also read each 2–3 word
    # window. Three-word amounts ("four fifty five") stay exact.
    if windows and len(run) >= 4 and all(w in _SMALL_WORDS for w in run):
        for size in (2, 3):
            for start in range(0, len(run) - size + 1):
                out.update(_parse_spoken_run(run[start : start + size], windows=False))
    return out


def _small_value(words: list[str]) -> int | None:
    """1–99 from [teen] | [unit] | [tens] | [tens unit]; else None."""
    if len(words) == 1:
        w = words[0]
        return _UNITS.get(w) or _TEENS.get(w) or _TENS.get(w)
    if len(words) == 2 and words[0] in _TENS and words[1] in _UNITS:
        return _TENS[words[0]] + _UNITS[words[1]]
    return None


def _standard_value(words: list[str]) -> int | None:
    """Well-formed English: "one hundred and fifty", "a hundred", "twelve hundred".

    Rejects a unit/teen followed by another small number ("four fifty"),
    which the colloquial path handles instead.
    """
    total = 0
    current = 0
    prev = ""
    for w in words:
        if w in _ARTICLES:
            current = 1
        elif w == "and":
            pass
        elif w in _SMALL_WORDS:
            if prev in _SMALL_WORDS and not (prev in _TENS and w in _UNITS):
                return None
            current += _UNITS.get(w) or _TEENS.get(w) or _TENS.get(w, 0)
        elif w == "hundred":
            current = (current or 1) * 100
        else:  # thousand / grand
            total += (current or 1) * 1000
            current = 0
        prev = w
    return total + current


def _price_spoken(value: float, spoken: set[float]) -> bool:
    return any(abs(value - amount) < 0.005 for amount in spoken)


def _merge_schema(extra: dict[str, Any] | None) -> dict[str, Any]:
    schema = dict(_FACTS_JSON_SCHEMA)
    if not extra:
        return schema
    props = dict(schema.get("properties") or {})
    extra_props = extra.get("properties") if isinstance(extra.get("properties"), dict) else {}
    # The planner's schema may add fields but never redefine a canonical
    # Facts field: an invalid redefinition (e.g. a nullable enum without null)
    # makes the structured-output request fail and hang the extraction.
    for key, definition in extra_props.items():
        if key not in props:
            props[key] = definition
    schema["properties"] = props
    required = list(schema.get("required") or [])
    for key in extra.get("required") or []:
        if key not in required:
            required.append(key)
    if "confidence" not in required:
        required.append("confidence")
    schema["required"] = required
    return schema


def _parse_json_object(raw: str) -> dict[str, Any] | None:
    text = (raw or "").strip()
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start : end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


_PRICE_REJECT_PENALTY = 0.3


def _facts_from_payload(
    data: dict[str, Any],
    spoken: set[float] | None = None,
) -> Facts:
    """Build ``Facts`` from model JSON.

    When ``spoken`` is given (amounts found in the transcript), any price the
    model returned that is not in it is dropped and confidence falls by
    ``_PRICE_REJECT_PENALTY`` per dropped price.
    """
    cleaned: dict[str, Any] = {}
    confidence = data.get("confidence", 0)
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.0

    for key in _PRICE_KEYS:
        value = _as_price(data.get(key))
        if value is None:
            continue
        if spoken is not None and not _price_spoken(value, spoken):
            confidence = max(0.0, confidence - _PRICE_REJECT_PENALTY)
            continue
        cleaned[key] = value
    cleaned["confidence"] = round(confidence, 4)

    hours = data.get("laborHours")
    if isinstance(hours, (int, float)) and not isinstance(hours, bool) and hours > 0:
        cleaned["laborHours"] = float(hours)

    accepts = data.get("acceptsCustomerParts")
    if isinstance(accepts, bool):
        cleaned["acceptsCustomerParts"] = accepts

    parts_type = data.get("partsType")
    if parts_type in ("oem", "aftermarket"):
        cleaned["partsType"] = parts_type

    warranty = data.get("warrantyMonths")
    if isinstance(warranty, (int, float)) and not isinstance(warranty, bool) and warranty >= 0:
        cleaned["warrantyMonths"] = int(warranty)

    slot = data.get("earliestSlot")
    if isinstance(slot, str) and slot.strip():
        cleaned["earliestSlot"] = slot.strip()

    return Facts.model_validate(cleaned)


def _as_price(value: Any) -> float | None:
    """Keep only a stated positive dollar amount. Never coerce missing → 0."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("$", "")
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            return None
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    return None
