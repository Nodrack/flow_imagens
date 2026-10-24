from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from flowapp.schemas import ExportRules
from flowapp.quality import QualityRules
from flowapp.similarity import SimilarityRules


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    comfy_base_url: str = "http://127.0.0.1:8188"
    lmstudio_base_url: str = "http://127.0.0.1:1234"
    planner_model: str = ""
    vision_model: str = ""
    request_timeout_seconds: float = Field(default=20.0, ge=1, le=300)
    agent_timeout_seconds: int = Field(default=300, ge=10, le=3600)
    model_load_timeout_seconds: int = Field(default=600, ge=10, le=3600)
    generation_timeout_seconds: int = Field(default=1800, ge=10, le=14400)
    poll_interval_seconds: float = Field(default=1.0, ge=0.2, le=15)
    max_agent_attempts: int = Field(default=2, ge=1, le=3)
    candidates_per_concept: int = Field(default=1, ge=1, le=4)
    auto_regenerations: int = Field(default=1, ge=0, le=3)
    free_vram_between_phases: bool = True
    generation_sizes: dict[str, tuple[int, int]] = Field(
        default_factory=lambda: {
            "horizontal": (1344, 768),
            "vertical": (768, 1344),
            "square": (1024, 1024),
        }
    )
    # Multiplicador aplicado ao tamanho da imagem escolhida quando o workflow de upscale mapeia width/height.
    upscale_factor: float = Field(default=2.0, ge=1, le=8)
    quality_rules: QualityRules = Field(default_factory=QualityRules)
    similarity_rules: SimilarityRules = Field(default_factory=SimilarityRules)
    export_rules: ExportRules = Field(default_factory=ExportRules)
    # Cronômetro de horas que passar disso é tratado como esquecido e pede os minutos reais.
    timer_max_hours: float = Field(default=12.0, ge=1, le=24)


ENV_CONFIG = {
    "FLOW_COMFY_BASE_URL": "comfy_base_url",
    "FLOW_LMSTUDIO_BASE_URL": "lmstudio_base_url",
    "FLOW_PLANNER_MODEL": "planner_model",
    "FLOW_VISION_MODEL": "vision_model",
}


def load_config(path: Path) -> AppConfig:
    values: dict[str, Any] = {}
    if path.exists():
        values = json.loads(path.read_text(encoding="utf-8"))
    for environment_name, field_name in ENV_CONFIG.items():
        environment_value = os.getenv(environment_name)
        if environment_value:
            values[field_name] = environment_value
    return AppConfig.model_validate(values)


def save_config(path: Path, config: AppConfig) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(config.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
