"""Agency-specific local packages and recorded policy reviews. No portal access."""
from __future__ import annotations

import csv
import io
import json
from datetime import date
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from flowapp.moderation import today_local
from flowapp.schemas import ExportRules


class AgencyExportProfile(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = False
    ai_policy: Literal["allowed", "conditional", "not_allowed", "unknown"] = "unknown"
    policy_verified: bool = False
    policy_source: str = ""
    policy_checked_on: date | None = None
    policy_notes: str = Field(default="", max_length=2000)
    file_format: Literal["JPEG", "PNG"] = "JPEG"
    allowed_formats: list[Literal["JPEG", "PNG"]] = Field(default_factory=lambda: ["JPEG", "PNG"], min_length=1)
    metadata_format: Literal["json", "csv", "adobe_csv"] = "json"
    max_width: int | None = Field(default=None, ge=1, le=32768)
    max_height: int | None = Field(default=None, ge=1, le=32768)
    min_short_edge: int | None = Field(default=None, ge=1, le=32768)
    rules: ExportRules = Field(default_factory=ExportRules)
    ai_title_suffix: str = Field(default="", max_length=100)
    required_keywords: list[str] = Field(default_factory=list, max_length=50)

    def activation_errors(self, today: date | None = None) -> list[str]:
        today = today if today is not None else today_local()
        errors = []
        if self.ai_policy not in {"allowed", "conditional"}:
            errors.append("A política registrada não permite este envio de imagens geradas por IA.")
        if not self.policy_verified or self.policy_checked_on is None or self.policy_checked_on > today:
            errors.append("Registre a conferência da política de IA com uma data válida.")
        source = urlsplit(self.policy_source)
        if source.scheme not in {"http", "https"} or not source.hostname:
            errors.append("Informe a fonte oficial da política de IA (URL HTTP/HTTPS).")
        if self.file_format not in self.allowed_formats:
            errors.append("O formato escolhido não consta nos formatos conferidos para este perfil.")
        if self.id != "adobe":
            if not self.rules.technical_rules_verified or not self.rules.source.strip():
                errors.append("Confira os requisitos técnicos e registre a fonte antes de ativar o perfil.")
            try:
                checked = date.fromisoformat(self.rules.checked_at)
                if checked > today:
                    raise ValueError
            except (ValueError, TypeError):
                errors.append("Informe uma data válida para a conferência dos requisitos técnicos.")
        return errors

    def require_active(self) -> None:
        errors = self.activation_errors()
        if not self.enabled:
            errors.insert(0, "Perfil de exportação desativado.")
        if errors:
            raise ValueError(" ".join(errors))

    @model_validator(mode="after")
    def validate_profile(self):
        if self.rules.min_megapixels and self.rules.max_megapixels and self.rules.min_megapixels > self.rules.max_megapixels:
            raise ValueError("O mínimo de megapixels não pode superar o máximo.")
        if self.id == "adobe":
            if self.file_format != "JPEG" or self.metadata_format != "adobe_csv" or self.max_width or self.max_height or self.min_short_edge:
                raise ValueError("O perfil Adobe usa JPEG/CSV Adobe e os limites técnicos da configuração existente.")
        elif self.metadata_format == "adobe_csv":
            raise ValueError("O CSV Adobe é exclusivo do perfil Adobe.")
        if self.enabled:
            self.require_active()
        return self

    def metadata_values(self, title: str, keywords: list[str]) -> tuple[str, list[str]]:
        if self.ai_title_suffix and not title.casefold().endswith(self.ai_title_suffix.casefold()):
            title = f"{title.rstrip()} {self.ai_title_suffix.lstrip()}"
        result, seen = [], set()
        for keyword in [*keywords, *self.required_keywords]:
            keyword = keyword.strip()
            if keyword and keyword.casefold() not in seen:
                result.append(keyword)
                seen.add(keyword.casefold())
        return title, result


def default_profiles() -> list[AgencyExportProfile]:
    checked = date(2026, 10, 25)
    return [
        AgencyExportProfile(id="adobe", name="Adobe Stock", enabled=True, ai_policy="conditional", policy_verified=True,
            policy_source="https://helpx.adobe.com/stock/contributor/submit-your-content/submit-generative-ai-content/generative-ai-content-guidelines.html",
            policy_checked_on=checked, file_format="JPEG", allowed_formats=["JPEG"], metadata_format="adobe_csv",
            policy_notes="Declarar conteúdo criado com IA no portal e conferir direitos, pessoas e propriedades. O fluxo Adobe e seus limites existentes continuam disponíveis."),
        AgencyExportProfile(id="dreamstime", name="Dreamstime", enabled=True, ai_policy="conditional", policy_verified=True,
            policy_source="https://www.dreamstime.com/faqs-detail-2", policy_checked_on=checked,
            policy_notes="Conferir direitos de uso, identificar IA no título/descrição e selecionar a categoria Generative AI no portal. Não enviar rostos humanos realistas gerados; as exceções da FAQ precisam de revisão humana.",
            allowed_formats=["JPEG"], ai_title_suffix=" [AI-generated]",
            rules=ExportRules(min_megapixels=3, technical_rules_verified=True, checked_at=checked.isoformat(),
                              source="https://www.dreamstime.com/faqs-detail-2")),
        AgencyExportProfile(id="freepik", name="Freepik / Magnific (rascunho)", ai_policy="conditional", policy_verified=True,
            policy_source="https://www.magnific.com/ai/contributors/ai-generated-resources-general-guidelines",
            policy_checked_on=checked, allowed_formats=["JPEG"], required_keywords=["_ai_generated"],
            policy_notes="A orientação oficial exige marcar conteúdo como criado por IA. Perfil pendente de conferência dos limites técnicos; não há esquema CSV de importação validado neste app."),
        AgencyExportProfile(id="shutterstock", name="Shutterstock — IA não aceita", ai_policy="not_allowed", policy_verified=True,
            policy_source="https://submit.shutterstock.com/help/en/articles/10594622-content-policy-updates-ai-generated-content",
            policy_checked_on=checked, policy_notes="A política consultada não aceita conteúdo gerado por IA enviado por colaboradores."),
        AgencyExportProfile(id="getty", name="Getty / iStock — IA não aceita", ai_policy="not_allowed", policy_verified=True,
            policy_source="https://www.gettyimages.co.uk/workwithus", policy_checked_on=checked,
            policy_notes="A página para colaboradores não aceita arquivos criados ou modificados por modelos generativos."),
    ]


class AgencyExportsConfig(BaseModel):
    profiles: list[AgencyExportProfile] = Field(default_factory=default_profiles, min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_profiles(self):
        ids = [profile.id for profile in self.profiles]
        if len(ids) != len(set(ids)) or "adobe" not in ids:
            raise ValueError("Os identificadores devem ser únicos e o perfil Adobe deve ser preservado.")
        return self

    def profile(self, profile_id: str) -> AgencyExportProfile:
        for profile in self.profiles:
            if profile.id == profile_id:
                return profile
        raise ValueError("Perfil de exportação não encontrado.")


def metadata_reference(report: dict, profile: AgencyExportProfile) -> tuple[str, str]:
    """Reference sidecar, intentionally not advertised as any agency's CSV import schema."""
    if profile.metadata_format == "json":
        data = {key: report[key] for key in ("filename", "title", "keywords", "format", "width", "height",
                                             "megapixels", "size_bytes", "sha256")}
        data.update(agency=profile.name, ai_generated=True, policy_source=profile.policy_source,
                    policy_checked_on=profile.policy_checked_on.isoformat(), manual_conditions=profile.policy_notes)
        return json.dumps(data, ensure_ascii=False, indent=2), ".metadata.json"
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["filename", "title", "keywords", "ai_generated", "agency", "policy_source", "policy_checked_on", "manual_conditions"])
    values = [report["filename"], report["title"], ", ".join(report["keywords"]), "true", profile.name,
              profile.policy_source, profile.policy_checked_on.isoformat(), profile.policy_notes]
    writer.writerow(["'"+value if value.lstrip().startswith(("=", "+", "-", "@")) else value for value in values])
    return output.getvalue(), ".metadata.csv"
