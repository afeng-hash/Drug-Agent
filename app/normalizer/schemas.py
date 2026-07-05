"""症状标准化模块的数据模型。"""

from pydantic import BaseModel, Field


class NormalizedSymptom(BaseModel):
    """单个症状的标准化结果。"""

    raw: str = Field(description="原始输入的症状名称，如 '喉咙不舒服'")
    standard: str = Field(description="标准化后的 KG 症状名，如 '咽喉痛'")
    confidence: float = Field(description="匹配置信度 0.0~1.0", ge=0.0, le=1.0)
    method: str = Field(
        description="匹配方式：exact | alias | contains | llm"
    )
    level: int = Field(
        default=1, description="匹配到的 KG 症状层级 (1=coarse, 2=specific, 3=fine-grained)"
    )


class NormalizationResult(BaseModel):
    """批量症状标准化的汇总结果。"""

    results: list[NormalizedSymptom] = Field(
        description="每个输入症状的标准化结果，顺序与输入一致"
    )
    total_time_ms: float = Field(default=0.0, description="标准化总耗时（毫秒）")
    llm_calls: int = Field(default=0, description="本次 LLM 实际调用次数")
    cache_hits: int = Field(default=0, description="LLM 缓存命中次数")
    discarded_count: int = Field(default=0, description="因风险分层被丢弃的症状数量")
