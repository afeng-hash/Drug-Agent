"""
DrugGraphRepository — Cypher query methods for drug recommendation use cases.

All Cypher is encapsulated here. Callers never write raw queries.
Every method checks client.is_available() before querying and returns
safe defaults when Neo4j is unavailable.
"""

import logging
from typing import Optional

from app.kg.client import Neo4jClient
from app.kg.schemas import (
    ContraindicationResult,
    DrugCandidate,
    MatchDetail,
)

logger = logging.getLogger(__name__)

# Decay factor for ancestor symptoms (1-2 hops away via IS_A).
# Direct match (0 hops) = 1.0, ancestor match (1-2 hops) = 0.7
ANCESTOR_DECAY = 0.7


class DrugGraphRepository:
    """# 用于药物推荐的高层图查询 API。
        #
        # 每个公开方法（public method）均对应一个具体的业务使用场景（business use case）。
        # Cypher 查询语句仅作为私有实现细节内联在方法内部——
        # 外部调用方无需关心底层查询，只会接收到类型安全的 Pydantic 结果对象。
    """

    def __init__(self, client: Neo4jClient):
        self._client = client

    # ── F2: Symptom → Drug Candidates ──────────────────────

    async def find_candidates_by_symptoms(
        self,
        symptoms: list[dict],
        categories: list[str] | None = None,
    ) -> list[DrugCandidate]:
        """通过图遍历（Graph Traversal）查找与症状列表相匹配的候选药物。

            针对每个症状，执行以下逻辑：
              1. 通过标准名称（canonical name）匹配“症状（Symptom）”节点。
              2. 向上扩展 IS_A 关系 0 到 2 层，以查找所有祖先节点（即包含该症状及其上级分类）。
              3. 对于每个“药物-症状”对，计算它们之间的最短路径距离（SHORTEST path distance）。
              4. 计算得分：得分 = 症状权重(symptom_weight) × 治疗强度(treats_strength) × 距离衰减(decay(distance))。
              5. 累加每种药物的各项得分，并按降序（得分从高到低）返回结果。

            参数 (Args):
                symptoms: 症状列表，格式为 [{"name": "头痛", "weight": 1.0}, {"name": "流鼻涕", "weight": 0.5}]
                          其中 weight=1.0 代表主要症状，weight=0.5 代表次要症状。
                categories: 可选参数，用于按类别名称进行过滤的列表。

            返回值 (Returns):
                按得分降序排列的 DrugCandidate（药物候选）列表，得分最高者排在最前。
                若 Neo4j 数据库不可用或未找到匹配项，则返回空列表。
        """
        if not self._client.is_available():
            logger.info("Neo4j unavailable — returning empty candidate list")
            return []

        if not symptoms:
            return []

        cypher = """
        UNWIND $symptoms AS sym
        MATCH (s:Symptom {name: sym.name})

        // Expand IS_A upward (0..2 hops): s → ... → ancestor
        MATCH path = (s)-[:IS_A*0..2]->(ancestor:Symptom)
        // Find drugs that treat this ancestor
        MATCH (d:Drug)-[t:TREATS]->(ancestor)

        // For each (drug, symptom) pair, take the shortest path
        WITH d, sym, t, length(path) AS dist
        ORDER BY dist ASC
        WITH d.generic_name AS drug,
             sym.name AS matched_symptom,
             sym.weight AS symptom_weight,
             HEAD(COLLECT([dist, t.strength])) AS best

        WITH drug, matched_symptom, symptom_weight,
             best[0] AS min_dist, best[1] AS strength

        WITH drug, matched_symptom, min_dist, strength,
             strength * symptom_weight *
               CASE WHEN min_dist = 0 THEN 1.0 ELSE $decay END AS contribution

        RETURN drug,
               SUM(contribution) AS total_score,
               COLLECT(DISTINCT matched_symptom) AS matched_symptoms,
               COLLECT({
                 symptom: matched_symptom,
                 strength: strength,
                 distance: min_dist,
                 decay: CASE WHEN min_dist = 0 THEN 1.0 ELSE $decay END,
                 contribution: contribution
               }) AS match_details
        ORDER BY total_score DESC
        """

        params: dict = {
            "symptoms": symptoms,
            "decay": ANCESTOR_DECAY,
        }
        if categories:
            # Add category filter: only drugs in specified categories
            cypher = cypher.replace(
                "MATCH (d:Drug)-[t:TREATS]->(ancestor)",
                "MATCH (d:Drug)-[t:TREATS]->(ancestor)\n"
                "MATCH (d)-[:BELONGS_TO]->(cat:Category)\n"
                "WHERE cat.name IN $categories",
            )
            params["categories"] = categories

        try:
            rows = await self._client.run(cypher, params)
        except Exception as exc:
            logger.error("find_candidates_by_symptoms failed: %s", exc)
            return []

        return [
            DrugCandidate(
                generic_name=row["drug"],
                score=round(row["total_score"], 4),
                matched_symptoms=row["matched_symptoms"],
                match_details=[
                    MatchDetail(
                        symptom=d["symptom"],
                        strength=d["strength"],
                        distance=d["distance"],
                        decay=d["decay"],
                        contribution=round(d["contribution"], 4),
                    )
                    for d in row["match_details"]
                ],
            )
            for row in rows
        ]

    # ── F3: Contraindication Checks ────────────────────────

    async def check_contraindications(
        self,
        drug_name: str,
        user_conditions: list[str] | None = None,
        special_population: str | None = None,
        allergies: list[str] | None = None,
    ) -> ContraindicationResult:
        """根据用户的禁忌症维度对药物进行核查。

         单次查询中同时执行以下三项检查：
           1. 药物 -[:CONTRAINDICATED_FOR]-> 疾病 ← 用户既往病史 (user_conditions)
           2. 药物 -[:CONTRAINDICATED_FOR]-> 特殊人群 ← 特殊人群标签 (special_population)
           3. 药物 -[:HAS_INGREDIENT]-> 成分 ← 过敏原 (allergies)

         参数 (Args):
             drug_name:          药物名称，例如 "布洛芬"
             user_conditions:    用户既往病史列表，例如 ["胃溃疡", "哮喘"]
             special_population: 特殊人群标签，例如 "孕妇"，若无则传 None
             allergies:          过敏原列表，例如 ["阿司匹林", "布洛芬"]

         返回值 (Returns):
             ContraindicationResult 对象，包含各维度下匹配到的禁忌项。
             若 Neo4j 数据库不可用，则返回默认的安全（无禁忌）结果。
        """
        user_conditions = user_conditions or []
        allergies = allergies or []

        if not self._client.is_available():
            logger.info("Neo4j unavailable — returning safe default for contraindications")
            return ContraindicationResult(drug_name=drug_name)

        result = ContraindicationResult(drug_name=drug_name)

        try:
            # 检查 1：禁忌症
            if user_conditions:
                cond_rows = await self._client.run(
                    """
                    MATCH (d:Drug {generic_name: $drug_name})
                          -[:CONTRAINDICATED_FOR]->(c:Condition)
                    WHERE c.name IN $user_conditions
                    RETURN c.name AS matched_condition
                    """,
                    {"drug_name": drug_name, "user_conditions": user_conditions},
                )
                result.matched_conditions = [r["matched_condition"] for r in cond_rows]

            #检查 2：禁忌人群
            if special_population:
                pop_rows = await self._client.run(
                    """
                    MATCH (d:Drug {generic_name: $drug_name})
                          -[:CONTRAINDICATED_FOR]->(p:Population)
                    WHERE p.name = $population
                    RETURN p.name AS matched_population
                    """,
                    {"drug_name": drug_name, "population": special_population},
                )
                result.matched_populations = [r["matched_population"] for r in pop_rows]

            # 检查 3：成分过敏检查
            if allergies:
                allergy_rows = await self._client.run(
                    """
                    MATCH (d:Drug {generic_name: $drug_name})
                          -[:HAS_INGREDIENT]->(i:Ingredient)
                    WHERE i.name IN $allergies
                    RETURN i.name AS matched_allergen
                    """,
                    {"drug_name": drug_name, "allergies": allergies},
                )
                result.matched_allergens = [r["matched_allergen"] for r in allergy_rows]

        except Exception as exc:
            logger.error("check_contraindications failed for %s: %s", drug_name, exc)
            return ContraindicationResult(drug_name=drug_name)

        result.has_contraindication = bool(
            result.matched_conditions
            or result.matched_populations
            or result.matched_allergens
        )
        return result

    # ── F4: Similar / Alternative Drugs ────────────────────

    async def get_similar_drugs(self, drug_name: str) -> list[str]:
        """通过 SIMILAR_TO（相似）关系查找替代药物（双向关系）。

         参数 (Args):
             drug_name: 药物名称，例如 "布洛芬"

         返回值 (Returns):
             替代药物的通用名（generic_name）字符串列表。
             若未找到替代药物或 Neo4j 数据库不可用，则返回空列表。
        """
        if not self._client.is_available():
            return []

        try:
            rows = await self._client.run(
                """
                MATCH (d:Drug {generic_name: $drug_name})-[:SIMILAR_TO]-(other:Drug)
                RETURN other.generic_name AS alternative
                """,
                {"drug_name": drug_name},
            )
            return [r["alternative"] for r in rows]
        except Exception as exc:
            logger.error("get_similar_drugs failed for %s: %s", drug_name, exc)
            return []

    # ── F3 (aux): Full Drug Profile ────────────────────────

    async def get_drug_profile(self, drug_name: str) -> dict:
        """获取单个药物的所有禁忌症与成分数据。

         供安全规则引擎（Safety Rules Engine）使用，用于在规则评估时补充图数据库中的相关数据。

         返回字典 (Returns dict):
           {
             "drug": "布洛芬",
             "contraindicated_conditions": ["胃溃疡", "哮喘"],  # 禁忌疾病/禁忌症
             "contraindicated_populations": ["孕妇"],          # 禁忌人群
             "ingredients": ["布洛芬"]                          # 药物成分
           }

         若未找到该药物或 Neo4j 数据库不可用，则返回默认的字典（空值）。
        """
        if not self._client.is_available():
            return {
                "drug": drug_name,
                "contraindicated_conditions": [],
                "contraindicated_populations": [],
                "ingredients": [],
            }

        try:
            rows = await self._client.run(
                """
                MATCH (d:Drug {generic_name: $drug_name})
                OPTIONAL MATCH (d)-[:CONTRAINDICATED_FOR]->(c:Condition)
                OPTIONAL MATCH (d)-[:CONTRAINDICATED_FOR]->(p:Population)
                OPTIONAL MATCH (d)-[:HAS_INGREDIENT]->(i:Ingredient)
                RETURN d.generic_name AS drug,
                       COLLECT(DISTINCT c.name) AS contraindicated_conditions,
                       COLLECT(DISTINCT p.name) AS contraindicated_populations,
                       COLLECT(DISTINCT i.name) AS ingredients
                """,
                {"drug_name": drug_name},
            )
            if rows:
                return dict(rows[0])
            return {
                "drug": drug_name,
                "contraindicated_conditions": [],
                "contraindicated_populations": [],
                "ingredients": [],
            }
        except Exception as exc:
            logger.error("get_drug_profile failed for %s: %s", drug_name, exc)
            return {
                "drug": drug_name,
                "contraindicated_conditions": [],
                "contraindicated_populations": [],
                "ingredients": [],
            }
