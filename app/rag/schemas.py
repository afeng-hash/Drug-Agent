"""
RAG data schemas — 药品说明书的数据模型。
"""

from pydantic import BaseModel


class Document(BaseModel):
    """一篇待入库的药品说明书文档。

    在数据初始化（data/seed.py）时从 data/rag_docs/ 目录读取 txt 文件，
    分块后转成 Document 列表，向量化后存入 Milvus。
    """

    drug_id: str
    """药品标识，如 "ibuprofen", "acetaminophen" """

    drug_name: str
    """药品通用名（中文），如 "布洛芬", "对乙酰氨基酚" """

    section: str
    """说明书段落类型，如 "不良反应" / "禁忌" / "注意事项" / "药物相互作用" / "通用" """

    content: str
    """段落文本内容"""


class Chunk(BaseModel):
    """一次向量检索返回的说明书片段。

    DrugManualRetriever.retrieve() 的返回值类型。
    """

    drug_name: str
    """药品通用名"""

    section: str
    """段落类型（不良反应、禁忌等）"""

    content: str
    """段落文本内容"""

    score: float
    """余弦相似度分数。越大表示与查询越相关。范围 -1 到 1"""
