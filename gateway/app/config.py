"""网关配置，全部来自环境变量。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- 后端 ---
    backend_base_url: str = "http://localhost:8091"
    model_id: str = "Qwen/Qwen-Image-2.1"

    # --- 并发 ---
    # 同步接口与异步 worker 共享同一个信号量，避免把扩散引擎压垮。
    # SGLang 的批处理是波次准入，单卡从 2 起步是稳妥值。
    max_concurrency: int = Field(2, ge=1)
    max_queue_depth: int = Field(32, ge=1)
    request_timeout_s: float = Field(300.0, gt=0)

    # --- 任务生命周期 ---
    task_ttl_hours: float = Field(24.0, gt=0)
    sweep_interval_s: float = Field(600.0, gt=0)

    # --- 请求约束 ---
    max_ref_images: int = Field(10, ge=1)  # Qwen-Image-2.1 上限
    max_upload_bytes: int = Field(16 * 1024 * 1024, gt=0)
    # 后端 multipart 参考图的字段名。SGLang 用 `image[]`，vllm-omni 用 `image` ——
    # 实测后若有出入，改这里即可，不必碰代码。
    edit_image_field: str = "image"
    edit_mask_field: str = "mask"

    # --- 对外 ---
    public_base_url: str = "http://localhost:8090"
    data_dir: Path = Path("./data")

    @property
    def tasks_db(self) -> Path:
        return self.data_dir / "tasks.db"

    @property
    def outputs_dir(self) -> Path:
        return self.data_dir / "outputs"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    def ensure_dirs(self) -> None:
        for p in (self.data_dir, self.outputs_dir, self.uploads_dir):
            p.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
