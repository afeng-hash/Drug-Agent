"""
Web search data models.
"""

from pydantic import BaseModel, Field


class WebSearchResult(BaseModel):
    """Single web search result."""

    title: str
    snippet: str
    url: str
    source: str = "web"


class WebSearchResponse(BaseModel):
    """Complete web search response."""

    query: str
    results: list[WebSearchResult] = Field(default_factory=list)
    total_estimated: int = 0
    source: str = "web"
    warning: str = (
        "以下信息来自互联网搜索，仅供参考，"
        "请以药品说明书或医生/药师意见为准"
    )
