"""Language of a task: detected once by the planner, used on the call and in the decision.

Gradium STT and TTS support English, Spanish, French, German and Portuguese.
The planner (General Compute) reports the language the user spoke; this
module keeps that per task in memory, offers an offline fallback detector,
and maps a language to a Gradium flagship voice.

Nothing here invents a language: unknown or unsupported input is English.
"""

from __future__ import annotations

import os
import re

SUPPORTED: tuple[str, ...] = ("en", "es", "fr", "de", "pt")
DEFAULT = "en"

NAMES: dict[str, str] = {
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "pt": "Portuguese",
}

# Gradium flagship voices (docs.gradium.ai/guides/voices/flagship-voices,
# read 2026-09-19). One natural adult voice per language; override any of
# them with GRADIUM_VOICE_ID_<LANG> in server/.env.
DEFAULT_VOICES: dict[str, str] = {
    "en": "4SZHfMpw-p46Ywgs",  # Harper, US
    "es": "VDwnGxAo68C8U8vC",  # Ximena, Mexico
    "fr": "YhIHaAfQ0cQPDV9R",  # Solène, France
    "de": "MAYVpVTYBzLRqNC7",  # Resi, Germany
    "pt": "6k6cRt7QJfm5ChrH",  # Rafaela, Brazil
}

# Small, high-frequency function words per language. Counting hits is enough
# to tell the five apart on a spoken sentence; ties and no hits mean English.
_MARKERS: dict[str, tuple[str, ...]] = {
    "es": ("el", "la", "los", "las", "un", "una", "para", "por", "con", "que", "de", "del",
           "me", "mi", "estoy", "necesito", "quiero", "precio", "frenos", "taller", "llama",
           "cerca", "dólares", "coche", "carro", "auto", "cotización", "cotizaron", "y"),
    "fr": ("le", "la", "les", "un", "une", "des", "pour", "avec", "que", "de", "du", "je",
           "suis", "besoin", "veux", "prix", "freins", "garage", "appelle", "près", "voiture",
           "devis", "et", "mon", "ma", "chez"),
    "de": ("der", "die", "das", "ein", "eine", "für", "mit", "und", "ich", "bin", "brauche",
           "möchte", "preis", "bremsen", "werkstatt", "anrufen", "nähe", "auto", "wagen",
           "angebot", "mir", "mein", "meine", "nicht"),
    "pt": ("o", "a", "os", "as", "um", "uma", "para", "com", "que", "de", "do", "da", "eu",
           "estou", "preciso", "quero", "preço", "freios", "oficina", "ligue", "perto",
           "carro", "orçamento", "e", "meu", "minha"),
    "en": ("the", "a", "an", "for", "with", "that", "of", "i", "am", "need", "want", "price",
           "brakes", "shop", "call", "near", "car", "quote", "quoted", "and", "my", "me"),
}

_TOKEN_RE = re.compile(r"[a-záéíóúñüçàâêîôûäöß]+")

_TASK_LANGUAGE: dict[str, str] = {}


def normalize(code: object) -> str:
    """``"es-MX"`` → ``"es"``; anything unsupported → ``"en"``."""
    text = str(code or "").strip().lower()
    if not text:
        return DEFAULT
    base = text.split("-")[0].split("_")[0]
    return base if base in SUPPORTED else DEFAULT


def detect_language(text: str) -> str:
    """Offline guess of the language of ``text`` from function words."""
    tokens = _TOKEN_RE.findall((text or "").casefold())
    if not tokens:
        return DEFAULT
    scores = {lang: sum(1 for t in tokens if t in words) for lang, words in _MARKERS.items()}
    best = max(scores.items(), key=lambda kv: kv[1])
    if best[1] == 0:
        return DEFAULT
    # Prefer English on a tie so a mixed sentence with English filler stays English.
    if scores.get("en", 0) >= best[1]:
        return "en"
    return best[0]


def name(lang: str) -> str:
    return NAMES.get(normalize(lang), NAMES[DEFAULT])


def voice_for(lang: str) -> str:
    """Gradium voice id for ``lang``: env override, else the flagship default."""
    code = normalize(lang)
    override = os.getenv(f"GRADIUM_VOICE_ID_{code.upper()}")
    if override:
        return override.strip()
    if code == "en":
        legacy = os.getenv("GRADIUM_VOICE_ID")
        if legacy:
            return legacy.strip()
    return DEFAULT_VOICES[code]


def set_task_language(task_id: str, lang: object) -> str:
    code = normalize(lang)
    if task_id:
        _TASK_LANGUAGE[task_id] = code
    return code


def get_task_language(task_id: str, fallback_text: str | None = None) -> str:
    """Language recorded for the task, else detected from ``fallback_text``."""
    code = _TASK_LANGUAGE.get(task_id or "")
    if code:
        return code
    if fallback_text:
        return detect_language(fallback_text)
    return DEFAULT


def clear() -> None:
    _TASK_LANGUAGE.clear()
