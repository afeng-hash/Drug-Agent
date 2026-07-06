"""
Web search service — abstraction + Tavily implementation.

Pluggable: swap TavilySearchService for another backend by implementing WebSearchService.
"""

import logging
from abc import ABC, abstractmethod

import httpx

from app.config import Settings
from app.search.schemas import WebSearchResponse, WebSearchResult

logger = logging.getLogger(__name__)

TAVILY_API_URL = "https://api.tavily.com/search"


class WebSearchService(ABC):
    """Abstract web search service. Implement to add new search backends."""

    @abstractmethod
    async def search(self, query: str, num_results: int = 5) -> WebSearchResponse: ...

    @property
    @abstractmethod
    def is_available(self) -> bool: ...


class TavilySearchService(WebSearchService):
    """Tavily Search API — AI-optimized web search.

    Tavily returns LLM-friendly structured results with cleaned content,
    making it ideal for AI Agent consumption.

    API docs: https://docs.tavily.com/
    """

    def __init__(self, settings: Settings):
        self._settings = settings

    @property
    def is_available(self) -> bool:
        return (
            self._settings.web_search_enabled
            and bool(self._settings.tavily_api_key)
        )

    async def search(self, query: str, num_results: int = 5) -> WebSearchResponse:
        """Call Tavily Search API and return normalized results."""
        if not self.is_available:
            return WebSearchResponse(
                query=query,
                results=[],
                warning="联网搜索服务未启用或未配置 API Key",
            )

        payload = {
            "api_key": self._settings.tavily_api_key,
            "query": query,
            "search_depth": "basic",
            "max_results": min(num_results, self._settings.web_search_max_results),
        }

        try:
            async with httpx.AsyncClient(timeout=self._settings.web_search_timeout) as client:
                resp = await client.post(TAVILY_API_URL, json=payload)
                resp.raise_for_status()
                data = resp.json()

            raw_results = data.get("results", [])
            results = [
                WebSearchResult(
                    title=r.get("title", ""),
                    snippet=r.get("content", ""),
                    url=r.get("url", ""),
                )
                for r in raw_results[:num_results]
            ]

            return WebSearchResponse(
                query=query,
                results=results,
                total_estimated=len(raw_results),
            )

        except httpx.HTTPStatusError as e:
            logger.warning("Tavily search HTTP error: %s", e)
            return WebSearchResponse(
                query=query,
                results=[],
                warning=f"联网搜索请求失败（HTTP {e.response.status_code}）",
            )
        except Exception as e:
            logger.warning("Tavily search failed: %s", e)
            return WebSearchResponse(
                query=query,
                results=[],
                warning=f"联网搜索服务异常：{str(e)}",
            )
