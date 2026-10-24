from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Literal
from zoneinfo import ZoneInfo

from agentkit.comfy import WorkflowInputRef, WorkflowProfile  # noqa: F401 - WorkflowInputRef reexportado
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class WorkflowState(StrEnum):
    DRAFT = "draft"
    CONCEPTS_READY = "concepts_ready"
    PROMPTS_APPROVED = "prompts_approved"
    GENERATING = "generating"
    CANDIDATES_READY = "candidates_ready"
    SELECTED = "selected"
    UPSCALING = "upscaling"
    FINAL_REVIEW = "final_review"
    METADATA_READY = "metadata_ready"
    LOCALLY_APPROVED = "locally_approved"
    PREPARED_FOR_TRANSFER = "prepared_for_transfer"
    TRANSFERRED = "transferred"
    SUBMITTED = "submitted"
    ADOBE_APPROVED = "adobe_approved"
    ADOBE_REJECTED = "adobe_rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"


STATE_TRANSITIONS: dict[WorkflowState, set[WorkflowState]] = {
    WorkflowState.DRAFT: {WorkflowState.CONCEPTS_READY, WorkflowState.CANCELLED, WorkflowState.FAILED},
    WorkflowState.CONCEPTS_READY: {
        WorkflowState.DRAFT, WorkflowState.PROMPTS_APPROVED,
        WorkflowState.CANCELLED, WorkflowState.FAILED,
    },
    WorkflowState.PROMPTS_APPROVED: {
        WorkflowState.GENERATING, WorkflowState.CANCELLED, WorkflowState.FAILED,
    },
    WorkflowState.GENERATING: {
        WorkflowState.CANDIDATES_READY, WorkflowState.CANCELLED, WorkflowState.FAILED,
    },
    WorkflowState.CANDIDATES_READY: {
        WorkflowState.GENERATING, WorkflowState.SELECTED,
        WorkflowState.CANCELLED, WorkflowState.FAILED,
    },
    WorkflowState.SELECTED: {WorkflowState.UPSCALING, WorkflowState.CANCELLED, WorkflowState.FAILED},
    WorkflowState.UPSCALING: {
        WorkflowState.SELECTED, WorkflowState.FINAL_REVIEW, WorkflowState.CANCELLED, WorkflowState.FAILED,
    },
    WorkflowState.FINAL_REVIEW: {
        WorkflowState.SELECTED, WorkflowState.METADATA_READY,
        WorkflowState.CANCELLED, WorkflowState.FAILED,
    },
    WorkflowState.METADATA_READY: {
        WorkflowState.FINAL_REVIEW, WorkflowState.LOCALLY_APPROVED,
        WorkflowState.CANCELLED, WorkflowState.FAILED,
    },
    WorkflowState.LOCALLY_APPROVED: {
        WorkflowState.METADATA_READY, WorkflowState.PREPARED_FOR_TRANSFER,
        WorkflowState.CANCELLED, WorkflowState.FAILED,
    },
    WorkflowState.PREPARED_FOR_TRANSFER: {
        WorkflowState.TRANSFERRED, WorkflowState.CANCELLED, WorkflowState.FAILED,
    },
    WorkflowState.TRANSFERRED: {WorkflowState.SUBMITTED, WorkflowState.FAILED},
    WorkflowState.SUBMITTED: {WorkflowState.ADOBE_APPROVED, WorkflowState.ADOBE_REJECTED},
    WorkflowState.FAILED: {
        WorkflowState.DRAFT, WorkflowState.CONCEPTS_READY, WorkflowState.PROMPTS_APPROVED,
        WorkflowState.CANDIDATES_READY, WorkflowState.SELECTED,
        WorkflowState.FINAL_REVIEW, WorkflowState.METADATA_READY,
    },
    WorkflowState.CANCELLED: {
        WorkflowState.DRAFT, WorkflowState.PROMPTS_APPROVED,
        WorkflowState.CANDIDATES_READY, WorkflowState.SELECTED,
    },
    # SUBMITTED representa "pendente": permite corrigir uma decisão registrada por engano.
    WorkflowState.ADOBE_APPROVED: {WorkflowState.ADOBE_REJECTED, WorkflowState.SUBMITTED},
    WorkflowState.ADOBE_REJECTED: {WorkflowState.ADOBE_APPROVED, WorkflowState.SUBMITTED},
}

ModerationStatus = Literal["pending", "approved", "rejected"]
ModerationSource = Literal["flow", "external"]


class CollectionBrief(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    theme: str = Field(min_length=3, max_length=240)
    objective: str = Field(min_length=3, max_length=1000)
    audience: str = Field(default="", max_length=500)
    commercial_application: str = Field(default="", max_length=500)
    orientation: Literal["horizontal", "vertical", "square", "mixed"] = "mixed"
    concept_count: int = Field(default=1, ge=1, le=20)
    composition_constraints: list[str] = Field(default_factory=list, max_length=20)
    existing_catalog: str = Field(default="", max_length=5000)
    sales_observations: str = Field(default="", max_length=3000)


class ConceptPlan(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    concept: str = Field(min_length=5, max_length=300)
    commercial_application: str = Field(min_length=3, max_length=500)
    main_subject: str = Field(min_length=2, max_length=300)
    composition_and_copy_space: str = Field(min_length=3, max_length=1000)
    orientation: str = Field(pattern="^(horizontal|vertical|square)$")
    prompt_en: str = Field(min_length=10, max_length=4000)
    negative_prompt: str = Field(default="", max_length=2000)
    differentiation_rationale: str = Field(min_length=3, max_length=1000)


class PlannerOutput(BaseModel):
    concepts: list[ConceptPlan] = Field(min_length=1, max_length=10)
    collection_summary: str = Field(default="", max_length=2000)
    style_guide: str = Field(default="", max_length=2000)


class PromptRevision(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    prompt_en: str = Field(min_length=10, max_length=4000)
    negative_prompt: str = Field(default="", max_length=2000)
    removed_terms: list[str] = Field(default_factory=list, max_length=20)
    change_summary: str = Field(default="", max_length=1000)

    @model_validator(mode="after")
    def removed_terms_stay_removed(self) -> "PromptRevision":
        # Barreira determinística: o que o refinador declarou ter tirado não pode voltar
        # ao prompt; a violação vira erro de contrato e o agente tenta de novo.
        prompt = self.prompt_en.casefold()
        kept = [term for term in self.removed_terms if term.strip() and term.strip().casefold() in prompt]
        if kept:
            raise ValueError(
                "prompt_en ainda contém termos que deveriam ter sido removidos: " + ", ".join(kept)
            )
        return self


class CorrectionInstruction(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    instruction_en: str = Field(min_length=10, max_length=600)
    summary: str = Field(default="", max_length=500)


class VisualReview(BaseModel):
    observations: list[str] = Field(default_factory=list, max_length=30)
    possible_defects: list[dict[str, str]] = Field(default_factory=list, max_length=30)
    composition_quality: str = Field(max_length=1000)
    concept_coherence: str = Field(max_length=1000)
    recommendation: str = Field(pattern="^(review|select|reject)$")
    uncertainties: list[str] = Field(default_factory=list, max_length=20)
    analysis_source: str = "image"

    @field_validator("analysis_source")
    @classmethod
    def require_image_source(cls, value: str) -> str:
        if value != "image":
            raise ValueError("Uma revisão visual precisa declarar análise da imagem.")
        return value


class MetadataSuggestion(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title_en: str = Field(min_length=3, max_length=200)
    keywords: list[str] = Field(min_length=1, max_length=50)
    category_suggestion: str = Field(default="", max_length=200)
    terms_to_confirm: list[str] = Field(default_factory=list, max_length=30)
    analysis_source: str = Field(pattern="^(image|description)$")

    @field_validator("keywords")
    @classmethod
    def normalize_keywords(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for value in values:
            term = " ".join(value.split())
            key = term.casefold()
            if term and key not in seen:
                seen.add(key)
                cleaned.append(term)
        if not cleaned:
            raise ValueError("Inclua ao menos uma palavra-chave.")
        return cleaned

    @field_validator("title_en")
    @classmethod
    def exclude_ai_label_from_title(cls, value: str) -> str:
        if "generative ai" in value.casefold():
            raise ValueError("Não inclua 'generative AI' no título comercial.")
        return value


REQUIRED_PROFILE_ROLES = {
    "generation": {"prompt"},
    "upscale": {"image"},
    "inpainting": {"image", "prompt"},
}
ALLOWED_PROFILE_ROLES = {
    "prompt", "negative_prompt", "seed", "width", "height", "steps", "cfg",
    "sampler_name", "scheduler", "filename_prefix", "image", "multiplier",
}


class ComfyWorkflowProfile(WorkflowProfile):
    """Workflow do agentkit restrito aos tipos e papéis usados pelo flow_imagens."""

    kind: str = Field(pattern="^(generation|upscale|inpainting)$")

    @model_validator(mode="after")
    def require_flow_roles(self) -> ComfyWorkflowProfile:
        missing_roles = self.missing_roles(REQUIRED_PROFILE_ROLES[self.kind])
        if missing_roles:
            raise ValueError(
                "Mapeie as entradas obrigatórias: " + ", ".join(sorted(missing_roles)) + "."
            )
        for role in self.mappings:
            if role not in ALLOWED_PROFILE_ROLES:
                raise ValueError(f"Papel de entrada não suportado: {role}.")
        return self


class ExportRules(BaseModel):
    min_megapixels: float | None = Field(default=None, gt=0)
    max_megapixels: float | None = Field(default=None, gt=0)
    max_file_bytes: int | None = Field(default=None, gt=0)
    jpeg_quality: int = Field(default=95, ge=85, le=100)
    technical_rules_verified: bool = False
    checked_at: str = "2026-09-30"
    source: str = ""


class PerformanceEntry(BaseModel):
    report_date: date
    download_count: int = Field(default=0, ge=0)
    revenue: float = Field(default=0, ge=0)
    revenue_currency: str = Field(default="USD", pattern="^[A-Z]{3}$")
    moderation_status: Literal["unknown", "pending", "accepted", "rejected"] = "unknown"
    generation_seconds: float = Field(default=0, ge=0)
    upscale_seconds: float = Field(default=0, ge=0)
    manual_minutes: float = Field(default=0, ge=0)
    user_cost: float = Field(default=0, ge=0)
    cost_currency: str = Field(default="USD", pattern="^[A-Z]{3}$")
    notes: str = Field(default="", max_length=1000)


# Fuso usado para semanas e meses dos indicadores (os timestamps do banco ficam em UTC).
LOCAL_TZ = ZoneInfo("Europe/Madrid")


class CollectionCostEntry(BaseModel):
    cost_date: date
    amount: float = Field(gt=0, le=1_000_000_000, allow_inf_nan=False)
    currency: str = Field(default="USD", pattern="^[A-Z]{3}$")
    notes: str = Field(default="", max_length=1000)

    @field_validator("cost_date")
    @classmethod
    def cost_not_in_future(cls, value: date) -> date:
        if value > datetime.now(LOCAL_TZ).date():
            raise ValueError("A data do custo não pode estar no futuro.")
        return value


class MonthlySalesEntry(BaseModel):
    """Downloads e receita de um mês, conferidos manualmente no painel da Adobe."""

    month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    downloads: int = Field(default=0, ge=0)
    revenue: float = Field(default=0, ge=0)
    currency: str = Field(default="USD", pattern="^[A-Z]{3}$")
    notes: str = Field(default="", max_length=1000)

    @field_validator("month")
    @classmethod
    def month_not_in_future(cls, value: str) -> str:
        if value > datetime.now(LOCAL_TZ).strftime("%Y-%m"):
            raise ValueError("O mês do lançamento não pode estar no futuro.")
        return value
