"""
ReactAgent Skills — 代码控制流程，LLM 负责语义推理。

Skills 将 ReactAgent 从 "LLM 自由 ReAct 循环" 改为 "代码控制执行"：
  - TaskClassifier:    LLM #1 — 语义分类 + 参数提取
  - SOPEngine:         Code  — 确定性工具链执行
  - ResponseGenerator: LLM #2 — 结构化数据 → 自然语言
  - SkillRouter:       Code  — intent → task_type 确定性路由

旧 ReactAgent 保留为 fallback（处理闲聊和未分类查询）。
"""

from app.agent.react.skills.classifier import TaskClassifier
from app.agent.react.skills.generator import ResponseGenerator
from app.agent.react.skills.router import SkillRouter
from app.agent.react.skills.sop import SOPEngine
from app.agent.react.skills.task_definitions import TASK_SOP_MAP, ALL_TASK_DEFINITIONS
from app.agent.react.skills.types import (
    SOP,
    SOPResult,
    SOPStep,
    StepResult,
    TaskClassification,
    TaskType,
)

__all__ = [
    # 核心组件
    "SOPEngine",
    "TaskClassifier",
    "ResponseGenerator",
    "SkillRouter",
    # 数据类型
    "TaskType",
    "TaskClassification",
    "SOP",
    "SOPStep",
    "SOPResult",
    "StepResult",
    # SOP 定义
    "TASK_SOP_MAP",
    "ALL_TASK_DEFINITIONS",
]
