"""
BaseEvidence — 所有证据规则的抽象基类。

每一条 Evidence 规则是一个"观察者"：检查 (症状槽位, 药品) 的某个方面，
输出一个 EvidenceResult。规则是：
  - 确定性的：相同输入 → 相同输出，永远一致
  - 独立的：规则之间不通信，各自独立评估
  - 可测试的：evaluate() 是纯函数，不依赖外部状态
  - 单一职责的：每条规则只关注一个特征维度

设计灵感来自推荐系统的特征工程：把复杂的"这个药适合不适合"问题
拆解成多个独立的、可组合的小判断。
"""

from abc import ABC, abstractmethod

from app.scorer.schemas import EvidenceResult


class BaseEvidence(ABC):
    """证据规则的抽象基类。

    子类只需实现三个成员：
      - feature_name (property)  → 影响哪个特征维度
      - description (property)   → 人类可读的描述
      - evaluate(slots, drug)    → 核心评估逻辑

    示例（实现一个新规则）：
        class PregnancyWarning(BaseEvidence):
            @property
            def feature_name(self): return "safety"

            @property
            def description(self): return "孕妇安全警告"

            def evaluate(self, slots, drug):
                if slots.get("special_population") == "孕妇":
                    return EvidenceResult(
                        feature_name="safety", value=0.2,
                        reason="孕妇应谨慎用药", merge_strategy="min"
                    )
                return EvidenceResult(
                    feature_name="safety", value=1.0,
                    reason="非孕妇，无此风险", merge_strategy="min"
                )
    """

    @property
    @abstractmethod
    def feature_name(self) -> str:
        """这条证据影响哪个特征维度。

        标准维度名（与 weights_config 表中的 key 一一对应）：
          - 'symptom_match'       ← 症状与适应症的匹配程度
          - 'safety'              ← 安全风险（禁忌症、过敏等）
          - 'age_suitability'     ← 年龄段适用性（儿童/老人/成人）
          - 'otc_safety_level'    ← OTC 分类等级（乙类 vs 甲类）
          - 'ingredient_coverage' ← 有效成分对症状的覆盖度
          - 'evidence_quality'    ← 证据质量评分（预留，当前固定 0.5）
        """
        ...

    @property
    @abstractmethod
    def description(self) -> str:
        """人类可读的规则描述，用于审计和日志。

        如 "症状关键词与药品适应症文本的匹配度"。
        """
        ...

    @abstractmethod
    def evaluate(self, slots: dict, drug) -> EvidenceResult:
        """对给定的患者槽位和候选药品，评估这条证据。

        这是证据规则的核心方法，由 EvidenceEngine 对每个候选药品调用一次。

        Args:
            slots: consult_slots 字典，来自 ConversationState。可能包含：
              - symptoms (list[dict])         ← 症状列表 [{"name":"头痛","severity":"中度"},...]
              - temperature (float|None)      ← 体温（℃）
              - duration_days (int|None)      ← 持续天数
              - medications_taken (list[str]) ← 已服药物
              - special_population (str|None) ← 特殊人群（孕妇/哺乳期/儿童/老人）
              - age (int|None)                ← 年龄
              - chronic_conditions (list[str])← 慢性病史
              - allergies (list[str])         ← 过敏史
              - other_symptoms (list[str])    ← 伴随症状

            drug: Drug ORM 实例。可用字段：
              - generic_name, brand_names     ← 通用名、商品名
              - category, otc_type           ← 类别、OTC 分类
              - indication_summary           ← 适应症文本
              - active_ingredients           ← 有效成分列表
              - dosage_form, strength        ← 剂型、规格
              - usage_adult/child/elderly    ← 各年龄段用法

        Returns:
            EvidenceResult：包含 feature_name、value(0~1)、reason、merge_strategy
        """
        ...
