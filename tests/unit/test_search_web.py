"""Unit tests for SearchWebTool and empty result wrapping."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.react.agent import _wrap_tool_result
from app.agent.react.schemas import ToolResult
from app.agent.react.tools.search_web import SearchWebTool
from app.search.schemas import WebSearchResponse, WebSearchResult


# ═══════════════════════════════════════════════════════════
# SearchWebTool tests
# ═══════════════════════════════════════════════════════════


class TestSearchWebTool:
    @pytest.mark.asyncio
    async def test_web_search_returns_results(self):
        """联网搜索返回结果时格式正确。"""
        mock_service = MagicMock()
        mock_service.search = AsyncMock(return_value=WebSearchResponse(
            query="布洛芬 副作用",
            results=[
                WebSearchResult(
                    title="布洛芬说明书",
                    snippet="布洛芬常见副作用包括...",
                    url="https://example.com/1",
                ),
            ],
            total_estimated=100,
        ))

        tool = SearchWebTool(web_search_service=mock_service)
        result = await tool.execute(query="布洛芬 副作用")

        assert result["found"] is True
        assert result["source"] == "web"
        assert len(result["results"]) == 1
        assert result["results"][0]["url"] == "https://example.com/1"
        assert result["results"][0]["source"] == "web"

    @pytest.mark.asyncio
    async def test_web_search_empty_results(self):
        """联网搜索返回空结果时标记 found=false。"""
        mock_service = MagicMock()
        mock_service.search = AsyncMock(return_value=WebSearchResponse(
            query="罕见药",
            results=[],
        ))

        tool = SearchWebTool(web_search_service=mock_service)
        result = await tool.execute(query="罕见药")

        assert result["found"] is False
        assert result["results"] == []

    @pytest.mark.asyncio
    async def test_web_search_marks_source(self):
        """联网搜索结果带 source='web' 标记。"""
        mock_service = MagicMock()
        mock_service.search = AsyncMock(return_value=WebSearchResponse(
            query="test",
            results=[
                WebSearchResult(title="T", snippet="S", url="https://x.com"),
            ],
        ))

        tool = SearchWebTool(web_search_service=mock_service)
        result = await tool.execute(query="test")

        assert result["source"] == "web"
        assert "warning" in result

    def test_web_search_fallback_tools_empty(self):
        """联网搜索是最后一级，无替代工具。"""
        tool = SearchWebTool(web_search_service=MagicMock())
        assert tool.fallback_tools == []

    def test_search_web_in_prompt(self):
        """验证 search_web 在 REACT_SYSTEM_PROMPT 中。"""
        from app.agent.prompts import REACT_SYSTEM_PROMPT
        assert "search_web" in REACT_SYSTEM_PROMPT
        assert "联网搜索" in REACT_SYSTEM_PROMPT


# ═══════════════════════════════════════════════════════════
# Empty result wrapping tests
# ═══════════════════════════════════════════════════════════


class TestEmptyResultWrapping:
    def test_empty_list_wrapped_as_not_found(self):
        """工具返回 [] → 包装为 found=false。"""
        tr = ToolResult(tool_name="search_manual", success=True, data=[])
        wrapped = _wrap_tool_result(tr)
        assert wrapped["found"] is False
        assert wrapped["results"] == []
        assert "message" in wrapped

    def test_empty_dict_wrapped_as_not_found(self):
        """工具返回 {} → 包装为 found=false。"""
        tr = ToolResult(tool_name="search_manual", success=True, data={})
        wrapped = _wrap_tool_result(tr)
        assert wrapped["found"] is False

    def test_non_empty_list_preserved(self):
        """正常数据不被包装。"""
        tr = ToolResult(
            tool_name="search_manual",
            success=True,
            data=[{"name": "布洛芬"}],
        )
        wrapped = _wrap_tool_result(tr)
        assert wrapped == [{"name": "布洛芬"}]

    def test_non_empty_dict_preserved_with_found(self):
        """非空 dict 被添加 found=true。"""
        tr = ToolResult(
            tool_name="get_drug_detail",
            success=True,
            data={"name": "布洛芬"},
        )
        wrapped = _wrap_tool_result(tr)
        assert wrapped["found"] is True
        assert wrapped["name"] == "布洛芬"

    def test_error_result_preserved(self):
        """error 结果不被额外包装。"""
        tr = ToolResult(
            tool_name="search_drug",
            success=False,
            error="DB connection failed",
        )
        wrapped = _wrap_tool_result(tr)
        assert "error" in wrapped
        assert wrapped["error"] == "DB connection failed"

    def test_explicit_empty_preserved(self):
        """已有的 empty 标记不被覆盖。"""
        tr = ToolResult(
            tool_name="search_web",
            success=True,
            data={"found": False, "empty": True, "message": "Service down"},
        )
        wrapped = _wrap_tool_result(tr)
        assert wrapped["found"] is False
        assert wrapped["message"] == "Service down"
