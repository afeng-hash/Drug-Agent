"""
ReactAgent 工具包。

每个工具封装为一个 BaseTool 子类，包含定义、执行逻辑和容错元数据。

加新工具：
  1. 在此目录新建文件，实现 BaseTool 子类
  2. 在 builder.py 的工具列表里加一行

工具分类：
  - drug_discovery:  search_drug（药品发现）
  - drug_profile:    get_drug_detail（完整档案，DB）
  - drug_qa:         search_manual（药品问答，Milvus）
  - web_search:      search_web（联网搜索兜底，Bing）
  - state_access:    get_recommendation, get_user_profile（状态读取）
"""

from app.agent.react.tools.base import BaseTool
from app.agent.react.tools.get_drug_detail import GetDrugDetailTool
from app.agent.react.tools.get_recommendation import GetRecommendationTool
from app.agent.react.tools.get_user_profile import GetUserProfileTool
from app.agent.react.tools.registry import ToolRegistry
from app.agent.react.tools.search_drug import SearchDrugTool
from app.agent.react.tools.search_manual import SearchManualTool
from app.agent.react.tools.search_web import SearchWebTool

__all__ = [
    "BaseTool",
    "ToolRegistry",
    "SearchDrugTool",
    "GetDrugDetailTool",
    "SearchManualTool",
    "SearchWebTool",
    "GetRecommendationTool",
    "GetUserProfileTool",
]
