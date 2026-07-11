from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from app.core.config import Settings
from app.core.exceptions import SearchProviderError


class WebSearchService:
    """联网搜索服务：支持多查询并行检索、去重和结果重排。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def search(
        self,
        query: str,
        *,
        search_plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        plan = self._normalize_search_plan(query, search_plan)
        search_context = self._build_search_context(plan)

        if self.settings.search_provider == "mock":
            return {
                "results": [
                    {
                        "source_id": "S1",
                        "title": "Mock Search Result",
                        "url": "https://example.com/mock-search-result",
                        "snippet": f"这是针对查询“{plan['queries'][0]}”的模拟搜索结果。",
                        "score": 1.0,
                        "matched_queries": list(plan["queries"]),
                    }
                ],
                "provider": "mock",
                "query": query,
                "queries": plan["queries"],
                "search_plan": plan,
                "search_context": search_context,
                "request_payloads": [],
            }

        if self.settings.search_provider == "tavily":
            if not self.settings.tavily_api_key:
                raise SearchProviderError("未配置 Tavily API Key")
            return await self._search_with_tavily(query, plan, search_context)

        raise SearchProviderError(
            f"不支持的搜索服务：{self.settings.search_provider}。请配置 SEARCH_PROVIDER=tavily。"
        )

    async def _search_with_tavily(
        self,
        original_query: str,
        plan: dict[str, Any],
        search_context: dict[str, Any],
    ) -> dict[str, Any]:
        request_payloads = [
            self._build_tavily_request_payload(query, plan)
            for query in plan["queries"]
        ]

        async with httpx.AsyncClient(timeout=30) as client:
            raw_responses = await asyncio.gather(
                *[
                    self._post_tavily_search(client, payload)
                    for payload in request_payloads
                ],
                return_exceptions=True,
            )

        successful_queries: list[str] = []
        responses: list[dict[str, Any]] = []
        errors: list[str] = []
        for query, response in zip(plan["queries"], raw_responses):
            if isinstance(response, Exception):
                errors.append(str(response))
                continue
            successful_queries.append(query)
            responses.append(response)

        if not responses:
            first_error = raw_responses[0] if raw_responses else None
            if isinstance(first_error, SearchProviderError):
                raise first_error
            raise SearchProviderError(
                "Tavily search failed for every planned query",
                detail={"errors": errors},
            )

        ranked_results = self._merge_and_rank_results(
            responses,
            successful_queries,
            limit=max(1, self.settings.search_max_citations),
        )
        return {
            "results": ranked_results,
            "provider": "tavily",
            "query": original_query,
            "queries": plan["queries"],
            "search_plan": plan,
            "search_context": search_context,
            "request_payloads": [self._safe_payload(item) for item in request_payloads],
            "response_times": [item.get("response_time") for item in responses],
            "search_errors": errors,
            "provider_answers": [
                item.get("answer") for item in responses if item.get("answer")
            ],
        }

    async def _post_tavily_search(
        self,
        client: httpx.AsyncClient,
        request_payload: dict[str, Any],
    ) -> dict[str, Any]:
        response = await client.post(
            "https://api.tavily.com/search",
            json=request_payload,
        )
        if response.status_code >= 400:
            raise SearchProviderError(
                "Tavily 搜索失败",
                detail={
                    "status_code": response.status_code,
                    "body": response.text[:2000],
                    "request_payload": self._safe_payload(request_payload),
                },
            )
        return response.json()

    def _build_tavily_request_payload(
        self,
        query: str,
        plan: dict[str, Any],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "api_key": self.settings.tavily_api_key,
            "query": query,
            "search_depth": "advanced",
            "max_results": max(1, self.settings.tavily_max_results_per_query),
            "include_answer": False,
            "include_raw_content": False,
        }

        topic = plan.get("topic")
        if topic in {"general", "news", "finance"}:
            payload["topic"] = topic

        time_range = plan.get("time_range")
        if time_range in {"day", "week", "month", "year"}:
            payload["time_range"] = time_range

        return payload

    def _normalize_search_plan(
        self,
        original_query: str,
        search_plan: dict[str, Any] | None,
    ) -> dict[str, Any]:
        raw_plan = search_plan or {}
        max_queries = max(1, self.settings.search_max_queries)
        queries: list[str] = []
        for item in raw_plan.get("queries", []):
            normalized = " ".join(str(item).strip().split())
            if normalized and normalized not in queries:
                queries.append(normalized)
            if len(queries) >= max_queries:
                break

        normalized_original = " ".join(original_query.strip().split())
        if not queries:
            queries = [normalized_original]
        elif normalized_original and normalized_original not in queries and len(queries) < max_queries:
            # Preserve the user's exact entities as a final recall-oriented query.
            queries.append(normalized_original)

        topic = str(raw_plan.get("topic") or "general").strip().lower()
        if topic not in {"general", "news", "finance"}:
            topic = "general"

        time_range_value = raw_plan.get("time_range")
        time_range = (
            str(time_range_value).strip().lower()
            if time_range_value is not None
            else None
        )
        if time_range not in {"day", "week", "month", "year"}:
            time_range = None

        return {
            "queries": queries,
            "topic": topic,
            "time_range": time_range,
            "reason": str(raw_plan.get("reason", "")).strip(),
        }

    @staticmethod
    def _build_search_context(plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "current_time": datetime.now().astimezone().isoformat(),
            "topic": plan["topic"],
            "time_range": plan["time_range"],
            "queries": plan["queries"],
        }

    @classmethod
    def _merge_and_rank_results(
        cls,
        responses: list[dict[str, Any]],
        queries: list[str],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for query_index, (query, payload) in enumerate(zip(queries, responses)):
            for rank, item in enumerate(payload.get("results", [])):
                url = str(item.get("url", "")).strip()
                title = str(item.get("title", "")).strip()
                snippet = str(item.get("content", "")).strip()
                if not url or not (title or snippet):
                    continue

                key = cls._canonicalize_url(url) or f"{title.lower()}::{snippet[:120].lower()}"
                provider_score = cls._safe_float(item.get("score"))
                rank_score = 1.0 / (rank + 1) + provider_score
                existing = merged.get(key)
                if existing is None:
                    merged[key] = {
                        "title": title,
                        "url": url,
                        "snippet": snippet[:2400],
                        "score": provider_score,
                        "published_date": item.get("published_date"),
                        "domain": urlsplit(url).netloc.lower(),
                        "matched_queries": [query],
                        "_rank_score": rank_score,
                        "_query_indexes": {query_index},
                    }
                    continue

                if query not in existing["matched_queries"]:
                    existing["matched_queries"].append(query)
                if query_index not in existing["_query_indexes"]:
                    existing["_rank_score"] += 0.75 / (rank + 1)
                    existing["_query_indexes"].add(query_index)
                existing["score"] = max(existing["score"], provider_score)
                if len(snippet) > len(existing["snippet"]):
                    existing["snippet"] = snippet[:2400]
                if not existing.get("published_date") and item.get("published_date"):
                    existing["published_date"] = item.get("published_date")

        ranked = sorted(
            merged.values(),
            key=lambda item: (
                len(item["matched_queries"]),
                item["_rank_score"],
                item["score"],
            ),
            reverse=True,
        )[:limit]

        results: list[dict[str, Any]] = []
        for index, item in enumerate(ranked, start=1):
            item.pop("_rank_score", None)
            item.pop("_query_indexes", None)
            item["source_id"] = f"S{index}"
            results.append(item)
        return results

    @staticmethod
    def _canonicalize_url(url: str) -> str:
        try:
            parts = urlsplit(url)
        except ValueError:
            return url
        filtered_query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_")
            and key.lower() not in {"ref", "source", "fbclid", "gclid"}
        ]
        path = parts.path.rstrip("/") or "/"
        return urlunsplit(
            (
                parts.scheme.lower(),
                parts.netloc.lower(),
                path,
                urlencode(filtered_query),
                "",
            )
        )

    @staticmethod
    def _safe_float(value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _safe_payload(payload: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in payload.items() if key != "api_key"}
