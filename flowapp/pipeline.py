"""Agentes do flow_imagens: planejar, gerar, revisar e recriar imagens de uma coleção.

A mecânica de execução (fila em segundo plano, status por agente, log, cancelamento,
retomada e trava de GPU entre processos) vem do `agentkit`; as execuções aparecem no
painel central (`python -m agentkit.hub`). Aqui ficam só as regras de domínio.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agentkit import Pipeline, PipelineAbort, StepContext

from flowapp.agents import INSTRUCTION_VERSION, PLANNER_INSTRUCTION_VERSION, AgentError, adobe_lessons
from flowapp.briefings import concept_reference
from flowapp.comfy import ComfyError
from flowapp.database import InvalidTransition, utc_now
from flowapp.images import ImageValidationError
from flowapp.orchestrator import PROJECT, Orchestrator
from flowapp.profiles import load_profile
from flowapp.schemas import CollectionBrief, ConceptPlan, WorkflowState
from flowapp.seasonal import seasonal_context

PLANNER = "planner"
GENERATOR = "generator"
REVIEWER = "reviewer"
REFINER = "refiner"
UPSCALER = "upscaler"
METADATA = "metadata"

AGENT_LABELS = {
    PLANNER: "Planejador (LM Studio)",
    GENERATOR: "Gerador de imagens (ComfyUI)",
    REVIEWER: "Revisor visual (LM Studio)",
    REFINER: "Refinador de prompt (LM Studio)",
    UPSCALER: "Ampliador (ComfyUI)",
    METADATA: "Metadados (LM Studio)",
}
MODE_LABELS = {
    "full": "Criação da coleção",
    "new_image": "Nova imagem",
    "regenerate": "Recriação de imagem",
    "review": "Revisão de imagens",
    "upscale_all": "Ampliação em lote",
    "metadata_all": "Metadados em lote",
}
MODE_AGENTS = {
    "full": [PLANNER, GENERATOR, REVIEWER, REFINER],
    "new_image": [PLANNER, GENERATOR, REVIEWER, REFINER],
    "regenerate": [REFINER, GENERATOR, REVIEWER],
    "review": [REVIEWER],
    "upscale_all": [UPSCALER],
    "metadata_all": [METADATA],
}
PLANNER_BATCH_SIZE = 5
REVIEWABLE_ROLES = {"candidate", "correction"}


class AgentPipeline:
    def __init__(self, orchestrator: Orchestrator) -> None:
        self.orchestrator = orchestrator
        self.database = orchestrator.database
        self.config = orchestrator.config
        self.lmstudio = orchestrator.lmstudio
        self.comfy = orchestrator.comfy
        self.pipeline = Pipeline(PROJECT, AGENT_LABELS, store=orchestrator.runs)
        for mode, runner in (
            ("full", self._run_full),
            ("new_image", self._run_new_image),
            ("regenerate", self._run_regenerate),
            ("review", self._run_review),
            ("upscale_all", self._run_upscale_all),
            ("metadata_all", self._run_metadata_all),
        ):
            self.pipeline.flow(mode, MODE_AGENTS[mode], MODE_LABELS[mode])(runner)

    # --- Execuções -----------------------------------------------------------

    def create(
        self,
        collection_id: str,
        mode: str,
        planner_model: str,
        vision_model: str,
        *,
        concept_id: str | None = None,
        feedback: str = "",
        hint: str = "",
        discard_asset_id: str | None = None,
    ) -> str:
        if mode not in MODE_AGENTS:
            raise ValueError("Modo de execução desconhecido.")
        collection = self.database.get_collection(collection_id)
        if collection is None:
            raise KeyError("Coleção não encontrada.")
        self.orchestrator.ensure_normal(collection_id)
        if mode == "regenerate" and not concept_id:
            raise ValueError("Informe o conceito a recriar.")
        params = {
            "planner_model": planner_model,
            "vision_model": vision_model,
            "concept_id": concept_id,
            "feedback": feedback.strip(),
            "hint": hint.strip(),
            "discard_asset_id": discard_asset_id,
        }
        return self.pipeline.create(
            mode, subject=collection_id, subject_label=collection["name"], params=params,
        )

    def start(
        self, collection_id: str, mode: str, planner_model: str, vision_model: str,
        **options: Any,
    ) -> str:
        """Cria a execução e a coloca na fila em segundo plano."""
        run_id = self.create(collection_id, mode, planner_model, vision_model, **options)
        self.pipeline.executor.submit(run_id, self.pipeline.execute)
        return run_id

    def execute(self, run_id: str) -> None:
        run = self.pipeline.status(run_id, 0)
        if run is not None:
            try:
                self.orchestrator.ensure_normal(run["subject"])
            except ValueError:
                self.pipeline.cancel(run_id)  # execução antiga de uma coleção hoje externa: não roda
                raise
        self.pipeline.execute(run_id)

    def cancel(self, run_id: str) -> None:
        self.pipeline.cancel(run_id)

    def runs(self, collection_id: str, limit: int = 10, active_only: bool = False) -> list[dict]:
        return self.pipeline.runs(collection_id, limit, active_only)

    def status(self, run_id: str, events: int = 40) -> dict | None:
        return self.pipeline.status(run_id, events)

    def recover(self) -> int:
        recovered = self.pipeline.recover()
        for collection in self.database.list_collections():
            if collection.get("kind") == "external":
                for run in self.pipeline.runs(collection["id"], limit=20, active_only=True):
                    self.pipeline.cancel(run["id"])  # nada deve ficar ativo na coleção externa
        return recovered

    # --- Modos ---------------------------------------------------------------

    def _run_full(self, ctx: StepContext) -> None:
        brief = self._brief(ctx)
        concepts = self.database.list_concepts(ctx.subject)
        if concepts:
            ctx.agent(
                PLANNER, state="done", done=len(concepts), total=len(concepts),
                message="Conceitos já existentes foram reaproveitados.",
            )
            concept_ids = [concept["id"] for concept in concepts]
        else:
            concept_ids = self._plan(ctx, brief, brief.concept_count)
        self._produce(ctx, concept_ids, only_missing=True)

    def _run_new_image(self, ctx: StepContext) -> None:
        concept_ids = self._plan(ctx, self._brief(ctx), 1, hint=ctx.params.get("hint", ""))
        self._produce(ctx, concept_ids, only_missing=True)

    def _run_regenerate(self, ctx: StepContext) -> None:
        concept_id = ctx.params.get("concept_id") or ""
        concept = self.database.get_concept(concept_id)
        if concept is None or concept["collection_id"] != ctx.subject:
            raise PipelineAbort("Conceito não encontrado nesta coleção.")
        discard = ctx.params.get("discard_asset_id")
        feedback = ctx.params.get("feedback", "")
        if discard:
            try:
                self.database.reject_asset(
                    concept_id, discard,
                    f"Recriação solicitada pelo usuário. {feedback}".strip(),
                )
                ctx.event("Imagem anterior descartada a pedido do usuário.")
            except (ValueError, InvalidTransition) as error:
                ctx.event(f"Imagem anterior mantida: {error}", "pipeline", "warning")
        if feedback or self._latest_review(concept_id):
            self._refine(ctx, [concept_id], feedback)
        else:
            ctx.agent(
                REFINER, state="skipped",
                message="Sem feedback nem revisão; recriando com o mesmo prompt e nova seed.",
            )
        self._produce(ctx, [concept_id], only_missing=False)

    def _run_review(self, ctx: StepContext) -> None:
        """Revisa só as imagens ainda sem parecer (ex.: correções), de um conceito ou da coleção."""
        concept_id = ctx.params.get("concept_id")
        concept_ids = [concept_id] if concept_id else [
            concept["id"] for concept in self.database.list_concepts(ctx.subject)
        ]
        self._review(ctx, concept_ids)

    def _run_upscale_all(self, ctx: StepContext) -> None:
        ctx.phase("Ampliando imagens selecionadas no ComfyUI")
        collection_root = self.orchestrator.images.ensure_collection_dirs(ctx.subject)
        if load_profile(collection_root, "upscale") is None:
            raise PipelineAbort("Nenhum workflow de upscale configurado. Importe um em Configuração → Workflows ComfyUI.")
        targets = upscale_targets(self.database, ctx.subject)
        if not targets:
            ctx.agent(UPSCALER, state="done", message="Nenhuma imagem selecionada aguardando upscale.")
            return
        done, total = ctx.counter(UPSCALER, len(targets))
        ctx.agent(UPSCALER, state="running", done=done, total=total,
                  message=f"Ampliando {total} imagem(ns).")
        failures = 0
        for concept, asset in targets:
            ctx.check_cancel()
            # O estado pode ter mudado enquanto a execução esperava na fila.
            concept = self.database.get_concept(concept["id"]) or concept
            label = _short(ConceptPlan.model_validate(concept["data"]).concept)
            if concept["state"] != WorkflowState.SELECTED.value:
                ctx.event(f"{label} ignorado: não está mais aguardando upscale.", UPSCALER, "warning")
            else:
                try:
                    self.orchestrator.upscale_selected(
                        ctx.subject, concept, asset,
                        progress=ctx.progress(UPSCALER, prefix=f"{label}: "),
                    )
                    ctx.event(f"Upscale salvo para {label}.", UPSCALER)
                except ComfyError as error:
                    if error.transient:
                        raise PipelineAbort(f"ComfyUI indisponível: {error}") from error
                    failures += 1
                    ctx.event(f"Falha em {label}: {error}", UPSCALER, "error")
                except (ValueError, ImageValidationError, InvalidTransition) as error:
                    failures += 1
                    ctx.event(f"Falha em {label}: {error}", UPSCALER, "error")
            done = ctx.advance(UPSCALER)
            ctx.agent(UPSCALER, done=done, message=f"{done}/{total} imagem(ns) processada(s).")
        ctx.agent(
            UPSCALER, state="failed" if failures == len(targets) else "done",
            message=f"{done}/{total} imagem(ns) processada(s)"
            + (f"; {failures} falha(s)." if failures else "."),
        )

    def _run_metadata_all(self, ctx: StepContext) -> None:
        ctx.phase("Gerando metadados no LM Studio")
        model = ctx.params.get("vision_model") or ""
        if not model:
            raise PipelineAbort("Escolha um modelo com visão para gerar os metadados em lote.")
        targets = metadata_targets(self.database, ctx.subject)
        if not targets:
            ctx.agent(METADATA, state="done", message="Nenhuma imagem aprovada aguardando metadados.")
            return
        done, total = ctx.counter(METADATA, len(targets))
        ctx.agent(METADATA, state="running", done=done, total=total,
                  message=f"Gerando metadados de {total} imagem(ns).")
        failures = 0
        for concept, asset in targets:
            ctx.check_cancel()
            plan = ConceptPlan.model_validate(concept["data"])
            label = _short(plan.concept)
            # Pode ter sido preenchido manualmente enquanto a execução esperava na fila.
            if (self.database.get_asset(asset["id"]) or {}).get("metadata"):
                ctx.event(f"{label} ignorado: já tem metadados.", METADATA, "warning")
            else:
                try:
                    metadata, duration, attempts = self.lmstudio.suggest_metadata(
                        model, plan, "", self.config.max_agent_attempts, Path(asset["path"]),
                    )
                    result = metadata.model_dump(mode="json")
                    self.database.update_asset_metadata(asset["id"], result)
                    self.database.record_agent_run(
                        ctx.subject, concept["id"], "metadata", model,
                        INSTRUCTION_VERSION, "completed", result, "", duration, attempts,
                    )
                    ctx.event(f"Metadados gerados para {label}.", METADATA)
                except AgentError as error:
                    failures += 1
                    self.database.record_agent_run(
                        ctx.subject, concept["id"], "metadata", model,
                        INSTRUCTION_VERSION, "failed", None, str(error),
                        error.duration_ms, error.attempts,
                    )
                    ctx.event(f"Metadados de {label} falharam: {error}", METADATA, "error")
            done = ctx.advance(METADATA)
            ctx.agent(METADATA, done=done, message=f"{done}/{total} imagem(ns) processada(s).")
        ctx.agent(
            METADATA, state="failed" if failures == len(targets) else "done",
            message=f"{done}/{total} imagem(ns) processada(s)"
            + (f"; {failures} falha(s)." if failures else "."),
        )

    # --- Fases ---------------------------------------------------------------

    def _plan(self, ctx: StepContext, brief: CollectionBrief, count: int, hint: str = "") -> list[str]:
        model = ctx.params.get("planner_model") or ""
        if not model:
            raise PipelineAbort("Selecione um modelo de planejamento do LM Studio.")
        ctx.phase("Planejando conceitos e prompts")
        collection = self.database.get_collection(ctx.subject) or {}
        planning = dict(collection.get("planning") or {})
        style_guide = planning.get("style_guide", "")
        previous = [concept["data"] for concept in self.database.list_concepts(ctx.subject)]
        avoid = [plan["concept"] for plan in previous]
        seasonal = seasonal_context(self.config.seasonal_calendar)
        created: list[str] = []
        ctx.agent(
            PLANNER, state="running", done=0, total=count,
            message=f"Analisando o briefing para {count} imagem(ns).",
        )
        while len(created) < count:
            ctx.check_cancel()
            batch = min(PLANNER_BATCH_SIZE, count - len(created))
            try:
                output, duration, attempts = self.lmstudio.plan_collection(
                    model, brief, self.config.max_agent_attempts,
                    ctx.progress(PLANNER),
                    count=batch, avoid=avoid, style_guide=style_guide, hint=hint,
                    lessons=adobe_lessons(self.database.adobe_feedback(), examples=True),
                    seasonal=seasonal,
                    visual_references=[concept_reference(plan, self.config.visual_reference_prompt_chars)
                                       for plan in previous[-self.config.visual_reference_limit:]],
                )
            except AgentError as error:
                self.database.record_agent_run(
                    ctx.subject, None, "planner", model, PLANNER_INSTRUCTION_VERSION, "failed",
                    None, str(error), error.duration_ms, error.attempts,
                )
                raise
            if not planning.get("style_guide") and (output.style_guide or output.collection_summary):
                planning = {
                    "collection_summary": output.collection_summary,
                    "style_guide": output.style_guide,
                    "planner_model": model,
                    "planned_at": utc_now(),
                }
                style_guide = output.style_guide
                self.database.update_collection_planning(ctx.subject, planning)
            for plan in output.concepts:
                concept_id = self.database.create_concept(ctx.subject, plan)
                self.database.transition_concept(
                    concept_id, WorkflowState.PROMPTS_APPROVED,
                    "Prompt criado pelo agente planejador; geração automática autorizada pelo usuário",
                )
                self.database.record_agent_run(
                    ctx.subject, concept_id, "planner", model, PLANNER_INSTRUCTION_VERSION,
                    "completed", plan.model_dump(mode="json"), "", duration, attempts,
                )
                avoid.append(plan.concept)
                previous.append(plan.model_dump())
                created.append(concept_id)
                ctx.event(f"Conceito planejado: {plan.concept}", PLANNER)
            ctx.agent(
                PLANNER, done=len(created),
                message=f"{len(created)}/{count} conceito(s) planejado(s).",
            )
        ctx.agent(PLANNER, state="done", message=f"{len(created)} conceito(s) e prompt(s) prontos.")
        return created

    def _produce(self, ctx: StepContext, concept_ids: list[str], only_missing: bool) -> None:
        self._generate(ctx, concept_ids, only_missing)
        self._review(ctx, concept_ids)
        refined = False
        for round_number in range(1, self.config.auto_regenerations + 1):
            rejected = [concept_id for concept_id in concept_ids if self._needs_retry(concept_id)]
            if not rejected:
                break
            ctx.event(
                f"Rodada {round_number}: {len(rejected)} imagem(ns) reprovada(s) pelo revisor; recriando.",
            )
            self._refine(ctx, rejected)
            refined = True
            self._generate(ctx, rejected, only_missing=False)
            self._review(ctx, rejected)
        if not refined and ctx.agent_state(REFINER) == "waiting":
            ctx.agent(
                REFINER, state="skipped",
                message="Nenhuma imagem reprovada pelo revisor; nada a recriar.",
            )

    def _generate(self, ctx: StepContext, concept_ids: list[str], only_missing: bool) -> None:
        ctx.phase("Gerando imagens no ComfyUI")
        profile = load_profile(
            self.orchestrator.images.ensure_collection_dirs(ctx.subject), "generation"
        )
        if profile is None:
            raise PipelineAbort(
                "Nenhum workflow de geração configurado. Importe um em Configuração → Workflows ComfyUI "
                "(pode ser salvo como padrão) e use Retomar agentes."
            )
        if "seed" not in profile.mappings:
            raise PipelineAbort("O workflow de geração não mapeia seed; corrija-o em Configuração → Workflows ComfyUI.")
        amount = self.config.candidates_per_concept
        targets: list[dict] = []
        for concept_id in concept_ids:
            concept = self._prepare_for_generation(ctx, concept_id)
            if concept is None:
                continue
            has_candidates = any(
                asset["role"] in REVIEWABLE_ROLES for asset in self.database.list_assets(concept_id)
            )
            if only_missing and has_candidates:
                continue
            targets.append(concept)
        if not targets:
            if ctx.agent_state(GENERATOR) == "waiting":
                ctx.agent(GENERATOR, state="done", message="Todas as imagens já existiam.")
            return
        done, total = ctx.counter(GENERATOR, len(targets) * amount)
        ctx.agent(
            GENERATOR, state="running", done=done, total=total,
            message=f"Gerando {len(targets) * amount} imagem(ns).",
        )
        failures = 0
        for concept in targets:
            ctx.check_cancel()
            plan = ConceptPlan.model_validate(concept["data"])
            width, height = self.config.generation_sizes.get(plan.orientation, (1024, 1024))
            label = _short(plan.concept)
            try:
                assets = self.orchestrator.generate_candidates(
                    ctx.subject, concept, amount, int(width), int(height),
                    progress=ctx.progress(GENERATOR, prefix=f"{label}: "),
                )
                ctx.event(f"{len(assets)} imagem(ns) gerada(s) para {label}.", GENERATOR)
            except ComfyError as error:
                if error.transient:
                    raise PipelineAbort(f"ComfyUI indisponível: {error}") from error
                failures += 1
                ctx.event(f"Falha em {label}: {error}", GENERATOR, "error")
            except (ValueError, ImageValidationError, InvalidTransition) as error:
                failures += 1
                ctx.event(f"Falha em {label}: {error}", GENERATOR, "error")
            done = ctx.advance(GENERATOR, amount)
            ctx.agent(GENERATOR, done=done, message=f"{done}/{total} imagem(ns) processada(s).")
        ctx.agent(
            GENERATOR, state="failed" if failures == len(targets) else "done",
            message=f"{done}/{total} imagem(ns) processada(s)"
            + (f"; {failures} falha(s)." if failures else "."),
        )

    def _review(self, ctx: StepContext, concept_ids: list[str]) -> None:
        model = ctx.params.get("vision_model") or ""
        if not model:
            ctx.agent(
                REVIEWER, state="skipped",
                message="Nenhum modelo com visão selecionado; revise as imagens manualmente.",
            )
            return
        pending: list[tuple[ConceptPlan, dict]] = []
        for concept_id in concept_ids:
            concept = self.database.get_concept(concept_id)
            if concept is None or concept["state"] != WorkflowState.CANDIDATES_READY.value:
                continue
            plan = ConceptPlan.model_validate(concept["data"])
            for asset in self.database.list_assets(concept_id):
                if (
                    asset["role"] in REVIEWABLE_ROLES
                    and asset["status"] == "candidate"
                    and not asset["review"].get("recommendation")
                    and Path(asset["path"]).is_file()
                ):
                    pending.append((plan, asset))
        if not pending:
            if ctx.agent_state(REVIEWER) == "waiting":
                ctx.agent(REVIEWER, state="done", message="Nenhuma imagem nova para revisar.")
            return
        ctx.phase("Revisando imagens")
        done, total = ctx.counter(REVIEWER, len(pending))
        ctx.agent(
            REVIEWER, state="running", done=done, total=total,
            message=f"Revisando {len(pending)} imagem(ns).",
        )
        lessons = adobe_lessons(self.database.adobe_feedback())
        for plan, asset in pending:
            ctx.check_cancel()
            label = _short(plan.concept)
            try:
                review, duration, attempts = self.lmstudio.review_image(
                    model, Path(asset["path"]), plan, self.config.max_agent_attempts,
                    progress=ctx.progress(REVIEWER, prefix=f"{label}: "), lessons=lessons,
                )
                result = review.model_dump(mode="json")
                result.update({"reviewer_model": model, "reviewed_at": utc_now()})
                self.database.update_asset_review(asset["id"], result)
                self.database.record_agent_run(
                    ctx.subject, asset["concept_id"], "visual_reviewer", model,
                    INSTRUCTION_VERSION, "completed", result, "", duration, attempts,
                )
                ctx.event(
                    f"{label}: recomendação "
                    f"{_RECOMMENDATION.get(review.recommendation, review.recommendation)}.",
                    REVIEWER,
                )
            except AgentError as error:
                self.database.record_agent_run(
                    ctx.subject, asset["concept_id"], "visual_reviewer", model,
                    INSTRUCTION_VERSION, "failed", None, str(error),
                    error.duration_ms, error.attempts,
                )
                ctx.event(f"Revisão de {label} falhou: {error}", REVIEWER, "error")
                if "suporte visual" in str(error):
                    ctx.agent(REVIEWER, state="failed", message=str(error))
                    return
            done = ctx.advance(REVIEWER, 1)
            ctx.agent(REVIEWER, done=done, message=f"{done}/{total} imagem(ns) revisada(s).")
        ctx.agent(REVIEWER, state="done", message=f"{done}/{total} imagem(ns) revisada(s).")

    def _refine(self, ctx: StepContext, concept_ids: list[str], feedback: str = "") -> None:
        model = ctx.params.get("planner_model") or ""
        if not model:
            ctx.agent(
                REFINER, state="skipped",
                message="Sem modelo de texto; recriando com o prompt atual.",
            )
            return
        ctx.phase("Refinando prompts")
        done, total = ctx.counter(REFINER, len(concept_ids))
        ctx.agent(
            REFINER, state="running", done=done, total=total,
            message=f"Ajustando {len(concept_ids)} prompt(s).",
        )
        lessons = adobe_lessons(self.database.adobe_feedback())
        for concept_id in concept_ids:
            ctx.check_cancel()
            concept = self.database.get_concept(concept_id)
            if concept is None:
                continue
            plan = ConceptPlan.model_validate(concept["data"])
            label = _short(plan.concept)
            try:
                revision, duration, attempts = self.lmstudio.refine_prompt(
                    model, plan, self._latest_review(concept_id), feedback,
                    self.config.max_agent_attempts, ctx.progress(REFINER, prefix=f"{label}: "),
                    lessons=lessons,
                )
                revised = ConceptPlan.model_validate({
                    **plan.model_dump(mode="json"),
                    "prompt_en": revision.prompt_en,
                    "negative_prompt": revision.negative_prompt,
                })
                self.database.revise_concept_prompt(concept_id, revised)
                self.database.record_agent_run(
                    ctx.subject, concept_id, "prompt_refiner", model, INSTRUCTION_VERSION,
                    "completed",
                    {**revision.model_dump(mode="json"), "previous_prompt_en": plan.prompt_en,
                     "user_feedback": feedback},
                    "", duration, attempts,
                )
                ctx.event(f"{label}: {revision.change_summary or 'prompt ajustado.'}", REFINER)
            except (AgentError, InvalidTransition, ValueError) as error:
                if isinstance(error, AgentError):
                    self.database.record_agent_run(
                        ctx.subject, concept_id, "prompt_refiner", model, INSTRUCTION_VERSION,
                        "failed", None, str(error), error.duration_ms, error.attempts,
                    )
                ctx.event(f"{label}: prompt mantido ({error}).", REFINER, "warning")
            done = ctx.advance(REFINER, 1)
            ctx.agent(REFINER, done=done, message=f"{done}/{total} prompt(s) ajustado(s).")
        ctx.agent(REFINER, state="done", message=f"{done}/{total} prompt(s) ajustado(s).")

    # --- Regras auxiliares ---------------------------------------------------

    def _prepare_for_generation(self, ctx: StepContext, concept_id: str) -> dict | None:
        concept = self.database.get_concept(concept_id)
        if concept is None:
            return None
        state = WorkflowState(concept["state"])
        has_candidates = any(
            asset["role"] in REVIEWABLE_ROLES for asset in self.database.list_assets(concept_id)
        )
        if state == WorkflowState.GENERATING:
            self.orchestrator.resume_jobs(
                ctx.subject, ctx.progress(GENERATOR), concept_id
            )
        elif state in {WorkflowState.FAILED, WorkflowState.CANCELLED}:
            target = WorkflowState.CANDIDATES_READY if has_candidates else WorkflowState.PROMPTS_APPROVED
            self.database.transition_concept(concept_id, target, "Reaberto pelo pipeline de agentes")
        elif state == WorkflowState.CONCEPTS_READY:
            self.database.transition_concept(
                concept_id, WorkflowState.PROMPTS_APPROVED,
                "Prompt aprovado pelo pipeline autorizado pelo usuário",
            )
        concept = self.database.get_concept(concept_id)
        if concept and concept["state"] in {
            WorkflowState.PROMPTS_APPROVED.value, WorkflowState.CANDIDATES_READY.value,
        }:
            return concept
        if concept:
            ctx.event(
                f"{_short(concept['data']['concept'])} ignorado no estado {concept['state']}.",
                GENERATOR, "warning",
            )
        return None

    def _needs_retry(self, concept_id: str) -> bool:
        concept = self.database.get_concept(concept_id)
        if concept is None or concept["state"] != WorkflowState.CANDIDATES_READY.value:
            return False
        pending = [
            asset for asset in self.database.list_assets(concept_id)
            if asset["role"] in REVIEWABLE_ROLES and asset["status"] == "candidate"
        ]
        if not pending:
            return False
        return all(asset["review"].get("recommendation") == "reject" for asset in pending)

    def _latest_review(self, concept_id: str) -> dict | None:
        reviewed = [
            asset for asset in self.database.list_assets(concept_id)
            if asset["role"] in REVIEWABLE_ROLES and asset["review"].get("recommendation")
        ]
        return reviewed[-1]["review"] if reviewed else None

    def _brief(self, ctx: StepContext) -> CollectionBrief:
        collection = self.database.get_collection(ctx.subject)
        if collection is None:
            raise PipelineAbort("Coleção não encontrada.")
        return CollectionBrief.model_validate(collection["brief"])


_RECOMMENDATION = {"select": "aprovar", "review": "revisar", "reject": "recriar"}


def upscale_targets(database, collection_id: str) -> list[tuple[dict, dict]]:
    """Conceitos com imagem selecionada que ainda aguardam upscale, com o asset escolhido."""
    targets = []
    for concept in database.list_concepts(collection_id):
        if concept["state"] != WorkflowState.SELECTED.value:
            continue
        asset = database.get_asset(concept.get("selected_asset_id") or "")
        if asset:
            targets.append((concept, asset))
    return targets


def final_upscale(database, concept_id: str) -> dict | None:
    """Upscale mais recente do conceito, o mesmo exibido na revisão final."""
    upscaled = [asset for asset in database.list_assets(concept_id) if asset["role"] == "upscaled"]
    return upscaled[-1] if upscaled else None


def metadata_targets(database, collection_id: str) -> list[tuple[dict, dict]]:
    """Conceitos com upscale aprovado na revisão final e ainda sem metadados."""
    targets = []
    for concept in database.list_concepts(collection_id):
        if concept["state"] != WorkflowState.FINAL_REVIEW.value:
            continue
        asset = final_upscale(database, concept["id"])
        if asset and (asset.get("review") or {}).get("human_approved") and not asset.get("metadata"):
            targets.append((concept, asset))
    return targets


def metadata_approval_targets(database, collection_id: str) -> list[tuple[dict, dict]]:
    """Conceitos com metadados prontos aguardando a aprovação local (FINAL_REVIEW → METADATA_READY)."""
    targets = []
    for concept in database.list_concepts(collection_id):
        if concept["state"] != WorkflowState.FINAL_REVIEW.value:
            continue
        asset = final_upscale(database, concept["id"])
        if asset and (asset.get("review") or {}).get("human_approved") and asset.get("metadata"):
            targets.append((concept, asset))
    return targets


def _short(text: str, limit: int = 48) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
