"""
Skills 数据类型 — TaskType, TaskClassification, SOP/StepResult 等。

所有类型独立于 LangGraph、ReactAgent、LLMClient。
TaskClassifier / SOPEngine / ResponseGenerator 之间的接口只依赖这些类型。
"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class TaskType(str, Enum):
    """任务类型枚举 — 每个用户查询被归类为其中一种。"""

    SIDE_EFFECTS = "side_effects"
    CONTRAINDICATIONS = "contraindications"
    DOSAGE = "dosage"
    EFFICACY = "efficacy"
    SPECIAL_POPULATION = "special_population"
    DRUG_INTERACTION = "drug_interaction"
    DRUG_COMPARISON = "drug_comparison"
    RECOMMENDATION_EXPLANATION = "recommendation_explanation"
    INVENTORY_CHECK = "inventory_check"


class TaskClassification(BaseModel):
    """LLM #1 (TaskClassifier) 的输出 — 分类结果 + 提取的参数。

    这是 LLM 的唯一"决策"——分类 + 提取。后续执行全由代码控制。
    """

    task_type: TaskType
    """分类后的任务类型"""

    drug_names: list[str] = Field(default_factory=list)
    """从 query/历史中提取的药品名（通用名），如 ["布洛芬", "对乙酰氨基酚"]"""

    population: str | None = None
    """特殊人群，仅 special_population 类型使用：孕妇 / 哺乳期 / 儿童 / 老人"""

    custom_focus: str | None = None
    """用户特别关心的具体方面。如"对肝脏的影响"、"会不会影响睡眠" """

    sub_scene: str | None = None
    """推荐解释的子场景。why_recommend（为什么推荐）/ why_not_recommend（为什么不推荐）"""

    target_drug: str | None = None
    """为什么不推荐 XX 中的 XX。仅 why_not_recommend 子场景使用"""

    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    """分类置信度。低于阈值（0.7）时走 ReAct fallback"""


# ── SOP 步骤定义 ────────────────────────────────────────────


class SOPStep(BaseModel):
    """SOP 中的一个执行步骤。"""

    order: int
    """步序号，从 1 开始"""

    tool_name: str
    """要调用的工具名，如 search_manual / get_drug_detail / search_web"""

    args_template: dict[str, str] = Field(default_factory=dict)
    """参数模板。形如 {"drug_name": "{drug_name}", "question": "副作用 不良反应"}。
    "{drug_name}" / "{population}" / "{question}" 等占位符在执行时填充。
    """

    is_critical: bool = True
    """是否为关键步骤。关键步骤失败会记录但不会中止整个 SOP。"""

    parallel_group: int = 0
    """并行组号。同组 step 并行执行，不同组顺序执行。0 表示此步与上一步不同组。"""


class SOP(BaseModel):
    """一个任务类型的完整 SOP 定义（纯数据，不含执行逻辑）。"""

    task_type: TaskType
    """该 SOP 对应的任务类型"""

    steps: list[SOPStep] = Field(default_factory=list)
    """执行步骤列表"""

    response_structure: str = ""
    """回复结构建议（自然语言，注入到 ResponseGenerator prompt 中）。
    如："先说明常见副作用，再说明偶见/罕见副作用，最后给出观察建议"
    """

    mandatory_reminders: list[str] = Field(default_factory=list)
    """强制性安全提醒列表。代码注入到 generator prompt——不依赖 LLM 自觉。"""

    fallback_response: str = ""
    """全部工具都返回空时的兜底回复模板。
    可用 {drug_name} 占位符，执行时填充。
    """


# ── 执行结果 ────────────────────────────────────────────────


class StepResult(BaseModel):
    """SOP 中单个步骤的执行结果。"""

    step_order: int
    tool_name: str
    success: bool
    data: Any = None
    error: str | None = None


class SOPResult(BaseModel):
    """SOPEngine 执行完整 SOP 后的聚合结果。"""

    task_type: TaskType
    steps: list[StepResult] = Field(default_factory=list)
    """每步的执行结果。包含成功和失败的步骤。"""

    has_usable_data: bool = False
    """本地数据源是否至少有一个返回了有效信息。决定是否触发联网兜底。"""

    triggered_web_fallback: bool = False
    """是否触发了联网搜索兜底。用于 generator 判断是否需要标注网络来源。"""
