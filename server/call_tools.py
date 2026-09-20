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

# Spanish, French, German and Portuguese number words fold into the same
# three tables so "cuatrocientos veinte", "quatre cent vingt", "vierhundert-
# zwanzig" and "quatrocentos e vinte" all read as 420. Accented spellings are
# listed as spoken; the tokenizer keeps accented letters.
_UNITS.update({
    # es
    "cero": 0, "uno": 1, "una": 1, "un": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
    "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12,
    "trece": 13, "catorce": 14, "quince": 15, "dieciséis": 16, "dieciseis": 16,
    "diecisiete": 17, "dieciocho": 18, "diecinueve": 19, "veintiuno": 21, "veintiún": 21,
    "veintidós": 22, "veintidos": 22, "veintitrés": 23, "veintitres": 23, "veinticuatro": 24,
    "veinticinco": 25, "veintiséis": 26, "veintiseis": 26, "veintisiete": 27, "veintiocho": 28,
    "veintinueve": 29,
    # fr
    "zéro": 0, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "sept": 7,
    "huit": 8, "neuf": 9, "dix": 10, "onze": 11, "douze": 12, "treize": 13, "quatorze": 14,
    "seize": 16, "dix-sept": 17, "dix-huit": 18, "dix-neuf": 19,
    # de
    "null": 0, "eins": 1, "ein": 1, "eine": 1, "zwei": 2, "zwo": 2, "drei": 3, "vier": 4,
    "fünf": 5, "fuenf": 5, "sechs": 6, "sieben": 7, "acht": 8, "neun": 9, "zehn": 10,
    "elf": 11, "zwölf": 12, "zwoelf": 12, "dreizehn": 13, "vierzehn": 14, "fünfzehn": 15,
    "sechzehn": 16, "siebzehn": 17, "achtzehn": 18, "neunzehn": 19,
    # pt
    "um": 1, "uma": 1, "dois": 2, "duas": 2, "três": 3, "quatro": 4,
    "sete": 7, "oito": 8, "nove": 9, "dez": 10, "doze": 12, "treze": 13,
    "quinze": 15, "dezesseis": 16, "dezassete": 17,
    "dezessete": 17, "dezoito": 18, "dezenove": 19, "dezanove": 19,
})
_TENS.update({
    # es
    "veinte": 20, "treinta": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60,
    "setenta": 70, "ochenta": 80, "noventa": 90,
    # fr (quatre-vingt / soixante-dix are rewritten to huitante / septante first)
    "vingt": 20, "trente": 30, "quarante": 40, "cinquante": 50, "soixante": 60,
    "septante": 70, "huitante": 80, "octante": 80, "nonante": 90,
    # de
    "zwanzig": 20, "dreißig": 30, "dreissig": 30, "vierzig": 40, "fünfzig": 50,
    "fuenfzig": 50, "sechzig": 60, "siebzig": 70, "achtzig": 80, "neunzig": 90,
    # pt
    "vinte": 20, "trinta": 30, "cinquenta": 50, "sessenta": 60,
    "oitenta": 80,
})
# Hundreds that are one word: value is taken literally, not multiplied.
_HUNDRED_WORDS: dict[str, int] = {
    # es
    "cien": 100, "ciento": 100, "doscientos": 200, "doscientas": 200, "trescientos": 300,
    "trescientas": 300, "cuatrocientos": 400, "cuatrocientas": 400, "quinientos": 500,
    "quinientas": 500, "seiscientos": 600, "seiscientas": 600, "setecientos": 700,
    "setecientas": 700, "ochocientos": 800, "ochocientas": 800, "novecientos": 900,
    "novecientas": 900,
    # pt
    "cem": 100, "cento": 100, "duzentos": 200, "duzentas": 200, "trezentos": 300,
    "trezentas": 300, "quatrocentos": 400, "quatrocentas": 400, "quinhentos": 500,
    "quinhentas": 500, "seiscentos": 600, "seiscentas": 600, "setecentos": 700,
    "setecentas": 700, "oitocentos": 800, "oitocentas": 800, "novecentos": 900,
    "novecentas": 900,
}
_SCALE_ALIASES: dict[str, str] = {
    "cent": "hundred", "cents": "hundred", "hundert": "hundred", "hundred": "hundred",
    "mil": "thousand", "mille": "thousand", "tausend": "thousand", "thousand": "thousand",
}
# Words allowed between number words without breaking the run ("y", "e", "et", "und").
_CONNECTORS = ("and", "y", "e", "et", "und")
_GERMAN_SPLIT_RE = re.compile(r"(hundert|tausend|und)")
_FRENCH_REWRITES = (
    ("quatre-vingt-dix", "nonante"), ("quatre vingt dix", "nonante"),
    ("quatre-vingts", "huitante"), ("quatre-vingt", "huitante"), ("quatre vingts", "huitante"),
    ("quatre vingt", "huitante"), ("soixante-dix", "septante"), ("soixante dix", "septante"),
)
_FRACTIONS: dict[str, float] = {"half": 0.5, "quarter": 0.25}
_DIGIT_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
_WORD_RE = re.compile(r"[a-záéíóúñüçàâêîôûäöß]+(?:-[a-záéíóúñüçàâêîôûäöß]+)*|\d+(?:\.\d+)?")
# Clause breaks: "one eighty, one twenty" must not read as one number run.
_CLAUSE_RE = re.compile(r"[,;:!?\n]|\.(?!\d)")


def _is_number_word(tok: str) -> bool:
    return (
        tok in _UNITS or tok in _TENS or tok in _SCALES or tok in _HUNDRED_WORDS
        or tok in _SCALE_ALIASES or tok.isdigit()
    )


def _expand_tokens(words: list[str]) -> list[str]:
    """Normalise foreign number tokens onto the English tables.

    German compounds split on hundert / tausend / und ("vierhundertzwanzig" ->
    vier hundred zwanzig, "einundzwanzig" -> zwanzig ein). Scale aliases map
    to "hundred" / "thousand". Everything else passes through unchanged.
    """
    out: list[str] = []
    for tok in words:
        if tok in _SCALE_ALIASES:
            out.append(_SCALE_ALIASES[tok])
            continue
        if tok in _UNITS or tok in _TENS or tok in _HUNDRED_WORDS or tok.isdigit():
            out.append(tok)
            continue
        if "-" in tok:
            # French "vingt-cinq", "cent-vingt": split when every piece is a number word.
            pieces = tok.split("-")
            if all(p in _UNITS or p in _TENS or p in _HUNDRED_WORDS or p in _SCALE_ALIASES for p in pieces):
                out.extend(_SCALE_ALIASES.get(p, p) for p in pieces)
                continue
        if "hundert" in tok or "tausend" in tok or "und" in tok:
            parts = [p for p in _GERMAN_SPLIT_RE.split(tok) if p]
            if all(p in _UNITS or p in _TENS or p in _SCALE_ALIASES or p == "und" for p in parts):
                i = 0
                while i < len(parts):
                    p = parts[i]
                    if p == "und":
                        i += 1
                        continue
                    if p in _UNITS and i + 2 < len(parts) and parts[i + 1] == "und" and parts[i + 2] in _TENS:
                        out.extend([parts[i + 2], p])  # ein-und-zwanzig -> zwanzig ein
                        i += 3
                        continue
                    out.append(_SCALE_ALIASES.get(p, p))
                    i += 1
                continue
        out.append(tok)
    return out


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
            elif tok in _HUNDRED_WORDS:
                current += _HUNDRED_WORDS[tok]
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
    for old, new in _FRENCH_REWRITES:
        lowered = lowered.replace(old, new)
    words = _expand_tokens(_WORD_RE.findall(lowered))
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
            elif (
                tok in _CONNECTORS
                and j + 1 < n
                and (words[j + 1] in _UNITS or words[j + 1] in _TENS)
                and run
                and (run[-1] in _SCALES or run[-1] in _HUNDRED_WORDS or run[-1] in _TENS)
            ):
                # "four hundred and twenty", "cuatrocientos y veinte", "vingt et un"
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
    # Fractions in the other languages: "dos horas y media", "deux heures et
    # demie", "zweieinhalb", "duas horas e meia"; "media hora" style on its own.
    m = re.search(
        r"\b([a-záéíóúñüçàâêîôûäöß]+)(?:\s+[a-záéíóúñüçàâêîôûäöß]+)?\s+(?:y|e|et|und)\s+(?:media|medio|meia|meio|demie?|halb)\b",
        lowered,
    )
    if m and m.group(1) in _UNITS:
        found.add(_UNITS[m.group(1)] + 0.5)
    m = re.search(r"\b([a-zäöüß]+)einhalb\b", lowered)
    if m and m.group(1) in _UNITS:
        found.add(_UNITS[m.group(1)] + 0.5)
    if re.search(r"\banderthalb\b", lowered):
        found.add(1.5)
    if re.search(r"\b(?:media|meia|demi[- ]?|halbe?)\s*(?:hora|heure|stunde)\b", lowered):
        found.add(0.5)
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
