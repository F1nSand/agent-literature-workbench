from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None,
        env_prefix="",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Literature Workbench API"
    environment: str = "development"
    database_url: SecretStr = SecretStr(
        "postgresql+psycopg://workbench:workbench@localhost:5432/workbench"
    )
    siliconflow_api_key: SecretStr | None = None
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    siliconflow_chat_model: str = "deepseek-ai/DeepSeek-V3.2"
    siliconflow_ocr_model: str = "Qwen/Qwen3-VL-8B-Instruct"
    siliconflow_vision_model: str = "Qwen/Qwen3-VL-32B-Instruct"
    siliconflow_embedding_model: str = "Qwen/Qwen3-Embedding-4B"
    siliconflow_reranker_model: str = "BAAI/bge-reranker-v2-m3"
    embedding_dimensions: int = 2560
    data_dir: str = "/data"
    max_pdf_bytes: int = 100 * 1024 * 1024
    max_pdf_pages: int = 500


@lru_cache
def get_settings() -> Settings:
    return Settings()
