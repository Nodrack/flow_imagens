from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from urllib.parse import urlsplit

from flowapp.schemas import ExportRules
from flowapp.quality import QualityRules
from flowapp.similarity import SimilarityRules
from flowapp.seasonal import SeasonalCalendar
from flowapp.briefings import CommercialBriefings


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    comfy_base_url: str = "http://127.0.0.1:8188"
    comfy_generation_url: str = ""
    comfy_upscale_url: str = ""
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
    seasonal_calendar: SeasonalCalendar = Field(default_factory=SeasonalCalendar)
    commercial_briefings: CommercialBriefings = Field(default_factory=CommercialBriefings)
    visual_reference_limit: int = Field(default=12, ge=1, le=100)
    visual_reference_prompt_chars: int = Field(default=600, ge=50, le=4000)
    export_rules: ExportRules = Field(default_factory=ExportRules)
    # Cronômetro de horas que passar disso é tratado como esquecido e pede os minutos reais.
    timer_max_hours: float = Field(default=12.0, ge=1, le=24)

    @field_validator("comfy_base_url", "comfy_generation_url", "comfy_upscale_url")
    @classmethod
    def validate_comfy_url(cls, value: str, info) -> str:
        value = value.strip().rstrip("/")
        if not value:
            if info.field_name == "comfy_base_url":
                raise ValueError("Informe a URL base do ComfyUI.")
            return value
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("Use uma URL HTTP/HTTPS válida para o ComfyUI.")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("A URL do ComfyUI deve ser a base do serviço, sem credenciais, query ou fragmento.")
        _ = parts.port  # Reject invalid port numbers before any request.
        return value

    def comfy_url_for(self, stage: str) -> str:
        selected = self.comfy_upscale_url if stage == "upscale" else self.comfy_generation_url
        return selected or self.comfy_base_url


ENV_CONFIG = {
    "FLOW_COMFY_BASE_URL": "comfy_base_url",
    "FLOW_COMFY_GENERATION_URL": "comfy_generation_url",
    "FLOW_COMFY_UPSCALE_URL": "comfy_upscale_url",
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
