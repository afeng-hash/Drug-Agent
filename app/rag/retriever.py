"""
Drug manual retriever — Milvus 向量搜索。

将药品说明书文档分块后存入 Milvus，检索时用向量相似度找出最相关的内容。
用于 Explain 节点：当用户问某个药品的副作用/禁忌/用法时，从 Milvus 中检索。

数据结构：
  Collection: drug_manuals
  Fields: id (int64), drug_id (varchar), drug_name (varchar),
          section (varchar), content (varchar), vector (float_vector[1024])
"""

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
"""Milvus 中的集合名称"""

VECTOR_DIM = 1024
"""向量维度。text-embedding-v3 默认输出 1024 维"""


class DrugManualRetriever:
    """药品说明书向量检索器。

    负责：
      1. 连接 Milvus 并确保 collection 存在
      2. 将查询文本向量化后做 ANN 搜索
      3. 按药品名过滤 + 语义相似度排序
    """

    def __init__(self, settings: Settings, llm_client: LLMClient):
        """初始化检索器。

        Args:
            settings:   应用配置（包含 Milvus host/port）
            llm_client: LLM 客户端（用于生成 embedding）
        """
        self.settings = settings
        self.llm_client = llm_client

    async def ensure_collection(self) -> None:
        """连接 Milvus 并确保 drug_manuals 集合存在。

        如果集合不存在，创建它并设置 IVF_FLAT 索引。
        在应用启动和健康检查时调用。
        """
        # 建立连接
        connections.connect(
            alias="default",
            host=self.settings.milvus_host,
            port=str(self.settings.milvus_port),
        )

        client = MilvusClient(
            uri=f"http://{self.settings.milvus_host}:{self.settings.milvus_port}"
        )

        # 集合已存在 → 跳过创建
        if client.has_collection(COLLECTION_NAME):
            return

        # 定义 schema
        fields = [
            FieldSchema(name="id", dtype=DataType.INT64, is_primary=True, auto_id=True),
            FieldSchema(name="drug_id", dtype=DataType.VARCHAR, max_length=50),
            FieldSchema(name="drug_name", dtype=DataType.VARCHAR, max_length=100),
            FieldSchema(name="section", dtype=DataType.VARCHAR, max_length=50),
            # section: 说明书段落类型，如 "不良反应" / "禁忌" / "注意事项" / "药物相互作用" / "通用"
            FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=2000),
            # content: 说明书的实际文本片段
            FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=VECTOR_DIM),
            # vector: 文本的 embedding 向量
        ]
        schema = CollectionSchema(fields, description="Drug manual chunks for RAG")
        collection = Collection(name=COLLECTION_NAME, schema=schema)

        # 创建 IVF_FLAT 索引（适合中等规模数据，<1M 条）
        index_params = {
            "metric_type": "COSINE",      # 余弦相似度
            "index_type": "IVF_FLAT",
            "params": {"nlist": 128},     # 聚类中心数
        }
        collection.create_index(field_name="vector", index_params=index_params)
        collection.load()  # 加载到内存

    async def retrieve_multi(
        self, drug_name: str, question: str, top_k: int = 5
    ) -> list[Chunk]:
        """检索与查询最相关的药品说明书片段（多用途接口）。

        与 retrieve() 功能相同，但参数命名更语义化：
          - question 替代 query（更贴近用户场景）
          - 由 search_manual 和 get_drug_detail 工具调用

        Args:
            drug_name: 药品通用名（用于过滤）
            question:  用户关心的问题，如 "副作用" "孕妇能用吗"
            top_k:     返回的最相似结果数量

        Returns:
            按相似度降序排列的 Chunk 列表
        """
        return await self.retrieve(drug_name, query=question, top_k=top_k)

    async def retrieve(
        self, drug_name: str, query: str, top_k: int = 5
    ) -> list[Chunk]:
        """检索与查询最相关的药品说明书片段。

        流程：
          1. 把查询文本向量化（embedding）
          2. 在 Milvus 中做 ANN 搜索，过滤 drug_name
          3. 返回 top_k 个最相似的结果

        Args:
            drug_name: 药品通用名（用于过滤，只检索该药品的说明书）
            query:     搜索查询文本，如 "不良反应 禁忌 注意事项"
            top_k:     返回的最相似结果数量

        Returns:
            按相似度降序排列的 Chunk 列表
        """
        # 向量化查询
        query_vectors = await self.llm_client.embed([query])
        query_vector = query_vectors[0]

        collection = Collection(name=COLLECTION_NAME)
        collection.load()

        # ANN 搜索
        search_params = {"metric_type": "COSINE", "params": {"nprobe": 16}}
        results = collection.search(
            data=[query_vector],
            anns_field="vector",
            param=search_params,
            limit=top_k,
            expr=f'drug_name == "{drug_name}"',  # 只搜这个药品的说明书
            output_fields=["drug_name", "section", "content"],
        )

        chunks = []
        for hits in results:
            for hit in hits:
                chunks.append(Chunk(
                    drug_name=hit.entity.get("drug_name", ""),
                    section=hit.entity.get("section", ""),
                    content=hit.entity.get("content", ""),
                    score=hit.score,  # 余弦相似度，越大越相关
                ))

        # 按相似度降序
        chunks.sort(key=lambda c: c.score, reverse=True)
        return chunks[:top_k]

    def insert(self, data: list[dict]) -> None:
        """向 Milvus 批量插入说明书片段。

        由 seed.py 在数据初始化时调用。

        Args:
            data: 片段列表，每项包含：
              - drug_id:   药品标识（如 "ibuprofen"）
              - drug_name: 药品通用名（如 "布洛芬"）
              - section:   段落类型（如 "不良反应"）
              - content:   文本内容
              - vector:    由 LLM embed() 生成的 1024 维向量
        """
        collection = Collection(name=COLLECTION_NAME)
        collection.load()
        collection.insert(data)
        collection.flush()  # 持久化到磁盘
