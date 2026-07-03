"""
ScoringPipeline — 编排 EvidenceEngine → WeightConfig → ScoringEngine 全流程。

这是评分子系统的唯一对外入口。recommend_node 只调这一个方法。

管线执行步骤：
  1. 加载权重配置（从 PostgreSQL weights_config 表，含 A/B 路由）
  2. 校验配置是否符合策略约束（balanced / safety_first）
  3. 对每个候选药品执行证据评估 → 特征向量
  4. 用权重 × 特征值 → 评分
  5. 附上配置版本号

容错设计：
  如果任何一步失败（如数据库不可用），降级为简单排序：
  乙类 OTC 优先 → 按通用名字母顺序。
  不阻塞用户推荐流程。
"""

from app.db.repositories.weight_config import WeightConfigRepository
from app.scorer.engine import score_all
from app.scorer.evidence import (
    AgeSuitability,
    AllergyCheck,
    ContraindicationCheck,
    IngredientCoverage,
    OtcSafetyLevel,
    SymptomKeywordMatch,
    SymptomSeverityMatch,
)
from app.scorer.evidence_engine import EvidenceEngine
from app.scorer.schemas import ScoringResult
from app.scorer.strategy import StrategyValidator


class ScoringPipeline:
    """药品评分管线的一站式编排器。

    使用方式（在 recommend_node 中）：
        pipeline = ScoringPipeline()                              # 应用启动时创建一次
        result = await pipeline.run(candidates, slots, sid, repo) # 每次推荐时调用
    """

    def __init__(self):
        """初始化管线：创建 EvidenceEngine 并注册 7 条默认规则。"""
        # ── 证据引擎 ──
        self._evidence_engine = EvidenceEngine()
        self._register_default_evidence()

        # ── 策略校验器 ──
        self._validator = StrategyValidator()

    def _register_default_evidence(self) -> None:
        """注册全部 7 条内置证据规则。

        注册顺序无所谓（合并策略决定最终值）。
        """
        # symptom_match 维度（max 合并）
        self._evidence_engine.register(SymptomKeywordMatch())    # 症状关键词匹配
        self._evidence_engine.register(SymptomSeverityMatch())   # 发热成分加成

        # safety 维度（min 合并）
        self._evidence_engine.register(ContraindicationCheck())  # 禁忌症检查
        self._evidence_engine.register(AllergyCheck())           # 过敏检查

        # 其他维度
        self._evidence_engine.register(AgeSuitability())         # 年龄段适用性
        self._evidence_engine.register(IngredientCoverage())     # 成分覆盖度
        self._evidence_engine.register(OtcSafetyLevel())         # OTC 安全等级

    async def run(
        self,
        candidates: list,
        slots: dict,
        session_id: str,
        weight_repo: WeightConfigRepository,
    ) -> ScoringResult:
        """执行完整的评分管线。

        这是推荐环节的核心 — 接收候选用药品列表和用户症状，
        返回排序后的评分结果。

        步骤：
          1. 加载权重配置（含 A/B 测试路由）
          2. 校验配置是否符合策略约束
          3. 逐个药品评估证据 → 特征向量
          4. 批量评分排序

        Args:
            candidates:  Drug ORM 实例列表（已排除安全规则过滤的药品）
            slots:       consult_slots 字典（症状、年龄、过敏史等）
            session_id: 会话 UUID（用于 A/B 权重分配）
            weight_repo: 权重配置仓库（含活跃 DB session）

        Returns:
            ScoringResult：已排序的药品列表 + 配置版本 + 耗时
        """
        try:
            # ── 1. 加载权重配置（含 A/B 路由） ──
            config = await weight_repo.get_active(session_id)

            # ── 2. 校验配置 ──
            is_valid, reason = self._validator.validate(config.weights, config.policy)
            if not is_valid:
                # 校验失败只记录日志，不阻塞用户
                import logging
                logging.getLogger("drug-scorer").warning(
                    f"Weight config validation warning: {reason}"
                )

            # ── 3. 逐个药品评估证据 ──
            features_list = []
            evidence_details_list = []
            for drug in candidates:
                features, details = self._evidence_engine.evaluate_with_detail(slots, drug)
                features_list.append(features)
                evidence_details_list.append(details)

            # ── 4. 批量评分 ──
            result = score_all(
                drugs=candidates,
                features_list=features_list,
                weights=config.weights,
                safety_threshold=config.safety_block_threshold,
                evidence_details_list=evidence_details_list,
            )

            # ── 5. 附上配置版本 ──
            result.config_version = config.version
            return result

        except Exception as e:
            # 降级：简单排序（乙类优先 → 字母序）
            import logging
            logging.getLogger("drug-scorer").error(
                f"Scoring pipeline failed, using fallback sort: {e}"
            )
            return _fallback_sort(candidates)


# ═══════════════════════════════════════════════════════════════
# 降级排序
# ═══════════════════════════════════════════════════════════════

def _fallback_sort(candidates: list) -> ScoringResult:
    """当评分管线异常时的简单降级排序。

    规则：
      1. 乙类 OTC 优先（安全性更高）
      2. 同类型按通用名字母序

    不依赖数据库、LLM 或任何外部服务 — 纯内存排序。

    Args:
        candidates: Drug ORM 实例列表

    Returns:
        降级 ScoringResult（total_score 统一为 0.5）
    """
    def sort_key(drug):
        # 乙类优先（priority=0），甲类次之（priority=1）
        otc_priority = 0 if drug.otc_type == "乙类" else 1
        return (otc_priority, drug.generic_name)

    sorted_drugs = sorted(candidates, key=sort_key)

    from app.scorer.schemas import ScoredDrug
    scored = [
        ScoredDrug(
            drug_id=d.id,
            generic_name=d.generic_name,
            total_score=0.5,   # 统一中等分数（降级模式不做真实评分）
            excluded=False,
        )
        for d in sorted_drugs
    ]
    return ScoringResult(drugs=scored, config_version="fallback")
