"""Drug manual retriever — Milvus vector search for drug instructions."""

from pymilvus import (
    Collection,
    CollectionSchema,
    DataType,
    FieldSchema,
    MilvusClient,
    connections,
)

from app.config import Settings
from app.llm.client import LLMClient
from app.rag.schemas import Chunk

COLLECTION_NAME = "drug_manuals"
VECTOR_DIM = 1024  # text-embedding-v3 default dimension


class DrugManualRetriever:
    """Retrieves drug manual chunks from Milvus via vector similarity search."""

    def __init__(self, settings: Settings, llm_client: LLMClient):
        self.settings = settings
        self.llm_client = llm_client

    async def ensure_collection(self) -> None:
        """Connect to Milvus and create the collection if it doesn't exist."""
        connections.connect(
            alias="default",
            host=self.settings.milvus_host,
            port=str(self.settings.milvus_port),
        )

        client = MilvusClient(
            uri=f"http://{self.settings.milvus_host}:{self.settings.milvus_port}"
        )

        if client.has_collection(COLLECTION_NAME):
            return

        fields = [
            FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
            FieldSchema(name="drug_id", dtype=DataType.VARCHAR, max_length=50),
            FieldSchema(name="drug_name", dtype=DataType.VARCHAR, max_length=100),
            FieldSchema(name="section", dtype=DataType.VARCHAR, max_length=50),
            FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=2000),
            FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=VECTOR_DIM),
        ]
        schema = CollectionSchema(fields, description="Drug manual chunks for RAG")
        collection = Collection(name=COLLECTION_NAME, schema=schema)

        index_params = {
            "metric_type": "COSINE",
            "index_type": "IVF_FLAT",
            "params": {"nlist": 128},
        }
        collection.create_index(field_name="vector", index_params=index_params)
        collection.load()

    async def retrieve(
        self, drug_name: str, query: str, top_k: int = 5
    ) -> list[Chunk]:
        """Retrieve the most relevant drug manual chunks.

        Args:
            drug_name: Filter results to this drug's generic name.
            query: The search query (e.g., "副作用 不良反应").
            top_k: Number of chunks to return.

        Returns:
            List of Chunk objects sorted by relevance score (descending).
        """
        query_vectors = await self.llm_client.embed([query])
        query_vector = query_vectors[0]

        collection = Collection(name=COLLECTION_NAME)
        collection.load()

        search_params = {"metric_type": "COSINE", "params": {"nprobe": 16}}
        results = collection.search(
            data=[query_vector],
            anns_field="vector",
            param=search_params,
            limit=top_k,
            expr=f'drug_name == "{drug_name}"',
            output_fields=["drug_name", "section", "content"],
        )

        chunks = []
        for hits in results:
            for hit in hits:
                chunks.append(Chunk(
                    drug_name=hit.entity.get("drug_name", ""),
                    section=hit.entity.get("section", ""),
                    content=hit.entity.get("content", ""),
                    score=hit.score,
                ))

        # Sort by score descending
        chunks.sort(key=lambda c: c.score, reverse=True)
        return chunks[:top_k]

    def insert(self, data: list[dict]) -> None:
        """Insert multiple chunks into Milvus.

        Args:
            data: List of dicts with keys: drug_id, drug_name, section, content, vector.
        """
        collection = Collection(name=COLLECTION_NAME)
        collection.load()
        collection.insert(data)
        collection.flush()
