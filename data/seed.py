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
        print(f"✅ Inserted {len(drugs_data)} drugs")

        # ── Inventory ──
        with open(os.path.join(data_dir, "inventory.json"), "r", encoding="utf-8") as f:
            inventory_data = json.load(f)

        drug_repo = DrugRepository(db)
        count = 0
        for item in inventory_data:
            drug_name = item.pop("drug_generic_name")
            drug = await drug_repo.find_by_name(drug_name)
            if not drug:
                print(f"⚠️  Drug not found: {drug_name}, skipping inventory item")
                continue
            inv = Inventory(drug_id=drug.id, **item)
            db.add(inv)
            count += 1

        await db.commit()
        print(f"✅ Inserted {count} inventory items")

    # ── Weight Config (default) ──
    async with session_factory() as db:
        from app.db.models import WeightConfig
        from sqlalchemy import select as sa_select

        existing = await db.execute(
            sa_select(WeightConfig).where(WeightConfig.version == "v1.0.0")
        )
        if not existing.scalar_one_or_none():
            config = WeightConfig(
                version="v1.0.0",
                policy="balanced",
                weights={
                    "symptom_match": 0.30,
                    "safety": 0.25,
                    "age_suitability": 0.20,
                    "otc_safety_level": 0.10,
                    "ingredient_coverage": 0.10,
                    "evidence_quality": 0.05,
                },
                feature_defaults={
                    "symptom_match": 0.0,
                    "safety": 1.0,
                    "age_suitability": 0.5,
                    "otc_safety_level": 0.7,
                    "ingredient_coverage": 0.0,
                    "evidence_quality": 0.5,
                },
                safety_block_threshold=0.2,
                is_active=True,
                description="初始默认权重：均衡推荐策略",
                changed_by="seed",
            )
            db.add(config)
            await db.commit()
            print("✅ Inserted default weight config v1.0.0")
        else:
            print("ℹ️  Weight config v1.0.0 already exists")

    # ── RAG Documents ──
    llm_client = LLMClient(settings)
    retriever = DrugManualRetriever(settings, llm_client)
    try:
        await retriever.ensure_collection()
        rag_dir = os.path.join(data_dir, "rag_docs")
        if os.path.isdir(rag_dir) and os.listdir(rag_dir):
            chunk_count = await ingest_documents(rag_dir, llm_client, retriever)
            print(f"✅ Ingested {chunk_count} RAG chunks into Milvus")
        else:
            print("⚠️  No RAG documents found in data/rag_docs/")
    except Exception as e:
        print(f"⚠️  RAG ingestion skipped (Milvus may not be available): {e}")

    await engine.dispose()
    print("\n🎉 Seed data import complete!")


if __name__ == "__main__":
    asyncio.run(seed())
