from __future__ import annotations

import base64
import json
import os
import webbrowser
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

import sys

import httpx
import streamlit as st
from pydantic import ValidationError


def _discard_stale_flowapp() -> None:
    """O Streamlit reexecuta app.py mas mantém os módulos importados; descarta flowapp se mudou."""
    version = max(path.stat().st_mtime for path in (Path(__file__).resolve().parent / "flowapp").glob("*.py"))
    package = sys.modules.get("flowapp")
    # Sem o pacote (o Streamlit pode descartá-lo sozinho), submódulos antigos ainda podem estar carregados.
    if package is None or getattr(package, "_code_version", None) != version:
        for name in [name for name in sys.modules if name == "flowapp" or name.startswith("flowapp.")]:
            del sys.modules[name]
    import flowapp
    flowapp._code_version = version


_discard_stale_flowapp()

from flowapp.adobe import ADOBE_CATEGORIES, CSV_TITLE_LIMIT, adobe_csv, category_number
from flowapp.agents import AgentError, INSTRUCTION_VERSION, adobe_lessons
from flowapp.comfy import ComfyError
from flowapp.config import AppConfig, load_config, save_config
from flowapp.database import (
    APPROVAL_PREFIX, Database, DuplicateJobError, InvalidTransition, MAX_WORK_MINUTES,
    REJECTION_PREFIX, TimerTooLong,
)
from flowapp.images import ImageStore, ImageValidationError
from flowapp.metrics import (
    approval_rate, approved_per_week, collection_key, downloads_per_approved, format_hours,
    format_per_hour, revenue_per_hour,
)
from flowapp.moderation import MODERATION_BY_LABEL, MODERATION_LABELS, moderation_changes
from flowapp.orchestrator import Orchestrator
from flowapp.pipeline import (
    AgentPipeline, final_upscale, metadata_approval_targets, metadata_targets, upscale_targets,
)
from flowapp.profiles import (
    load_default_profile,
    load_profile,
    profile_source,
    save_default_profile,
    save_profile,
)
from flowapp.schemas import (
    LOCAL_TZ,
    CollectionBrief,
    ComfyWorkflowProfile,
    ConceptPlan,
    MetadataSuggestion,
    PerformanceEntry,
    WorkflowInputRef,
    WorkflowState,
)


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DATA_ROOT = ROOT / "data"

st.set_page_config(page_title="Fluxo Imagens", page_icon="🖼", layout="wide")


def code_version() -> float:
    """Muda quando um módulo de flowapp é editado; renova os objetos em cache com o código novo."""
    return max(path.stat().st_mtime for path in (ROOT / "flowapp").glob("*.py"))


@st.cache_resource
def get_orchestrator(config_json: str, version: float) -> Orchestrator:
    return Orchestrator(ROOT, AppConfig.model_validate_json(config_json))


@st.cache_resource
def get_agent_pipeline(config_json: str, version: float) -> AgentPipeline:
    return AgentPipeline(get_orchestrator(config_json, version))


def current_pipeline() -> AgentPipeline:
    return get_agent_pipeline(get_config().model_dump_json(), code_version())


def get_config() -> AppConfig:
    try:
        return load_config(CONFIG_PATH)
    except (OSError, json.JSONDecodeError, ValidationError) as error:
        st.error(f"Configuração inválida: {error}")
        return AppConfig()


def show_error(error: Exception) -> None:
    st.error(str(error))
    if isinstance(error, ComfyError) and error.transient:
        st.info("Falha transitória. O trabalho foi preservado para reconciliação; use a retomada com o mesmo ID.")
    elif isinstance(error, DuplicateJobError):
        st.info("O trabalho não foi repetido. Consulte o histórico ou retome o item pendente.")


def format_visual_review(review: dict[str, Any]) -> str:
    lines = [
        f"Recomendação: {review.get('recommendation', 'não informada')}",
        f"Composição: {review.get('composition_quality', 'sem observação')}",
        f"Coerência com o conceito: {review.get('concept_coherence', 'sem observação')}",
        "",
        "Observações:",
    ]
    lines.extend(f"- {item}" for item in review.get("observations", []))
    lines.append("\nPossíveis defeitos:")
    for defect in review.get("possible_defects", []):
        name = defect.get("defect", "defeito não especificado")
        location = defect.get("location", "localização não especificada")
        lines.append(f"- {name} (localização: {location})")
    lines.append("\nIncertezas:")
    lines.extend(f"- {item}" for item in review.get("uncertainties", []))
    return "\n".join(lines)


def model_list(base_url: str) -> list[dict[str, Any]]:
    try:
        response = httpx.get(f"{base_url.rstrip('/')}/api/v1/models", timeout=5)
        response.raise_for_status()
        return response.json().get("models", [])
    except (httpx.HTTPError, ValueError):
        return []


def model_options(models: list[dict[str, Any]], vision: bool | None = None) -> list[str]:
    return [
        model["key"]
        for model in models
        if model.get("type") == "llm"
        and (vision is None or (model.get("capabilities") or {}).get("vision") is vision)
    ]


def render_diagnostics(config: AppConfig, orchestrator: Orchestrator) -> None:
    st.subheader("Diagnóstico local")
    st.caption("A consulta abaixo só lê os serviços; não carrega modelos nem executa workflows.")
    if st.button("Verificar serviços locais", icon=":material/health_and_safety:"):
        with st.spinner("Consultando serviços locais…"):
            try:
                stats = orchestrator.comfy.system_stats()
                system = stats.get("system", {})
                st.success(
                    f"ComfyUI conectado · versão {system.get('comfyui_version', 'não informada')} "
                    f"· Python {system.get('python_version', 'não informado').split()[0]}"
                )
                for device in stats.get("devices", []):
                    total_gib = device.get("vram_total", 0) / 1024**3
                    free_gib = device.get("vram_free", 0) / 1024**3
                    st.write(f"GPU: {device.get('name')} · VRAM {free_gib:.1f}/{total_gib:.1f} GiB livres")
                queue = orchestrator.comfy.queue()
                st.write(
                    f"Fila ComfyUI: {len(queue.get('queue_running', []))} executando, "
                    f"{len(queue.get('queue_pending', []))} aguardando."
                )
            except ComfyError as error:
                st.warning(f"ComfyUI: {error}")
            models = model_list(config.lmstudio_base_url)
            if models:
                st.success(f"LM Studio conectado · {len(models)} modelos descobertos")
                for model in models:
                    if model.get("type") != "llm":
                        continue
                    capabilities = model.get("capabilities") or {}
                    loaded = "carregado" if model.get("loaded_instances") else "não carregado"
                    vision = "visão declarada" if capabilities.get("vision") is True else "sem visão declarada"
                    st.caption(f"{model.get('key')} · {loaded} · {vision}")
            else:
                st.warning(f"LM Studio indisponível em {config.lmstudio_base_url}/api/v1/models")


def render_settings(config: AppConfig) -> AppConfig:
    st.subheader("Conexões e limites")
    st.caption("Os endereços são locais por padrão. O token opcional do LM Studio vem da variável LM_STUDIO_API_KEY.")
    models = model_list(config.lmstudio_base_url)
    planner_options = model_options(models)
    vision_options = model_options(models, vision=True)
    with st.form("settings"):
        comfy_url = st.text_input("URL base do ComfyUI", config.comfy_base_url)
        lm_url = st.text_input("URL base do LM Studio", config.lmstudio_base_url)
        planner_choices = [""] + (planner_options or ([config.planner_model] if config.planner_model else []))
        vision_choices = [""] + (vision_options or ([config.vision_model] if config.vision_model else []))
        planner = st.selectbox(
            "Modelo para planejamento e metadados",
            planner_choices,
            index=planner_choices.index(config.planner_model) if config.planner_model in planner_choices else 0,
            help="Modelos são descobertos por GET /api/v1/models. Só haverá inferência ao solicitar um agente.",
        )
        vision = st.selectbox(
            "Modelo visual (opcional)",
            vision_choices,
            index=vision_choices.index(config.vision_model) if config.vision_model in vision_choices else 0,
            help="Apenas modelos cuja capacidade vision=true foi declarada pelo LM Studio são listados.",
        )
        request_timeout = st.number_input(
            "Timeout HTTP (s)", min_value=1.0, max_value=300.0,
            value=float(config.request_timeout_seconds), step=1.0,
        )
        agent_timeout = st.number_input(
            "Timeout de resposta do agente (s)", min_value=10, max_value=3600,
            value=int(config.agent_timeout_seconds), step=30,
        )
        model_load_timeout = st.number_input(
            "Timeout de carga do modelo (s)", min_value=10, max_value=3600,
            value=int(config.model_load_timeout_seconds), step=60,
        )
        generation_timeout = st.number_input(
            "Tempo máximo de workflow (s)", min_value=10, max_value=14400,
            value=int(config.generation_timeout_seconds), step=30,
        )
        max_attempts = st.number_input(
            "Tentativas máximas do agente", min_value=1, max_value=3,
            value=int(config.max_agent_attempts), step=1,
        )
        st.markdown("**Agentes automáticos**")
        candidates_per_concept = st.number_input(
            "Imagens geradas por conceito em cada rodada", min_value=1, max_value=4,
            value=int(config.candidates_per_concept), step=1,
        )
        auto_regenerations = st.number_input(
            "Recriações automáticas quando o revisor reprova", min_value=0, max_value=3,
            value=int(config.auto_regenerations), step=1,
        )
        free_vram = st.checkbox(
            "Liberar VRAM entre LM Studio e ComfyUI (descarrega modelos ao trocar de fase)",
            value=config.free_vram_between_phases,
        )
        size_columns = st.columns(3)
        sizes = {}
        for column, orientation_key, label in zip(
            size_columns, ("horizontal", "vertical", "square"), ("Horizontal", "Vertical", "Quadrada"),
        ):
            default_width, default_height = config.generation_sizes.get(orientation_key, (1024, 1024))
            width_value = column.number_input(
                f"{label} · largura", 256, 8192, int(default_width), step=64, key=f"size_w_{orientation_key}",
            )
            height_value = column.number_input(
                f"{label} · altura", 256, 8192, int(default_height), step=64, key=f"size_h_{orientation_key}",
            )
            sizes[orientation_key] = (int(width_value), int(height_value))
        upscale_factor = st.number_input(
            "Fator de ampliação do upscale (aplicado à largura e altura da imagem escolhida)",
            min_value=1.0, max_value=8.0, value=float(config.upscale_factor), step=0.5,
        )
        min_mp = st.number_input(
            "Mínimo de megapixels (0 = não configurado)", min_value=0.0,
            value=float(config.export_rules.min_megapixels or 0), step=0.5,
        )
        max_mp = st.number_input(
            "Máximo de megapixels (0 = não configurado)", min_value=0.0,
            value=float(config.export_rules.max_megapixels or 0), step=0.5,
        )
        max_bytes = st.number_input(
            "Tamanho máximo em bytes (0 = não configurado)", min_value=0,
            value=int(config.export_rules.max_file_bytes or 0), step=1_000_000,
        )
        verified = st.checkbox(
            "Confirmei estes limites na documentação oficial atual da Adobe",
            value=config.export_rules.technical_rules_verified,
        )
        source = st.text_input("Fonte e data de conferência dos limites", config.export_rules.source)
        quality = st.slider("Qualidade JPEG", 85, 100, config.export_rules.jpeg_quality)
        timer_max_hours = st.number_input(
            "Limite do cronômetro de horas (h)", min_value=1.0, max_value=24.0,
            value=float(config.timer_max_hours), step=1.0,
            help="Acima disso o cronômetro é tratado como esquecido: ao parar, você informa os minutos reais.",
        )
        saved = st.form_submit_button("Salvar configuração", type="primary")
    if saved:
        new_config = AppConfig(
            comfy_base_url=comfy_url,
            lmstudio_base_url=lm_url,
            planner_model=planner,
            vision_model=vision,
            request_timeout_seconds=request_timeout,
            agent_timeout_seconds=int(agent_timeout),
            model_load_timeout_seconds=int(model_load_timeout),
            generation_timeout_seconds=int(generation_timeout),
            max_agent_attempts=int(max_attempts),
            candidates_per_concept=int(candidates_per_concept),
            auto_regenerations=int(auto_regenerations),
            free_vram_between_phases=free_vram,
            generation_sizes=sizes,
            upscale_factor=float(upscale_factor),
            timer_max_hours=float(timer_max_hours),
            export_rules={
                "min_megapixels": min_mp or None,
                "max_megapixels": max_mp or None,
                "max_file_bytes": max_bytes or None,
                "jpeg_quality": quality,
                "technical_rules_verified": verified,
                "checked_at": "2026-09-30",
                "source": source,
            },
        )
        save_config(CONFIG_PATH, new_config)
        get_orchestrator.clear()
        get_agent_pipeline.clear()
        st.success("Configuração salva em config.json (sem credenciais).")
        return new_config
    return config


def render_manual_collection_creation(database: Database, orchestrator: Orchestrator) -> None:
    with st.form("new_manual_collection"):
        name = st.text_input("Nome da coleção", placeholder="Jardins urbanos")
        theme = st.text_input("Tema", placeholder="Cultivo de alimentos em apartamentos")
        objective = st.text_area("Objetivo", placeholder="Criar imagens versáteis para campanhas de sustentabilidade")
        audience = st.text_input("Público", placeholder="Marcas de jardinagem e moradia")
        application = st.text_input("Aplicação comercial", placeholder="Campanha digital, banner e editorial")
        constraints = st.text_input("Restrições de composição", placeholder="espaço para texto à esquerda; sem texto visível")
        catalog = st.text_area("Catálogo existente para evitar repetição", height=80)
        sales = st.text_area("Dados observados de vendas (opcional)", height=80)
        st.markdown("#### Conceito inicial")
        concept = st.text_input("Conceito", placeholder="Estufa compacta em varanda urbana")
        main_subject = st.text_input("Assunto principal", placeholder="Estufa com plantas comestíveis")
        composition = st.text_area("Composição e espaço para texto", placeholder="Assunto à direita, espaço negativo à esquerda")
        orientation = st.selectbox("Orientação", ["horizontal", "vertical", "square"])
        prompt = st.text_area("Prompt em inglês para o ComfyUI", height=100)
        negative_prompt = st.text_area("Negative prompt (se suportado)", height=60)
        differentiation = st.text_area("Diferenciação em relação ao catálogo", height=70)
        prompt_approved = st.checkbox("Revisei e aprovo este conceito e prompt para geração")
        create = st.form_submit_button("Criar coleção e adicionar prompt", type="primary")
    if create:
        try:
            if not prompt_approved:
                raise ValueError("Aprove o conceito e o prompt antes de criar a coleção para geração direta.")
            brief = CollectionBrief(
                theme=theme,
                objective=objective,
                audience=audience,
                commercial_application=application,
                concept_count=1,
                composition_constraints=[item.strip() for item in constraints.split(";") if item.strip()],
                existing_catalog=catalog,
                sales_observations=sales,
            )
            initial_plan = ConceptPlan(
                concept=concept,
                commercial_application=application or objective,
                main_subject=main_subject,
                composition_and_copy_space=composition,
                orientation=orientation,
                prompt_en=prompt,
                negative_prompt=negative_prompt,
                differentiation_rationale=differentiation,
            )
            collection_id = database.create_collection(name or theme, brief)
            orchestrator.images.ensure_collection_dirs(collection_id)
            concept_id = database.create_concept(collection_id, initial_plan)
            database.transition_concept(
                concept_id, WorkflowState.PROMPTS_APPROVED,
                "Prompt inicial escrito e aprovado pelo usuário na criação da coleção",
            )
            st.session_state["active_collection"] = collection_id
            st.session_state["new_collection_notice"] = "Coleção e prompt inicial adicionados. Abra Ampliação para gerar imagens no ComfyUI."
            st.rerun()
        except (ValidationError, ValueError) as error:
            show_error(error)


def render_collection_overview(
    database: Database,
    orchestrator: Orchestrator,
    collections: list[dict[str, Any]],
    active_id: str | None,
) -> None:
    st.subheader("Coleções")
    if not collections:
        st.info("Ainda não há coleções. Crie uma abaixo para começar.")
        return
    status_labels = {
        "concepts_ready": "conceitos para revisar",
        "prompts_approved": "prompts aprovados",
        "generating": "em geração",
        "candidates_ready": "com candidatos",
        "selected": "com seleção",
        "upscaling": "em upscale",
        "final_review": "em revisão final",
        "metadata_ready": "metadados prontos",
        "locally_approved": "aprovados localmente",
        "prepared_for_transfer": "preparados para envio",
    }
    for collection in collections:
        collection_id = collection["id"]
        concepts = database.list_concepts(collection_id)
        with st.expander(
            f"{collection['name']} · {len(concepts)} conceito(s)"
            + (" · ativa" if active_id == collection_id else ""),
            expanded=active_id == collection_id,
        ):
            counts: dict[str, int] = {}
            assets: list[tuple[dict[str, Any], dict[str, Any]]] = []
            for concept in concepts:
                counts[concept["state"]] = counts.get(concept["state"], 0) + 1
                for asset in database.list_assets(concept["id"]):
                    if asset["role"] in {"candidate", "correction", "upscaled"} and Path(asset["path"]).is_file():
                        assets.append((concept, asset))
            status_text = " · ".join(
                f"{amount} {status_labels.get(state, state)}"
                for state, amount in counts.items()
            )
            st.caption(status_text or "Planejamento ainda não iniciado")
            if assets:
                status_priority = {"selected": 0, "upscaled": 1, "candidate": 2, "correction": 3, "not_selected": 4, "rejected": 5}
                assets.sort(
                    key=lambda pair: (
                        status_priority.get(pair[1]["status"], 9),
                        pair[1]["created_at"],
                    )
                )
                previews = assets[:3]
                columns = st.columns(len(previews))
                for column, (concept, asset) in zip(columns, previews):
                    column.image(
                        asset["path"],
                        caption=f"{concept['data']['concept']} · {asset['role']} · {asset['width']}×{asset['height']}",
                    )
                st.caption(f"{len(assets)} imagem(ns) no total · previews priorizam seleção e versões finais.")
            else:
                st.caption("Ainda não há imagens para preview.")
            if st.button(
                "Abrir coleção" if active_id != collection_id else "Coleção aberta",
                disabled=active_id == collection_id,
                key=f"open_collection_{collection_id}",
            ):
                st.session_state["active_collection"] = collection_id
                st.rerun()
            active_jobs = [
                job for job in database.list_recoverable_jobs(collection_id)
                if job["status"] in {"queued", "running"}
            ]
            open_timer = database.open_timer()
            timer_blocks = bool(open_timer and open_timer["collection_id"] == collection_id)
            with st.expander("Remover coleção", expanded=False):
                st.warning(
                    "Esta ação permanente apaga a coleção, seus conceitos, imagens, workflows, "
                    "histórico e métricas locais. Não remove arquivos fora da pasta desta coleção."
                )
                if active_jobs:
                    st.error(
                        f"Remoção bloqueada: há {len(active_jobs)} trabalho(s) ComfyUI ativos. "
                        "Aguarde a conclusão ou cancele-os antes."
                    )
                if timer_blocks:
                    st.error(
                        "Remoção bloqueada: há um cronômetro de horas aberto nesta coleção. "
                        "Pare ou descarte-o na aba Resultados ou na barra lateral."
                    )
                confirmation = st.text_input(
                    f"Digite {collection['name']} para confirmar",
                    key=f"delete_collection_confirm_{collection_id}",
                )
                delete_clicked = st.button(
                    "Remover permanentemente",
                    type="secondary",
                    disabled=bool(active_jobs) or timer_blocks or confirmation != collection["name"],
                    key=f"delete_collection_{collection_id}",
                )
                if delete_clicked:
                    try:
                        orchestrator.delete_collection(collection_id)
                        if active_id == collection_id:
                            st.session_state.pop("active_collection", None)
                        st.success(f"Coleção {collection['name']} removida.")
                        st.rerun()
                    except (KeyError, ValueError, OSError) as error:
                        show_error(error)


AGENT_STATE_ICONS = {
    "waiting": "⏳", "running": "🔄", "done": "✅", "failed": "❌",
    "skipped": "⏭️", "cancelled": "⛔", "interrupted": "⚠️",
}
AGENT_STATE_LABELS = {
    "waiting": "aguardando", "running": "trabalhando", "done": "concluído", "failed": "falhou",
    "skipped": "não necessário", "cancelled": "cancelado", "interrupted": "interrompido",
}
RUN_STATUS_LABELS = {
    "queued": "na fila", "running": "em execução", "completed": "concluída",
    "failed": "falhou", "cancelled": "cancelada", "interrupted": "interrompida",
}
EVENT_ICONS = {"info": "•", "warning": "⚠️", "error": "❌"}
ORIENTATION_LABELS = {
    "horizontal": "Horizontal", "vertical": "Vertical", "square": "Quadrada",
    "mixed": "Variada (o planejador decide)",
}
RECOMMENDATION_BADGES = {
    "select": ("Revisor: aprovar", "green"),
    "review": ("Revisor: revisar", "orange"),
    "reject": ("Revisor: recriar", "red"),
}
ASSET_STATUS_LABELS = {
    "candidate": "aguardando sua avaliação", "selected": "selecionada",
    "not_selected": "não selecionada", "rejected": "descartada",
}


HUB_URL = "http://127.0.0.1:8700"


@st.cache_resource
def recover_stale_runs() -> int:
    """Executa uma vez por processo: execuções de um processo anterior ficaram órfãs."""
    return current_pipeline().recover()


def start_pipeline(
    orchestrator: Orchestrator, collection_id: str, mode: str,
    planner_model: str, vision_model: str, **options: Any,
) -> str:
    return current_pipeline().start(collection_id, mode, planner_model, vision_model, **options)


def agent_models(config: AppConfig, collection_id: str) -> tuple[str, str]:
    """Modelos da última execução da coleção, ou os padrões da configuração."""
    for run in current_pipeline().runs(collection_id, limit=5):
        params = run.get("params") or {}
        if params.get("planner_model"):
            return params["planner_model"], params.get("vision_model") or ""
    return config.planner_model, config.vision_model


def _elapsed(start: str | None, end: str | None = None) -> str:
    if not start:
        return "--:--"
    try:
        begin = datetime.fromisoformat(start)
        finish = datetime.fromisoformat(end) if end else datetime.now(UTC)
    except ValueError:
        return "--:--"
    seconds = max(int((finish - begin).total_seconds()), 0)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


@st.fragment(run_every="2s")
def render_agent_status(collection_id: str, compact: bool = False) -> None:
    pipeline = current_pipeline()
    runs = pipeline.runs(collection_id, limit=10)
    if not runs:
        if not compact:
            st.caption("Nenhuma execução de agentes nesta coleção ainda.")
        return
    active = [run for run in runs if run["status"] in {"queued", "running"}]
    current = next((run for run in runs if run["status"] == "running"), None)
    current = current or (active[-1] if active else runs[0])
    run = pipeline.status(current["id"], events=40) or current
    agents = run.get("agents", [])

    # Recarrega a página inteira quando surgem imagens/revisões novas ou a execução termina.
    signature = (
        run["id"], run["status"],
        tuple((agent["agent"], agent["state"], agent["done"]) for agent in agents),
    )
    seen_key = f"pipeline_signature_{collection_id}_{compact}"
    previous = st.session_state.get(seen_key)
    st.session_state[seen_key] = signature
    if previous is not None and previous != signature and not compact:
        st.rerun(scope="app")

    elapsed = _elapsed(run["created_at"], run.get("finished_at"))
    status_label = RUN_STATUS_LABELS.get(run["status"], run["status"])
    if compact:
        st.markdown(f"**Agentes · {status_label}** ({elapsed})")
        for agent in agents:
            icon = AGENT_STATE_ICONS.get(agent["state"], "•")
            progress = f" {agent['done']}/{agent['total']}" if agent["total"] else ""
            st.caption(f"{icon} {agent['label']}{progress}")
        if len(active) > 1:
            st.caption(f"+{len(active) - 1} execução(ões) na fila")
        return

    header = f"**{run['mode_label']}** · {status_label} · ⏱ {elapsed}"
    if run.get("phase") and run["status"] == "running":
        header += f" · fase: {run['phase']}"
    st.markdown(header)
    queued = [item for item in active if item["id"] != run["id"]]
    if queued:
        st.caption(f"{len(queued)} execução(ões) aguardando na fila depois desta.")
    columns = st.columns(max(len(agents), 1))
    for column, agent in zip(columns, agents):
        with column.container(border=True):
            icon = AGENT_STATE_ICONS.get(agent["state"], "•")
            st.markdown(f"{icon} **{agent['label']}**")
            st.caption(AGENT_STATE_LABELS.get(agent["state"], agent["state"]))
            if agent["total"]:
                st.progress(
                    min(agent["done"] / agent["total"], 1.0),
                    text=f"{agent['done']}/{agent['total']}",
                )
            if agent["message"]:
                st.caption(agent["message"])
    if run["status"] in {"failed", "interrupted"} and run.get("error"):
        st.error(run["error"])
    if run["status"] in {"queued", "running"}:
        if run.get("cancel_requested"):
            st.caption("Cancelamento solicitado; aguardando a tarefa atual terminar.")
        elif st.button("Cancelar agentes", key=f"cancel_run_{run['id']}", icon=":material/stop_circle:"):
            pipeline.cancel(run["id"])
            st.rerun(scope="fragment")
    st.caption(f"Todos os projetos e a GPU no painel central: {HUB_URL} (`iniciar_painel.bat`).")
    agent_labels = {agent["agent"]: agent["label"] for agent in agents}
    with st.expander("Log dos agentes", expanded=run["status"] == "failed"):
        for event in run.get("events", []):
            moment = event["created_at"][11:19]
            agent_name = agent_labels.get(event["agent"], "Pipeline")
            icon = EVENT_ICONS.get(event["level"], "•")
            st.caption(f"{moment} · {icon} {agent_name}: {event['message']}")


def render_agent_models(
    database: Database, config: AppConfig, collection_id: str,
) -> tuple[str, str]:
    models = model_list(config.lmstudio_base_url)
    text_options = model_options(models)
    vision_options = model_options(models, vision=True)
    planner_default, vision_default = agent_models(config, collection_id)
    with st.expander("Modelos usados pelos agentes", expanded=False):
        planner_choices = text_options or ([planner_default] if planner_default else [""])
        planner = st.selectbox(
            "Modelo de texto (planejador e refinador)", planner_choices,
            index=planner_choices.index(planner_default) if planner_default in planner_choices else 0,
            key=f"eval_planner_{collection_id}",
        )
        vision_choices = [""] + vision_options
        if vision_default and vision_default not in vision_choices:
            vision_choices.append(vision_default)
        vision = st.selectbox(
            "Modelo com visão (revisor)", vision_choices,
            index=vision_choices.index(vision_default) if vision_default in vision_choices else 0,
            format_func=lambda value: value or "Sem revisão automática",
            key=f"eval_vision_{collection_id}",
        )
    return planner, vision


def render_collection_creation(database: Database, orchestrator: Orchestrator, config: AppConfig) -> None:
    st.subheader("Nova coleção")
    st.caption(
        "Preencha o briefing. O LM Studio completa conceitos, composição e prompts; os agentes "
        "geram e revisam as imagens. Depois você avalia e escolhe as melhores na aba Criação."
    )
    if load_default_profile(DATA_ROOT, "generation") is None:
        st.warning(
            "Nenhum workflow de geração padrão foi importado. Os agentes vão planejar os prompts, "
            "mas a geração só começa depois que você importar um workflow em **Configuração → Workflows ComfyUI** "
            "(marcando *usar como padrão*) e clicar em **Retomar agentes**."
        )
    models = model_list(config.lmstudio_base_url)
    text_options = model_options(models)
    vision_options = model_options(models, vision=True)
    with st.form("new_collection"):
        name = st.text_input("Nome da coleção", placeholder="Jardins urbanos")
        theme = st.text_input("Tema", placeholder="Cultivo de alimentos em apartamentos")
        objective = st.text_area("Objetivo", placeholder="Criar imagens versáteis para campanhas de sustentabilidade")
        audience = st.text_input("Público", placeholder="Marcas de jardinagem e moradia")
        orientation_column, count_column = st.columns(2)
        orientation = orientation_column.selectbox(
            "Orientação", list(ORIENTATION_LABELS), format_func=ORIENTATION_LABELS.get,
        )
        count = count_column.number_input("Quantidade de imagens", min_value=1, max_value=20, value=4)
        application = st.text_input("Aplicação comercial", placeholder="Campanha digital, banner e editorial")
        with st.expander("Opções avançadas (opcional)"):
            constraints = st.text_input(
                "Restrições de composição (separe por ;)",
                placeholder="espaço para texto à esquerda; sem texto visível",
            )
            catalog = st.text_area("Catálogo existente para evitar repetição", height=80)
            sales = st.text_area("Dados observados de vendas", height=80)
        planner_choices = text_options or ([config.planner_model] if config.planner_model else [""])
        planner = st.selectbox(
            "Modelo do planejador (LM Studio)", planner_choices,
            index=planner_choices.index(config.planner_model) if config.planner_model in planner_choices else 0,
        )
        vision_choices = [""] + (vision_options or ([config.vision_model] if config.vision_model else []))
        default_vision = config.vision_model if config.vision_model in vision_choices else (
            vision_options[0] if vision_options else ""
        )
        vision = st.selectbox(
            "Modelo do revisor visual", vision_choices,
            index=vision_choices.index(default_vision),
            format_func=lambda value: value or "Sem revisão automática",
            help="Somente modelos que o LM Studio declara com visão.",
        )
        create = st.form_submit_button("Criar coleção e acionar agentes", type="primary")
    if create:
        try:
            if not planner:
                raise ValueError("Nenhum modelo de texto disponível no LM Studio.")
            brief = CollectionBrief(
                theme=theme,
                objective=objective,
                audience=audience,
                commercial_application=application,
                orientation=orientation,
                concept_count=int(count),
                composition_constraints=[item.strip() for item in constraints.split(";") if item.strip()],
                existing_catalog=catalog,
                sales_observations=sales,
            )
            collection_id = database.create_collection(name or theme, brief)
            orchestrator.images.ensure_collection_dirs(collection_id)
            start_pipeline(orchestrator, collection_id, "full", planner, vision)
            st.session_state["active_collection"] = collection_id
            st.session_state["new_collection_notice"] = (
                "Coleção criada e agentes acionados. Acompanhe o status na aba Agentes e avalie as imagens na aba Criação."
            )
            st.rerun()
        except (ValidationError, ValueError) as error:
            show_error(error)


def _select_asset(orchestrator: Orchestrator, concept_id: str, asset_id: str, reason: str) -> None:
    asset = orchestrator.database.get_asset(asset_id)
    if asset is None:
        raise ValueError("Imagem não encontrada.")
    orchestrator.images.promote_selected(asset)
    orchestrator.database.select_asset(concept_id, asset_id, reason)


ZOOM_LEVELS = {"Ajustar à janela": None, "100% (pixels reais)": 1.0, "200%": 2.0, "300%": 3.0, "400%": 4.0}


@st.dialog("Inspecionar imagem", width="large")
def show_image_zoom(asset: dict) -> None:
    path = Path(asset["path"])
    level = st.segmented_control(
        "Zoom", list(ZOOM_LEVELS), default="100% (pixels reais)", key=f"zoom_level_{asset['id']}",
    ) or "Ajustar à janela"
    factor = ZOOM_LEVELS[level]
    size = "width:100%" if factor is None else f"width:{int(asset['width'] * factor)}px;max-width:none"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    # Rolagem nos dois eixos para percorrer a imagem ampliada; `pixelated` evita suavizar acima de 100%.
    st.html(
        f'<div style="max-height:72vh;overflow:auto;border:1px solid rgba(128,128,128,.3);border-radius:6px">'
        f'<img src="data:{mime};base64,{encoded}" style="{size};display:block;'
        f'image-rendering:{"pixelated" if factor and factor > 1 else "auto"}"></div>'
    )
    st.caption(f"{path.name} · {asset['width']}×{asset['height']} · use as barras de rolagem para percorrer a imagem.")
    if st.button("Abrir no visualizador do Windows", icon=":material/open_in_new:"):
        try:
            os.startfile(path)
        except (AttributeError, OSError) as error:
            show_error(error)


def render_unselect_button(orchestrator: Orchestrator, concept: dict, key_prefix: str) -> None:
    if concept["state"] != WorkflowState.SELECTED.value:
        return
    if st.button(
        "Desfazer seleção", icon=":material/undo:", key=f"{key_prefix}_unselect_{concept['id']}",
        help="Devolve as imagens deste conceito para avaliação na aba Criação.",
    ):
        try:
            orchestrator.database.unselect_asset(concept["id"], "Seleção desfeita pelo usuário")
            st.rerun()
        except (KeyError, ValueError, InvalidTransition) as error:
            show_error(error)


def suggested_correction_feedback(asset: dict) -> str:
    """Observações e defeitos do parecer do revisor, como ponto de partida editável."""
    review = asset.get("review") or {}
    lines = list(review.get("observations", []))
    lines.extend(
        f"{item.get('defect', 'Possible defect')} (location: {item.get('location', 'unspecified')})"
        for item in review.get("possible_defects", [])
    )
    if not lines and asset["role"] == "correction":
        return asset["metadata"].get("correction_feedback", "")
    return "\n".join(lines)


def render_correction(
    orchestrator: Orchestrator, collection_id: str, concept: dict, asset: dict,
    models: tuple[str, str],
) -> None:
    database = orchestrator.database
    root_source_id = asset["metadata"].get("root_source_asset_id", asset["id"])
    corrections_used = sum(
        1 for other in database.list_assets(concept["id"])
        if other["metadata"].get("root_source_asset_id") == root_source_id
    )
    with st.popover(f"Corrigir ({corrections_used}/2)", icon=":material/healing:", width="stretch"):
        st.caption(
            "Edita esta imagem com o workflow de inpainting, mantendo o tamanho original. "
            "O resultado é uma nova imagem; a origem não é alterada."
        )
        with st.form(f"correction_form_{asset['id']}"):
            feedback = st.text_area(
                "O que corrigir",
                value=suggested_correction_feedback(asset),
                height=150,
                help=(
                    "Sugestão a partir do parecer do revisor; edite antes de enviar. Escreva no fim "
                    "o que você quer (ex.: o relógio deve mostrar 2027, sem dois-pontos). Com modelo "
                    "de texto ativo, isso vira uma ordem curta de edição antes de ir ao ComfyUI."
                ),
            )
            correct = st.form_submit_button(
                "Gerar versão corrigida", type="primary", disabled=corrections_used >= 2,
            )
        if correct:
            try:
                with st.status("Executando correção…", expanded=True) as status:
                    orchestrator.correct_candidate(
                        collection_id, concept, asset, feedback, progress=st.write,
                        model_id=models[0],
                    )
                    status.update(label="Nova versão salva para avaliação", state="complete")
                if models[1]:
                    # A correção roda fora do pipeline; o revisor visual é acionado em seguida.
                    start_pipeline(
                        orchestrator, collection_id, "review", *models, concept_id=concept["id"],
                    )
                st.rerun()
            except Exception as error:
                show_error(error)


PREVIEW_MAX_SIDE = 480


def render_preview(asset: dict, caption: str, key: str) -> None:
    """Prévia com o maior lado limitado (imagens verticais ampliadas não ocupam a tela) + Zoom."""
    longest = max(asset["width"], asset["height"]) or 1
    st.image(asset["path"], caption=caption, width=max(1, int(PREVIEW_MAX_SIDE * asset["width"] / longest)))
    if st.button("Zoom", icon=":material/zoom_in:", key=f"zoom_{key}_{asset['id']}"):
        show_image_zoom(asset)


def render_asset_card(
    orchestrator: Orchestrator, collection_id: str, concept: dict, asset: dict,
    models: tuple[str, str], correction_ready: bool = False,
) -> None:
    database = orchestrator.database
    st.image(asset["path"], width="stretch")
    if st.button("Zoom", icon=":material/zoom_in:", key=f"zoom_{asset['id']}", width="stretch"):
        show_image_zoom(asset)
    review = asset.get("review") or {}
    badge = RECOMMENDATION_BADGES.get(review.get("recommendation", ""))
    if badge:
        st.badge(badge[0], color=badge[1])
    elif asset["status"] == "candidate":
        st.badge("Sem revisão automática", color="gray")
    label = ASSET_STATUS_LABELS.get(asset["status"], asset["status"])
    origin = " · corrigida" if asset["role"] == "correction" else ""
    st.caption(f"{label}{origin} · {asset['width']}×{asset['height']}")
    if review.get("recommendation"):
        with st.expander("Parecer do revisor"):
            st.text(format_visual_review(review))
    if asset["status"] != "candidate" or concept["state"] != WorkflowState.CANDIDATES_READY.value:
        return
    if not review.get("recommendation") and st.button(
        "Solicitar revisão", icon=":material/rate_review:", key=f"request_review_{asset['id']}",
        width="stretch", disabled=not models[1],
        help="Envia ao revisor visual as imagens deste conceito que ainda não têm parecer."
        if models[1] else "Escolha um modelo com visão (revisor) acima para usar a revisão.",
    ):
        start_pipeline(orchestrator, collection_id, "review", *models, concept_id=concept["id"])
        st.rerun()
    select_column, discard_column = st.columns(2)
    if select_column.button("Selecionar", key=f"eval_select_{asset['id']}", icon=":material/star:", type="primary"):
        try:
            _select_asset(orchestrator, concept["id"], asset["id"], "Escolhida na avaliação da coleção")
            st.rerun()
        except (ValueError, InvalidTransition, ImageValidationError, OSError) as error:
            show_error(error)
    if discard_column.button("Descartar", key=f"eval_discard_{asset['id']}", icon=":material/delete:"):
        try:
            database.reject_asset(concept["id"], asset["id"], "Descartada na avaliação da coleção")
            st.rerun()
        except (ValueError, InvalidTransition) as error:
            show_error(error)
    with st.popover("Recriar", icon=":material/refresh:", width="stretch"):
        with st.form(f"recreate_{asset['id']}"):
            feedback = st.text_area(
                "O que mudar? (opcional)",
                placeholder="Ex.: remover o texto na placa, luz mais quente, mãos mais naturais",
            )
            discard = st.checkbox("Descartar esta imagem", value=True)
            submit = st.form_submit_button("Recriar com os agentes", type="primary")
        if submit:
            start_pipeline(
                orchestrator, collection_id, "regenerate", *models,
                concept_id=concept["id"], feedback=feedback,
                discard_asset_id=asset["id"] if discard else None,
            )
            st.rerun()
    if correction_ready:
        render_correction(orchestrator, collection_id, concept, asset, models)


def render_collapsed_concept(
    index: int, plan: ConceptPlan, concept: dict, assets: list[dict], expanded_key: str,
) -> None:
    """Linha compacta para conceitos que já têm imagem escolhida; expande sob demanda."""
    chosen = next((asset for asset in assets if asset["id"] == concept["selected_asset_id"]), None)
    with st.container(border=True):
        thumb, text, action = st.columns([1, 5, 1], vertical_alignment="center")
        if chosen and Path(chosen["path"]).is_file():
            thumb.image(chosen["path"], width="stretch")
        text.markdown(f"**{index}. {plan.concept}**")
        text.caption(f"Imagem escolhida · {plan.commercial_application}")
        if action.button("Expandir", icon=":material/unfold_more:", key=f"expand_{concept['id']}"):
            st.session_state[expanded_key] = True
            st.rerun()


def render_evaluation(orchestrator: Orchestrator, config: AppConfig, collection_id: str | None) -> None:
    if not collection_id:
        st.info("Crie uma coleção no Painel ou selecione uma no menu lateral.")
        return
    database = orchestrator.database
    collection = database.get_collection(collection_id)
    if collection is None:
        st.warning("Coleção não encontrada.")
        return
    brief = collection["brief"]
    st.subheader(collection["name"])
    st.caption(
        f"Tema: {brief.get('theme', '')} · Público: {brief.get('audience') or '-'} · "
        f"Orientação: {ORIENTATION_LABELS.get(brief.get('orientation', 'mixed'), '-')} · "
        f"Quantidade pedida: {brief.get('concept_count', '-')}"
    )
    notice = st.session_state.pop("new_collection_notice", None)
    if notice:
        st.success(notice)

    models = render_agent_models(database, config, collection_id)
    pipeline = current_pipeline()
    runs = pipeline.runs(collection_id, limit=1)
    busy = bool(pipeline.runs(collection_id, limit=1, active_only=True))
    concepts = [
        concept for concept in database.list_concepts(collection_id)
        if concept["state"] != WorkflowState.CANCELLED.value
    ]
    waiting = [
        concept for concept in concepts
        if concept["state"] in {WorkflowState.PROMPTS_APPROVED.value, WorkflowState.CONCEPTS_READY.value}
    ]
    if not busy and (not runs or runs[0]["status"] != "completed" or waiting):
        if st.button(
            "Retomar agentes", icon=":material/play_arrow:",
            help="Planeja o que falta, gera imagens pendentes e revisa as que ainda não foram revisadas.",
            key=f"resume_pipeline_{collection_id}",
        ):
            start_pipeline(orchestrator, collection_id, "full", *models)
            st.rerun()

    planning = collection.get("planning") or {}
    if planning.get("collection_summary") or planning.get("style_guide"):
        with st.expander("Análise do planejador"):
            if planning.get("collection_summary"):
                st.markdown(planning["collection_summary"])
            if planning.get("style_guide"):
                st.caption("Guia de estilo da coleção")
                st.code(planning["style_guide"], language=None, wrap_lines=True)

    if not concepts:
        st.info("Os conceitos aparecerão aqui assim que o planejador terminar.")
        return

    all_assets = {concept["id"]: database.list_assets(concept["id"]) for concept in concepts}
    images = [
        asset for assets in all_assets.values() for asset in assets
        if asset["role"] in {"candidate", "correction"}
    ]
    chosen = sum(1 for concept in concepts if concept.get("selected_asset_id"))
    pending = sum(1 for asset in images if asset["status"] == "candidate")
    metrics = st.columns(4)
    metrics[0].metric("Imagens planejadas", len(concepts))
    metrics[1].metric("Imagens geradas", len(images))
    metrics[2].metric("Aguardando avaliação", pending)
    metrics[3].metric("Selecionadas", f"{chosen}/{len(concepts)}")
    correction_ready = load_profile(orchestrator.images.collection_path(collection_id), "inpainting") is not None
    if not correction_ready:
        st.caption("Para corrigir imagens, importe um workflow de inpainting em Configuração → Workflows ComfyUI.")
    pending_jobs = [
        job for job in database.list_recoverable_jobs(collection_id) if job["kind"] != "upscale"
    ]
    if pending_jobs and not busy:
        st.warning(f"{len(pending_jobs)} trabalho(s) de geração/correção aguardam retomada no ComfyUI.")
        resume_column, cancel_column = st.columns(2)
        if resume_column.button("Retomar trabalhos existentes", key=f"resume_jobs_{collection_id}"):
            try:
                with st.spinner("Conferindo histórico e fila antes de qualquer envio…"):
                    for concept_id in {job["concept_id"] for job in pending_jobs}:
                        orchestrator.resume_jobs(collection_id, st.write, concept_id)
                st.rerun()
            except Exception as error:
                show_error(error)
        if cancel_column.button("Cancelar esses trabalhos", key=f"cancel_jobs_{collection_id}"):
            try:
                for concept_id in {job["concept_id"] for job in pending_jobs}:
                    orchestrator.cancel_jobs(collection_id, concept_id)
                st.rerun()
            except Exception as error:
                show_error(error)
    st.divider()

    for index, concept in enumerate(concepts, start=1):
        plan = ConceptPlan.model_validate(concept["data"])
        state = WorkflowState(concept["state"])
        expanded_key = f"eval_expanded_{concept['id']}"
        if concept.get("selected_asset_id") and not st.session_state.get(expanded_key):
            render_collapsed_concept(index, plan, concept, all_assets[concept["id"]], expanded_key)
            continue
        with st.container(border=True):
            st.markdown(f"**{index}. {plan.concept}**")
            st.caption(f"{plan.commercial_application} · {ORIENTATION_LABELS.get(plan.orientation, plan.orientation)}")
            with st.expander("Prompt e detalhes do planejador"):
                st.markdown(f"**Assunto:** {plan.main_subject}")
                st.markdown(f"**Composição:** {plan.composition_and_copy_space}")
                st.markdown(f"**Diferenciação:** {plan.differentiation_rationale}")
                st.code(plan.prompt_en, language=None, wrap_lines=True)
                if plan.negative_prompt:
                    st.caption("Negative prompt")
                    st.code(plan.negative_prompt, language=None, wrap_lines=True)
            assets = [
                asset for asset in all_assets[concept["id"]]
                if asset["role"] in {"candidate", "correction"} and Path(asset["path"]).is_file()
            ]
            visible = [asset for asset in assets if asset["status"] != "rejected"]
            discarded = [asset for asset in assets if asset["status"] == "rejected"]
            if state == WorkflowState.GENERATING:
                st.info("Gerando imagem…")
            elif state in {WorkflowState.PROMPTS_APPROVED, WorkflowState.CONCEPTS_READY} and not visible:
                st.caption("Aguardando o gerador.")
            elif state == WorkflowState.FAILED:
                history = database.transition_history(concept["id"])
                st.error(history[-1]["reason"] if history else "Falha na geração.")
            if visible:
                columns = st.columns(3)
                for position, asset in enumerate(visible):
                    with columns[position % 3]:
                        render_asset_card(
                            orchestrator, collection_id, concept, asset, models, correction_ready,
                        )
            if concept.get("selected_asset_id"):
                st.success("Imagem escolhida. Continue com o upscale na aba Ampliação e os metadados na aba Metadados.")
                render_unselect_button(orchestrator, concept, "eval")
                if st.button("Recolher", icon=":material/unfold_less:", key=f"collapse_{concept['id']}"):
                    st.session_state[expanded_key] = False
                    st.rerun()
            elif state in {WorkflowState.CANDIDATES_READY, WorkflowState.FAILED}:
                actions = st.columns(2)
                if actions[0].button(
                    "Gerar outra variação", icon=":material/add_photo_alternate:",
                    key=f"variation_{concept['id']}",
                ):
                    start_pipeline(
                        orchestrator, collection_id, "regenerate", *models, concept_id=concept["id"],
                    )
                    st.rerun()
                if actions[1].button(
                    "Remover da coleção", icon=":material/remove_circle:", key=f"drop_{concept['id']}",
                    help="Cancela este conceito; as imagens continuam guardadas no disco.",
                ):
                    try:
                        database.transition_concept(
                            concept["id"], WorkflowState.CANCELLED, "Removido na avaliação da coleção",
                        )
                        st.rerun()
                    except InvalidTransition as error:
                        show_error(error)
            if discarded:
                with st.expander(f"Descartadas ({len(discarded)})"):
                    columns = st.columns(4)
                    for position, asset in enumerate(discarded):
                        columns[position % 4].image(asset["path"], width="stretch")

    st.divider()
    st.markdown("#### Nova imagem para esta coleção")
    with st.form(f"new_image_{collection_id}"):
        hint = st.text_area(
            "Ideia (opcional)",
            placeholder="Deixe vazio para o planejador sugerir algo novo que complemente a coleção.",
        )
        add = st.form_submit_button("Planejar e criar nova imagem", type="primary", icon=":material/add:")
    if add:
        start_pipeline(orchestrator, collection_id, "new_image", *models, hint=hint)
        st.rerun()
    if busy:
        st.caption("Novas solicitações entram na fila e rodam depois da execução atual.")


def render_workflow_import(orchestrator: Orchestrator, collection_id: str | None) -> None:
    st.subheader("Workflows ComfyUI")
    if not collection_id:
        st.caption("Sem coleção ativa: o workflow importado será salvo como padrão para todas as coleções.")
    st.info("No ComfyUI, use **File → Export Workflow (API)**. O JSON visual com `nodes` e `links` não é executável pela API.")
    kind = st.selectbox("Etapa", ["generation", "upscale", "inpainting"], format_func={
        "generation": "Geração", "upscale": "Upscale", "inpainting": "Inpainting (opcional)"
    }.get)
    if kind == "inpainting":
        st.caption(
            "width/height devem apontar para o latente que chega de fato ao KSampler (ex.: EmptyLatentImage). "
            "Se o workflow usa um switch para escolher o latente, deixe-o ligado nesse nó antes de exportar."
        )
    if kind == "upscale":
        st.caption(
            "Se o nó de resize usa \"scale by multiplier\", mapeie multiplier (campo resize_type.multiplier); "
            "se usa \"scale dimensions\", mapeie width e height. O app preenche com o fator de ampliação da "
            "Configuração, ignorando o valor salvo no workflow."
        )
    uploaded = st.file_uploader("Workflow API JSON", type=["json"], key=f"workflow_{kind}")
    if uploaded:
        if uploaded.size > 10_000_000:
            st.error("O arquivo excede 10 MB.")
            return
        try:
            workflow = json.loads(uploaded.getvalue())
            if not isinstance(workflow, dict):
                raise ValueError("A raiz do workflow deve ser um objeto JSON.")
            if "nodes" in workflow and "links" in workflow:
                raise ValueError(
                    "Este é o workflow visual (nodes/links). No ComfyUI, use File → Export Workflow (API)."
                )
            if isinstance(workflow.get("prompt"), dict):
                workflow = workflow["prompt"]
            workflow = {
                node_id: node for node_id, node in workflow.items()
                if isinstance(node, dict) and "class_type" in node
            }
            if not workflow:
                raise ValueError("Nenhum nó com `class_type` encontrado; exporte em formato API.")
            node_ids = list(workflow)
            info = {}
            try:
                info = orchestrator.comfy.object_info()
            except ComfyError as error:
                st.warning(f"Não foi possível consultar tipos de nó agora: {error}. A validação será feita ao salvar/usar.")
            output_nodes = [
                node_id for node_id, node in workflow.items()
                if info.get(node.get("class_type"), {}).get("output_node", False)
            ]
            if not output_nodes:
                output_nodes = [
                    node_id for node_id, node in workflow.items()
                    if node.get("class_type") in {"SaveImage", "PreviewImage"}
                ]
            with st.form(f"map_workflow_{kind}"):
                if kind == "generation":
                    roles = ["prompt", "negative_prompt", "seed", "width", "height", "steps", "cfg"]
                elif kind == "inpainting":
                    roles = ["image", "prompt", "negative_prompt", "seed", "width", "height", "steps", "cfg"]
                else:
                    roles = ["image", "multiplier", "width", "height"]
                roles += ["filename_prefix"]
                mappings: dict[str, WorkflowInputRef] = {}
                for role in roles:
                    required = {"prompt", "image"} | (
                        {"seed", "width", "height"} if kind in {"generation", "inpainting"} else set()
                    )
                    # No upscale, já marca os campos de tamanho que o workflow expõe (ex.: resize_type.multiplier).
                    detected = kind == "upscale" and role in {"multiplier", "width", "height"} and any(
                        name.rsplit(".", 1)[-1] == role and isinstance(value, (int, float))
                        for node in workflow.values() for name, value in node.get("inputs", {}).items()
                    )
                    active = role in required or st.checkbox(
                        f"Mapear {role}", value=detected, key=f"map_toggle_{kind}_{role}"
                    )
                    if not active:
                        continue
                    suggested = [
                        node_id for node_id, node in workflow.items()
                        if role in node.get("inputs", {})
                        # Nós de resize do ComfyUI expõem o tamanho como "resize_type.width"/"resize_type.height".
                        or (role in {"width", "height", "multiplier"} and any(name.endswith(f".{role}") for name in node.get("inputs", {})))
                        or (role == "seed" and any(name in node.get("inputs", {}) for name in ("seed", "noise_seed")))
                        or (role == "prompt" and node.get("class_type") == "CLIPTextEncode" and "text" in node.get("inputs", {}))
                        or (role == "image" and "image" in node.get("inputs", {}))
                    ]
                    if role in {"width", "height"}:
                        # Prefere o nó de latente vazio: é ele que define o tamanho da saída.
                        suggested.sort(key=lambda value: "Latent" not in workflow[value].get("class_type", ""))
                    candidate_ids = suggested or node_ids
                    node_id = st.selectbox(
                        f"Nó para {role}", candidate_ids,
                        format_func=lambda value: f"{value} · {workflow[value].get('class_type', '?')}",
                        key=f"map_node_{kind}_{role}",
                    )
                    input_names = list(workflow[node_id].get("inputs", {}))
                    preferred_inputs = {
                        "prompt": ("text", "prompt"),
                        "image": ("image",),
                        "seed": ("seed", "noise_seed"),
                    }.get(role, (role,))
                    default_input = next(
                        (name for name in preferred_inputs if name in input_names),
                        next((name for name in input_names if name.endswith(f".{role}")),
                             input_names[0] if input_names else ""),
                    )
                    if not input_names:
                        st.error(f"O nó {node_id} não tem entradas configuráveis.")
                        continue
                    input_name = st.selectbox(
                        f"Campo ComfyUI para {role}", input_names,
                        index=input_names.index(default_input),
                        key=f"map_field_{kind}_{role}",
                    )
                    mappings[role] = WorkflowInputRef(node_id=node_id, input_name=input_name)
                selected_outputs = st.multiselect(
                    "Nós de saída que contêm imagens",
                    output_nodes or node_ids,
                    default=output_nodes if kind in {"generation", "upscale", "inpainting"} else [],
                    format_func=lambda value: f"{value} · {workflow[value].get('class_type', '?')}",
                ) if kind in {"generation", "upscale", "inpainting"} else []
                has_default = load_default_profile(DATA_ROOT, kind) is not None
                as_default = st.checkbox(
                    "Usar como padrão para todas as coleções (inclusive as criadas pelos agentes)",
                    value=not collection_id or not has_default,
                    disabled=not collection_id,
                    key=f"workflow_default_{kind}",
                )
                save = st.form_submit_button("Validar e salvar workflow", type="primary")
            if save:
                try:
                    profile = ComfyWorkflowProfile(
                        kind=kind, workflow=workflow, mappings=mappings,
                        output_node_ids=selected_outputs,
                    )
                    issues = orchestrator.comfy.validate_profile(profile)
                    if issues:
                        for issue in issues:
                            st.error(issue)
                    else:
                        locations = []
                        if collection_id:
                            locations.append(
                                save_profile(orchestrator.images.collection_path(collection_id), profile)
                            )
                        if as_default or not collection_id:
                            locations.append(save_default_profile(DATA_ROOT, profile))
                        st.success(
                            "Workflow validado e salvo: "
                            + ", ".join(str(location.relative_to(ROOT)) for location in locations)
                        )
                except (ValidationError, ComfyError, ValueError) as error:
                    show_error(error)
        except (json.JSONDecodeError, ValueError) as error:
            show_error(error)
    if not collection_id:
        defaults = [kind_name for kind_name in ("generation", "upscale", "inpainting")
                    if load_default_profile(DATA_ROOT, kind_name)]
        st.caption("Workflows padrão: " + (", ".join(defaults) if defaults else "nenhum"))
    if collection_id:
        root = orchestrator.images.collection_path(collection_id)
        source_labels = {"collection": "desta coleção", "default": "padrão"}
        configured = [
            f"{kind_name} ({source_labels[source]})"
            for kind_name in ("generation", "upscale", "inpainting")
            if (source := profile_source(root, kind_name))
        ]
        st.caption("Workflows configurados: " + (", ".join(configured) if configured else "nenhum"))
        generation_profile = load_profile(root, "generation")
        if generation_profile and "seed" not in generation_profile.mappings:
            seed_refs = [
                WorkflowInputRef(node_id=node_id, input_name=input_name)
                for node_id, node in generation_profile.workflow.items()
                for input_name in node.get("inputs", {})
                if input_name in {"seed", "noise_seed"}
            ]
            if len(seed_refs) == 1:
                seed_ref = seed_refs[0]
                st.warning(
                    f"Este workflow não aplicava seeds aleatórias. Foi encontrado um campo "
                    f"`{seed_ref.input_name}` no nó `{seed_ref.node_id}`; corrija o mapeamento antes do próximo lote."
                )
                if st.button("Mapear seed neste único campo", key=f"map_seed_{collection_id}"):
                    mappings = dict(generation_profile.mappings)
                    mappings["seed"] = seed_ref
                    try:
                        corrected_profile = ComfyWorkflowProfile.model_validate({
                            **generation_profile.model_dump(mode="json"),
                            "mappings": mappings,
                        })
                        issues = orchestrator.comfy.validate_profile(corrected_profile)
                        if issues:
                            for issue in issues:
                                st.error(issue)
                        else:
                            path = save_profile(root, corrected_profile)
                            st.success(f"Seed mapeada e workflow revalidado: {path.relative_to(ROOT)}")
                            st.rerun()
                    except (ValidationError, ComfyError, ValueError) as error:
                        show_error(error)
            elif len(seed_refs) > 1:
                st.warning(
                    "Este workflow tem vários campos de seed; reimporte-o e escolha manualmente "
                    "qual deles controla o ruído de geração."
                )


UPSCALE_STATES = {
    WorkflowState.SELECTED.value, WorkflowState.UPSCALING.value, WorkflowState.FINAL_REVIEW.value,
}


def render_concept(orchestrator: Orchestrator, config: AppConfig, collection_id: str, concept: dict) -> None:
    database = orchestrator.database
    state = WorkflowState(concept["state"])
    plan = ConceptPlan.model_validate(concept["data"])
    st.markdown(f"### {plan.concept}")
    st.caption(f"Estado: `{state.value}` · conceito `{concept['id'][:8]}`")
    if state in {WorkflowState.FAILED, WorkflowState.CANCELLED}:
        candidates = [asset for asset in database.list_assets(concept["id"]) if asset["role"] in {"candidate", "correction"}]
        target = (
            WorkflowState.SELECTED if concept.get("selected_asset_id")
            else WorkflowState.CANDIDATES_READY if candidates
            else WorkflowState.PROMPTS_APPROVED
        )
        st.warning("Confira o histórico e corrija a causa antes de reabrir esta etapa.")
        confirm_reopen = st.checkbox(
            "Confirmei que a causa da falha/cancelamento foi resolvida",
            key=f"reopen_confirm_{concept['id']}",
        )
        if st.button(
            "Reabrir etapa para nova ação humana",
            disabled=not confirm_reopen,
            key=f"reopen_{concept['id']}",
        ):
            try:
                database.transition_concept(
                    concept["id"], target,
                    "Reabertura explícita após conferência da falha pelo usuário",
                )
                st.rerun()
            except InvalidTransition as error:
                show_error(error)
    if state in {WorkflowState.SELECTED, WorkflowState.UPSCALING}:
        selected = database.get_asset(concept.get("selected_asset_id") or "")
        if selected:
            st.markdown("#### Imagem selecionada")
            render_preview(selected, f"Original · {selected['width']} × {selected['height']}", "upscale_source")
            render_unselect_button(orchestrator, concept, "upscale")
            profile = load_profile(orchestrator.images.collection_path(collection_id), "upscale")
            if not profile:
                st.warning("Importe um workflow API de upscale para continuar.")
            else:
                with st.form(f"upscale_{concept['id']}"):
                    run = st.form_submit_button("Ampliar somente a imagem selecionada", type="primary")
                if run:
                    try:
                        with st.status("Executando upscale…", expanded=True) as status:
                            result = orchestrator.upscale_selected(collection_id, concept, selected, st.write)
                            status.update(label="Upscale salvo", state="complete")
                        st.rerun()
                    except Exception as error:
                        show_error(error)
        pending_upscale = [
            job for job in database.list_recoverable_jobs(collection_id)
            if job["concept_id"] == concept["id"] and job["kind"] == "upscale"
        ]
        if pending_upscale:
            st.warning("Upscale ainda ativo ou interrompido no ComfyUI; confira o mesmo ID antes de repetir.")
            if st.button("Retomar upscale existente", key=f"resume_upscale_{concept['id']}"):
                try:
                    orchestrator.resume_jobs(collection_id, st.write, concept["id"])
                    st.rerun()
                except Exception as error:
                    show_error(error)
            if st.button("Cancelar upscale", key=f"cancel_upscale_{concept['id']}"):
                try:
                    orchestrator.cancel_jobs(collection_id, concept["id"])
                    st.rerun()
                except Exception as error:
                    show_error(error)

    if state == WorkflowState.FINAL_REVIEW:
        selected = database.get_asset(concept.get("selected_asset_id") or "")
        upscaled = [asset for asset in database.list_assets(concept["id"]) if asset["role"] == "upscaled"]
        if upscaled:
            final_asset = upscaled[-1]
            before, after = st.columns(2)
            with before:
                source = selected or final_asset
                render_preview(source, f"Origem selecionada · {source['width']} × {source['height']}", "review_source")
                if selected and st.button("Abrir original no visualizador", key=f"open_original_{concept['id']}"):
                    try:
                        os.startfile(selected["path"])
                    except (AttributeError, OSError) as error:
                        show_error(error)
            with after:
                render_preview(
                    final_asset, f"Resultado ampliado · {final_asset['width']} × {final_asset['height']}", "review_result",
                )
                if st.button("Abrir upscale no visualizador", key=f"open_upscale_{concept['id']}"):
                    try:
                        os.startfile(final_asset["path"])
                    except (AttributeError, OSError) as error:
                        show_error(error)
            st.caption("Use o visualizador local para inspecionar a imagem a 100%; a recomendação visual é auxiliar.")
            if st.button("Aprovar upscale", type="primary", icon=":material/check:", key=f"approve_upscale_{concept['id']}"):
                database.update_asset_review(final_asset["id"], {
                    "human_approved": True, "human_rejected": False,
                    "reviewed_at": datetime.now().isoformat(),
                })
                st.success("Upscale aprovado. Gere os metadados na aba Metadados.")
            with st.expander("Rejeitar upscale"):
                with st.form(f"reject_upscale_{concept['id']}"):
                    reason = st.text_input("Motivo da rejeição")
                    defects = st.text_area("Defeitos observados (opcional)")
                    reject = st.form_submit_button("Rejeitar e voltar para a seleção")
                if reject:
                    if not reason.strip():
                        st.error("Informe o motivo da rejeição.")
                    else:
                        database.update_asset_review(final_asset["id"], {
                            "human_approved": False, "human_rejected": True,
                            "reason": reason, "observations": defects,
                            "reviewed_at": datetime.now().isoformat(),
                        })
                        database.transition_concept(
                            concept["id"], WorkflowState.SELECTED,
                            f"Upscale rejeitado na revisão final: {reason}",
                        )
                        st.rerun()


METADATA_STATES = {
    WorkflowState.FINAL_REVIEW, WorkflowState.METADATA_READY, WorkflowState.LOCALLY_APPROVED,
    WorkflowState.PREPARED_FOR_TRANSFER, WorkflowState.TRANSFERRED, WorkflowState.SUBMITTED,
}


def render_metadata_tab(orchestrator: Orchestrator, config: AppConfig, collection_id: str) -> None:
    database = orchestrator.database
    render_metadata_all(orchestrator, config, collection_id)
    concepts = [
        concept for concept in database.list_concepts(collection_id)
        if WorkflowState(concept["state"]) in METADATA_STATES and final_upscale(database, concept["id"])
    ]
    if not concepts:
        st.info("Nenhuma imagem ampliada ainda. Amplie e aprove os upscales na aba Ampliação.")
        return
    for concept in concepts:
        st.divider()
        render_concept_metadata(orchestrator, config, collection_id, concept)


def render_metadata_all(orchestrator: Orchestrator, config: AppConfig, collection_id: str) -> None:
    database = orchestrator.database
    pending = metadata_targets(database, collection_id)
    to_approve = metadata_approval_targets(database, collection_id)
    vision_models = model_options(model_list(config.lmstudio_base_url), vision=True)
    planner_model, preferred = agent_models(config, collection_id)
    running = any(
        run.get("mode") == "metadata_all"
        for run in current_pipeline().runs(collection_id, active_only=True)
    )
    with st.container(border=True):
        model = None
        if vision_models:
            model = st.selectbox(
                "Modelo com visão para o lote", vision_models,
                index=vision_models.index(preferred) if preferred in vision_models else 0,
                key=f"metadata_all_model_{collection_id}",
            )
        create_column, approve_column = st.columns(2)
        with create_column:
            if st.button(
                f"Criar todos os metadados ({len(pending)})", type="primary", icon=":material/sell:",
                disabled=running or not pending or not model, width="stretch",
                key=f"metadata_all_{collection_id}",
            ):
                start_pipeline(orchestrator, collection_id, "metadata_all", planner_model, model)
                st.rerun()
            if running:
                st.caption("Metadados em lote em andamento; acompanhe o progresso no painel lateral.")
            elif not vision_models:
                st.caption("Nenhum modelo com visão no LM Studio; gere os metadados um a um com uma descrição.")
            elif not pending:
                st.caption("Nenhum upscale aprovado aguardando metadados.")
            else:
                st.caption("Roda em segundo plano: o modelo analisa cada upscale aprovado.")
        with approve_column:
            with st.popover(
                f"Aprovar todos os metadados ({len(to_approve)})", icon=":material/done_all:",
                disabled=running or not to_approve, width="stretch",
            ):
                st.write(f"Aprovar localmente os metadados de {len(to_approve)} imagem(ns)?")
                st.caption("Confira títulos e palavras-chave abaixo antes de confirmar.")
                if st.button("Confirmar aprovação", type="primary", key=f"approve_all_meta_{collection_id}"):
                    failures = []
                    for concept, _asset in to_approve:
                        try:
                            database.transition_concept(
                                concept["id"], WorkflowState.METADATA_READY,
                                "Metadados aprovados em lote pelo usuário",
                            )
                        except InvalidTransition as error:
                            failures.append(f"{concept['id'][:8]}: {error}")
                    if failures:
                        show_error(RuntimeError("Falha ao aprovar: " + "; ".join(failures)))
                    else:
                        st.rerun()
            st.caption(
                "Nenhum metadado aguardando aprovação." if not to_approve
                else "Move para 'metadados prontos' todas as imagens com metadados gerados."
            )


def render_concept_metadata(
    orchestrator: Orchestrator, config: AppConfig, collection_id: str, concept: dict,
) -> None:
    database = orchestrator.database
    state = WorkflowState(concept["state"])
    plan = ConceptPlan.model_validate(concept["data"])
    final_asset = final_upscale(database, concept["id"])
    if state not in METADATA_STATES or not final_asset:
        return
    upscale_approved = state != WorkflowState.FINAL_REVIEW or (final_asset.get("review") or {}).get("human_approved")
    stored = final_asset.get("metadata") or {}
    metadata_column, details_column = st.columns([3, 2], gap="large")

    with details_column:
        st.markdown(f"### {plan.concept}")
        st.caption(f"Estado: `{state.value}` · conceito `{concept['id'][:8]}`")
        st.image(final_asset["path"], width="stretch", caption=f"Upscale · {final_asset['width']} × {final_asset['height']}")
        if not upscale_approved:
            st.info("Aprove o upscale na aba Ampliação antes de sugerir metadados.")
        else:
            models = model_list(config.lmstudio_base_url)
            vision_models = model_options(models, vision=True)
            use_vision = bool(vision_models) and st.checkbox(
                "Usar modelo com visão para analisar a imagem (ação envia pixels ao modelo)",
                value=True, key=f"metadata_vision_{concept['id']}",
            )
            if use_vision:
                selected_model = st.selectbox("Modelo visual", vision_models, key=f"metadata_vision_model_{concept['id']}")
            else:
                text_models = model_options(models)
                selected_model = st.selectbox(
                    "Modelo de metadados",
                    text_models or ([config.planner_model] if config.planner_model else [""]),
                    key=f"meta_model_{concept['id']}",
                )
            validated_description = st.text_area(
                "Descrição visual conferida por você" + (" (opcional)" if use_vision else ""),
                placeholder="Descreva somente elementos que você confirmou na imagem.",
                key=f"meta_description_{concept['id']}",
            )
            if st.button(
                "Gerar de novo" if stored else "Sugerir metadados", icon=":material/auto_awesome:",
                disabled=not selected_model, key=f"suggest_metadata_{concept['id']}",
            ):
                try:
                    with st.spinner("Preparando sugestão, sem armazenar raciocínio interno…"):
                        metadata, duration, attempts = orchestrator.lmstudio.suggest_metadata(
                            selected_model, plan, validated_description,
                            config.max_agent_attempts,
                            Path(final_asset["path"]) if use_vision else None,
                        )
                    saved_metadata = metadata.model_dump(mode="json")
                    database.update_asset_metadata(final_asset["id"], saved_metadata)
                    database.record_agent_run(
                        collection_id, concept["id"], "metadata", selected_model,
                        INSTRUCTION_VERSION, "completed", saved_metadata, "", duration, attempts,
                    )
                    st.rerun()
                except Exception as error:
                    if isinstance(error, AgentError):
                        database.record_agent_run(
                            collection_id, concept["id"], "metadata", selected_model,
                            INSTRUCTION_VERSION, "failed", None, str(error),
                            error.duration_ms, error.attempts,
                        )
                    show_error(error)

    with metadata_column:
        st.markdown("#### Metadados")
        if not stored:
            st.caption("Ainda sem metadados. Use **Sugerir metadados** ao lado ou o botão de lote no topo da aba.")
            return
        if state != WorkflowState.FINAL_REVIEW:
            st.success("Metadados aprovados.", icon=":material/check_circle:")
        title_length = len(stored.get("title_en", ""))
        if title_length > CSV_TITLE_LIMIT:
            st.warning(f"Título com {title_length} caracteres; o CSV do Adobe Stock aceita até {CSV_TITLE_LIMIT}.")
        if stored.get("analysis_source") == "image":
            st.caption(":material/visibility: Gerados a partir da imagem.")
        else:
            st.warning("Gerados só a partir da descrição; confira os termos na imagem.")
        with st.form(f"edit_metadata_{concept['id']}"):
            title = st.text_input(f"Título em inglês ({title_length}/{CSV_TITLE_LIMIT} caracteres)", stored.get("title_en", ""))
            keywords = st.text_area(
                f"Palavras-chave ordenadas, uma por linha ({len(stored.get('keywords', []))})",
                "\n".join(stored.get("keywords", [])), height=260,
            )
            category_options = [0, *ADOBE_CATEGORIES]
            suggested = category_number(stored.get("category_suggestion")) or 0
            category = st.selectbox(
                "Categoria Adobe Stock", category_options, index=category_options.index(suggested),
                format_func=lambda number: f"{number}. {ADOBE_CATEGORIES[number]}" if number else "Sem categoria",
            )
            terms = st.text_area("Termos a confirmar", "\n".join(stored.get("terms_to_confirm", [])), height=90)
            save_metadata = st.form_submit_button("Salvar edição", icon=":material/save:")
        if save_metadata:
            try:
                edited = MetadataSuggestion(
                    title_en=title,
                    keywords=[term.strip() for term in keywords.splitlines() if term.strip()],
                    category_suggestion=ADOBE_CATEGORIES.get(category, ""),
                    terms_to_confirm=[term.strip() for term in terms.splitlines() if term.strip()],
                    analysis_source=stored.get("analysis_source", "description"),
                )
                database.update_asset_metadata(final_asset["id"], edited.model_dump(mode="json"))
                st.rerun()
            except ValidationError as error:
                show_error(error)
        if state == WorkflowState.FINAL_REVIEW and st.button(
            "Aprovar metadados localmente", type="primary", icon=":material/check:",
            key=f"approve_meta_{concept['id']}",
        ):
            try:
                database.transition_concept(concept["id"], WorkflowState.METADATA_READY, "Metadados revisados e aprovados pelo usuário")
                st.rerun()
            except InvalidTransition as error:
                show_error(error)


def render_upscale_all(orchestrator: Orchestrator, collection_id: str) -> None:
    pending = upscale_targets(orchestrator.database, collection_id)
    if not pending:
        return
    profile = load_profile(orchestrator.images.collection_path(collection_id), "upscale")
    running = any(
        run.get("mode") == "upscale_all"
        for run in current_pipeline().runs(collection_id, active_only=True)
    )
    label = f"Ampliar todas as imagens selecionadas ({len(pending)})"
    if st.button(
        label, type="primary", icon=":material/zoom_out_map:",
        disabled=profile is None or running, key=f"upscale_all_{collection_id}",
    ):
        start_pipeline(orchestrator, collection_id, "upscale_all", "", "")
        st.rerun()
    if profile is None:
        st.caption("Importe um workflow API de upscale em Configuração → Workflows ComfyUI para ampliar as imagens.")
    elif running:
        st.caption("Ampliação em lote em andamento; acompanhe o progresso no painel lateral.")
    else:
        st.caption("Roda em segundo plano, uma imagem por vez, sem sobrescrever as originais.")


EXPORT_DONE_STATES = {
    WorkflowState.PREPARED_FOR_TRANSFER, WorkflowState.TRANSFERRED, WorkflowState.SUBMITTED,
    WorkflowState.ADOBE_APPROVED, WorkflowState.ADOBE_REJECTED,
}


def render_export(orchestrator: Orchestrator, collection_id: str | None) -> None:
    st.subheader("Preparação e envio manual")
    if not collection_id:
        st.info("Selecione uma coleção.")
        return
    database = orchestrator.database
    concepts = database.list_concepts(collection_id)
    rules = orchestrator.config.export_rules
    st.caption("Nenhum endpoint de upload Adobe será chamado. Transferência, submissão e decisão Adobe têm confirmações separadas.")
    if not rules.technical_rules_verified:
        st.warning("Limites técnicos ainda não foram verificados na fonte oficial; exportação mostra avisos e não presume aprovação.")
    render_prepare_all(orchestrator, concepts)
    render_adobe_csv(orchestrator, collection_id, concepts)
    render_transfer_all(orchestrator, collection_id, concepts)
    for concept in concepts:
        st.divider()
        render_concept_export(orchestrator, collection_id, concept)


def render_transfer_all(orchestrator: Orchestrator, collection_id: str, concepts: list[dict]) -> None:
    exports = orchestrator.images.collection_path(collection_id) / "exports"
    prepared = [concept for concept in concepts if concept["state"] == WorkflowState.PREPARED_FOR_TRANSFER.value]
    if not prepared and not exports.is_dir():
        return
    st.markdown("#### Envio para o Adobe Stock")
    st.caption(f"Pasta dos pacotes: {exports}")
    folder_column, portal_column = st.columns(2)
    if folder_column.button("Abrir pasta preparada", icon=":material/folder_open:", key=f"open_folder_{collection_id}"):
        try:
            os.startfile(exports)
        except (AttributeError, OSError) as error:
            show_error(error)
    if portal_column.button("Abrir portal Adobe Stock Contributor", icon=":material/open_in_new:", key=f"open_portal_{collection_id}"):
        webbrowser.open("https://contributor.stock.adobe.com/")
    st.caption("No portal, marque conteúdo criado com IA; confirme pessoas/propriedades fictícias quando aplicável e inclua liberações necessárias.")
    if prepared:
        with st.form(f"transfer_all_{collection_id}"):
            note = st.text_input("Nota da transferência para a coleção (opcional)")
            register = st.form_submit_button(
                f"Registrar transferência de todas as imagens ({len(prepared)})", type="primary",
                icon=":material/cloud_done:",
            )
        if register:
            batch_transition(
                orchestrator, prepared, WorkflowState.TRANSFERRED,
                note.strip() or "Transferência manual confirmada pelo usuário",
            )
    transferred = [concept for concept in concepts if concept["state"] == WorkflowState.TRANSFERRED.value]
    if transferred:
        st.caption("Transferência registrada ainda não equivale a submissão para revisão da Adobe.")
        if st.button(
            f"Confirmo que finalizei a submissão no portal ({len(transferred)})", type="primary",
            icon=":material/send:", key=f"submit_all_{collection_id}",
        ):
            batch_transition(
                orchestrator, transferred, WorkflowState.SUBMITTED, "Submissão manual confirmada pelo usuário",
            )
    submitted = [concept for concept in concepts if concept["state"] == WorkflowState.SUBMITTED.value]
    if submitted:
        st.caption("Aprove ou rejeite cada imagem abaixo, ou marque as rejeitadas e aprove as restantes de uma vez.")
        with st.form(f"approve_all_{collection_id}"):
            note = st.text_input("Nota do resultado para a coleção (opcional)")
            approve = st.form_submit_button(
                f"Marcar todas as submetidas como aprovadas ({len(submitted)})", type="primary",
                icon=":material/verified:",
            )
        if approve:
            batch_transition(
                orchestrator, submitted, WorkflowState.ADOBE_APPROVED,
                f"{APPROVAL_PREFIX}: {note.strip()}" if note.strip() else APPROVAL_PREFIX,
            )


def batch_transition(orchestrator: Orchestrator, concepts: list[dict], target: WorkflowState, reason: str) -> None:
    failures = []
    for concept in concepts:
        try:
            orchestrator.database.transition_concept(concept["id"], target, reason)
        except InvalidTransition as error:
            failures.append(f"{concept['data']['concept']}: {error}")
    for failure in failures:
        st.error(failure)
    if not failures:
        st.rerun()


def render_prepare_all(orchestrator: Orchestrator, concepts: list[dict]) -> None:
    ready = [
        concept for concept in concepts
        if concept["state"] == WorkflowState.METADATA_READY.value and final_upscale(orchestrator.database, concept["id"])
    ]
    if not ready:
        return
    if st.button(
        f"Preparar todos os pacotes ({len(ready)})", type="primary", icon=":material/inventory_2:",
        key=f"prepare_all_{concepts[0]['collection_id']}",
    ):
        failures = []
        progress = st.progress(0.0, text="Preparando pacotes…")
        for index, concept in enumerate(ready, start=1):
            try:
                orchestrator.prepare_package(concept)
            except Exception as error:
                failures.append(f"{concept['data']['concept']}: {error}")
            progress.progress(index / len(ready), text=f"{index}/{len(ready)} pacote(s) processado(s)")
        if failures:
            for failure in failures:
                st.error(failure)
            st.warning(f"{len(ready) - len(failures)} de {len(ready)} pacote(s) preparado(s).")
        else:
            st.rerun()
    st.caption("Gera o JPEG final, com título e palavras-chave embutidos, de cada imagem com metadados aprovados.")


def render_adobe_csv(orchestrator: Orchestrator, collection_id: str, concepts: list[dict]) -> None:
    rows = []
    for concept in concepts:
        if WorkflowState(concept["state"]) not in EXPORT_DONE_STATES:
            continue
        asset = final_upscale(orchestrator.database, concept["id"])
        entry = (asset or {}).get("export") or {}
        if entry:
            # Metadados atuais: edições feitas depois da preparação também entram no CSV.
            metadata = asset.get("metadata") or {}
            rows.append({
                "filename": entry["filename"],
                "title": metadata.get("title_en") or entry["title"],
                "keywords": metadata.get("keywords") or entry["keywords"],
                "category": metadata.get("category_suggestion") or entry.get("category_suggestion"),
            })
    if not rows:
        return
    content, warnings = adobe_csv(rows)
    exports = orchestrator.images.collection_path(collection_id) / "exports"
    csv_path = exports / "adobe_stock.csv"
    if not csv_path.is_file() or csv_path.read_text(encoding="utf-8") != content:
        exports.mkdir(parents=True, exist_ok=True)
        csv_path.write_text(content, encoding="utf-8")
    st.download_button(
        f"Baixar CSV para o portal Adobe ({len(rows)} imagem(ns))", content.encode("utf-8"),
        file_name="adobe_stock.csv", mime="text/csv", icon=":material/download:",
        key=f"adobe_csv_{collection_id}",
    )
    st.caption(
        "Título e palavras-chave já vão dentro de cada JPEG. O CSV é um reforço que também define a "
        f"categoria: no portal, Uploaded Files → New → Upload CSV. Cópia salva em {csv_path}."
    )
    for warning in warnings:
        st.warning(warning)


def render_concept_export(orchestrator: Orchestrator, collection_id: str, concept: dict) -> None:
    database = orchestrator.database
    state = WorkflowState(concept["state"])
    asset = final_upscale(database, concept["id"])
    info_column, image_column = st.columns([3, 2], gap="large")
    with image_column:
        if asset:
            st.image(asset["path"], width="stretch", caption=f"Upscale · {asset['width']} × {asset['height']}")
    with info_column:
        st.markdown(f"#### {concept['data']['concept']}")
        st.caption(f"Estado: `{state.value}`")
        if state == WorkflowState.METADATA_READY and asset:
            metadata = asset.get("metadata") or {}
            st.markdown(f"**{metadata.get('title_en', '')}**")
            st.caption(f"{len(metadata.get('keywords', []))} palavras-chave")
            if st.button("Preparar JPEG", type="primary", key=f"prepare_package_{concept['id']}"):
                try:
                    output, _ = orchestrator.prepare_package(concept)
                    st.success(f"Pacote preparado: {output}")
                    st.rerun()
                except Exception as error:
                    show_error(error)
        elif state in EXPORT_DONE_STATES and asset:
            render_package_status(orchestrator, collection_id, concept, state, asset)
        else:
            st.caption("O pacote estará disponível após revisão final, metadados e aprovação humana local.")


def render_package_status(
    orchestrator: Orchestrator, collection_id: str, concept: dict, state: WorkflowState, asset: dict,
) -> None:
    database = orchestrator.database
    package = asset.get("export") or {}
    if package:
        st.success(
            f"{package['filename']} · {package['width']} × {package['height']} · "
            f"{package['megapixels']:.2f} MP · {package['size_bytes'] / 1_000_000:.2f} MB"
        )
        for warning in package.get("warnings") or []:
            st.warning(warning)
    else:
        st.warning("Registro do pacote não encontrado; prepare o JPEG novamente se precisar dos detalhes.")
    if state == WorkflowState.PREPARED_FOR_TRANSFER:
        st.caption("Aguardando transferência; registre no topo da página.")
    elif state == WorkflowState.TRANSFERRED:
        st.caption("Transferida; aguardando submissão. Confirme no topo da página.")
    elif state == WorkflowState.SUBMITTED:
        if st.button(
            "Marcar como aprovada", type="primary", icon=":material/verified:", key=f"approve_{concept['id']}",
        ):
            database.transition_concept(concept["id"], WorkflowState.ADOBE_APPROVED, APPROVAL_PREFIX)
            st.rerun()
        with st.form(f"reject_{concept['id']}"):
            reason = st.text_input("Motivo da rejeição pela Adobe")
            reject = st.form_submit_button("Marcar como rejeitada", icon=":material/block:")
        if reject:
            if not reason.strip():
                st.error("Informe o motivo da rejeição.")
            else:
                database.transition_concept(concept["id"], WorkflowState.ADOBE_REJECTED, f"{REJECTION_PREFIX}: {reason.strip()}")
                st.rerun()
    elif state == WorkflowState.ADOBE_APPROVED:
        st.success("Aprovada pela Adobe.", icon=":material/verified:")
    elif state == WorkflowState.ADOBE_REJECTED:
        st.error("Rejeitada pela Adobe.", icon=":material/block:")


def render_history(orchestrator: Orchestrator, collection_id: str | None) -> None:
    st.subheader("Histórico de execuções")
    history = orchestrator.database.list_history(100)
    if history:
        st.dataframe(history, width="stretch", hide_index=True)
    else:
        st.info("Ainda não há chamadas a agentes ou trabalhos ComfyUI registrados.")
    if collection_id:
        st.subheader("Trabalhos da coleção")
        jobs = orchestrator.database.list_jobs(collection_id)
        if jobs:
            st.dataframe(jobs, width="stretch", hide_index=True)


def elapsed_seconds(started_at: str) -> float:
    return max((datetime.now(UTC) - datetime.fromisoformat(started_at)).total_seconds(), 0)


def elapsed_label(started_at: str) -> str:
    """Há quanto tempo um cronômetro está aberto (HH:MM)."""
    minutes = int(elapsed_seconds(started_at) // 60)
    return f"{minutes // 60}h{minutes % 60:02d}"


def render_timer_stop(orchestrator: Orchestrator, running: dict[str, Any], key: str) -> str | None:
    """Botão Parar com proteção contra cronômetro esquecido. Devolve a mensagem do resultado, se parou."""
    database = orchestrator.database
    max_minutes = orchestrator.config.timer_max_hours * 60
    overrun_key = f"timer_overrun_{running['id']}"
    if overrun_key in st.session_state:
        measured = st.session_state[overrun_key]
        st.warning(
            f"O cronômetro mediu {measured / 60:.1f} h, acima do limite de "
            f"{orchestrator.config.timer_max_hours:.0f} h. Informe os minutos realmente trabalhados."
        )
        minutes = st.number_input(
            "Minutos reais", min_value=1.0, max_value=float(MAX_WORK_MINUTES),
            value=float(min(max(measured, 1.0), MAX_WORK_MINUTES)) if measured <= MAX_WORK_MINUTES
            else float(max_minutes),
            step=5.0, key=f"{key}_override_minutes",
        )
        if st.button("Confirmar minutos e parar", key=f"{key}_override_confirm", type="primary"):
            try:
                database.stop_timer(running["id"], minutes_override=minutes, max_minutes=max_minutes)
                st.session_state.pop(overrun_key, None)
                return f"Cronômetro parado: {minutes:.1f} min registrados."
            except ValueError as error:
                show_error(error)
        return None
    if st.button("Parar cronômetro", key=f"{key}_stop", type="primary", width="stretch"):
        try:
            minutes = database.stop_timer(running["id"], max_minutes=max_minutes)
        except TimerTooLong as error:
            st.session_state[overrun_key] = error.minutes
            st.rerun()
        except ValueError as error:
            show_error(error)
        else:
            if minutes is None:
                return "Menos de 1 minuto: cronômetro descartado, nada foi registrado."
            return f"Cronômetro parado: {minutes:.1f} min registrados em {running['collection_name']}."
    return None


@st.fragment(run_every=30)
def _sidebar_timer_fragment(orchestrator: Orchestrator) -> None:
    running = orchestrator.database.open_timer()
    if not running:
        return
    limit = orchestrator.config.timer_max_hours
    elapsed_hours = elapsed_seconds(running["started_at"]) / 3600
    with st.container(border=True):
        st.markdown(f"**Cronômetro aberto** em {running['collection_name']}")
        text = f"Rodando há {elapsed_label(running['started_at'])}"
        if elapsed_hours > limit:
            st.warning(f"{text}, acima do limite de {limit:.0f} h. Esqueceu de parar?")
        else:
            st.caption(text)
        message = render_timer_stop(orchestrator, running, "sidebar_timer")
    if message:
        st.toast(message)
        st.rerun()


def render_sidebar_timer(orchestrator: Orchestrator) -> None:
    """Cronômetro aberto, compacto, para não ser esquecido (atualiza a cada 30 s)."""
    if orchestrator.database.open_timer():
        _sidebar_timer_fragment(orchestrator)


def render_work_hours(orchestrator: Orchestrator, collection_id: str) -> None:
    database = orchestrator.database
    st.markdown("#### Horas trabalhadas")
    st.caption(
        "A receita por hora soma duas fontes de tempo: estes lançamentos/cronômetro e o "
        "\"Tempo manual (minutos)\" informado junto com os relatórios acima. "
        "O tempo de um lançamento ou cronômetro que atravessa a meia-noite vai inteiro para a data de início."
    )
    message = st.session_state.pop("work_hours_message", None)
    if message:
        st.success(message)
    running = database.open_timer()
    if running is None:
        if st.button("Iniciar cronômetro", key="timer_start"):
            try:
                database.start_timer(collection_id)
                st.rerun()
            except (ValueError, KeyError) as error:
                show_error(error)
    else:
        same = running["collection_id"] == collection_id
        if same:
            st.info(f"Cronômetro rodando há {elapsed_label(running['started_at'])}.")
        else:
            st.warning(
                f"Há um cronômetro aberto na coleção \"{running['collection_name']}\" "
                f"(há {elapsed_label(running['started_at'])}). Pare-o para iniciar outro aqui."
            )
        columns = st.columns(2)
        with columns[0]:
            stop_message = render_timer_stop(orchestrator, running, "timer")
        if stop_message:
            st.session_state["work_hours_message"] = stop_message
            st.rerun()
        if columns[1].button("Descartar cronômetro", key="timer_discard"):
            try:
                database.discard_timer(running["id"])
                st.session_state.pop(f"timer_overrun_{running['id']}", None)
                st.rerun()
            except ValueError as error:
                show_error(error)
    with st.form("work_session_entry", clear_on_submit=True):
        st.markdown("**Lançamento manual**")
        form_columns = st.columns(3)
        work_date = form_columns[0].date_input("Data do trabalho", value=date.today())
        hours = form_columns[1].number_input("Horas", min_value=0.0, max_value=24.0, step=0.25, help="Aceita decimais (1,5 = 1h30).")
        extra_minutes = form_columns[2].number_input("Minutos adicionais", min_value=0, max_value=59, step=5)
        work_notes = st.text_input("Nota (opcional)", max_chars=1000)
        add = st.form_submit_button("Lançar horas")
    if add:
        try:
            database.add_work_session(
                collection_id, work_date.isoformat(), hours * 60 + extra_minutes, work_notes,
            )
            st.session_state["work_hours_message"] = "Lançamento salvo."
            st.rerun()
        except (ValueError, KeyError) as error:
            show_error(error)
    sessions = database.list_work_sessions(collection_id)
    if sessions:
        labels = {"manual": "manual", "timer": "cronômetro"}
        st.dataframe(
            [
                {
                    "data": item["work_date"],
                    "origem": labels[item["source"]] + (" (em andamento)" if item["minutes"] is None else ""),
                    "minutos": item["minutes"],
                    "nota": item["notes"],
                }
                for item in sessions
            ],
            width="stretch", hide_index=True,
        )
        closed = [item for item in sessions if item["minutes"] is not None]
        if closed:
            with st.expander("Excluir lançamento"):
                choice = st.selectbox(
                    "Lançamento", [item["id"] for item in closed], key="work_delete_choice",
                    format_func=lambda value: next(
                        f"{item['work_date']} - {item['minutes']:.1f} min ({labels[item['source']]})"
                        + (f" - {item['notes']}" if item["notes"] else "")
                        for item in closed if item["id"] == value
                    ),
                )
                confirmed = st.checkbox(
                    "Confirmo a exclusão deste lançamento", key=f"work_delete_confirm_{choice}",
                )
                if st.button("Excluir lançamento", disabled=not confirmed, key="work_delete"):
                    try:
                        database.delete_work_session(choice)
                        st.session_state["work_hours_message"] = "Lançamento excluído."
                        st.rerun()
                    except KeyError as error:
                        show_error(error)


def collection_labels(
    database: Database, *row_groups: list[dict[str, Any]],
) -> dict[str | None, str]:
    """Nome de cada chave de coleção; removidas aparecem como "<nome> (removida)"."""
    labels: dict[str | None, str] = {item["id"]: item["name"] for item in database.list_collections()}
    for rows in row_groups:
        for row in rows:
            key = collection_key({"collection_id": row.get("collection_id"), "detached_from": row.get("detached_from")})
            if key and key not in labels:
                labels[key] = f"{row.get('collection_name') or 'Coleção'} (removida)"
    labels[None] = "Sem coleção"
    return labels


def render_indicators(
    database: Database, sales: list[dict[str, Any]], hours_metrics: dict[str, Any],
    archived: list[dict[str, Any]],
) -> None:
    st.markdown("#### Indicadores")
    moderation = database.list_moderation()
    rates = approval_rate(moderation)
    weekly = approved_per_week(moderation, datetime.now(UTC))
    per_image = downloads_per_approved(sales, moderation)
    latest = max(per_image["total"]) if per_image["total"] else None
    total_rate = rates["total"]["rate"]
    per_hour_items = [
        f"{values['per_hour']:.2f} {currency}/h"
        for currency, values in hours_metrics["total"]["by_currency"].items()
        if values["per_hour"] is not None
    ]
    columns = st.columns(4)
    columns[0].metric(
        "Taxa de aprovação", "-" if total_rate is None else f"{total_rate:.0%}",
        help="Aprovadas / (aprovadas + rejeitadas); pendentes ficam fora.",
    )
    columns[1].metric(
        "Aprovadas por semana", "-" if weekly["average"] is None else f"{weekly['average']:.1f}",
        help="Média das semanas completas entre as últimas 12 (Europe/Madrid), a partir da semana da "
        "primeira aprovação; a semana em curso fica de fora da média.",
    )
    last_value = per_image["total"][latest]["per_image"] if latest else None
    columns[2].metric(
        f"Downloads por aprovada ({latest})" if latest else "Downloads por aprovada",
        "-" if last_value is None else f"{last_value:.2f}",
        help="Downloads do último mês lançado / imagens aprovadas acumuladas até o fim dele.",
    )
    columns[3].metric(
        "Receita por hora", " · ".join(per_hour_items) if per_hour_items else "-",
        help="Receita de cada moeda / horas totais (sessões + tempo manual). Sem horas: \"-\".",
    )
    import pandas as pd

    chart = pd.DataFrame(
        {"aprovadas": [item["approved"] for item in weekly["series"]]},
        index=[item["week"] for item in weekly["series"]],
    )
    st.caption("Imagens aprovadas por semana (últimas 12; a última barra é a semana em curso)")
    st.bar_chart(chart)
    labels = collection_labels(database, moderation, sales, archived)
    keys = set(rates["collections"]) | set(per_image["collections"]) | set(hours_metrics["collections"])
    table = []
    for key in sorted(keys, key=lambda value: (value is None, str(value).startswith("removida:"), labels.get(value, ""))):
        rate = rates["collections"].get(key, {}).get("rate")
        weekly_collection = approved_per_week(
            [row for row in moderation if collection_key(row) == key], datetime.now(UTC),
        )
        months = per_image["collections"].get(key, {})
        last_month = max(months) if months else None
        per_hour = [
            format_per_hour(values["per_hour"], currency)
            for currency, values in hours_metrics["collections"].get(key, {}).get("by_currency", {}).items()
        ]
        table.append({
            "coleção": labels.get(key, "Coleção removida"),
            "taxa de aprovação": None if rate is None else round(rate * 100, 1),
            "aprovadas/semana (12 sem.)": None if weekly_collection["average"] is None else round(weekly_collection["average"], 2),
            "downloads por aprovada": None if last_month is None or months[last_month]["per_image"] is None else round(months[last_month]["per_image"], 2),
            "mês de referência": last_month,
            "receita/hora": " · ".join(per_hour) if per_hour else None,
        })
    if table:
        st.dataframe(
            table, width="stretch", hide_index=True,
            column_config={"taxa de aprovação": st.column_config.NumberColumn(format="%.1f%%")},
        )
    st.caption(
        "Valores informados manualmente e conferidos no painel da Adobe; sem conversão de moedas nem projeção. "
        "\"Sem coleção\" reúne imagens e lançamentos fora do fluxo; coleções removidas aparecem pelo nome, "
        "com as horas arquivadas na remoção. Aprovada sem data de decisão usa a data de envio ou da "
        "última atualização. Células vazias significam sem dados (sem decisões, sem horas ou sem aprovadas)."
    )


def render_monthly_sales(
    orchestrator: Orchestrator, collection_id: str | None, sales: list[dict[str, Any]],
) -> None:
    database = orchestrator.database
    st.markdown("#### Downloads e receita mensal")
    st.caption(
        "Lance o total do mês conforme o painel da Adobe (um lançamento por coleção e mês). Lançar de "
        "novo o mesmo mês da mesma coleção substitui o anterior, inclusive a moeda (correção)."
    )
    message = st.session_state.pop("monthly_sales_message", None)
    if message:
        st.success(message)
    now = datetime.now(LOCAL_TZ)
    collections = database.list_collections()
    names = {item["id"]: item["name"] for item in collections}
    options = ([collection_id] if collection_id in names else []) + [""] + [
        cid for cid in names if cid != collection_id
    ]
    with st.form("monthly_sales_entry"):
        columns = st.columns(3)
        year = columns[0].selectbox("Ano", list(range(now.year, now.year - 6, -1)))
        month_number = columns[1].selectbox("Mês", list(range(1, 13)), index=now.month - 1, format_func=lambda v: f"{v:02d}")
        target = columns[2].selectbox(
            "Coleção", options, format_func=lambda value: "Sem coleção (fora do fluxo)" if not value else names[value],
        )
        value_columns = st.columns(3)
        downloads = value_columns[0].number_input("Downloads", min_value=0, step=1)
        revenue = value_columns[1].number_input("Receita", min_value=0.0, step=0.1)
        currency = value_columns[2].selectbox("Moeda", ["USD", "EUR", "BRL", "CAD", "AUD"])
        notes = st.text_input("Nota ou fonte (opcional)", max_chars=1000)
        save = st.form_submit_button("Lançar mês", type="primary")
    if save:
        try:
            database.upsert_monthly_sales(
                target or None, f"{year}-{month_number:02d}", int(downloads), revenue, currency, notes,
            )
            st.session_state["monthly_sales_message"] = "Lançamento mensal salvo."
            st.rerun()
        except (ValidationError, ValueError, KeyError) as error:
            show_error(error)
    if sales:
        st.dataframe(
            [
                {
                    "mês": item["month"],
                    "coleção": (
                        f"{item['collection_name']} (removida)" if item["detached_from"] and item["collection_name"]
                        else item["collection_name"] or "Sem coleção"
                    ),
                    "downloads": item["downloads"],
                    "receita": item["revenue"],
                    "moeda": item["currency"],
                    "nota": item["notes"],
                }
                for item in sales
            ],
            width="stretch", hide_index=True,
        )
        with st.expander("Excluir lançamento mensal"):
            choice = st.selectbox(
                "Lançamento", [item["id"] for item in sales], key="monthly_delete_choice",
                format_func=lambda value: next(
                    f"{item['month']} · {item['collection_name'] or 'Sem coleção'}"
                    f"{' (removida)' if item['detached_from'] else ''} · "
                    f"{item['revenue']:.2f} {item['currency']}"
                    for item in sales if item["id"] == value
                ),
            )
            confirmed = st.checkbox("Confirmo a exclusão deste lançamento", key=f"monthly_delete_confirm_{choice}")
            if st.button("Excluir lançamento mensal", disabled=not confirmed, key="monthly_delete"):
                try:
                    database.delete_monthly_sales(choice)
                    st.session_state["monthly_sales_message"] = "Lançamento mensal excluído."
                    st.rerun()
                except KeyError as error:
                    show_error(error)


def render_performance(orchestrator: Orchestrator, collection_id: str | None) -> None:
    st.subheader("Resultados e custos informados")
    st.caption(
        "Números informados manualmente e conferidos no painel da Adobe; não há conversão de moedas "
        "nem projeção de ganhos."
    )
    database = orchestrator.database
    comparisons = database.performance_comparison()
    sales = database.list_monthly_sales()
    archived = [
        {**item, "collection_id": None} for item in database.list_archived_work()
    ]
    hours_metrics = revenue_per_hour(comparisons, database.work_minutes_by_collection(), sales, archived)
    render_indicators(database, sales, hours_metrics, archived)
    render_monthly_sales(orchestrator, collection_id, sales)
    with st.expander("Registro avulso (legado)"):
        st.warning(
            "A receita lançada aqui também entra na receita/hora, somada aos lançamentos mensais. "
            "Não registre o mesmo valor nos dois lugares."
        )
        if collection_id:
            concepts = orchestrator.database.list_concepts(collection_id)
            concept_options = [""] + [concept["id"] for concept in concepts]
            with st.form("performance_entry"):
                report_date = st.date_input("Data do relatório", value=date.today())
                concept_id = st.selectbox(
                    "Conceito (opcional)", concept_options,
                    format_func=lambda value: "Coleção inteira" if not value else next(
                        item["data"]["concept"] for item in concepts if item["id"] == value
                    ),
                )
                downloads = st.number_input("Downloads", min_value=0, step=1)
                revenue_columns = st.columns(2)
                revenue = revenue_columns[0].number_input("Receita registrada", min_value=0.0, step=0.1)
                revenue_currency = revenue_columns[1].selectbox("Moeda da receita", ["USD", "EUR", "BRL", "CAD", "AUD"])
                moderation = st.selectbox("Resultado de moderação", ["unknown", "pending", "accepted", "rejected"], format_func={
                    "unknown": "Não informado", "pending": "Pendente", "accepted": "Aceito", "rejected": "Rejeitado"
                }.get)
                time_columns = st.columns(3)
                generation_seconds = time_columns[0].number_input("Geração (segundos)", min_value=0.0, step=1.0)
                upscale_seconds = time_columns[1].number_input("Upscale (segundos)", min_value=0.0, step=1.0)
                manual_minutes = time_columns[2].number_input("Tempo manual (minutos)", min_value=0.0, step=1.0)
                cost_columns = st.columns(2)
                user_cost = cost_columns[0].number_input("Custo informado", min_value=0.0, step=0.1)
                cost_currency = cost_columns[1].selectbox("Moeda do custo", ["USD", "EUR", "BRL", "CAD", "AUD"])
                notes = st.text_area("Nota ou fonte do registro", max_chars=1000)
                save = st.form_submit_button("Registrar dados", type="primary")
            if save:
                try:
                    entry = PerformanceEntry(
                        report_date=report_date,
                        download_count=int(downloads),
                        revenue=revenue,
                        revenue_currency=revenue_currency,
                        moderation_status=moderation,
                        generation_seconds=generation_seconds,
                        upscale_seconds=upscale_seconds,
                        manual_minutes=manual_minutes,
                        user_cost=user_cost,
                        cost_currency=cost_currency,
                        notes=notes,
                    )
                    orchestrator.database.record_performance(collection_id, entry, concept_id or None)
                    st.success("Registro salvo. A comparação usa somente os dados informados.")
                    st.rerun()
                except (ValidationError, ValueError) as error:
                    show_error(error)
            records = orchestrator.database.list_performance(collection_id)
            if records:
                st.markdown("#### Registros desta coleção")
                st.dataframe(records, width="stretch", hide_index=True)
        else:
            st.caption("Selecione uma coleção para usar o registro avulso.")

    if collection_id:
        render_work_hours(orchestrator, collection_id)

    today = date.today()
    # Uma linha por coleção e moeda, com a receita das duas fontes (avulso + mensal); moedas que só
    # existem nos lançamentos mensais ganham linha própria.
    table_rows: list[dict[str, Any]] = []
    by_collection: dict[str, list[dict[str, Any]]] = {}
    for item in comparisons:
        by_collection.setdefault(item["collection_id"], []).append(item)
    for cid, items in by_collection.items():
        metrics = hours_metrics["collections"].get(cid, {"hours": 0.0, "by_currency": {}})
        currencies = list(metrics["by_currency"])
        base = items[0]
        created = datetime.fromisoformat(base["created_at"]).date()
        for position, currency in enumerate(currencies or [None]):
            legacy = next((item for item in items if item["revenue_currency"] == currency), None)
            values = metrics["by_currency"].get(currency, {})
            table_rows.append({
                "coleção": base["name"],
                "idade (dias)": max((today - created).days, 0),
                "moeda": currency or "-",
                "receita registrada (avulso + mensal)": values.get("revenue"),
                "downloads (avulso)": (legacy or {}).get("downloads") or 0,
                "candidatos": base["candidates"],
                "selecionados": base["selected"],
                "geração (s)": (legacy or {}).get("generation_seconds") or 0,
                "upscale (s)": (legacy or {}).get("upscale_seconds") or 0,
                "tempo manual (min)": sum(item["manual_minutes"] or 0 for item in items) if position == 0 else 0,
                "horas trabalhadas": format_hours(metrics["hours"]),
                "receita/hora": format_per_hour(values.get("per_hour"), currency or ""),
            })
    if table_rows:
        st.markdown("#### Comparação por coleção")
        st.dataframe(table_rows, width="stretch", hide_index=True)
        total = hours_metrics["total"]
        st.markdown("#### Total geral por moeda")
        if total["by_currency"]:
            st.dataframe(
                [
                    {
                        "moeda": currency,
                        "receita": values["revenue"],
                        "base de horas (todas as coleções)": format_hours(total["hours"]),
                        "receita/hora": format_per_hour(values["per_hour"], currency),
                    }
                    for currency, values in total["by_currency"].items()
                ],
                width="stretch", hide_index=True,
            )
        else:
            st.caption(f"Sem receita registrada. Horas totais: {format_hours(total['hours'])}.")
        st.caption("Receita registrada e receita/hora somam as duas fontes (registro avulso e lançamento mensal) por moeda; downloads, geração e upscale vêm só do registro avulso. O tempo manual aparece uma vez por coleção, na primeira linha.")
        st.caption("Coleções com mais de uma moeda repetem as mesmas horas em cada linha: não some a coluna \"horas trabalhadas\" entre linhas da mesma coleção.")
        st.caption("Horas = lançamentos/cronômetro + tempo manual dos relatórios. Receita/hora = receita da moeda / horas da coleção (ou de todas, no total); sem horas aparece \"-\".")
        st.caption("Receitas em moedas diferentes aparecem em linhas separadas; custos e resultados não são convertidos nem extrapolados.")
    render_moderation(orchestrator, collection_id)
    render_adobe_learning(orchestrator)


def _cell(value: Any) -> str:
    return "" if value is None or value != value else str(value).strip()


def render_moderation_editor(
    orchestrator: Orchestrator, key: str, rows: list[dict[str, Any]], *, external: bool,
) -> None:
    """Tabela editável de moderação; salva só as linhas alteradas, comparadas por id."""
    import pandas as pd

    database = orchestrator.database
    for level, text in st.session_state.pop(f"{key}_messages", []):
        getattr(st, level)(text)
    name_column = "imagem" if external else "conceito"
    detail_column = "coleção" if external else "estado no app"
    table = pd.DataFrame(
        [
            {
                name_column: row["title"],
                detail_column: (row.get("collection_name") or "Sem coleção") if external else row["state"],
                "resultado": MODERATION_LABELS[row["status"]],
                "motivo": row["reason"],
                "ID Adobe": row["adobe_id"],
            }
            for row in rows
        ],
        index=pd.Index([row["id"] for row in rows], name="id"),
    )
    # A versão na chave descarta as edições pendentes do widget depois de salvar.
    version = st.session_state.get(f"{key}_version", 0)
    edited = st.data_editor(
        table, key=f"{key}_editor_{version}", hide_index=True, width="stretch", num_rows="fixed",
        disabled=[name_column, detail_column],
        column_config={
            "resultado": st.column_config.SelectboxColumn(
                "resultado", required=True,
                options=[label for status, label in MODERATION_LABELS.items() if status or not external],
            ),
        },
    )
    if not st.button("Salvar moderação", key=f"{key}_save", icon=":material/save:"):
        return
    by_id = {row["id"]: row for row in rows}
    changes = moderation_changes(
        by_id,
        {
            row_id: {
                "status": MODERATION_BY_LABEL[_cell(line["resultado"])],
                "reason": _cell(line["motivo"]), "adobe_id": _cell(line["ID Adobe"]),
            }
            for row_id, line in edited.iterrows()
        },
    )
    if not changes:
        st.info("Nenhuma alteração para salvar.")
        return
    messages: list[tuple[str, str]] = []
    saved = 0
    for change in changes:
        if change["error"]:
            messages.append(("error", change["error"]))
        elif change["warning"]:
            messages.append(("warning", change["warning"]))
        else:
            row = by_id[change["id"]]
            try:
                if external:
                    database.update_moderation_record(
                        row["id"], status=change["status"], reason=change["reason"],
                        adobe_id=change["adobe_id"], notes=row["notes"],
                    )
                else:
                    database.set_moderation(
                        row["concept_id"], change["status"], change["reason"], adobe_id=change["adobe_id"],
                    )
                saved += 1
            except (ValueError, KeyError) as error:
                messages.append(("error", f"{change['title']}: {error}"))
    if not saved:
        for level, text in messages:
            getattr(st, level)(text)
        return
    messages.insert(0, ("success", f"{saved} linha(s) salva(s)."))
    st.session_state[f"{key}_messages"] = messages
    st.session_state[f"{key}_version"] = version + 1
    st.rerun()


def render_moderation(orchestrator: Orchestrator, collection_id: str | None) -> None:
    database = orchestrator.database
    st.markdown("#### Registro da moderação")
    totals = database.adobe_feedback(limit=0)
    local = {"approved": 0, "rejected": 0, "pending": 0}
    if collection_id:
        for record in database.list_moderation(collection_id):
            local[record["status"]] += 1
    columns = st.columns(3)
    for column, status, label in zip(
        columns, ("approved", "rejected", "pending"), ("Aprovadas", "Rejeitadas", "Pendentes"),
    ):
        column.metric(
            label, totals[f"{status}_count"],
            delta=f"{local[status]} na coleção ativa" if collection_id else None, delta_color="off",
        )
    if collection_id:
        candidates = database.moderation_candidates(collection_id)
        if candidates:
            st.caption(
                "Marque o resultado informado pelo portal da Adobe. O estado do conceito avança "
                "automaticamente até a decisão; a rejeição exige motivo."
            )
            for row in candidates:
                row["id"], row["title"] = row["concept_id"], row["concept"]
            render_moderation_editor(orchestrator, "moderation_flow", candidates, external=False)
        else:
            st.info("Nenhum conceito desta coleção chegou à preparação do pacote ainda.")
    else:
        st.caption("Selecione uma coleção na barra lateral para registrar a moderação dos conceitos.")

    collections = database.list_collections()
    names = {item["id"]: item["name"] for item in collections}
    with st.expander("Registrar imagem enviada fora do fluxo"):
        with st.form("moderation_external", clear_on_submit=True):
            title = st.text_input("Título da imagem")
            options = [""] + list(names)
            target = st.selectbox(
                "Coleção", options, index=options.index(collection_id) if collection_id in options else 0,
                format_func=lambda value: names.get(value, "Sem coleção"),
            )
            result = st.selectbox(
                "Resultado", ["approved", "rejected", "pending"],
                format_func=lambda value: MODERATION_LABELS[value],
            )
            reason = st.text_input("Motivo (obrigatório se rejeitada)")
            adobe_id = st.text_input("ID Adobe")
            submitted_on = st.date_input("Data de envio (opcional)", value=None)
            decided_on = st.date_input(
                "Data da decisão (aprovada ou rejeitada; padrão hoje)", value=date.today(),
                help="Usada nas aprovadas por semana. Ignorada se o resultado for pendente.",
            )
            notes = st.text_area("Nota")
            add = st.form_submit_button("Registrar", icon=":material/add:")
        if add:
            try:
                database.add_external_moderation(
                    title, result, target or None, reason, adobe_id,
                    submitted_at=submitted_on.isoformat() if submitted_on else None, notes=notes,
                    decided_at=(
                        datetime.combine(decided_on, time(12, 0), tzinfo=LOCAL_TZ).astimezone(UTC).isoformat(timespec="seconds")
                        if decided_on and decided_on != date.today() else None
                    ),
                )
                st.rerun()
            except (ValueError, KeyError) as error:
                show_error(error)
    # Sem conceito vinculado (externos e registros órfãos de coleções removidas) só se edita aqui.
    externals = [
        {**record, "status": record["status"]}
        for scope in dict.fromkeys([collection_id, None] if collection_id else [None])
        for record in database.list_moderation(scope) if record["concept_id"] is None
    ]
    if externals:
        st.caption("Imagens sem conceito vinculado (fora do fluxo ou de coleções removidas), da coleção ativa e sem coleção.")
        render_moderation_editor(orchestrator, "moderation_external_table", externals, external=True)


def render_adobe_learning(orchestrator: Orchestrator) -> None:
    st.markdown("#### Aprendizado com a moderação da Adobe")
    feedback = orchestrator.database.adobe_feedback()
    st.caption(
        f"Aprovadas: {feedback['approved_count']} · Rejeitadas: {feedback['rejected_count']} · "
        f"Pendentes: {feedback['pending_count']}. Os números vêm do registro de moderação, que inclui "
        "imagens enviadas fora do fluxo. Os motivos de rejeição de todas as coleções são enviados ao planejador, ao revisor visual "
        "e ao refinador de prompt; o planejador também recebe exemplos de conceitos aprovados."
    )
    if feedback["rejected"]:
        st.dataframe(
            [
                {"motivo": item["reason"], "conceito": item["concept"], "coleção": item["theme"],
                 "data": item["created_at"][:10]}
                for item in feedback["rejected"]
            ],
            width="stretch", hide_index=True,
        )
    else:
        st.info("Nenhuma rejeição da Adobe registrada ainda.")
    lessons = adobe_lessons(feedback, examples=True)
    if lessons:
        with st.expander("Texto enviado ao planejador"):
            st.code(lessons, language=None)


def main() -> None:
    config = get_config()
    config_json = config.model_dump_json()
    orchestrator = get_orchestrator(config_json, code_version())
    if type(orchestrator) is not Orchestrator or type(orchestrator.database) is not Database:
        # Objeto em cache criado com classes de flowapp que já foram recarregadas; recria com o código atual.
        get_agent_pipeline.clear()
        get_orchestrator.clear()
        orchestrator = get_orchestrator(config_json, code_version())
    database = orchestrator.database
    recover_stale_runs()
    collections = database.list_collections()

    with st.sidebar:
        st.title("Fluxo Imagens")
        st.caption("Pipeline local com aprovações humanas")
        labels = {item["id"]: item["name"] for item in collections}
        selected = st.selectbox(
            "Coleção ativa",
            [""] + list(labels),
            format_func=lambda value: "Selecione…" if not value else labels[value],
            index=([""] + list(labels)).index(st.session_state.get("active_collection", ""))
            if st.session_state.get("active_collection", "") in [""] + list(labels) else 0,
        )
        if selected:
            st.session_state["active_collection"] = selected
        else:
            st.session_state.pop("active_collection", None)
        render_sidebar_timer(orchestrator)
        if st.session_state.get("active_collection"):
            with st.container(border=True):
                render_agent_status(st.session_state["active_collection"], compact=True)
        st.caption("Inferências de LM Studio e ComfyUI são serializadas. Modelos podem continuar carregados na VRAM; descarregue-os no serviço quando necessário.")

    collection_id = st.session_state.get("active_collection")
    tabs = st.tabs([
        "Painel", "Criação", "Ampliação", "Metadados", "Exportação", "Resultados", "Histórico", "Configuração", "Agentes",
    ])
    with tabs[0]:
        st.title("Produção de imagens para Adobe Stock")
        st.caption("Planejamento → prompts → candidatos → seleção → upscale → revisão final → metadados → preparação manual")
        render_diagnostics(config, orchestrator)
        st.divider()
        counts = {state.value: 0 for state in WorkflowState}
        for collection in collections:
            for concept in database.list_concepts(collection["id"]):
                counts[concept["state"]] = counts.get(concept["state"], 0) + 1
        columns = st.columns(4)
        for column, label, state in zip(
            columns,
            ("Prompts", "Candidatos", "Revisão final", "Pacotes prontos"),
            ("concepts_ready", "candidates_ready", "final_review", "prepared_for_transfer"),
        ):
            column.metric(label, counts.get(state, 0))
        st.caption("A declaração de conteúdo gerado por IA e a elegibilidade final são conferidas manualmente no portal Adobe.")
        st.divider()
        render_collection_overview(database, orchestrator, collections, collection_id)
        st.divider()
        render_collection_creation(database, orchestrator, config)
        with st.expander("Modo manual: escrever conceito e prompt sem o LM Studio"):
            render_manual_collection_creation(database, orchestrator)
    with tabs[1]:
        st.title("Criação da coleção")
        render_evaluation(orchestrator, config, collection_id)
    with tabs[2]:
        st.title("Ampliação")
        if collection_id:
            collection = database.get_collection(collection_id)
            st.divider()
            st.subheader(collection["name"] if collection else "Coleção")
            render_upscale_all(orchestrator, collection_id)
            concepts = [
                concept for concept in database.list_concepts(collection_id)
                if concept["state"] in UPSCALE_STATES
                or (concept["state"] == WorkflowState.FAILED.value and concept.get("selected_asset_id"))
            ]
            if not concepts:
                st.info("Nenhuma imagem aguardando ampliação. Selecione as imagens na aba Criação.")
            for concept in concepts:
                st.divider()
                render_concept(orchestrator, config, collection_id, concept)
        else:
            st.info("Selecione uma coleção no painel lateral ou abra uma pelo Painel para iniciar a produção.")
    with tabs[3]:
        st.title("Metadados")
        if collection_id:
            render_metadata_tab(orchestrator, config, collection_id)
        else:
            st.info("Selecione uma coleção no painel lateral para gerar os metadados.")
    with tabs[4]:
        st.title("Exportação e envio humano")
        render_export(orchestrator, collection_id)
    with tabs[5]:
        st.title("Resultados e esforço")
        render_performance(orchestrator, collection_id)
    with tabs[6]:
        st.title("Histórico")
        render_history(orchestrator, collection_id)
    with tabs[7]:
        st.title("Configuração")
        updated = render_settings(config)
        if updated != config:
            st.info("Recarregue a aplicação para aplicar URLs e modelos alterados aos clientes em memória.")
        st.divider()
        render_workflow_import(orchestrator, collection_id)
        st.markdown("**Exportar workflow API:** abra o workflow no ComfyUI, use `File → Export Workflow (API)` e importe o JSON resultante. O workflow visual `nodes/links` é rejeitado.")
        st.divider()
        st.markdown(
            "**Diagnóstico observado:** Python do aplicativo 3.13.3; GPU NVIDIA RTX 4060 Laptop com "
            "8 GiB de VRAM. Modelos e versões dos serviços são consultados em tempo real no painel."
        )
        rules = config.export_rules
        st.caption(
            f"Limites técnicos Adobe editáveis acima; verificados em {rules.checked_at} na fonte oficial: {rules.source}"
            if rules.technical_rules_verified and rules.source
            else "Limites técnicos Adobe editáveis acima; ainda não verificados na fonte oficial."
        )
    with tabs[8]:
        st.title("Status dos agentes")
        if collection_id:
            render_agent_status(collection_id)
        else:
            st.info("Selecione uma coleção no painel lateral para acompanhar os agentes.")


if __name__ == "__main__":
    main()
