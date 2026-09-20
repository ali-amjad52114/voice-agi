"""Session 9 tool loop: scripted model, fake tools, no network, no SerpAPI."""

from __future__ import annotations

import json
import sys
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import patch

_SERVER = Path(__file__).resolve().parents[1]
_REPO = Path(__file__).resolve().parents[2]
for _p in (str(_REPO), str(_SERVER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from server import discovery, gc_loop, gc_usage, web_agent  # noqa: E402
from server.models import Agent, Business, Task  # noqa: E402

CAMRY_REQUEST = (
    "One mechanic quoted me $800 for front brakes on my 2019 Camry, I'm in Fremont. "
    "Find the part price online, call mechanics and dealers near me."
)
PLAN = {
    "title": "Brake repair",
    "vehicle": "2019 Camry",
    "location": "Fremont",
    "factsNeeded": ["allInPrice", "laborRatePerHour"],
    "businessCount": 8,
}
LAT, LNG = 37.5485, -121.9886

THREE_SHOPS = [
    {"name": "Fremont Brake Pros", "phone": "+15105550101", "url": "https://fbp.test", "type": "mechanic"},
    {"name": "Bay Toyota", "phone": "+15105550102", "url": None, "type": "dealer"},
    {"name": "Mission Auto Care", "phone": "+15105550103", "url": "https://mac.test", "type": "mechanic"},
]
ONE_PART = [
    {"seller": "RockAuto", "url": "https://rockauto.test/kit", "price": 98.5, "partsType": "aftermarket"},
]

HALLUCINATED_FINAL = (
    "Found 3 shops and 1 part; also Ninth Shop at +15105559999 has a $50 part."
)


def _blocked(*_args, **_kwargs):
    raise AssertionError("network is forbidden in gc_loop tests")


def _tool_call(name: str, arguments: dict, call_id: str = "call_1") -> dict:
    """One OpenAI-shaped assistant message with a single function call."""
    return {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)},
                        }
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 20},
        "model": "gemma-4-31B-it",
    }


def _text(content: str) -> dict:
    return {
        "choices": [{"message": {"content": content, "tool_calls": None}}],
        "usage": {"prompt_tokens": 200, "completion_tokens": 30},
        "model": "gemma-4-31B-it",
    }


class _Scripted:
    """``complete`` stand-in that replays responses and records the messages it saw."""

    def __init__(self, responses: list, *, repeat_last: bool = False) -> None:
        self.responses = list(responses)
        self.repeat_last = repeat_last
        self.seen: list[list[dict]] = []

    def __call__(self, messages: list[dict]) -> dict:
        self.seen.append([dict(m) for m in messages])
        if not self.responses:
            raise AssertionError("scripted model ran out of responses")
        if self.repeat_last and len(self.responses) == 1:
            return self.responses[0]
        return self.responses.pop(0)


class _FakeTools:
    def __init__(self, shops: list[dict], parts: list[dict]) -> None:
        self.shops = shops
        self.parts = parts
        self.discover_calls: list[tuple] = []
        self.lookup_calls: list[str] = []

    def discover_shops(self, lat: float, lng: float, query: str) -> list[dict]:
        self.discover_calls.append((lat, lng, query))
        return [dict(s) for s in self.shops]

    def lookup_part(self, query: str) -> list[dict]:
        self.lookup_calls.append(query)
        return [dict(p) for p in self.parts]

    def mapping(self) -> dict:
        return {"discover_shops": self.discover_shops, "lookup_part": self.lookup_part}


class _NoNetwork(unittest.TestCase):
    def setUp(self) -> None:
        gc_usage.clear()
        for target in (
            patch("urllib.request.urlopen", _blocked),
            patch.object(gc_loop, "_client", side_effect=_blocked),
            patch.object(discovery, "_serpapi_maps_once", side_effect=_blocked),
            patch.object(web_agent, "_http_text", side_effect=_blocked),
        ):
            target.start()
            self.addCleanup(target.stop)


class TestHappyPath(_NoNetwork):
    def _run(self):
        model = _Scripted(
            [
                _tool_call("discover_shops", {"lat": LAT, "lng": LNG, "query": "brake repair 2019 Camry Fremont"}),
                _tool_call("lookup_part", {"query": "2019 Camry front brake pads and rotors"}, "call_2"),
                _text(HALLUCINATED_FINAL),
            ]
        )
        tools = _FakeTools(THREE_SHOPS, ONE_PART)
        result = gc_loop.run_gc_loop(
            CAMRY_REQUEST, PLAN, LAT, LNG, task_id="t_1", tools=tools.mapping(), complete=model
        )
        return result, model, tools

    def test_shops_and_parts_are_exactly_the_tool_output(self) -> None:
        result, _model, _tools = self._run()

        self.assertFalse(result.fell_back)
        self.assertEqual(result.steps, 3)
        self.assertEqual(result.shops, THREE_SHOPS)
        self.assertEqual(result.parts, ONE_PART)
        self.assertEqual(result.final_message, HALLUCINATED_FINAL)
        # The hallucinated shop and dollar live only in the prose, never in data.
        blob = json.dumps({"shops": result.shops, "parts": result.parts})
        self.assertNotIn("Ninth Shop", blob)
        self.assertNotIn("5559999", blob)
        self.assertNotIn("50", [str(p["price"]) for p in result.parts])

    def test_tool_calls_are_recorded_with_result_counts(self) -> None:
        result, _model, tools = self._run()

        self.assertEqual(
            [(c["name"], c["result_count"]) for c in result.tool_calls],
            [("discover_shops", 3), ("lookup_part", 1)],
        )
        self.assertEqual(len(tools.discover_calls), 1)
        self.assertEqual(tools.discover_calls[0][:2], (LAT, LNG))
        self.assertEqual(tools.lookup_calls, ["2019 Camry front brake pads and rotors"])

    def test_messages_carry_tool_results_back_to_the_model(self) -> None:
        _result, model, _tools = self._run()

        self.assertEqual(len(model.seen), 3)
        first = model.seen[0]
        self.assertEqual([m["role"] for m in first], ["system", "user"])
        self.assertIn("discover_shops", first[0]["content"])
        user = json.loads(first[1]["content"])
        self.assertEqual(user["lat"], LAT)
        self.assertEqual(user["plan"]["vehicle"], "2019 Camry")
        self.assertEqual(user["plan"]["city"], "Fremont")

        third = model.seen[2]
        roles = [m["role"] for m in third]
        self.assertEqual(roles, ["system", "user", "assistant", "tool", "assistant", "tool"])
        self.assertEqual(third[2]["tool_calls"][0]["function"]["name"], "discover_shops")
        self.assertEqual(third[3]["tool_call_id"], "call_1")
        self.assertEqual(json.loads(third[3]["content"])["results"], THREE_SHOPS)
        self.assertEqual(third[5]["tool_call_id"], "call_2")
        self.assertEqual(json.loads(third[5]["content"])["results"], ONE_PART)

    def test_usage_is_recorded_per_step(self) -> None:
        result, _model, _tools = self._run()

        self.assertEqual(len(result.usage), 3)
        self.assertEqual([u["step"] for u in result.usage], [1, 2, 3])
        self.assertEqual(result.usage[0]["tool_calls"], ["discover_shops"])
        self.assertEqual(result.usage[0]["prompt_tokens"], 120)
        ledger = gc_usage.recent("t_1")
        self.assertEqual(len(ledger), 3)
        self.assertTrue(all(e["stage"] == "gc_loop" for e in ledger))

    def test_model_coordinates_are_ignored_for_the_task_coordinates(self) -> None:
        model = _Scripted(
            [
                _tool_call("discover_shops", {"lat": 40.7, "lng": -74.0, "query": "brake repair"}),
                _text("done"),
            ]
        )
        tools = _FakeTools(THREE_SHOPS, ONE_PART)
        gc_loop.run_gc_loop(CAMRY_REQUEST, PLAN, LAT, LNG, tools=tools.mapping(), complete=model)

        self.assertEqual(tools.discover_calls[0][:2], (LAT, LNG))


class TestFallbacks(_NoNetwork):
    def test_step_cap_sets_fell_back_and_keeps_what_was_gathered(self) -> None:
        model = _Scripted(
            [_tool_call("discover_shops", {"lat": LAT, "lng": LNG, "query": "brake repair"})],
            repeat_last=True,
        )
        tools = _FakeTools(THREE_SHOPS, ONE_PART)
        result = gc_loop.run_gc_loop(
            CAMRY_REQUEST, PLAN, LAT, LNG, max_steps=4, tools=tools.mapping(), complete=model
        )

        self.assertTrue(result.fell_back)
        self.assertEqual(result.steps, 4)
        self.assertIsNone(result.final_message)
        self.assertEqual(len(model.seen), 4)
        self.assertEqual(result.shops, THREE_SHOPS, "same three shops, deduped across repeats")
        self.assertEqual(result.parts, [])

    def test_api_exception_on_step_one_falls_back_empty(self) -> None:
        def boom(_messages):
            raise RuntimeError("General Compute 502")

        tools = _FakeTools(THREE_SHOPS, ONE_PART)
        result = gc_loop.run_gc_loop(CAMRY_REQUEST, PLAN, LAT, LNG, tools=tools.mapping(), complete=boom)

        self.assertTrue(result.fell_back)
        self.assertEqual(result.steps, 0)
        self.assertEqual(result.shops, [])
        self.assertEqual(result.parts, [])
        self.assertEqual(result.tool_calls, [])
        self.assertEqual(tools.discover_calls, [])

    def test_api_exception_mid_loop_keeps_earlier_tool_output(self) -> None:
        calls = {"n": 0}

        def flaky(_messages):
            calls["n"] += 1
            if calls["n"] == 1:
                return _tool_call("discover_shops", {"lat": LAT, "lng": LNG, "query": "brake repair"})
            raise TimeoutError("45 s")

        tools = _FakeTools(THREE_SHOPS, ONE_PART)
        result = gc_loop.run_gc_loop(CAMRY_REQUEST, PLAN, LAT, LNG, tools=tools.mapping(), complete=flaky)

        self.assertTrue(result.fell_back)
        self.assertEqual(result.steps, 1)
        self.assertEqual(result.shops, THREE_SHOPS)

    def test_tool_exception_is_fed_back_not_raised(self) -> None:
        def broken(*_a, **_k):
            raise RuntimeError("SERPAPI_API_KEY is not set")

        model = _Scripted(
            [
                _tool_call("discover_shops", {"lat": LAT, "lng": LNG, "query": "brake repair"}),
                _text("No shops were returned."),
            ]
        )
        result = gc_loop.run_gc_loop(
            CAMRY_REQUEST, PLAN, LAT, LNG,
            tools={"discover_shops": broken, "lookup_part": lambda q: []},
            complete=model,
        )

        self.assertFalse(result.fell_back)
        self.assertEqual(result.shops, [])
        self.assertEqual(result.tool_calls[0]["result_count"], 0)
        self.assertIn("SERPAPI", result.tool_calls[0]["error"])
        tool_msg = model.seen[1][3]
        self.assertEqual(tool_msg["role"], "tool")
        self.assertIn("SERPAPI", json.loads(tool_msg["content"])["error"])

    def test_unknown_tool_is_reported_and_loop_continues(self) -> None:
        model = _Scripted([_tool_call("book_appointment", {"when": "now"}), _text("ok")])
        result = gc_loop.run_gc_loop(CAMRY_REQUEST, PLAN, LAT, LNG, tools={}, complete=model)

        self.assertFalse(result.fell_back)
        self.assertEqual(result.steps, 2)
        self.assertIn("unknown tool", result.tool_calls[0]["error"])


class TestRealClientPath(unittest.TestCase):
    """The default ``complete``: params handed to the OpenAI client, object-style response."""

    def test_api_params_and_object_response(self) -> None:
        from types import SimpleNamespace

        seen: list[dict] = []
        replies = [
            SimpleNamespace(
                model="gemma-4-31B-it",
                usage=SimpleNamespace(prompt_tokens=90, completion_tokens=12),
                choices=[SimpleNamespace(message=SimpleNamespace(
                    content=None,
                    tool_calls=[SimpleNamespace(
                        id="chatcmpl-tool-1", type="function",
                        function=SimpleNamespace(name="lookup_part", arguments='{"query": "2019 Camry front brake pads and rotors"}'),
                    )],
                ))],
            ),
            SimpleNamespace(
                model="gemma-4-31B-it",
                usage=SimpleNamespace(prompt_tokens=140, completion_tokens=9),
                choices=[SimpleNamespace(message=SimpleNamespace(content="Found 1 part.", tool_calls=None))],
            ),
        ]

        def create(**kwargs):
            seen.append(dict(kwargs, messages=[dict(m) for m in kwargs["messages"]]))
            return replies.pop(0)

        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        tools = _FakeTools(THREE_SHOPS, ONE_PART)
        with patch.object(gc_loop, "_client", return_value=fake_client):
            with patch.dict("os.environ", {"GENERAL_COMPUTE_MODEL": "gemma-4-31B-it"}):
                result = gc_loop.run_gc_loop(CAMRY_REQUEST, PLAN, LAT, LNG, tools=tools.mapping())

        self.assertFalse(result.fell_back)
        self.assertEqual(result.steps, 2)
        self.assertEqual(result.parts, ONE_PART)
        self.assertEqual(result.final_message, "Found 1 part.")
        self.assertEqual(result.usage[0]["prompt_tokens"], 90)

        first = seen[0]
        self.assertEqual(first["model"], "gemma-4-31B-it")
        self.assertIs(first["tools"], gc_loop.TOOLS)
        self.assertEqual(first["tool_choice"], "auto")
        self.assertEqual(first["temperature"], 0.1)
        self.assertEqual(first["max_tokens"], 300)
        self.assertNotIn("stream_options", first)
        second_roles = [m["role"] for m in seen[1]["messages"]]
        self.assertEqual(second_roles, ["system", "user", "assistant", "tool"])
        self.assertEqual(seen[1]["messages"][3]["tool_call_id"], "chatcmpl-tool-1")


class TestGuardrails(unittest.TestCase):
    def test_validate_against_tools_drops_unknown_shop(self) -> None:
        candidates = [
            {"name": "Fremont Brake Pros", "phone": "(510) 555-0101", "type": "mechanic"},
            {"name": "bay toyota", "phone": "", "type": "dealer", "allInPrice": 480},
            {"name": "Ninth Shop", "phone": "+15105559999", "type": "mechanic"},
            {"name": "Mission Auto Care", "phone": "+15105550103"},
            {"name": "Mission Auto Care", "phone": "+15105550103"},
        ]
        kept = gc_loop.validate_against_tools(candidates, THREE_SHOPS)

        self.assertEqual([s["name"] for s in kept], ["Fremont Brake Pros", "Bay Toyota", "Mission Auto Care"])
        self.assertEqual(kept[0], THREE_SHOPS[0], "tool record wins over the candidate")
        self.assertNotIn("allInPrice", kept[1])
        self.assertTrue(all("Ninth" not in s["name"] for s in kept))

    def test_validate_against_tools_with_no_tool_output_keeps_nothing(self) -> None:
        self.assertEqual(gc_loop.validate_against_tools(THREE_SHOPS, []), [])

    def test_validate_prices_drops_unknown_price(self) -> None:
        candidates = [
            {"seller": "RockAuto", "price": "$98.50", "partsType": "aftermarket"},
            {"seller": "RockAuto", "price": 50.0},
            {"seller": "Ninth Parts", "price": 98.5},
            {"seller": "rockauto", "price": 98.5, "url": "https://model-made-this-up.test"},
        ]
        kept = gc_loop.validate_prices(candidates, ONE_PART)

        self.assertEqual(kept, ONE_PART)

    def test_query_builders(self) -> None:
        self.assertEqual(gc_loop.shop_query(PLAN, CAMRY_REQUEST), "brake repair 2019 Camry Fremont")
        self.assertEqual(gc_loop.part_query(PLAN, CAMRY_REQUEST), "2019 Camry front brake pads and rotors")
        self.assertEqual(gc_loop.part_query({"vehicle": "2015 Civic"}, "rear brakes"), "2015 Civic rear brake pads and rotors")
        self.assertEqual(gc_loop.shop_query({"vehicle": "2015 Civic", "city": "Oakland"}, "oil change"), "car repair 2015 Civic Oakland")

    def test_tools_schema_shape(self) -> None:
        names = [t["function"]["name"] for t in gc_loop.TOOLS]
        self.assertEqual(names, ["discover_shops", "lookup_part"])
        self.assertTrue(all(t["type"] == "function" for t in gc_loop.TOOLS))
        discover = gc_loop.TOOLS[0]["function"]["parameters"]
        self.assertEqual(discover["required"], ["lat", "lng", "query"])
        self.assertIn("ONLY allowed source", gc_loop.TOOLS[0]["function"]["description"])
        self.assertIn("ONLY allowed source", gc_loop.TOOLS[1]["function"]["description"])


class TestWrappers(_NoNetwork):
    def test_discover_shops_tool_returns_only_allowed_keys(self) -> None:
        hits = [
            {"name": "Fremont Brake Pros", "phone": "+15105550101", "address": "1 Main St",
             "url": "https://fbp.test", "type": "mechanic", "price": 999},
            {"name": "Bay Toyota", "phone": "+15105550102", "address": "2 Auto Mall", "url": "", "type": "dealer"},
            {"name": "No Phone", "phone": "", "address": "", "url": "", "type": "mechanic"},
        ]
        with patch.object(discovery, "discover_shops", return_value=hits) as fn:
            rows = discovery.discover_shops_tool(LAT, LNG, "brake repair 2019 Camry Fremont")

        fn.assert_called_once_with(LAT, LNG, "brake repair 2019 Camry Fremont")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(set(r) == set(discovery.TOOL_SHOP_KEYS) for r in rows))
        self.assertEqual(rows[0], {"name": "Fremont Brake Pros", "phone": "+15105550101", "url": "https://fbp.test", "type": "mechanic"})
        self.assertIsNone(rows[1]["url"])

    def test_discover_shops_tool_respects_limit(self) -> None:
        hits = [{"name": f"Shop {i}", "phone": f"+1510555010{i}", "url": "", "type": "mechanic"} for i in range(6)]
        with patch.object(discovery, "discover_shops", return_value=hits):
            rows = discovery.discover_shops_tool(LAT, LNG, "brake repair", limit=2)
        self.assertEqual(len(rows), 2)

    def test_lookup_part_tool_returns_only_allowed_keys(self) -> None:
        hits = [
            (186.0, "Toyota Parts Deal", "https://example.test/toyota-kit", "oem"),
            (98.5, "RockAuto", None, "aftermarket"),
        ]
        with patch.object(web_agent, "_lookup_part_prices", return_value=(hits, None)) as fn:
            rows = web_agent.lookup_part_tool("2019 Camry front brake pads and rotors")

        fn.assert_called_once_with("2019 Camry front brake pads and rotors")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(set(r) == set(web_agent.TOOL_PART_KEYS) for r in rows))
        self.assertEqual(rows[0], {"seller": "Toyota Parts Deal", "url": "https://example.test/toyota-kit", "price": 186.0, "partsType": "oem"})
        self.assertIsNone(rows[1]["url"])
        self.assertIsInstance(rows[1]["price"], float)

    def test_lookup_part_tool_returns_empty_when_nothing_found(self) -> None:
        with patch.object(web_agent, "_lookup_part_prices", return_value=([], "no in-range shopping price")):
            self.assertEqual(web_agent.lookup_part_tool("2019 Camry front brake pads and rotors"), [])

    def test_lookup_part_tool_drops_out_of_range_price(self) -> None:
        hits = [(12.0, "eBay", "https://example.test/clip", "aftermarket")]
        with patch.object(web_agent, "_lookup_part_prices", return_value=(hits, None)):
            self.assertEqual(web_agent.lookup_part_tool("clip"), [])

    def test_default_tools_map_to_the_wrappers(self) -> None:
        tools = gc_loop._default_tools()
        self.assertIs(tools["discover_shops"], discovery.discover_shops_tool)
        self.assertIs(tools["lookup_part"], web_agent.lookup_part_tool)


def _make_task(agents: list[Agent] | None = None) -> Task:
    return Task(
        id="t_camry",
        title="Brake repair",
        request=CAMRY_REQUEST,
        createdAt="2026-09-19T00:00:00Z",
        status="running",
        agents=agents or [],
    )


class TestOrchestratorShape(_NoNetwork):
    def test_plan_agents_via_gc_returns_run_discover_shape(self) -> None:
        model = _Scripted(
            [
                _tool_call("discover_shops", {"lat": LAT, "lng": LNG, "query": "brake repair"}),
                _tool_call("lookup_part", {"query": "2019 Camry front brake pads and rotors"}, "call_2"),
                _text("Found 3 shops and 1 part."),
            ]
        )
        tools = _FakeTools(THREE_SHOPS, ONE_PART)
        loc = {"lat": LAT, "lng": LNG, "label": "Fremont"}
        out = gc_loop.plan_agents_via_gc(_make_task(), dict(PLAN, businessCount=2), loc, tools=tools.mapping(), complete=model)

        self.assertIsNotNone(out)
        shops, parts = out
        self.assertEqual(len(shops), 2, "capped by plan.businessCount like _run_discover")
        for shop in shops:
            self.assertEqual(set(shop), {"name", "phone", "url", "type"})
            self.assertIn(shop["type"], ("mechanic", "dealer", "parts"))
        self.assertIsNone(shops[1]["url"])
        self.assertEqual(parts, ONE_PART)

    def test_plan_agents_via_gc_returns_none_when_nothing_gathered(self) -> None:
        def boom(_messages):
            raise RuntimeError("offline")

        loc = {"lat": LAT, "lng": LNG}
        self.assertIsNone(gc_loop.plan_agents_via_gc(_make_task(), PLAN, loc, complete=boom))

    def test_web_agents_from_parts_matches_run_web_agent_shape(self) -> None:
        slot = Agent(id="a_web_slot", taskId="t_camry", kind="web", status="queued",
                     business=Business(name="Parts lookup", type="parts"))
        parts = ONE_PART + [{"seller": "Toyota Parts Deal", "url": "https://tpd.test", "price": 186.0, "partsType": "oem"}]
        agents = gc_loop.web_agents_from_parts(_make_task([slot]), parts)

        self.assertEqual(len(agents), 2)
        self.assertEqual(agents[0].id, "a_web_slot", "first agent reuses the queued slot id")
        self.assertTrue(all(a.kind == "web" and a.status == "done" for a in agents))
        self.assertTrue(all(a.business.type == "parts" for a in agents))
        self.assertEqual(agents[0].business.name, "RockAuto")
        self.assertEqual(agents[0].facts.partPrice, 98.5)
        self.assertEqual(agents[0].summary, "Aftermarket pads + rotors $98.5")
        self.assertEqual(agents[1].facts.partsType, "oem")
        self.assertEqual(agents[1].summary, "OEM pads + rotors $186")
        self.assertIsNone(agents[0].facts.allInPrice)


class TestOrchestratorWiring(_NoNetwork):
    """``on_task_created`` prefers the tool loop and falls back to the direct path."""

    TWO_SHOPS = THREE_SHOPS[:2]

    def setUp(self) -> None:
        super().setUp()
        from server import orchestrator
        from server.models import Location

        self.orch = orchestrator
        self.task = _make_task()
        self.discover = unittest.mock.MagicMock(return_value=[THREE_SHOPS[2]])
        self.spawn_web = unittest.mock.MagicMock(side_effect=lambda t: [_old_web_agent(t)])
        for target in (
            patch.object(orchestrator, "_db", None),
            patch.object(orchestrator, "_load_task", return_value=self.task),
            patch.object(orchestrator, "_run_plan", return_value=dict(PLAN)),
            patch.object(orchestrator, "_run_resolve_location", return_value=Location(lat=LAT, lng=LNG, label="Fremont")),
            patch.object(orchestrator, "_persist_task_fields", lambda task: None),
            patch.object(orchestrator, "_persist_agent", lambda agent: None),
            patch.object(orchestrator, "_run_calls_then_complete", lambda task, events: None),
            patch.object(orchestrator, "_run_discover", self.discover),
            patch.object(orchestrator, "_spawn_web_agent", self.spawn_web),
        ):
            target.start()
            self.addCleanup(target.stop)

    def test_gc_result_becomes_the_agents_and_discover_is_skipped(self) -> None:
        planner = unittest.mock.MagicMock(return_value=([dict(s) for s in self.TWO_SHOPS], [dict(ONE_PART[0])]))
        with patch.object(gc_loop, "plan_agents_via_gc", planner):
            steps = self.orch.on_task_created(self.task)

        planner.assert_called_once()
        called_task, called_plan, called_loc = planner.call_args.args
        self.assertIs(called_task, self.task)
        self.assertEqual(called_plan["vehicle"], "2019 Camry")
        self.assertEqual((called_loc.lat, called_loc.lng), (LAT, LNG))
        self.discover.assert_not_called()
        self.spawn_web.assert_not_called()
        self.assertIn("discover_gc", steps)
        self.assertNotIn("discover", steps)
        self.assertIn("dial_extract_complete", steps)

        calls = [a for a in self.task.agents if a.kind == "call"]
        self.assertEqual(
            [(a.business.name, a.business.phone, a.business.type) for a in calls],
            [(s["name"], s["phone"], s["type"]) for s in self.TWO_SHOPS],
        )
        self.assertTrue(all(a.status == "queued" and a.facts is None for a in calls))
        web = [a for a in self.task.agents if a.kind == "web"]
        self.assertEqual(len(web), 1)
        self.assertEqual(web[0].business.name, "RockAuto")
        self.assertEqual(web[0].facts.partPrice, 98.5)
        self.assertEqual(web[0].status, "done")
        self.assertEqual(self.task.status, "running")

    def test_gc_none_runs_the_old_path(self) -> None:
        planner = unittest.mock.MagicMock(return_value=None)
        with patch.object(gc_loop, "plan_agents_via_gc", planner):
            steps = self.orch.on_task_created(self.task)

        planner.assert_called_once()
        self.discover.assert_called_once()
        self.spawn_web.assert_called_once()
        self.assertIn("discover", steps)
        self.assertNotIn("discover_gc", steps)

        calls = [a for a in self.task.agents if a.kind == "call"]
        self.assertEqual([a.business.name for a in calls], ["Mission Auto Care"])
        web = [a for a in self.task.agents if a.kind == "web"]
        self.assertEqual([a.business.name for a in web], ["Old path parts"])

    def test_gc_exception_runs_the_old_path(self) -> None:
        with patch.object(gc_loop, "plan_agents_via_gc", side_effect=RuntimeError("boom")):
            steps = self.orch.on_task_created(self.task)

        self.discover.assert_called_once()
        self.spawn_web.assert_called_once()
        self.assertIn("discover", steps)
        self.assertEqual(self.task.status, "running")


def _old_web_agent(task: Task) -> Agent:
    return Agent(
        id="a_web_old",
        taskId=task.id,
        kind="web",
        status="done",
        business=Business(name="Old path parts", type="parts"),
        summary="Aftermarket pads + rotors $120",
    )


if __name__ == "__main__":
    unittest.main()
