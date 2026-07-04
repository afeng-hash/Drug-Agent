"""
知识图谱数据模型 —— 用于定义实体、关系及查询结果的 Pydantic 模式（Schemas）。
Neo4j 中的所有节点（nodes）和关系（relationships）均具备对应的 Pydantic 模型，
用于数据校验（validation）与序列化（serialization）。
查询结果同样进行了严格的类型定义（typed），以便于下游模块安全地消费与处理。
"""

from pydantic import BaseModel, Field


# ────────────────────────────────────────────────────────────
# Entity Nodes
# ────────────────────────────────────────────────────────────

class SymptomNode(BaseModel):
    """Neo4j 中的“症状（Symptom）”节点，
        通过 IS_A（属于/是一种）关系构成一个 3 层的有向无环图（DAG）。
    """
    name: str                           # canonical name, e.g. "头痛"
    level: int = 1                      # 1 (coarse) / 2 / 3 (fine-grained)
    aliases: list[str] = Field(default_factory=list)  # e.g. ["头疼", "脑壳疼"]


class DrugNode(BaseModel):
    """Drug node — linked to PG drugs table via generic_name."""
    generic_name: str                   # e.g. "布洛芬"
    otc_type: str = "甲类"              # 甲类 or 乙类
    dosage_form: str = ""               # e.g. "片剂/胶囊/混悬液"


class IngredientNode(BaseModel):
    """药物有效成分（Active Pharmaceutical Ingredient）节点"""
    name: str                           # e.g. "布洛芬"


class CategoryNode(BaseModel):
    """药物治疗类别（Drug Therapeutic Category）节点。"""
    name: str                           # e.g. "感冒退烧"


class ConditionNode(BaseModel):
    """Medical condition / chronic disease node (for contraindication matching)."""
    name: str                           # e.g. "胃溃疡"


class PopulationNode(BaseModel):
    """Special population node (for contraindication matching)."""
    name: str                           # e.g. "孕妇"


# ────────────────────────────────────────────────────────────
# Relationships
# ────────────────────────────────────────────────────────────

class TreatsRelation(BaseModel):
    """药物“治疗（TREATS）”症状的关系。"""
    drug: str                           # Drug.generic_name
    symptom: str                        # Symptom.name
    strength: float = 1.0               # 0-1, strong indication vs weak coverage


class ContraindicatedRelation(BaseModel):
    """药物“禁忌（CONTRAINDICATED_FOR）”疾病/人群的关系。"""
    drug: str
    target_type: str                    # "Condition" | "Population"
    target_name: str


class SimilarToRelation(BaseModel):
    """Drug SIMILAR_TO Drug relationship (bidirectional, stored once)."""
    drug_a: str
    drug_b: str


class IsARelation(BaseModel):
    """症状“属于（IS_A）”症状的关系（子节点 → 父节点，构成有向无环图 DAG）"""
    child: str                          # specific symptom name
    parent: str                         # more general symptom name


# ────────────────────────────────────────────────────────────
# Query Results
# ────────────────────────────────────────────────────────────

class MatchDetail(BaseModel):
    """Detail for a single (drug, symptom) match."""
    symptom: str
    strength: float
    distance: int                       # 0 = direct, 1-2 = ancestor
    decay: float                        # 1.0 or 0.7
    contribution: float                 # strength × weight × decay


class DrugCandidate(BaseModel):
    """通过“症状 → 药物”图查询得到的候选药物。"""
    generic_name: str
    score: float                        # Σ(strength × symptom_weight × decay)
    matched_symptoms: list[str] = Field(default_factory=list)
    match_details: list[MatchDetail] = Field(default_factory=list)


class ContraindicationResult(BaseModel):
    """药物与用户禁忌症进行核查后的结果。"""
    drug_name: str
    has_contraindication: bool = False
    matched_conditions: list[str] = Field(default_factory=list)
    matched_populations: list[str] = Field(default_factory=list)
    matched_allergens: list[str] = Field(default_factory=list)
