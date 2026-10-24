import io
import json

import httpx
import pytest
from PIL import Image

from flowapp.agents import LMStudioClient
from flowapp.config import AppConfig
from flowapp.orchestrator import Orchestrator
from flowapp.pipeline import AgentPipeline
from flowapp.profiles import save_default_profile
from flowapp.schemas import (
    CollectionBrief,
    ComfyWorkflowProfile,
    ConceptPlan,
    PlannerOutput,
    PromptRevision,
    VisualReview,
    WorkflowInputRef,
    WorkflowState,
)


def _plan(index: int, orientation: str = "horizontal") -> ConceptPlan:
    return ConceptPlan(
        concept=f"Conceito número {index}",
        commercial_application="Campanha digital",
        main_subject=f"Assunto {index}",
        composition_and_copy_space="Assunto à direita, espaço livre à esquerda",
        orientation=orientation,
        prompt_en=f"Detailed photo of subject {index}, soft daylight",
        differentiation_rationale="Ângulo distinto",
    )


class FakeLMStudio:
    def __init__(self, recommendations=None):
        self.plan_calls = []
        self.refine_calls = []
        self.reviews = 0
        self.recommendations = list(recommendations or [])
        self.counter = 0

    def plan_collection(self, model, brief, attempts, progress, *, count, avoid, style_guide, hint, lessons="", seasonal="", visual_references=None):
        self.plan_calls.append({
            "count": count, "avoid": list(avoid), "style": style_guide, "hint": hint, "lessons": lessons, "seasonal": seasonal, "visual_references": list(visual_references or []),
        })
        progress("planejando")
        concepts = []
        for _ in range(count):
            self.counter += 1
            concepts.append(_plan(self.counter, brief.orientation if brief.orientation != "mixed" else "square"))
        return PlannerOutput(
            concepts=concepts, collection_summary="Resumo", style_guide="Soft natural light",
        ), 10, 1

    def review_image(self, model, path, plan, attempts, progress=None, *, lessons=""):
        self.reviews += 1
        self.review_lessons = lessons
        recommendation = self.recommendations.pop(0) if self.recommendations else "select"
        return VisualReview(
            observations=["ok"], composition_quality="boa", concept_coherence="alta",
            recommendation=recommendation,
        ), 5, 1

    def refine_prompt(self, model, plan, review, feedback, attempts, progress=None, *, lessons=""):
        self.refine_calls.append({
            "concept": plan.concept, "review": review, "feedback": feedback, "lessons": lessons,
        })
        return PromptRevision(
            prompt_en=plan.prompt_en + ", correct anatomy", change_summary="Anatomia corrigida",
        ), 5, 1

    def unload_all_models(self):
        return []


class FakeComfy:
    def free_memory(self):
        return None


@pytest.fixture
def pipeline_env(tmp_path):
    config = AppConfig(auto_regenerations=1, candidates_per_concept=1)
    orchestrator = Orchestrator(tmp_path, config)
    orchestrator.lmstudio.close()
    orchestrator.comfy.close()
    fake_lm = FakeLMStudio()
    orchestrator.lmstudio = fake_lm
    orchestrator.comfy = FakeComfy()
    generated = []
    colors = iter(range(1, 10_000))

    def fake_generate(collection_id, concept, amount, width, height, progress=None):
        database = orchestrator.database
        database.transition_concept(concept["id"], WorkflowState.GENERATING, "teste")
        results = []
        for _ in range(amount):
            value = next(colors)
            output = io.BytesIO()
            Image.new("RGB", (64, 48), color=(value % 256, value // 256, 7)).save(output, format="PNG")
            asset_id, asset = orchestrator.images.register_candidate(
                collection_id, concept["id"], output.getvalue(), "fake.png"
            )
            results.append(asset)
        generated.append({"concept": concept["data"]["concept"], "size": (width, height),
                          "prompt": concept["data"]["prompt_en"]})
        database.transition_concept(concept["id"], WorkflowState.CANDIDATES_READY, "teste")
        return results

    orchestrator.generate_candidates = fake_generate
    profile = ComfyWorkflowProfile(
        kind="generation",
        workflow={
            "3": {"class_type": "KSampler", "inputs": {"seed": 1}},
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
            "9": {"class_type": "SaveImage", "inputs": {}},
        },
        mappings={
            "prompt": WorkflowInputRef(node_id="6", input_name="text"),
            "seed": WorkflowInputRef(node_id="3", input_name="seed"),
        },
        output_node_ids=["9"],
    )
    yield tmp_path, orchestrator, AgentPipeline(orchestrator), fake_lm, generated, profile
    orchestrator.database = None


def _collection(orchestrator, count=2, orientation="vertical"):
    brief = CollectionBrief(
        theme="Jardins urbanos", objective="Imagens para campanhas de sustentabilidade",
        audience="Marcas de jardinagem", commercial_application="Banners",
        orientation=orientation, concept_count=count,
    )
    return orchestrator.database.create_collection("Jardins", brief)


def test_full_run_plans_in_batches_generates_and_reviews(pipeline_env):
    tmp_path, orchestrator, pipeline, fake_lm, generated, profile = pipeline_env
    save_default_profile(tmp_path / "data", profile)
    collection_id = _collection(orchestrator, count=7, orientation="vertical")

    run_id = pipeline.create(collection_id, "full", "text-model", "vision-model")
    pipeline.execute(run_id)

    run = pipeline.status(run_id)
    assert run["status"] == "completed", run["error"]
    assert [call["count"] for call in fake_lm.plan_calls] == [5, 2]
    assert fake_lm.plan_calls[1]["style"] == "Soft natural light"
    assert len(fake_lm.plan_calls[1]["avoid"]) == 5
    concepts = orchestrator.database.list_concepts(collection_id)
    assert len(concepts) == 7
    assert all(concept["state"] == WorkflowState.CANDIDATES_READY.value for concept in concepts)
    assert all(item["size"] == (768, 1344) for item in generated)
    assert fake_lm.reviews == 7
    states = {agent["agent"]: agent["state"] for agent in run["agents"]}
    assert states == {"planner": "done", "generator": "done", "reviewer": "done", "refiner": "skipped"}
    assert orchestrator.database.get_collection(collection_id)["planning"]["style_guide"]


def test_rejected_image_is_refined_and_recreated_once(pipeline_env):
    tmp_path, orchestrator, pipeline, fake_lm, generated, profile = pipeline_env
    save_default_profile(tmp_path / "data", profile)
    fake_lm.recommendations = ["reject", "select"]
    collection_id = _collection(orchestrator, count=1)

    run_id = pipeline.create(collection_id, "full", "text-model", "vision-model")
    pipeline.execute(run_id)

    assert pipeline.status(run_id)["status"] == "completed"
    assert len(generated) == 2
    assert generated[1]["prompt"].endswith("correct anatomy")
    assert fake_lm.refine_calls[0]["review"]["recommendation"] == "reject"
    concept = orchestrator.database.list_concepts(collection_id)[0]
    recommendations = [asset["review"]["recommendation"] for asset in orchestrator.database.list_assets(concept["id"])]
    assert recommendations == ["reject", "select"]


def test_missing_workflow_fails_and_resume_does_not_replan(pipeline_env):
    tmp_path, orchestrator, pipeline, fake_lm, generated, profile = pipeline_env
    collection_id = _collection(orchestrator, count=2)

    first = pipeline.create(collection_id, "full", "text-model", "vision-model")
    pipeline.execute(first)
    run = pipeline.status(first)
    assert run["status"] == "failed"
    assert "workflow de geração" in run["error"]
    assert all(
        concept["state"] == WorkflowState.PROMPTS_APPROVED.value
        for concept in orchestrator.database.list_concepts(collection_id)
    )

    save_default_profile(tmp_path / "data", profile)
    second = pipeline.create(collection_id, "full", "text-model", "vision-model")
    pipeline.execute(second)
    assert pipeline.status(second)["status"] == "completed"
    assert len(fake_lm.plan_calls) == 1
    assert len(generated) == 2


def test_user_regeneration_discards_image_and_uses_feedback(pipeline_env):
    tmp_path, orchestrator, pipeline, fake_lm, generated, profile = pipeline_env
    save_default_profile(tmp_path / "data", profile)
    collection_id = _collection(orchestrator, count=1)
    pipeline.execute(pipeline.create(collection_id, "full", "text-model", ""))
    concept = orchestrator.database.list_concepts(collection_id)[0]
    old_asset = orchestrator.database.list_assets(concept["id"])[0]

    run_id = pipeline.create(
        collection_id, "regenerate", "text-model", "",
        concept_id=concept["id"], feedback="Remova o texto da placa", discard_asset_id=old_asset["id"],
    )
    pipeline.execute(run_id)

    assert pipeline.status(run_id)["status"] == "completed"
    assets = orchestrator.database.list_assets(concept["id"])
    assert assets[0]["status"] == "rejected"
    assert assets[1]["status"] == "candidate"
    assert fake_lm.refine_calls[-1]["feedback"] == "Remova o texto da placa"


def test_new_image_avoids_existing_concepts(pipeline_env):
    tmp_path, orchestrator, pipeline, fake_lm, generated, profile = pipeline_env
    save_default_profile(tmp_path / "data", profile)
    collection_id = _collection(orchestrator, count=2)
    pipeline.execute(pipeline.create(collection_id, "full", "text-model", "vision-model"))

    pipeline.execute(pipeline.create(collection_id, "new_image", "text-model", "vision-model", hint="vista noturna"))

    assert fake_lm.plan_calls[-1]["count"] == 1
    assert fake_lm.plan_calls[-1]["hint"] == "vista noturna"
    assert len(fake_lm.plan_calls[-1]["avoid"]) == 2
    assert len(orchestrator.database.list_concepts(collection_id)) == 3
    assert len(generated) == 3


def _adobe_result(orchestrator, collection_id, index, target, reason):
    database = orchestrator.database
    concept_id = database.create_concept(collection_id, _plan(index, "vertical"))
    with database.connect() as connection:
        connection.execute(
            "UPDATE concepts SET state = ? WHERE id = ?", (WorkflowState.SUBMITTED.value, concept_id),
        )
    database.transition_concept(concept_id, target, reason)


def test_adobe_moderation_feeds_planner_reviewer_and_refiner_across_collections(pipeline_env):
    tmp_path, orchestrator, pipeline, fake_lm, _, profile = pipeline_env
    save_default_profile(tmp_path / "data", profile)
    old_collection = _collection(orchestrator, count=1)
    for index in (90, 91):
        _adobe_result(
            orchestrator, old_collection, index, WorkflowState.ADOBE_REJECTED,
            "Rejeição confirmada no portal: Similar content",
        )
    _adobe_result(
        orchestrator, old_collection, 92, WorkflowState.ADOBE_APPROVED, "Aprovação confirmada no portal",
    )

    feedback = orchestrator.database.adobe_feedback()
    assert feedback["rejected_count"] == 2 and feedback["approved_count"] == 1
    assert feedback["rejected"][0]["reason"] == "Similar content"

    fake_lm.recommendations = ["reject", "select"]
    new_collection = _collection(orchestrator, count=1)
    pipeline.execute(pipeline.create(new_collection, "full", "text-model", "vision-model"))

    planner_lessons = fake_lm.plan_calls[0]["lessons"]
    assert "Similar content (2x)" in planner_lessons
    assert "Conceito número 92" in planner_lessons
    assert "Similar content" in fake_lm.review_lessons
    assert "Conceito número 92" not in fake_lm.review_lessons
    assert "Similar content" in fake_lm.refine_calls[0]["lessons"]


def test_adobe_lessons_are_empty_without_moderation_results():
    from flowapp.agents import adobe_lessons

    assert adobe_lessons({"rejected": [], "approved": []}) == ""
    assert adobe_lessons(None, examples=True) == ""


def test_cancel_and_stale_runs_are_reported(pipeline_env):
    _, orchestrator, pipeline, _, _, _ = pipeline_env
    collection_id = _collection(orchestrator)
    cancelled = pipeline.create(collection_id, "full", "text-model", "")
    pipeline.cancel(cancelled)
    pipeline.execute(cancelled)
    assert pipeline.status(cancelled)["status"] == "cancelled"

    stale = pipeline.create(collection_id, "full", "text-model", "")
    # Criada mas sem worker neste processo: o pipeline a considera órfã.
    assert pipeline.recover() == 1
    run = pipeline.status(stale)
    assert run["status"] == "interrupted"
    assert {agent["state"] for agent in run["agents"]} == {"interrupted"}
    with pytest.raises(ValueError):
        orchestrator.runs.update_run(stale, status="bogus")


def test_human_selection_keeps_agent_review(pipeline_env):
    tmp_path, orchestrator, pipeline, _, _, profile = pipeline_env
    save_default_profile(tmp_path / "data", profile)
    collection_id = _collection(orchestrator, count=1)
    pipeline.execute(pipeline.create(collection_id, "full", "text-model", "vision-model"))
    concept = orchestrator.database.list_concepts(collection_id)[0]
    asset = orchestrator.database.list_assets(concept["id"])[0]

    orchestrator.database.select_asset(concept["id"], asset["id"], "Melhor da coleção")

    review = orchestrator.database.get_asset(asset["id"])["review"]
    assert review["recommendation"] == "select"
    assert review["human_decision"] == "selected"


def test_delete_collection_removes_pipeline_records(pipeline_env):
    _, orchestrator, pipeline, _, _, _ = pipeline_env
    collection_id = _collection(orchestrator)
    active = pipeline.create(collection_id, "full", "text-model", "")
    with pytest.raises(ValueError, match="agentes em execução"):
        orchestrator.delete_collection(collection_id)
    pipeline.cancel(active)
    pipeline.execute(active)
    orchestrator.delete_collection(collection_id)
    assert pipeline.runs(collection_id) == []


def _mock_lmstudio(handler):
    """Cliente de domínio do flow com transporte HTTP simulado."""
    client = LMStudioClient("http://localhost:1234")
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def test_planner_requests_exact_batch_and_enforces_brief_orientation():
    sent = []

    def respond(request):
        if request.url.path == "/api/v1/models":
            return httpx.Response(200, json={"models": [{"key": "m", "loaded_instances": [{"id": "m"}]}]})
        body = json.loads(request.content)
        sent.append(body["messages"][1]["content"])
        concepts = [_plan(index, "square").model_dump() for index in (1, 2)]
        content = json.dumps({"collection_summary": "s", "style_guide": "g", "concepts": concepts})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    client = _mock_lmstudio(respond)
    brief = CollectionBrief(theme="Tema", objective="Objetivo", orientation="vertical", concept_count=9)
    output, _, _ = client.plan_collection(
        "m", brief, count=2, avoid=["Conceito antigo"], style_guide="Luz suave", hint="noite", seasonal="CONTEXTO SAZONAL DE TESTE",
    )
    client.close()

    assert {concept.orientation for concept in output.concepts} == {"vertical"}
    assert "exatamente 2 conceito" in sent[0]
    assert "Conceito antigo" in sent[0] and "Luz suave" in sent[0] and "noite" in sent[0]
    assert '"concept_count"' not in sent[0]
    assert 'CONTEXTO SAZONAL DE TESTE' in sent[0]


def test_upscale_all_processes_only_selected_concepts_and_continues_after_failure(pipeline_env):
    from flowapp.images import ImageValidationError

    tmp_path, orchestrator, pipeline, _, _, profile = pipeline_env
    save_default_profile(tmp_path / "data", profile)
    save_default_profile(tmp_path / "data", ComfyWorkflowProfile(
        kind="upscale",
        workflow={
            "1": {"class_type": "LoadImage", "inputs": {"image": ""}},
            "9": {"class_type": "SaveImage", "inputs": {}},
        },
        mappings={"image": WorkflowInputRef(node_id="1", input_name="image")},
        output_node_ids=["9"],
    ))
    collection_id = _collection(orchestrator, count=3)
    pipeline.execute(pipeline.create(collection_id, "full", "text-model", "vision-model"))
    database = orchestrator.database
    concepts = database.list_concepts(collection_id)
    for concept in concepts[:2]:
        asset = database.list_assets(concept["id"])[0]
        database.select_asset(concept["id"], asset["id"], "Escolhida")
    calls = []

    def fake_upscale(collection, concept, asset, progress=None):
        calls.append(concept["id"])
        if len(calls) == 1:
            raise ImageValidationError("saída inválida")
        database.transition_concept(concept["id"], WorkflowState.UPSCALING, "teste")
        database.transition_concept(concept["id"], WorkflowState.FINAL_REVIEW, "teste")

    orchestrator.upscale_selected = fake_upscale
    run_id = pipeline.create(collection_id, "upscale_all", "", "")
    pipeline.execute(run_id)

    run = pipeline.status(run_id)
    assert run["status"] == "completed", run["error"]
    assert sorted(calls) == sorted(concept["id"] for concept in concepts[:2])
    upscaler = next(agent for agent in run["agents"] if agent["agent"] == "upscaler")
    assert upscaler["state"] == "done" and upscaler["done"] == 2
    assert "1 falha" in upscaler["message"]


def test_metadata_all_uses_vision_on_approved_upscales_only(pipeline_env):
    from flowapp.schemas import MetadataSuggestion

    tmp_path, orchestrator, pipeline, fake_lm, _, profile = pipeline_env
    save_default_profile(tmp_path / "data", profile)
    collection_id = _collection(orchestrator, count=3)
    pipeline.execute(pipeline.create(collection_id, "full", "text-model", "vision-model"))
    database = orchestrator.database
    concepts = database.list_concepts(collection_id)
    upscales = {}
    for index, concept in enumerate(concepts):
        asset = database.list_assets(concept["id"])[0]
        database.select_asset(concept["id"], asset["id"], "Escolhida")
        database.transition_concept(concept["id"], WorkflowState.UPSCALING, "teste")
        output = io.BytesIO()
        Image.new("RGB", (128, 96), color=(200, index, 50)).save(output, format="PNG")
        upscaled_id, _ = orchestrator.images._register(
            collection_id, concept["id"], "upscaled", "upscaled", output.getvalue(), "up.png",
        )
        database.transition_concept(concept["id"], WorkflowState.FINAL_REVIEW, "teste")
        upscales[concept["id"]] = upscaled_id
    approved = concepts[:2]
    for concept in approved:
        database.update_asset_review(upscales[concept["id"]], {"human_approved": True})
    calls = []

    def fake_metadata(model, plan, description, attempts, image_path=None):
        calls.append((model, description, image_path))
        if len(calls) == 1:
            from flowapp.agents import AgentError
            raise AgentError("JSON inválido")
        return MetadataSuggestion(title_en="Garden scene", keywords=["garden"], analysis_source="image"), 3, 1

    fake_lm.suggest_metadata = fake_metadata
    run_id = pipeline.create(collection_id, "metadata_all", "text-model", "vision-model")
    pipeline.execute(run_id)

    run = pipeline.status(run_id)
    assert run["status"] == "completed", run["error"]
    assert len(calls) == 2 and all(model == "vision-model" and image for model, _, image in calls)
    with_metadata = [c["id"] for c in approved if database.get_asset(upscales[c["id"]])["metadata"]]
    assert len(with_metadata) == 1
    assert not database.get_asset(upscales[concepts[2]["id"]])["metadata"]
    agent = next(item for item in run["agents"] if item["agent"] == "metadata")
    assert agent["state"] == "done" and "1 falha" in agent["message"]



@pytest.mark.parametrize('enabled', [True, False])
def test_pipeline_supplies_seasonal_context_to_every_planner_batch(pipeline_env, monkeypatch, enabled):
    from datetime import date
    from flowapp.seasonal import SeasonalCalendar, SeasonalEvent
    tmp_path, orchestrator, pipeline, fake_lm, _, profile = pipeline_env
    monkeypatch.setattr('flowapp.seasonal.today_local', lambda: date(2026, 10, 24))
    orchestrator.config.seasonal_calendar = SeasonalCalendar(enabled=enabled, events=[
        SeasonalEvent(name='Campanha futura', rule='once', on_date=date(2027, 2, 9), lead_days=90),
        SeasonalEvent(name='Prazo perdido', rule='once', on_date=date(2026, 12, 25), lead_days=90),
    ])
    save_default_profile(tmp_path / 'data', profile)
    collection = _collection(orchestrator, count=6)
    run_id = pipeline.create(collection, 'full', 'text-model', '')
    pipeline.execute(run_id)
    assert pipeline.status(run_id)['status'] == 'completed'
    assert len(fake_lm.plan_calls) == 2
    for call in fake_lm.plan_calls:
        assert 'Prazo perdido' not in call['seasonal']
        if enabled:
            assert 'Campanha futura' in call['seasonal'] and '2026-11-11' in call['seasonal']
        else:
            assert call['seasonal'] == ''


def test_visual_references_cross_batches_and_new_images_with_configured_limits(pipeline_env):
    from flowapp.agents import PLANNER_INSTRUCTION_VERSION
    tmp_path, orchestrator, pipeline, fake_lm, _, profile = pipeline_env
    orchestrator.config.visual_reference_limit = 2
    orchestrator.config.visual_reference_prompt_chars = 50
    save_default_profile(tmp_path / 'data', profile)
    collection = _collection(orchestrator, count=6)
    run_id = pipeline.create(collection, 'full', 'text-model', '')
    pipeline.execute(run_id)
    assert pipeline.status(run_id)['status'] == 'completed'
    assert not fake_lm.plan_calls[0]['visual_references']
    references = [json.loads(value) for value in fake_lm.plan_calls[1]['visual_references']]
    assert [value['concept'] for value in references] == ['Conceito número 4', 'Conceito número 5']
    assert all(value['composition_and_copy_space'] == 'Assunto à direita, espaço livre à esquerda' for value in references)
    assert all(len(value['prompt_en']) <= 50 for value in references)
    assert len(fake_lm.plan_calls[1]['avoid']) == 5
    new_run = pipeline.create(collection, 'new_image', 'text-model', '')
    pipeline.execute(new_run)
    assert pipeline.status(new_run)['status'] == 'completed'
    assert json.loads(fake_lm.plan_calls[-1]['visual_references'][-1])['concept'] == 'Conceito número 6'
    with orchestrator.database.connect() as connection:
        versions = connection.execute("SELECT DISTINCT instruction_version FROM agent_runs WHERE agent_name = 'planner'").fetchall()
    assert [row['instruction_version'] for row in versions] == [PLANNER_INSTRUCTION_VERSION]


def test_planner_receives_commercial_rules_and_visual_context_in_request():
    captured = []

    def respond(request):
        if request.url.path == '/api/v1/models':
            return httpx.Response(200, json={'models': [{'key': 'm', 'loaded_instances': [{'id': 'm'}]}]})
        captured.append(json.loads(request.content)['messages'])
        output = PlannerOutput(concepts=[_plan(1)], collection_summary='Resumo', style_guide='Soft light')
        return httpx.Response(200, json={'choices': [{'message': {'content': output.model_dump_json()}}]})

    client = _mock_lmstudio(respond)
    try:
        client.plan_collection('m', CollectionBrief(theme='Tema', objective='Objetivo'),
                               visual_references=['{"composition":"Diagonal, blue palette, outdoor garden"}'])
    finally:
        client.close()
    system, user = captured[0][0]['content'], captured[0][1]['content']
    assert 'área limpa e de baixo contraste' in system
    assert 'composição (arranjo' in system and 'cor (paleta' in system and 'cenário (ambiente' in system
    assert 'Respeite as restrições explícitas do briefing' in system
    assert 'Diagonal, blue palette, outdoor garden' in user
    assert 'aplicação concreta' in system and 'sem dados fornecidos' in system
