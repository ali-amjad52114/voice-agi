"""Language plumbing: detection, voices, spoken numbers in five languages,
and the language instruction reaching the call brain and the decision."""

from __future__ import annotations

import json

import pytest

from server import call_audio, call_tools, lang, planner, synthesize


# --------------------------------------------------------------------------- #
# lang.py
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text, expected",
    [
        ("One mechanic quoted me $800 for front brakes on my 2019 Camry, I'm in Fremont", "en"),
        ("Un mecánico me cotizó 800 dólares por los frenos delanteros de mi Camry 2019, estoy en Fremont", "es"),
        ("Un garage m'a fait un devis de 800 dollars pour les freins avant de ma Camry 2019, je suis à Fremont", "fr"),
        ("Eine Werkstatt hat mir 800 Dollar für die vorderen Bremsen meines Camry 2019 angeboten, ich bin in Fremont", "de"),
        ("Uma oficina me deu um orçamento de 800 dólares para os freios dianteiros do meu Camry 2019, estou em Fremont", "pt"),
        ("", "en"),
        ("2019 Camry 800", "en"),
    ],
)
def test_detect_language(text, expected):
    assert lang.detect_language(text) == expected


def test_normalize_codes():
    assert lang.normalize("es-MX") == "es"
    assert lang.normalize("PT_BR") == "pt"
    assert lang.normalize("jp") == "en"
    assert lang.normalize(None) == "en"


def test_voice_for_defaults_and_overrides(monkeypatch):
    monkeypatch.delenv("GRADIUM_VOICE_ID", raising=False)
    monkeypatch.delenv("GRADIUM_VOICE_ID_ES", raising=False)
    assert lang.voice_for("en") == lang.DEFAULT_VOICES["en"]
    assert lang.voice_for("es") == lang.DEFAULT_VOICES["es"]
    assert lang.voice_for("es") != lang.voice_for("en")
    monkeypatch.setenv("GRADIUM_VOICE_ID_ES", "custom-es")
    assert lang.voice_for("es-MX") == "custom-es"
    monkeypatch.setenv("GRADIUM_VOICE_ID", "custom-en")
    assert lang.voice_for("en") == "custom-en"
    assert lang.voice_for("xx") == "custom-en"  # unsupported → English


def test_task_language_store_and_fallback():
    lang.clear()
    assert lang.get_task_language("t1") == "en"
    assert lang.get_task_language("t1", "necesito un precio para los frenos de mi carro") == "es"
    assert lang.set_task_language("t1", "fr-CA") == "fr"
    assert lang.get_task_language("t1", "necesito un precio") == "fr"
    lang.clear()


# --------------------------------------------------------------------------- #
# planner
# --------------------------------------------------------------------------- #


def test_offline_plan_carries_language():
    en = planner._offline_plan("One mechanic quoted me $800 for front brakes on my 2019 Camry, I'm in Fremont", None)
    es = planner._offline_plan("Un mecánico me cotizó 800 dólares por los frenos de mi Camry 2019, estoy en Fremont", None)
    assert en["language"] == "en"
    assert es["language"] == "es"
    assert "language" in planner._PLAN_JSON_SCHEMA["properties"]
    assert "language" in planner._PLAN_JSON_SCHEMA["required"]


def test_normalize_keeps_model_language_when_supported():
    fallback = planner._offline_plan("brakes on my 2019 Camry", None)
    assert planner._normalize({"language": "pt-BR"}, fallback)["language"] == "pt"
    assert planner._normalize({"language": "klingon"}, fallback)["language"] == "en"
    assert planner._normalize({}, fallback)["language"] == fallback["language"]


# --------------------------------------------------------------------------- #
# spoken numbers across languages
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text, expected",
    [
        # Spanish
        ("son cuatrocientos veinte todo incluido", 420),
        ("cuatrocientos y veinte dólares", 420),
        ("la mano de obra es ciento veinticinco la hora", 125),
        ("unas dos horas y media", 2.5),
        ("mil doscientos", 1200),
        # French
        ("ça fait quatre cent vingt dollars tout compris", 420),
        ("cent vingt-cinq de l'heure", 125),
        ("quatre-vingt-dix dollars", 90),
        ("soixante-dix", 70),
        # German
        ("das sind vierhundertzwanzig Dollar", 420),
        ("hundertfünfundzwanzig die Stunde", 125),
        ("einundzwanzig", 21),
        ("zweitausend", 2000),
        # Portuguese
        ("quatrocentos e vinte dólares", 420),
        ("cento e vinte e cinco por hora", 125),
        ("duas horas", 2),
        # English still works
        ("four twenty all in", 420),
        ("it's $420", 420),
    ],
)
def test_spoken_numbers_multilingual(text, expected):
    assert expected in call_tools.spoken_numbers(text), call_tools.spoken_numbers(text)


def test_note_fact_grounding_in_spanish():
    state = call_tools.CallToolState()
    state.record_business_line("Sí, son cuatrocientos veinte todo incluido con nuestras piezas.")
    assert state.apply_note("allInPrice", 420)["ok"] is True
    assert state.apply_note("laborRatePerHour", 999)["ok"] is False
    assert state.noted.get("allInPrice") == 420
    assert "laborRatePerHour" not in state.noted


def test_spanish_does_not_leak_wrong_numbers():
    # "y" as a connector must not glue two separate figures together.
    nums = call_tools.spoken_numbers("ciento ochenta por las pastillas, y ciento veinte por los discos")
    assert 180 in nums and 120 in nums
    assert 300 not in nums


# --------------------------------------------------------------------------- #
# call brain instruction
# --------------------------------------------------------------------------- #


def test_system_instruction_names_the_language():
    en = call_audio._system_instruction("en")
    es = call_audio._system_instruction("es")
    assert "Speak English for the whole call." in en
    assert "Speak Spanish for the whole call." in es
    assert "Ask every question in Spanish" in es
    assert "Never invent a dollar amount" in es
    assert "Ask every question in" not in en


# --------------------------------------------------------------------------- #
# decision prompt
# --------------------------------------------------------------------------- #


def test_language_instruction_only_for_non_english():
    assert synthesize._language_instruction("en") == ""
    assert synthesize._language_instruction(None) == ""
    text = synthesize._language_instruction("es-MX")
    assert "Spanish" in text
    assert "Bring your own part" not in text  # labels are referenced generically
    assert "label" in text


def test_synthesize_passes_language_to_the_model(monkeypatch):
    seen: dict[str, str] = {}

    def fake_complete(system, user, json_schema=None, **kwargs):
        seen["system"] = system
        payload = json.loads(user)
        shop = payload["shops"][0]["agentId"]
        return json.dumps(
            {
                "options": [
                    {
                        "label": "Shop supplies part",
                        "total": 420,
                        "breakdown": "$420 todo incluido en Taller Uno",
                        "agentIds": [shop],
                        "hassle": "una visita",
                    }
                ],
                "recommendedOptionIndex": 0,
                "why": "Solo un taller respondió con una cotización de $420. Es la única opción verificada.",
                "tradeoffs": ["Garantía no indicada", "Una sola visita"],
            }
        )

    monkeypatch.setattr(synthesize, "_llm_complete", fake_complete)
    monkeypatch.setattr(synthesize, "_llm_complete_stream", None)
    agents = [
        {
            "id": "a1",
            "kind": "call",
            "business": {"name": "Taller Uno", "type": "mechanic"},
            "facts": {"allInPrice": 420, "confidence": 0.9},
        }
    ]
    result = synthesize.synthesize(agents=agents, userQuote=800, language="es")
    assert "Spanish" in seen["system"]
    assert result["options"][0]["total"] == 420
    assert result["why"].startswith("Solo un taller")
