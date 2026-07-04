"""
GraphDataSync —— 将 YAML 数据批量导入 Neo4j 的同步模块。

本模块负责从 YAML 文件中读取知识图谱数据，并将其写入 Neo4j 图数据库。
主要用于 `seed.py` 脚本进行图谱的初始数据填充，也可供命令行工具（CLI）进行增量数据同步。

数据流转过程（Data flow）：
    YAML 文件 → Python 字典 → UNWIND Cypher 批量语句 → Neo4j 数据库

YAML 数据格式规范（所有文件均存放于 data/kg/ 目录下）：
    symptoms.yaml:       [{name, level, aliases, parents}, ...]      # 症状：包含名称、层级、别名、父级节点
    drugs.yaml:          [{generic_name, otc_type, category, ingredients, treats}, ...] # 药物：通用名、OTC类型、类别、成分、治疗症状
    conditions.yaml:     [{name}, ...]                               # 疾病/条件：包含名称
    populations.yaml:    [{name}, ...]                               # 人群特征：包含名称
    relationships.yaml:  {contraindicated_for: [...], similar_to: [...], interacts_with: [...]} # 关系定义：禁忌症、相似药物、药物相互作用
"""

import logging
from pathlib import Path

import yaml

from app.kg.client import Neo4jClient

logger = logging.getLogger(__name__)


class GraphDataSync:
    """Import / sync knowledge graph data between YAML files and Neo4j."""

    def __init__(self, client: Neo4jClient, data_dir: str):
        """Args:
            client:   Initialized Neo4jClient (must call initialize() before using).
            data_dir: Path to data/kg/ directory containing YAML files.
        """
        self._client = client
        self._data_dir = Path(data_dir)

    # ── Full Seed ──────────────────────────────────────────

    async def seed_all(self) -> dict:
        """Full re-initialization: clear → constraints → nodes → relationships.

        Returns:
            {"nodes": N, "relationships": M, "errors": [...]}
            Node/relationship counts may be 0 if Neo4j unavailable.
        """
        stats: dict = {"nodes": 0, "relationships": 0, "errors": []}

        if not self._client.is_available():
            stats["errors"].append("Neo4j not available — seed skipped")
            logger.warning("Neo4j not available — seed skipped")
            return stats

        try:
            # Step 1: Clear all existing data
            await self._client.run("MATCH (n) DETACH DELETE n", {})
            logger.info("KG cleared")

            # Step 2: Create constraints (idempotent — IF NOT EXISTS)
            await self._create_constraints()

            # Step 3: Load YAML data
            data = self._load_all_yaml()

            # Step 4: Import nodes
            node_count = 0
            node_count += await self._import_symptoms(data.get("symptoms", []))
            node_count += await self._import_drugs(data.get("drugs", []))
            node_count += await self._import_simple_nodes("Condition", data.get("conditions", []))
            node_count += await self._import_simple_nodes("Population", data.get("populations", []))
            node_count += await self._import_categories(data.get("drugs", []))
            node_count += await self._import_ingredients(data.get("drugs", []))
            stats["nodes"] = node_count

            # Step 5: Import relationships
            rel_count = 0
            rel_count += await self._import_treats(data.get("drugs", []))
            rel_count += await self._import_has_ingredient(data.get("drugs", []))
            rel_count += await self._import_belongs_to(data.get("drugs", []))
            rel_count += await self._import_is_a(data.get("symptoms", []))
            rels = data.get("relationships", {})
            rel_count += await self._import_contraindicated(rels.get("contraindicated_for", []))
            rel_count += await self._import_similar_to(rels.get("similar_to", []))
            rel_count += await self._import_interacts_with(rels.get("interacts_with", []))
            stats["relationships"] = rel_count

            logger.info("KG seed complete — %d nodes, %d relationships", node_count, rel_count)

        except Exception as exc:
            stats["errors"].append(str(exc))
            logger.error("KG seed failed: %s", exc)

        return stats

    # ── Incremental Sync ───────────────────────────────────

    async def sync_drug(self, generic_name: str) -> None:
        """Merge a single drug node and its basic relations from PG data.

        Currently a placeholder — full implementation requires reading drug
        metadata from PG DrugRepository and updating Neo4j accordingly.
        """
        logger.info("sync_drug(%s) — not yet implemented (placeholder)", generic_name)

    # ── Internal: Constraints ──────────────────────────────

    async def _create_constraints(self) -> None:
        """Create UNIQUE CONSTRAINTs for indexed lookup performance."""
        constraints = [
            "CREATE CONSTRAINT IF NOT EXISTS FOR (s:Symptom) REQUIRE s.name IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (d:Drug) REQUIRE d.generic_name IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (i:Ingredient) REQUIRE i.name IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (c:Category) REQUIRE c.name IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (c:Condition) REQUIRE c.name IS UNIQUE",
            "CREATE CONSTRAINT IF NOT EXISTS FOR (p:Population) REQUIRE p.name IS UNIQUE",
        ]
        for stmt in constraints:
            try:
                await self._client.run(stmt, {})
            except Exception as exc:
                logger.warning("Constraint creation warning: %s", exc)

    # ── Internal: YAML Loading ─────────────────────────────

    def _load_all_yaml(self) -> dict:
        """Load all YAML files from data_dir. Missing files → empty defaults."""
        files = {
            "symptoms": "symptoms.yaml",
            "drugs": "drugs.yaml",
            "conditions": "conditions.yaml",
            "populations": "populations.yaml",
            "relationships": "relationships.yaml",
        }
        data: dict = {}
        for key, filename in files.items():
            fpath = self._data_dir / filename
            if fpath.exists():
                with open(fpath, "r", encoding="utf-8") as f:
                    data[key] = yaml.safe_load(f) or []
            else:
                logger.warning("KG data file not found: %s", fpath)
                data[key] = [] if key != "relationships" else {}
        return data

    # ── Internal: Node Importers ───────────────────────────

    async def _import_symptoms(self, items: list[dict]) -> int:
        if not items:
            return 0
        await self._client.run(
            """
            UNWIND $items AS item
            CREATE (s:Symptom {name: item.name, level: item.level})
            SET s.aliases = item.aliases
            """,
            {"items": items},
        )
        return len(items)

    async def _import_drugs(self, items: list[dict]) -> int:
        if not items:
            return 0
        await self._client.run(
            """
            UNWIND $items AS item
            CREATE (d:Drug {generic_name: item.generic_name})
            SET d.otc_type = item.otc_type,
                d.dosage_form = item.dosage_form
            """,
            {"items": items},
        )
        return len(items)

    async def _import_simple_nodes(self, label: str, items: list[dict]) -> int:
        if not items:
            return 0
        await self._client.run(
            f"""
            UNWIND $items AS item
            CREATE (n:{label} {{name: item.name}})
            """,
            {"items": items},
        )
        return len(items)

    async def _import_categories(self, drugs: list[dict]) -> int:
        """Extract unique categories from drug definitions."""
        cats = sorted({d["category"] for d in drugs if d.get("category")})
        if not cats:
            return 0
        items = [{"name": c} for c in cats]
        await self._client.run(
            """
            UNWIND $items AS item
            MERGE (c:Category {name: item.name})
            """,
            {"items": items},
        )
        return len(items)

    async def _import_ingredients(self, drugs: list[dict]) -> int:
        """Extract unique ingredients from drug definitions."""
        ing_set = set()
        for d in drugs:
            for ing in d.get("ingredients", []):
                ing_set.add(ing)
        if not ing_set:
            return 0
        items = [{"name": i} for i in sorted(ing_set)]
        await self._client.run(
            """
            UNWIND $items AS item
            MERGE (i:Ingredient {name: item.name})
            """,
            {"items": items},
        )
        return len(items)

    # ── Internal: Relationship Importers ───────────────────

    async def _import_treats(self, drugs: list[dict]) -> int:
        rows = []
        for d in drugs:
            for t in d.get("treats", []):
                rows.append({
                    "drug": d["generic_name"],
                    "symptom": t["symptom"],
                    "strength": t.get("strength", 1.0),
                })
        if not rows:
            return 0
        await self._client.run(
            """
            UNWIND $rows AS row
            MATCH (d:Drug {generic_name: row.drug})
            MATCH (s:Symptom {name: row.symptom})
            CREATE (d)-[:TREATS {strength: row.strength}]->(s)
            """,
            {"rows": rows},
        )
        return len(rows)

    async def _import_has_ingredient(self, drugs: list[dict]) -> int:
        rows = []
        for d in drugs:
            for ing in d.get("ingredients", []):
                rows.append({"drug": d["generic_name"], "ingredient": ing})
        if not rows:
            return 0
        await self._client.run(
            """
            UNWIND $rows AS row
            MATCH (d:Drug {generic_name: row.drug})
            MATCH (i:Ingredient {name: row.ingredient})
            CREATE (d)-[:HAS_INGREDIENT]->(i)
            """,
            {"rows": rows},
        )
        return len(rows)

    async def _import_belongs_to(self, drugs: list[dict]) -> int:
        rows = []
        for d in drugs:
            if d.get("category"):
                rows.append({"drug": d["generic_name"], "category": d["category"]})
        if not rows:
            return 0
        await self._client.run(
            """
            UNWIND $rows AS row
            MATCH (d:Drug {generic_name: row.drug})
            MATCH (c:Category {name: row.category})
            CREATE (d)-[:BELONGS_TO]->(c)
            """,
            {"rows": rows},
        )
        return len(rows)

    async def _import_is_a(self, symptoms: list[dict]) -> int:
        rows = []
        for s in symptoms:
            for parent in s.get("parents", []):
                rows.append({"child": s["name"], "parent": parent})
        if not rows:
            return 0
        await self._client.run(
            """
            UNWIND $rows AS row
            MATCH (child:Symptom {name: row.child})
            MATCH (parent:Symptom {name: row.parent})
            CREATE (child)-[:IS_A]->(parent)
            """,
            {"rows": rows},
        )
        return len(rows)

    async def _import_contraindicated(self, items: list[dict]) -> int:
        if not items:
            return 0
        condition_rows = []
        population_rows = []
        for item in items:
            row = {"drug": item["drug"], "target": item["target"]}
            if item.get("target_type") == "Condition":
                condition_rows.append(row)
            elif item.get("target_type") == "Population":
                population_rows.append(row)

        count = 0
        if condition_rows:
            await self._client.run(
                """
                UNWIND $rows AS row
                MATCH (d:Drug {generic_name: row.drug})
                MATCH (c:Condition {name: row.target})
                CREATE (d)-[:CONTRAINDICATED_FOR]->(c)
                """,
                {"rows": condition_rows},
            )
            count += len(condition_rows)
        if population_rows:
            await self._client.run(
                """
                UNWIND $rows AS row
                MATCH (d:Drug {generic_name: row.drug})
                MATCH (p:Population {name: row.target})
                CREATE (d)-[:CONTRAINDICATED_FOR]->(p)
                """,
                {"rows": population_rows},
            )
            count += len(population_rows)
        return count

    async def _import_similar_to(self, items: list[dict]) -> int:
        if not items:
            return 0
        await self._client.run(
            """
            UNWIND $rows AS row
            MATCH (a:Drug {generic_name: row.drug_a})
            MATCH (b:Drug {generic_name: row.drug_b})
            CREATE (a)-[:SIMILAR_TO]->(b)
            """,
            {"rows": items},
        )
        return len(items)

    async def _import_interacts_with(self, items: list[dict]) -> int:
        if not items:
            return 0
        await self._client.run(
            """
            UNWIND $rows AS row
            MATCH (a:Drug {generic_name: row.drug_a})
            MATCH (b:Drug {generic_name: row.drug_b})
            CREATE (a)-[:INTERACTS_WITH]->(b)
            """,
            {"rows": items},
        )
        return len(items)
