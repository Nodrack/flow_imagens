"""Editable starting points for commercial briefs, without claims about demand or sales."""
from __future__ import annotations

import json
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from flowapp.schemas import ConceptPlan


class BriefingTemplate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    label: str = Field(min_length=1, max_length=100)
    theme: str = Field(min_length=3, max_length=240)
    objective: str = Field(min_length=3, max_length=1000)
    audience: str = Field(default="", max_length=500)
    commercial_application: str = Field(default="", max_length=500)
    orientation: Literal["horizontal", "vertical", "square", "mixed"] = "mixed"
    composition_constraints: list[str] = Field(default_factory=list, max_length=20)

    def brief_values(self) -> dict:
        return self.model_dump(exclude={"label"})


def default_templates() -> list[BriefingTemplate]:
    return [
        BriefingTemplate(
            label="Ingredientes e comida", theme="Ingredientes e comida em composições comerciais",
            objective="Apresentar ingredientes reconhecíveis e combinações apetitosas em cenas distintas, com área limpa para inserir uma mensagem depois.",
            audience="Marcas de alimentos, restaurantes e criadores de conteúdo culinário",
            commercial_application="Banners de cardápio, embalagens e campanhas de alimentos",
            composition_constraints=["Espaço limpo para texto, com posição definida em cada conceito",
                "Variar arranjo, ponto de vista, paleta e superfície entre as cenas", "Alimentos e utensílios sem letras ou marcas visíveis"],
        ),
        BriefingTemplate(
            label="Fundos com espaço para texto", theme="Fundos para peças de comunicação visual",
            objective="Criar fundos que permitam inserir títulos e chamadas legíveis, com composições e materiais distintos adequados ao uso proposto.",
            audience="Designers, agências e equipes de comunicação",
            commercial_application="Banners, capas de apresentações e anúncios digitais",
            composition_constraints=["Definir uma área contínua de baixo contraste para texto",
                "Concentrar elementos de interesse fora da área destinada ao texto", "Não gerar letras, números ou logotipos"],
        ),
        BriefingTemplate(
            label="Negócios com uso claro", theme="Situações concretas de negócios e trabalho",
            objective="Representar decisões, colaboração ou organização em situações compreensíveis, ligando cada conceito a uma peça de comunicação específica.",
            audience="Empresas, consultorias e equipes de comunicação corporativa",
            commercial_application="Capas de relatórios, apresentações comerciais e campanhas institucionais",
            composition_constraints=["Indicar área limpa para título e chamada", "Variar ação, enquadramento, paleta e local entre conceitos",
                "Usar objetos e ambientes genéricos sem marcas ou texto legível"],
        ),
        BriefingTemplate(
            label="Tecnologia com uso claro", theme="Aplicações compreensíveis de tecnologia",
            objective="Mostrar um uso concreto de tecnologia, como proteção de dados, automação ou conectividade, evitando misturas abstratas de símbolos sem uma mensagem clara.",
            audience="Empresas de tecnologia, educadores e equipes de marketing",
            commercial_application="Banners de produtos, materiais educativos e apresentações de soluções",
            composition_constraints=["Relacionar cada cena a um uso específico e reservar espaço para sua mensagem",
                "Variar estrutura visual, cores e ambiente entre conceitos", "Interfaces e dispositivos genéricos sem marcas ou texto legível"],
        ),
        BriefingTemplate(
            label="Ambientes", theme="Ambientes funcionais para comunicação comercial",
            objective="Apresentar espaços com uma função clara e materiais plausíveis, variando ponto de vista, disposição e cenário para usos distintos de comunicação.",
            audience="Marcas de decoração, arquitetura, imóveis e espaços de trabalho",
            commercial_application="Anúncios de imóveis, campanhas de decoração e capas de catálogos",
            composition_constraints=["Reservar uma parede ou área de baixo contraste para texto",
                "Variar disposição, ponto de vista, materiais e paleta sem repetir a mesma cena", "Geometria plausível e objetos sem marcas"],
        ),
    ]


class CommercialBriefings(BaseModel):
    templates: list[BriefingTemplate] = Field(default_factory=default_templates, max_length=100)

    @model_validator(mode="after")
    def unique_labels(self):
        labels = [template.label.casefold() for template in self.templates]
        if len(labels) != len(set(labels)):
            raise ValueError("Cada modelo de briefing deve ter um nome diferente.")
        return self


def template_widget_values(template: BriefingTemplate, prefix: str, *, include_orientation=True) -> dict:
    """Only editable brief fields; collection name, counts, catalog and sales stay independent."""
    fields = template.brief_values()
    constraints = fields.pop("composition_constraints")
    fields["constraints"] = "; ".join(constraints)
    if not include_orientation:
        fields.pop("orientation")
    return {f"{prefix}_{field}": value for field, value in fields.items()}


ORIENTATIONS = {"mixed": "Mista", "horizontal": "Horizontal", "vertical": "Vertical", "square": "Quadrada"}


def templates_rows(settings: CommercialBriefings) -> list[dict]:
    return [{"Modelo": t.label, "Tema": t.theme, "Objetivo": t.objective, "Público": t.audience,
             "Aplicação comercial": t.commercial_application, "Orientação": ORIENTATIONS[t.orientation],
             "Restrições (separe por ;)": "; ".join(t.composition_constraints)} for t in settings.templates]


def templates_from_rows(rows: list[dict]) -> CommercialBriefings:
    def value(row, key):
        result = row.get(key)
        return "" if result is None or isinstance(result, float) and math.isnan(result) else result

    orientations = {label: key for key, label in ORIENTATIONS.items()}
    templates = []
    for row in rows:
        if not any(value(row, key) for key in ("Modelo", "Tema", "Objetivo", "Público", "Aplicação comercial", "Restrições (separe por ;)")):
            continue
        templates.append(BriefingTemplate(
            label=value(row, "Modelo"), theme=value(row, "Tema"), objective=value(row, "Objetivo"),
            audience=value(row, "Público"), commercial_application=value(row, "Aplicação comercial"),
            orientation=orientations[value(row, "Orientação") or ORIENTATIONS["mixed"]],
            composition_constraints=[part.strip() for part in value(row, "Restrições (separe por ;)").split(";") if part.strip()],
        ))
    return CommercialBriefings(templates=templates)


def concept_reference(plan: ConceptPlan | dict, prompt_chars: int) -> str:
    data = plan.model_dump() if isinstance(plan, ConceptPlan) else plan
    # Include visual details so later batches can vary more than concept titles.
    selected = {key: data.get(key, "") for key in (
        "concept", "commercial_application", "main_subject", "composition_and_copy_space",
        "orientation", "prompt_en", "differentiation_rationale",
    )}
    selected["prompt_en"] = selected["prompt_en"][:prompt_chars]
    return json.dumps(selected, ensure_ascii=False)
