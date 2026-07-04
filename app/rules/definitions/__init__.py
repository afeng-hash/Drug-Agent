"""
Factory function to register all safety rules into the engine.

在 app 启动时调用 register_all_rules(engine)，一次性注册全部 5 条 BLOCK 安全规则。

  - R1_HighFever        → 高热（≥39°C）→ 建议立即就医
  - R2_InfantFever      → 婴儿（<3月）发热 → 必须就医
  - R3_PregnantFever    → 孕妇发热 → 必须就医
  - R4_EmergencySigns   → 紧急症状（呼吸困难、意识模糊等）
  - R5_SevereAllergy    → 严重过敏史（过敏性休克）

药品级别的禁忌过滤已移到 recommend_node 的 Neo4j 图谱查询。
执行顺序 = 注册顺序。首个触发的 BLOCK 规则立即短路线返回。
"""
from app.rules.definitions.r1_high_fever import R1_HighFever
from app.rules.definitions.r2_infant_fever import R2_InfantFever
from app.rules.definitions.r3_pregnant_fever import R3_PregnantFever
from app.rules.definitions.r4_emergency_signs import R4_EmergencySigns
from app.rules.definitions.r5_severe_allergy import R5_SevereAllergy
from app.rules.engine import RuleEngine


def register_all_rules(engine: RuleEngine) -> None:
    """向引擎注册全部 5 条 BLOCK 安全规则。

    执行顺序按注册顺序 R1→R2→R3→R4→R5。
    任一触发立即短路线返回就医警告。

    Args:
        engine: 规则引擎实例
    """
    engine.register(R1_HighFever())
    engine.register(R2_InfantFever())
    engine.register(R3_PregnantFever())
    engine.register(R4_EmergencySigns())
    engine.register(R5_SevereAllergy())
