from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock

from app.core.exceptions import ModelInvocationError
from app.services.agent.router import AgentRouter
from app.services.tools.web_search import WebSearchService


class WebSearchServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = SimpleNamespace(
            search_provider="tavily",
            tavily_api_key="configured",
            search_max_queries=3,
            search_max_citations=10,
            tavily_max_results_per_query=5,
        )
        self.service = WebSearchService(self.settings)

    def test_general_search_does_not_force_recent_news_filter(self) -> None:
        plan = self.service._normalize_search_plan(
            "movie release and cast",
            {
                "queries": ["movie release date", "movie cast"],
                "topic": "general",
                "time_range": None,
            },
        )
        payload = self.service._build_tavily_request_payload(
            plan["queries"][0],
            plan,
        )

        self.assertEqual(payload["topic"], "general")
        self.assertNotIn("time_range", payload)
        self.assertEqual(payload["search_depth"], "advanced")
        self.assertEqual(payload["max_results"], 5)

    def test_merges_duplicate_urls_and_rewards_cross_query_matches(self) -> None:
        responses = [
            {
                "results": [
                    {
                        "title": "Official announcement",
                        "url": "https://example.com/item?utm_source=test",
                        "content": "Release date information",
                        "score": 0.8,
                    },
                    {
                        "title": "Unrelated",
                        "url": "https://other.example/a",
                        "content": "Other result",
                        "score": 0.9,
                    },
                ]
            },
            {
                "results": [
                    {
                        "title": "Official announcement",
                        "url": "https://example.com/item",
                        "content": "Release date and cast information with more detail",
                        "score": 0.85,
                    }
                ]
            },
        ]

        results = self.service._merge_and_rank_results(
            responses,
            ["release date", "cast"],
            limit=10,
        )

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["source_id"], "S1")
        self.assertEqual(results[0]["url"], "https://example.com/item?utm_source=test")
        self.assertEqual(results[0]["matched_queries"], ["release date", "cast"])
        self.assertIn("more detail", results[0]["snippet"])


class AgentSearchPlanningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        settings = SimpleNamespace(
            deepseek_base_url="https://api.example.com",
            default_model="test-model",
        )
        self.router = AgentRouter(settings, MagicMock())

    async def test_model_plan_splits_compound_question(self) -> None:
        self.router.llm_client.complete_json = AsyncMock(
            return_value={
                "content": {
                    "queries": ["movie release date", "movie cast"],
                    "topic": "general",
                    "time_range": None,
                    "reason": "compound question",
                }
            }
        )

        plan = await self.router._plan_search(
            "When will the movie launch and who is in it?",
            now=datetime.now().astimezone(),
        )

        self.assertEqual(plan["queries"], ["movie release date", "movie cast"])
        self.assertEqual(plan["topic"], "general")
        self.assertIsNone(plan["time_range"])

    async def test_planner_failure_falls_back_without_recent_filter(self) -> None:
        self.router.llm_client.complete_json = AsyncMock(
            side_effect=ModelInvocationError("planning failed")
        )

        plan = await self.router._plan_search(
            "original question",
            now=datetime.now().astimezone(),
        )

        self.assertEqual(plan["queries"], ["original question"])
        self.assertEqual(plan["topic"], "general")
        self.assertIsNone(plan["time_range"])

    async def test_generates_and_normalizes_model_session_title(self) -> None:
        self.router.llm_client.complete_json = AsyncMock(
            return_value={"content": {"title": "  About Search Accuracy!  "}}
        )

        title = await self.router.generate_session_title(
            user_message="question",
            assistant_answer="answer",
        )

        self.assertEqual(title, "About Search Accuracy")
        self.router.llm_client.complete_json.assert_awaited_once()


class WebSearchPartialFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_keeps_successful_results_when_one_query_fails(self) -> None:
        settings = SimpleNamespace(
            search_provider="tavily",
            tavily_api_key="configured",
            search_max_queries=2,
            search_max_citations=10,
            tavily_max_results_per_query=5,
        )
        service = WebSearchService(settings)
        service._post_tavily_search = AsyncMock(
            side_effect=[
                ModelInvocationError("first query failed"),
                {
                    "results": [
                        {
                            "title": "Useful result",
                            "url": "https://example.com/useful",
                            "content": "Evidence",
                            "score": 0.9,
                        }
                    ],
                    "response_time": 0.2,
                },
            ]
        )
        plan = service._normalize_search_plan(
            "original",
            {
                "queries": ["query one", "query two"],
                "topic": "general",
                "time_range": None,
            },
        )

        payload = await service._search_with_tavily(
            "original",
            plan,
            service._build_search_context(plan),
        )

        self.assertEqual(len(payload["results"]), 1)
        self.assertEqual(payload["results"][0]["title"], "Useful result")
        self.assertEqual(len(payload["search_errors"]), 1)


if __name__ == "__main__":
    unittest.main()
