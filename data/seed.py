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
