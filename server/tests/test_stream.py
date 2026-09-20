"""Session 8: stream the why. No network anywhere in here.

- ``WhyStreamer`` pulls the ``why`` string out of streamed JSON at any split.
- ``llm.complete_stream`` iterates chunk objects, calls ``on_delta``, keeps
  the ``json_object`` fallback and records usage (tokens may be missing).
- ``result.partial`` is a ``TaskEvent``: the API coerces it and the
  worker-thread publisher hands it to the loop.
- The orchestrator emits ``result.partial`` before ``task.result``.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_SERVER = Path(__file__).resolve().parents[1]
_REPO = Path(__file__).resolve().parents[2]
for _p in (str(_REPO), str(_SERVER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_synthesize import DECISION_AGENTS, VALID_DECISION  # noqa: E402


def _blocked(*_args, **_kwargs):
    raise AssertionError("network is forbidden in stream tests")


def _split_every(text: str, n: int) -> list[str]:
    return [text[i : i + n] for i in range(0, len(text), n)]


# --------------------------------------------------------------------------- #
# WhyStreamer
# --------------------------------------------------------------------------- #


class TestWhyStreamer(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from server import synthesize as syn
        except ImportError:
            import synthesize as syn  # type: ignore

        self.syn = syn

    def _collect(self, pieces: list[str]) -> tuple[list[str], "object"]:
        streamer = self.syn.WhyStreamer()
        out = [streamer.feed(p) for p in pieces]
        return [o for o in out if o], streamer

    def test_whole_document_in_one_piece(self) -> None:
        doc = json.dumps(VALID_DECISION)
        got, streamer = self._collect([doc])
        self.assertEqual("".join(got), VALID_DECISION["why"])
        self.assertTrue(streamer.done)
        self.assertEqual(streamer.text, VALID_DECISION["why"])

    def test_one_character_at_a_time(self) -> None:
        doc = json.dumps(VALID_DECISION)
        got, streamer = self._collect(list(doc))
        self.assertEqual("".join(got), VALID_DECISION["why"])
        self.assertTrue(streamer.done)
        # every returned piece is exactly one character: nothing buffered or repeated
        self.assertTrue(all(len(p) == 1 for p in got))

    def test_every_split_width_agrees(self) -> None:
        doc = json.dumps(VALID_DECISION, indent=2)
        for n in (1, 2, 3, 5, 7, 11, 64):
            with self.subTest(width=n):
                got, _ = self._collect(_split_every(doc, n))
                self.assertEqual("".join(got), VALID_DECISION["why"])

    def test_split_mid_key(self) -> None:
        pieces = ['{"options": [], "w', 'h', 'y"', ' ', ':', ' "Two roads', ' diverged."', ', "tradeoffs": []}']
        got, streamer = self._collect(pieces)
        self.assertEqual(got, ["Two roads", " diverged."])
        self.assertTrue(streamer.done)

    def test_split_mid_escape_quote_and_newline(self) -> None:
        # raw JSON text: "why": "He said \"go\".\nThen left."
        pieces = ['{"why": "He said \\', '"go\\"', '.\\', 'nThen left."}']
        got, _ = self._collect(pieces)
        self.assertEqual("".join(got), 'He said "go".\nThen left.')
        # the half escape is held back, never emitted as a stray backslash
        self.assertNotIn("\\", "".join(got))

    def test_split_inside_unicode_escape_and_surrogate_pair(self) -> None:
        raw = '{"why": "caf\\u00e9 \\ud83d\\ude00 ok"}'
        expected = json.loads(raw)["why"]
        for n in (1, 2, 3, 4, 5):
            with self.subTest(width=n):
                got, _ = self._collect(_split_every(raw, n))
                self.assertEqual("".join(got), expected)

    def test_why_inside_another_string_value_is_ignored(self) -> None:
        raw = '{"breakdown": "ask why: \\"why\\" not", "label": "why", "why": "Real reason."}'
        got, _ = self._collect(list(raw))
        self.assertEqual("".join(got), "Real reason.")

    def test_why_key_anywhere_and_stops_at_closing_quote(self) -> None:
        raw = '{"why": "First.", "tradeoffs": ["not why", "\\"why\\": \\"nope\\""], "why": "Second."}'
        got, streamer = self._collect(_split_every(raw, 4))
        self.assertEqual("".join(got), "First.")
        self.assertTrue(streamer.done)
        # nothing more after done, even if fed
        self.assertEqual(streamer.feed('"why": "again"'), "")

    def test_non_string_why_is_skipped_until_a_string_why(self) -> None:
        raw = '{"why": null, "options": [{"why": "Nested wins."}]}'
        got, _ = self._collect(list(raw))
        self.assertEqual("".join(got), "Nested wins.")

    def test_no_why_yields_nothing(self) -> None:
        got, streamer = self._collect(list('{"options": [], "tradeoffs": []}'))
        self.assertEqual(got, [])
        self.assertFalse(streamer.done)

    def test_empty_and_none_deltas_are_harmless(self) -> None:
        streamer = self.syn.WhyStreamer()
        self.assertEqual(streamer.feed(""), "")
        self.assertEqual(streamer.feed(None), "")  # type: ignore[arg-type]
        self.assertEqual(streamer.feed('{"why":"x"}'), "x")


# --------------------------------------------------------------------------- #
# llm.complete_stream with a fake client
# --------------------------------------------------------------------------- #


def _chunk(content: str | None, *, model: str | None = "gemma-4-31B-it", usage=None):
    """Mimic an openai ``ChatCompletionChunk``: ``.choices[0].delta.content``."""
    choices = [] if content is None else [SimpleNamespace(delta=SimpleNamespace(content=content))]
    return SimpleNamespace(choices=choices, model=model, usage=usage)


class _FakeStreamCompletions:
    """``create(**params)`` records params; raises ``first_error`` once, then streams."""

    def __init__(self, first_error, chunks, *, fail_mid_stream: bool = False) -> None:
        self.first_error = first_error
        self.chunks = chunks
        self.fail_mid_stream = fail_mid_stream
        self.calls: list[dict] = []

    def create(self, **params):
        self.calls.append(params)
        if self.first_error is not None and len(self.calls) == 1:
            if self.fail_mid_stream:
                return self._failing_iter(self.first_error)
            raise self.first_error
        return iter(list(self.chunks))

    @staticmethod
    def _failing_iter(exc):
        raise exc
        yield  # pragma: no cover


class _SchemaRejected(Exception):
    status_code = 400


class TestCompleteStream(unittest.TestCase):
    def setUp(self) -> None:
        try:
            from server import gc_usage, llm
        except ImportError:
            import gc_usage  # type: ignore
            import llm  # type: ignore

        self.llm = llm
        self.gc_usage = gc_usage
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for p in (
            patch.object(gc_usage, "_LOG_PATH", Path(self.tmp.name) / "gc_usage.log"),
            patch("urllib.request.urlopen", _blocked),
        ):
            p.start()
            self.addCleanup(p.stop)
        gc_usage.clear()

    def _fake(self, chunks, first_error=None, **kw):
        completions = _FakeStreamCompletions(first_error, chunks, **kw)
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        p = patch.object(self.llm, "_client", lambda: client)
        p.start()
        self.addCleanup(p.stop)
        return completions

    def test_streams_deltas_and_returns_full_text_and_usage(self) -> None:
        final_usage = SimpleNamespace(prompt_tokens=300, completion_tokens=40)
        chunks = [
            _chunk(""),  # empty delta: not forwarded
            _chunk('{"why": "'),
            _chunk("Hel"),
            _chunk(None),  # no choices: skipped
            _chunk('lo."}'),
            _chunk(None, usage=final_usage),  # final chunk carries usage
        ]
        completions = self._fake(chunks)
        seen: list[str] = []
        schema = {"type": "object"}
        text, usage = self.llm.complete_stream(
            "SYS", "USER", schema, stage="synthesize", task_id="t1", on_delta=seen.append
        )

        self.assertEqual(text, '{"why": "Hello."}')
        self.assertEqual(seen, ['{"why": "', "Hel", 'lo."}'])
        self.assertEqual(len(completions.calls), 1)
        sent = completions.calls[0]
        self.assertTrue(sent["stream"])
        self.assertNotIn("stream_options", sent)
        self.assertEqual(sent["response_format"]["type"], "json_schema")
        self.assertEqual(sent["messages"][0], {"role": "system", "content": "SYS"})

        self.assertEqual(usage["json_mode"], "json_schema")
        self.assertEqual(usage["prompt_tokens"], 300)
        self.assertEqual(usage["completion_tokens"], 40)
        self.assertEqual(usage["model"], "gemma-4-31B-it")
        self.assertIsInstance(usage["latency_s"], float)
        self.assertIsInstance(usage["ttfb_s"], float)
        self.assertLessEqual(usage["ttfb_s"], usage["latency_s"])

        recent = self.gc_usage.recent()
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]["stage"], "synthesize")
        self.assertEqual(recent[0]["taskId"], "t1")
        self.assertEqual(recent[0]["ttfb_s"], usage["ttfb_s"])

    def test_missing_usage_gives_none_tokens_but_latency(self) -> None:
        completions = self._fake([_chunk("plain "), _chunk("text")])
        text, usage = self.llm.complete_stream("S", "U", stage="planner")
        self.assertEqual(text, "plain text")
        self.assertIsNone(usage["prompt_tokens"])
        self.assertIsNone(usage["completion_tokens"])
        self.assertIsInstance(usage["latency_s"], float)
        self.assertEqual(usage["json_mode"], "text")
        self.assertNotIn("response_format", completions.calls[0])
        self.assertEqual(self.gc_usage.recent()[-1]["stage"], "planner")

    def test_no_deltas_gives_none_ttfb_and_empty_text(self) -> None:
        self._fake([_chunk(None)])
        text, usage = self.llm.complete_stream("S", "U")
        self.assertEqual(text, "")
        self.assertIsNone(usage["ttfb_s"])

    def test_on_delta_optional(self) -> None:
        self._fake([_chunk("a"), _chunk("b")])
        text, _usage = self.llm.complete_stream("S", "U")
        self.assertEqual(text, "ab")

    def test_400_on_json_schema_falls_back_to_json_object_stream(self) -> None:
        schema = {"type": "object", "properties": {"why": {"type": "string"}}}
        completions = self._fake([_chunk('{"why": "ok"}')], _SchemaRejected("400 response_format"))
        seen: list[str] = []
        text, usage = self.llm.complete_stream("SYSTEM PROMPT", "USER", schema, on_delta=seen.append)

        self.assertEqual(text, '{"why": "ok"}')
        self.assertEqual(seen, ['{"why": "ok"}'])
        self.assertEqual(len(completions.calls), 2)
        first, second = completions.calls
        self.assertEqual(first["response_format"]["type"], "json_schema")
        self.assertEqual(second["response_format"], {"type": "json_object"})
        self.assertTrue(second["stream"])
        self.assertTrue(second["messages"][0]["content"].startswith("SYSTEM PROMPT"))
        self.assertIn(json.dumps(schema), second["messages"][0]["content"])
        self.assertEqual(usage["json_mode"], "json_object")

    def test_schema_rejection_while_reading_the_stream_also_falls_back(self) -> None:
        completions = self._fake(
            [_chunk("{}")],
            RuntimeError("unsupported json_schema in response_format"),
            fail_mid_stream=True,
        )
        text, usage = self.llm.complete_stream("S", "U", {"type": "object"})
        self.assertEqual(text, "{}")
        self.assertEqual(len(completions.calls), 2)
        self.assertEqual(usage["json_mode"], "json_object")

    def test_other_errors_are_not_retried(self) -> None:
        completions = self._fake([_chunk("x")], RuntimeError("connection reset"))
        with self.assertRaises(RuntimeError):
            self.llm.complete_stream("S", "U", {"type": "object"})
        self.assertEqual(len(completions.calls), 1)

    def test_non_stream_helper_still_shares_the_fallback(self) -> None:
        """Refactor check: complete_with_usage still retries as json_object."""
        message = SimpleNamespace(content='{"ok": true}')
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2),
            model="gemma-4-31B-it",
        )

        class _Completions:
            calls: list[dict] = []

            def create(self, **params):
                self.calls.append(params)
                if len(self.calls) == 1:
                    raise _SchemaRejected("400")
                return response

        completions = _Completions()
        client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        with patch.object(self.llm, "_client", lambda: client):
            text, usage = self.llm.complete_with_usage("S", "U", {"type": "object"})
        self.assertEqual(text, '{"ok": true}')
        self.assertEqual(usage["json_mode"], "json_object")
        self.assertNotIn("stream", completions.calls[0])
        self.assertNotIn("ttfb_s", usage)


# --------------------------------------------------------------------------- #
# result.partial as a TaskEvent: models, events payload, api coercion, thread
# --------------------------------------------------------------------------- #


class TestResultPartialEvent(unittest.TestCase):
    def test_model_and_payload(self) -> None:
        from server.events import _event_payload
        from server.models import ResultPartialEvent

        event = ResultPartialEvent(taskId="t1", whyDelta="Shop wins. ")
        self.assertEqual(
            _event_payload(event),
            {"type": "result.partial", "taskId": "t1", "whyDelta": "Shop wins. "},
        )

    def test_api_coerces_dict_and_instance(self) -> None:
        from server import api
        from server.models import ResultPartialEvent

        raw = {"type": "result.partial", "taskId": "t1", "whyDelta": "Hi"}
        coerced = api._coerce_task_event(raw)
        self.assertIsInstance(coerced, ResultPartialEvent)
        self.assertEqual(coerced.whyDelta, "Hi")
        self.assertIs(api._coerce_task_event(coerced), coerced)
        self.assertEqual(api._event_task_id(coerced, "fallback"), "t1")
        self.assertIsNone(api._coerce_task_event({"type": "result.partial", "taskId": "t1"}))

    def test_publisher_reaches_hub_from_a_worker_thread(self) -> None:
        """The orchestrator's sync streaming loop runs in a thread; the api
        publisher must hand result.partial to the loop without blocking."""
        from server import api

        async def main():
            published: list[tuple[str, object]] = []

            async def fake_publish(task_id, event):
                published.append((task_id, event))

            publisher = api._make_events_publisher("t1")
            payload = {"type": "result.partial", "taskId": "t1", "whyDelta": "Hi"}
            with patch.object(api.hub, "publish", fake_publish):
                await asyncio.to_thread(publisher, payload)
                await asyncio.to_thread(publisher, "t1", {**payload, "whyDelta": " there"})
                for _ in range(20):
                    if len(published) == 2:
                        break
                    await asyncio.sleep(0.01)
            return published

        published = asyncio.run(main())
        self.assertEqual([p[0] for p in published], ["t1", "t1"])
        self.assertEqual([p[1].whyDelta for p in published], ["Hi", " there"])


# --------------------------------------------------------------------------- #
# Orchestrator: result.partial events precede task.result
# --------------------------------------------------------------------------- #


class TestOrchestratorStreamsWhy(unittest.TestCase):
    def setUp(self) -> None:
        from server import orchestrator
        from server.models import Task

        self.orch = orchestrator
        self.task = Task.model_validate(
            {
                "id": "task_s8",
                "title": "Brake repair",
                "request": "2019 Camry brakes",
                "createdAt": "2026-09-19T14:02:00Z",
                "status": "running",
                "userQuote": 800,
                "agents": DECISION_AGENTS,
            }
        )
        for p in (
            patch("urllib.request.urlopen", _blocked),
            patch("server.dialer.run_dialer", return_value=[]),
            patch.object(orchestrator, "_db", None),
            patch.object(orchestrator, "_load_task", lambda _tid: self.task),
            patch("server.api.save_task", MagicMock()),
        ):
            p.start()
            self.addCleanup(p.stop)

    def _result_dict(self) -> dict:
        return {
            "options": [
                {
                    "label": "Bring your own part",
                    "total": 390,
                    "breakdown": "$140 part + 2.5 h x $100 labor",
                    "agentIds": ["w_partsgeek", "s_fremont"],
                },
                {
                    "label": "Shop supplies part",
                    "total": 480,
                    "breakdown": "All-in at Sam's Auto",
                    "agentIds": ["s_sams"],
                },
            ],
            "recommendedOptionIndex": 1,
            "recommendedAgentId": "s_sams",
            "why": VALID_DECISION["why"],
            "savingsVsQuote": 320,
        }

    def test_partial_events_then_result(self) -> None:
        deltas = ["Shop-supplied at Sam's Auto is $480", " against $390 bringing your own part."]

        def fake_synthesize(task, on_why_delta=None, **_kw):
            self.assertIsNotNone(on_why_delta)
            for d in deltas:
                on_why_delta(d)
            return self._result_dict()

        events: list[dict] = []
        with patch("server.synthesize.synthesize", side_effect=fake_synthesize) as mock:
            self.orch._run_calls_then_complete(self.task, events.append)

        self.assertEqual(mock.call_count, 1)
        self.assertIs(mock.call_args.args[0], self.task)
        types = [e["type"] for e in events]
        self.assertEqual(types, ["result.partial", "result.partial", "task.result", "task.updated"])
        self.assertEqual([e["whyDelta"] for e in events[:2]], deltas)
        self.assertTrue(all(e["taskId"] == "task_s8" for e in events[:2]))
        self.assertEqual(events[2]["taskId"], "task_s8")
        self.assertEqual(events[2]["result"]["why"], VALID_DECISION["why"])
        self.assertEqual(events[3]["task"]["status"], "complete")
        self.assertEqual(self.task.status, "complete")

    def test_no_partials_when_synthesize_never_streams(self) -> None:
        events: list[dict] = []
        with patch("server.synthesize.synthesize", return_value=self._result_dict()):
            self.orch._run_calls_then_complete(self.task, events.append)
        self.assertEqual([e["type"] for e in events], ["task.result", "task.updated"])


if __name__ == "__main__":
    unittest.main()
