"""Integration tests for the RAG retrieval pipeline."""

import pytest


@pytest.mark.asyncio
async def test_rag_schema():
    """Verify RAG schemas are well-formed."""
    from app.rag.schemas import Chunk, Document

    doc = Document(
        drug_id="test-1",
        drug_name="布洛芬",
        section="不良反应",
        content="常见不良反应包括胃肠道不适、头晕等。",
    )
    assert doc.drug_name == "布洛芬"
    assert doc.section == "不良反应"

    chunk = Chunk(
        drug_name="布洛芬",
        section="不良反应",
        content="常见不良反应...",
        score=0.95,
    )
    assert chunk.score > 0.9


@pytest.mark.asyncio
async def test_ingest_document_loading():
    """Test that documents can be loaded from disk."""
    import os
    import tempfile

    from app.rag.ingestor import load_documents

    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a test document
        doc_path = os.path.join(tmpdir, "测试药.txt")
        with open(doc_path, "w", encoding="utf-8") as f:
            f.write("【不良反应】偶见恶心、呕吐。\n【禁忌】过敏者禁用。")

        docs = load_documents(tmpdir)
        assert len(docs) == 1
        assert docs[0].drug_name == "测试药"


@pytest.mark.asyncio
async def test_section_detection():
    """Test that sections are correctly detected from drug manual content."""
    from app.rag.ingestor import detect_section

    assert detect_section("【不良反应】常见恶心、呕吐、头晕。") == "不良反应"
    assert detect_section("【禁忌】对本品过敏者禁用。严重肝肾功能不全者禁用。") == "禁忌"
    assert detect_section("【注意事项】饭后服用，避免饮酒。") == "注意事项"
    assert detect_section("【药物相互作用】与华法林合用增加出血风险。") == "药物相互作用"
    assert detect_section("本品用于治疗感冒引起的发热。") == "通用"
