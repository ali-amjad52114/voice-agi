"""Web agent (Session 3): query building, SerpAPI parsing, no network."""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

_SERVER = Path(__file__).resolve().parents[1]
_REPO = Path(__file__).resolve().parents[2]
for _p in (str(_REPO), str(_SERVER)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from server import web_agent  # noqa: E402
from server.models import Agent, Business, Task  # noqa: E402

CAMRY_REQUEST = "front brakes on my 2019 Camry"

# Canned SerpAPI google_shopping payload: three in range ($60–$600), one out
# of range ($12 clip), one with no price at all.
CANNED_SERPAPI = {
    "search_metadata": {"status": "Success"},
    "shopping_results": [
        {
            "position": 1,
            "title": "Genuine Toyota 2019 Camry Front Brake Pads and Rotors Kit",
            "source": "Toyota Parts Deal",
            "link": "https://example.test/toyota-kit",
            "price": "$186.00",
            "extracted_price": 186.0,
        },
        {
            "position": 2,
            "title": "Power Stop Z23 Front Brake Kit for Camry 2018-2022",
            "source": "Amazon",
            "link": "https://example.test/powerstop",
            "price": "$142.99",
            "extracted_price": 142.99,
        },
        {
            "position": 3,
            "title": "Front Brake Pad Retaining Clip",
            "source": "eBay",
            "link": "https://example.test/clip",
            "price": "$12.00",
            "extracted_price": 12.0,
        },
        {
            "position": 4,
            "title": "Bosch QuietCast Front Pads + Rotors Set Camry",
            "source": "RockAuto",
            "link": "https://example.test/bosch",
            "price": "$98.50",
            "extracted_price": 98.5,
        },
        {
            "position": 5,
            "title": "Camry front rotors (price on request)",
            "source": "Mystery Parts",
            "link": "https://example.test/noprice",
        },
    ],
}


def _blocked(*_args, **_kwargs):
    raise AssertionError("network is forbidden in web_agent tests")


class _FakeResponse(io.BytesIO):
    """Minimal stand-in for the object ``urlopen`` yields as a context manager."""

    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()
        return False


def _make_task(request: str = CAMRY_REQUEST, with_slot: bool = True) -> Task:
    agents = [
        Agent(
            id="a_call_1",
            taskId="t_1",
            kind="call",
            status="queued",
            business=Business(name="Fremont Auto Tech", type="mechanic", phone="+15550000000"),
        ),
        Agent(
            id="a_call_2",
            taskId="t_1",
            kind="call",
            status="queued",
            business=Business(name="Dealer", type="dealer", phone="+15550000001"),
        ),
    ]
    if with_slot:
        agents.append(
            Agent(
                id="a_web_slot",
                taskId="t_1",
                kind="web",
                status="queued",
                business=Business(name="Parts lookup", type="parts"),
            )
        )
    return Task(
        id="t_1",
        title="Brake repair",
        request=request,
        createdAt="2026-09-19T00:00:00Z",
        status="running",
        agents=agents,
    )


class _NoNetwork(unittest.TestCase):
    """Every test blocks the real socket path. Only explicit fakes answer."""

    def setUp(self) -> None:
        self._patches = [
            patch("urllib.request.urlopen", _blocked),
            patch.object(web_agent, "urlopen", _blocked),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(self._stop)

    def _stop(self) -> None:
        for p in reversed(self._patches):
            p.stop()


class TestBuildPartQuery(_NoNetwork):
    def test_camry_front_brakes(self) -> None:
        query = web_agent.build_part_query(CAMRY_REQUEST)
        self.assertIn("2019 Toyota Camry", query)
        self.assertIn("pads and rotors", query)
        self.assertIn("front", query)
        self.assertNotEqual(query, web_agent.DEFAULT_PART_QUERY)

    def test_front_only_when_spoken(self) -> None:
        query = web_agent.build_part_query("brakes on my 2019 Camry are squeaking")
        self.assertIn("2019 Toyota Camry", query)
        self.assertIn("pads and rotors", query)
        self.assertNotIn("front", query)

    def test_other_vehicle(self) -> None:
        query = web_agent.build_part_query("I need front brakes on a 2017 Accord")
        self.assertIn("2017 Honda Accord", query)
        self.assertNotIn("Camry", query)

    def test_fallback_without_vehicle(self) -> None:
        self.assertEqual(
            web_agent.build_part_query("my brakes are squeaking, help"),
            web_agent.DEFAULT_PART_QUERY,
        )
        self.assertEqual(web_agent.build_part_query(""), web_agent.DEFAULT_PART_QUERY)


class TestSerpApiSources(_NoNetwork):
    def _run_with_serpapi(self, payload: dict, task: Task | None = None):
        """Route every HTTP call through a recorder; SerpAPI gets ``payload``."""
        calls: list[str] = []

        def fake_http_text(url: str):
            calls.append(url)
            if url.startswith(web_agent.SERPAPI_ENDPOINT):
                return json.dumps(payload), None
            return None, "HTTP 403"

        with patch.dict(os.environ, {"SERPAPI_API_KEY": "test-key"}):
            with patch.object(web_agent, "_http_text", side_effect=fake_http_text):
                agents = web_agent.run_web_agent(task or _make_task())
        return agents, calls

    def test_in_range_results_become_separate_web_agents(self) -> None:
        agents, calls = self._run_with_serpapi(CANNED_SERPAPI)

        serp_calls = [u for u in calls if u.startswith(web_agent.SERPAPI_ENDPOINT)]
        self.assertEqual(len(serp_calls), 1, "exactly one SerpAPI call per task")
        self.assertEqual(len(calls), 1, "no HTTP fallback when SerpAPI answered")
        params = parse_qs(urlparse(serp_calls[0]).query)
        self.assertEqual(params["engine"], ["google_shopping"])
        self.assertIn("2019 Toyota Camry", params["q"][0])
        self.assertIn("pads and rotors", params["q"][0])

        self.assertEqual(len(agents), 3)
        self.assertTrue(all(isinstance(a, Agent) for a in agents))
        self.assertTrue(all(a.kind == "web" and a.status == "done" for a in agents))
        self.assertTrue(all(a.business.type == "parts" for a in agents))
        self.assertEqual([a.facts.partPrice for a in agents], [186.0, 142.99, 98.5])
        self.assertEqual(
            [a.business.name for a in agents], ["Toyota Parts Deal", "Amazon", "RockAuto"]
        )
        self.assertEqual(
            [a.business.url for a in agents],
            [
                "https://example.test/toyota-kit",
                "https://example.test/powerstop",
                "https://example.test/bosch",
            ],
        )
        self.assertTrue(all(a.facts.confidence == 0.75 for a in agents))
        self.assertTrue(all(a.facts.allInPrice is None for a in agents))
        self.assertTrue(all(a.taskId == "t_1" for a in agents))

    def test_oem_labeling(self) -> None:
        agents, _ = self._run_with_serpapi(CANNED_SERPAPI)
        self.assertEqual([a.facts.partsType for a in agents], ["oem", "aftermarket", "aftermarket"])
        self.assertEqual(agents[0].summary, "OEM pads + rotors $186")
        self.assertEqual(agents[1].summary, "Aftermarket pads + rotors $142.99")
        self.assertEqual(agents[2].summary, "Aftermarket pads + rotors $98.5")

    def test_parts_type_keywords(self) -> None:
        self.assertEqual(web_agent._parts_type("Genuine Toyota pads"), "oem")
        self.assertEqual(web_agent._parts_type("OEM Front Rotors"), "oem")
        self.assertEqual(web_agent._parts_type("Toyota 04465-33471"), "oem")
        self.assertEqual(web_agent._parts_type("Power Stop Z23 kit"), "aftermarket")
        self.assertEqual(web_agent._parts_type(""), "aftermarket")

    def test_first_agent_reuses_queued_slot_id(self) -> None:
        agents, _ = self._run_with_serpapi(CANNED_SERPAPI)
        self.assertEqual(agents[0].id, "a_web_slot")
        ids = [a.id for a in agents]
        self.assertEqual(len(set(ids)), 3)
        self.assertTrue(all(i.startswith("a_web_") for i in ids))

    def test_out_of_range_and_unpriced_are_never_used(self) -> None:
        agents, _ = self._run_with_serpapi(CANNED_SERPAPI)
        names = {a.business.name for a in agents}
        self.assertNotIn("eBay", names)
        self.assertNotIn("Mystery Parts", names)
        self.assertNotIn(12.0, [a.facts.partPrice for a in agents])

    def test_caps_at_three_sources(self) -> None:
        many = {
            "shopping_results": [
                {"title": f"Kit {i}", "source": f"Seller {i}", "link": f"https://example.test/{i}",
                 "extracted_price": 100.0 + i}
                for i in range(6)
            ]
        }
        agents, calls = self._run_with_serpapi(many)
        self.assertEqual(len(agents), 3)
        self.assertEqual(len(calls), 1)

    def test_zero_results_yields_one_failed_agent(self) -> None:
        agents, calls = self._run_with_serpapi({"shopping_results": []})
        self.assertEqual(len(agents), 1)
        failed = agents[0]
        self.assertEqual(failed.kind, "web")
        self.assertEqual(failed.status, "failed")
        self.assertIsNone(failed.facts)
        self.assertEqual(failed.id, "a_web_slot")
        self.assertIn("2019 Toyota Camry", failed.summary)
        self.assertIn("No part found online", failed.summary)
        # SerpAPI once, then the single HTTP fallback (which answered 403).
        self.assertEqual(len([u for u in calls if u.startswith(web_agent.SERPAPI_ENDPOINT)]), 1)
        self.assertLessEqual(len(calls), 2)

    def test_all_out_of_range_yields_failed_agent(self) -> None:
        junk = {
            "shopping_results": [
                {"title": "Clip", "source": "eBay", "extracted_price": 5.0},
                {"title": "Full caliper set", "source": "Shop", "extracted_price": 1200.0},
            ]
        }
        agents, _ = self._run_with_serpapi(junk)
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0].status, "failed")
        self.assertIsNone(agents[0].facts)

    def test_serpapi_error_payload_yields_failed_agent(self) -> None:
        agents, _ = self._run_with_serpapi({"error": "Your account has run out of searches."})
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0].status, "failed")
        self.assertIsNone(agents[0].facts)
        self.assertIn("run out of searches", agents[0].summary)

    def test_single_agent_helper_keeps_old_shape(self) -> None:
        with patch.dict(os.environ, {"SERPAPI_API_KEY": "test-key"}):
            with patch.object(
                web_agent, "_http_text", return_value=(json.dumps(CANNED_SERPAPI), None)
            ):
                agent = web_agent.run_web_agent_single(_make_task())
        self.assertIsInstance(agent, Agent)
        self.assertEqual(agent.facts.partPrice, 186.0)


class TestHttpFallbackAndTransport(_NoNetwork):
    def test_no_key_uses_single_http_fallback(self) -> None:
        calls: list[str] = []

        def fake_http_text(url: str):
            calls.append(url)
            return '<script type="application/ld+json">{"price": "89.99"}</script>', None

        with patch.dict(os.environ, {"SERPAPI_API_KEY": ""}):
            with patch.object(web_agent, "_http_text", side_effect=fake_http_text):
                agents = web_agent.run_web_agent(_make_task())

        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0].startswith(web_agent.SERPAPI_ENDPOINT))
        self.assertIn("2019+Toyota+Camry", calls[0])
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0].status, "done")
        self.assertEqual(agents[0].facts.partPrice, 89.99)
        self.assertEqual(agents[0].facts.partsType, "aftermarket")
        self.assertEqual(agents[0].business.name, "Advance Auto Parts")

    def test_transport_layer_never_opens_a_real_socket(self) -> None:
        """Drive the real ``_http_text`` through a fake ``urlopen``."""
        seen: list[str] = []

        def fake_urlopen(req, timeout=None):
            seen.append(req.full_url)
            return _FakeResponse(json.dumps(CANNED_SERPAPI).encode("utf-8"))

        with patch.dict(os.environ, {"SERPAPI_API_KEY": "test-key"}):
            with patch.object(web_agent, "urlopen", fake_urlopen):
                agents = web_agent.run_web_agent(_make_task())

        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].startswith(web_agent.SERPAPI_ENDPOINT))
        self.assertEqual(len(agents), 3)
        self.assertEqual(agents[0].facts.partPrice, 186.0)

    def test_blocked_network_is_reported_not_swallowed(self) -> None:
        """If anything reaches the real urlopen the guard raises, proving no network."""
        with patch.dict(os.environ, {"SERPAPI_API_KEY": "test-key"}):
            with self.assertRaises(AssertionError):
                web_agent.run_web_agent(_make_task())


class TestOrchestratorPlumbing(_NoNetwork):
    def setUp(self) -> None:
        super().setUp()
        from server import orchestrator

        self.orch = orchestrator

    def test_replace_web_agent_swaps_slot_and_keeps_calls(self) -> None:
        task = _make_task()
        web = [
            Agent(id="a_web_slot", taskId="t_1", kind="web", status="done",
                  business=Business(name="Toyota Parts Deal", type="parts")),
            Agent(id="a_web_x", taskId="t_1", kind="web", status="done",
                  business=Business(name="Amazon", type="parts")),
            Agent(id="a_web_y", taskId="t_1", kind="web", status="done",
                  business=Business(name="RockAuto", type="parts")),
        ]
        self.orch._replace_web_agent(task, web)
        self.assertEqual(
            [a.id for a in task.agents],
            ["a_call_1", "a_call_2", "a_web_slot", "a_web_x", "a_web_y"],
        )
        self.assertEqual([a.kind for a in task.agents if a.kind == "call"], ["call", "call"])
        self.assertEqual(task.agents[0].business.name, "Fremont Auto Tech")
        self.assertEqual(task.agents[1].business.name, "Dealer")

    def test_replace_web_agent_appends_when_no_slot(self) -> None:
        task = _make_task(with_slot=False)
        web = [Agent(id="a_web_n", taskId="t_1", kind="web", status="failed",
                     business=Business(name="Parts lookup", type="parts"))]
        self.orch._replace_web_agent(task, web)
        self.assertEqual([a.id for a in task.agents], ["a_call_1", "a_call_2", "a_web_n"])

    def test_spawn_web_agent_returns_list_without_touching_calls(self) -> None:
        task = _make_task()
        before = [a.model_dump() for a in task.agents if a.kind == "call"]

        with patch.dict(os.environ, {"SERPAPI_API_KEY": "test-key"}):
            with patch.object(
                web_agent, "_http_text", return_value=(json.dumps(CANNED_SERPAPI), None)
            ):
                with patch.object(self.orch, "_persist_agent", lambda _a: None):
                    result = self.orch._spawn_web_agent(task)

        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 3)
        self.assertTrue(all(a.kind == "web" for a in result))
        self.assertTrue(all(a.facts.allInPrice is None for a in result))
        after = [a.model_dump() for a in task.agents if a.kind == "call"]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
