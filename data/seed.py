"""Seed data import script — populates PostgreSQL and Milvus with initial data."""

import asyncio
import json
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings
from app.db.database import close_db, init_db
from app.db.models import Drug, Inventory
from app.db.repositories.drug import DrugRepository
from app.db.repositories.inventory import InventoryRepository
from app.llm.client import LLMClient
from app.rag.ingestor import ingest_documents
from app.rag.retriever import DrugManualRetriever
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


async def seed():
    settings = Settings()
    data_dir = os.path.dirname(os.path.abspath(__file__))

    # Init DB
    engine = create_async_engine(settings.database_url, echo=False)
    async with engine.begin() as conn:
        from app.db.database import Base
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as db:
        # ── Drugs ──
        with open(os.path.join(data_dir, "drugs.json"), "r", encoding="utf-8") as f:
            drugs_data = json.load(f)

        for d in drugs_data:
            existing = await db.execute(
                select(Drug).where(Drug.generic_name == d["generic_name"])
            )
            if existing.scalar_one_or_none():
                continue
            drug = Drug(**d)
            db.add(drug)

        await db.commit()
        print(f"[OK] Inserted {len(drugs_data)} drugs")

        # ── Inventory ──
        with open(os.path.join(data_dir, "inventory.json"), "r", encoding="utf-8") as f:
            inventory_data = json.load(f)

        drug_repo = DrugRepository(db)
        count = 0
        for item in inventory_data:
            drug_name = item.pop("drug_generic_name")
            drug = await drug_repo.find_by_name(drug_name)
            if not drug:
                print(f"[WARN]  Drug not found: {drug_name}, skipping inventory item")
                continue
            inv = Inventory(drug_id=drug.id, **item)
            db.add(inv)
            count += 1

        await db.commit()
        print(f"[OK] Inserted {count} inventory items")

    # ── Weight Config (default) ──
    async with session_factory() as db:
        from app.db.models import WeightConfig
        from sqlalchemy import select as sa_select

        # v1.0.0 — 几何加权平均（向后兼容）
        existing = await db.execute(
            sa_select(WeightConfig).where(WeightConfig.version == "v1.0.0")
        )
        if not existing.scalar_one_or_none():
            config = WeightConfig(
                version="v1.0.0",
                scoring_version="v1",
                policy="balanced",
                weights={
                    "symptom_match": 0.50,
                    "symptom_focus_ratio": 0.15,
                    "age_suitability": 0.25,
                    "otc_safety_level": 0.10,
                },
                feature_defaults={
                    "symptom_match": 0.0,
                    "symptom_focus_ratio": 1.0,
                    "age_suitability": 0.5,
                    "otc_safety_level": 0.7,
                },
                safety_block_threshold=0.2,
                is_active=True,
                description="v1 几何加权平均：均衡推荐策略",
                changed_by="seed",
            )
            db.add(config)
            await db.commit()
            print("[OK] Inserted default weight config v1.0.0")
        else:
            print("[INFO]  Weight config v1.0.0 already exists")

        # v2.0.0 — 层级乘法模型（推荐使用）
        existing_v2 = await db.execute(
            sa_select(WeightConfig).where(WeightConfig.version == "v2.0.0")
        )
        if not existing_v2.scalar_one_or_none():
            config_v2 = WeightConfig(
                version="v2.0.0",
                scoring_version="v2",
                policy="balanced",
                weights={
                    "focus": 0.5,
                    "age": 0.3,
                    "otc": 0.05,
                },
                safety_block_threshold=0.2,
                is_active=True,
                description="v2 层级乘法模型：主得分×√聚焦率×年龄软惩罚×OTC弱调节",
                changed_by="seed",
            )
            db.add(config_v2)
            await db.commit()
            print("[OK] Inserted default weight config v2.0.0")
        else:
            print("[INFO]  Weight config v2.0.0 already exists")

    # ── Neo4j Knowledge Graph ──
    try:
        from app.kg.client import Neo4jClient
        from app.kg.sync import GraphDataSync

        kg_client = Neo4jClient.from_settings(settings)
        await kg_client.initialize()
        if kg_client.is_available():
            kg_dir = os.path.join(data_dir, "kg")
            sync = GraphDataSync(kg_client, kg_dir)
            stats = await sync.seed_all()
            print(f"[OK] KG seeded: {stats['nodes']} nodes, {stats['relationships']} relationships")
            await kg_client.close()
        else:
            print("[WARN]  Neo4j not available — KG seed skipped")
    except Exception as e:
        print(f"[WARN]  KG seed skipped (Neo4j may not be available): {e}")

    # ── RAG Documents ──
    llm_client = LLMClient(settings)
    retriever = DrugManualRetriever(settings, llm_client)
    try:
        await retriever.ensure_collection()
        rag_dir = os.path.join(data_dir, "rag_docs")
        if os.path.isdir(rag_dir) and os.listdir(rag_dir):
            chunk_count = await ingest_documents(rag_dir, llm_client, retriever)
            print(f"[OK] Ingested {chunk_count} RAG chunks into Milvus")
        else:
            print("[WARN]  No RAG documents found in data/rag_docs/")
    except Exception as e:
        print(f"[WARN]  RAG ingestion skipped (Milvus may not be available): {e}")

    await engine.dispose()
    print("\n[DONE] Seed data import complete!")


if __name__ == "__main__":
    asyncio.run(seed())
