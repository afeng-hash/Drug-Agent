"""Application configuration via pydantic-settings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── LLM ──
    llm_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    llm_api_key: str = ""
    llm_model: str = "qwen-plus"
    embedding_model: str = "text-embedding-v3"

    # ── Database ──
    database_url: str = "postgresql+asyncpg://drug_agent:drug_agent@localhost:5432/drug_agent"

    # ── Milvus ──
    milvus_host: str = "localhost"
    milvus_port: int = 19530

    # ── LangSmith ──
    langsmith_api_key: str = ""
    langsmith_project: str = "drug-agent"

    # ── App ──
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    debug: bool = True

    # ── Session ──
    session_expire_minutes: int = 30
    max_consult_rounds: int = 6
