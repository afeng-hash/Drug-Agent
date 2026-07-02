"""RAG data schemas."""

from pydantic import BaseModel


class Document(BaseModel):
    """A source document for RAG ingestion."""
    drug_id: str
    drug_name: str
    section: str  # e.g. "不良反应", "禁忌", "注意事项", "药物相互作用", "通用"
    content: str


class Chunk(BaseModel):
    """A retrieved chunk from vector search."""
    drug_name: str
    section: str
    content: str
    score: float
