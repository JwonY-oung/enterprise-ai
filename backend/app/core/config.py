from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Enterprise AI Knowledge Base"
    database_url: str = "sqlite:///./data/app.db"
    secret_key: str = "change-this-in-production"
    access_token_expire_minutes: int = 1440
    upload_dir: Path = Path("./data/uploads")
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4.1-mini"
    embedding_model: str = "BAAI/bge-small-zh-v1.5"
    embedding_dimensions: int = 512
    retrieval_top_k: int = 8
    retrieval_candidate_multiplier: int = 4
    rerank_top_k: int = 3
    retrieval_min_score: float = 0.25
    evidence_min_score: float = 0.38
    evidence_min_lexical_score: float = 0.08
    chunk_size: int = 700
    chunk_overlap: int = 100
    chat_history_messages: int = 8
    rerank_provider: str = "llm"
    rerank_api_key: str = ""
    rerank_base_url: str = ""
    rerank_model: str = "BAAI/bge-reranker-v2-m3"

    # 视觉模型（扫描 PDF OCR / 图片理解）。留空则复用主 LLM；VISION_ENABLED=false 时禁用视觉调用。
    vision_enabled: bool = False
    vision_api_key: str = ""
    vision_base_url: str = ""
    vision_model: str = ""
    ocr_enabled: bool = True
    ocr_min_chars: int = 20          # PDF 单页抽取文本低于该字符数视为扫描页，触发 OCR
    ocr_image_max_side: int = 1600   # 送入视觉模型前限制图片长边（像素）

    # 任务队列 / Worker（断点恢复）
    task_mode: str = "inline"        # inline：请求内同步处理；worker：独立进程轮询持久化队列
    worker_poll_interval_seconds: int = 5
    worker_stale_timeout_seconds: int = 300  # processing 超过该时长未完成视为中断，可被重新认领
    worker_max_attempts: int = 3

    # 安全审计
    audit_enabled: bool = True

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
