"""
Web search module — pluggable web search for ReactAgent fallback.

Provides:
  - WebSearchService (abstract) + BingSearchService (default)
  - WebSearchResult, WebSearchResponse (Pydantic models)
"""

from app.search.schemas import WebSearchResponse, WebSearchResult
from app.search.service import TavilySearchService, WebSearchService

__all__ = [
    "WebSearchService",
    "TavilySearchService",
    "WebSearchResult",
    "WebSearchResponse",
]
