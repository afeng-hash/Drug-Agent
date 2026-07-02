"""RAG document ingestion — load, chunk, embed, and store drug manuals."""

import os
import re
import uuid

from langchain_text_splitters import RecursiveCharacterTextSplitter

from app.llm.client import LLMClient
from app.rag.retriever import DrugManualRetriever
from app.rag.schemas import Document

# Section detection patterns — match headers in drug manuals
SECTION_PATTERNS = {
    "不良反应": re.compile(r"【不良反应】|不良反应", re.IGNORECASE),
    "禁忌": re.compile(r"【禁忌】|禁忌(?!症)", re.IGNORECASE),
    "注意事项": re.compile(r"【注意事项】|注意事项", re.IGNORECASE),
    "药物相互作用": re.compile(r"【药物相互作用】|药物相互作用", re.IGNORECASE),
}


def detect_section(text: str) -> str:
    """Detect which drug manual section a text chunk belongs to."""
    for section_name, pattern in SECTION_PATTERNS.items():
        if pattern.search(text):
            return section_name
    return "通用"


def load_documents(data_dir: str) -> list[Document]:
    """Load drug manual .txt files from a directory.

    File naming: <drug_name>.txt  (e.g., "布洛芬.txt" → drug_name="布洛芬")
    """
    documents = []
    for filename in os.listdir(data_dir):
        if not filename.endswith(".txt"):
            continue
        drug_name = os.path.splitext(filename)[0]
        filepath = os.path.join(data_dir, filename)
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        drug_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, drug_name))
        section = detect_section(content)
        documents.append(Document(
            drug_id=drug_id,
            drug_name=drug_name,
            section=section,
            content=content,
        ))
    return documents


async def ingest_documents(
    data_dir: str,
    llm_client: LLMClient,
    retriever: DrugManualRetriever,
    chunk_size: int = 500,
    chunk_overlap: int = 50,
) -> int:
    """Ingest all drug manual documents into Milvus.

    1. Load .txt files from data_dir
    2. Split each document into chunks
    3. Detect section for each chunk
    4. Generate embeddings
    5. Insert into Milvus

    Returns:
        Total number of chunks ingested.
    """
    documents = load_documents(data_dir)
    if not documents:
        return 0

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", "。", "；", "，", " ", ""],
    )

    total_chunks = 0
    for doc in documents:
        chunks = splitter.split_text(doc.content)
        if not chunks:
            continue

        embeddings = await llm_client.embed(chunks)

        batch = []
        for i, chunk_text in enumerate(chunks):
            section = detect_section(chunk_text) if len(chunks) > 1 else doc.section
            batch.append({
                "drug_id": doc.drug_id,
                "drug_name": doc.drug_name,
                "section": section,
                "content": chunk_text,
                "vector": embeddings[i],
            })

        retriever.insert(batch)
        total_chunks += len(batch)

    return total_chunks
