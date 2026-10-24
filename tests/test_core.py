import io
import json

import httpx
import pytest
from PIL import Image, ImageCms
from pydantic import ValidationError

from flowapp.agents import AgentError, LMStudioClient
from flowapp.comfy import ComfyUIClient
from flowapp.database import Database, DuplicateJobError, InvalidTransition
from flowapp.images import ImageStore
from flowapp.schemas import (
    CollectionBrief,
    ComfyWorkflowProfile,
    ConceptPlan,
    CorrectionInstruction,
    ExportRules,
    MetadataSuggestion,
    PerformanceEntry,
    PromptRevision,
    WorkflowInputRef,
    WorkflowState,
)


@pytest.fixture
def setup_project(tmp_path):
    database = Database(tmp_path / "flow.sqlite3")
    brief = CollectionBrief(theme="Jardim urbano", objective="Imagens comerciais para sustentabilidade")
    collection_id = database.create_collection("Jardim urbano", brief)
    plan = ConceptPlan(
        concept="Estufa compacta em varanda urbana",
        commercial_application="Campanha de sustentabilidade residencial",
        main_subject="Estufa com plantas comestíveis",
        composition_and_copy_space="Estufa no lado direito, espaço negativo limpo à esquerda",
        orientation="horizontal",
        prompt_en="Compact edible greenhouse on a modern apartment balcony",
        differentiation_rationale="Enquadramento residencial em vez de horta comunitária",
    )
    concept_id = database.create_concept(collection_id, plan)
    return tmp_path, database, collection_id, concept_id, plan


def test_state_transitions_are_validated_and_persisted(setup_project):
    _, database, _, concept_id, _ = setup_project
    with pytest.raises(InvalidTransition):
        database.transition_concept(concept_id, WorkflowState.GENERATING, "pular aprovação")

    database.transition_concept(concept_id, WorkflowState.PROMPTS_APPROVED, "Prompt revisado")
    database.transition_concept(concept_id, WorkflowState.GENERATING, "Geração autorizada")
    database.transition_concept(concept_id, WorkflowState.CANDIDATES_READY, "Candidatos salvos")

    concept = database.get_concept(concept_id)
    assert concept["state"] == WorkflowState.CANDIDATES_READY.value
    assert [event["to_state"] for event in database.transition_history(concept_id)] == [
        "concepts_ready", "prompts_approved", "generating", "candidates_ready"
    ]


def test_job_idempotency_prevents_duplicate_queue_entries(setup_project):
    _, database, collection_id, concept_id, _ = setup_project
    database.create_job(collection_id, concept_id, "generation", "batch-key", {"seed": 1})

    with pytest.raises(DuplicateJobError):
        database.create_job(collection_id, concept_id, "generation", "batch-key", {"seed": 1})


def test_job_batch_persists_state_before_work_and_rolls_back_duplicates(setup_project):
    _, database, collection_id, concept_id, _ = setup_project
    database.transition_concept(concept_id, WorkflowState.PROMPTS_APPROVED, "Aprovação explícita")
    jobs = database.create_job_batch(
        collection_id,
        concept_id,
        "generation",
        [("batch-a", {"seed": 10}), ("batch-b", {"seed": 20})],
    )
    concept = database.get_concept(concept_id)
    assert concept["state"] == WorkflowState.GENERATING.value
    assert len(database.list_recoverable_jobs(collection_id)) == 2

    second_project = Database(database.path)
    with pytest.raises(InvalidTransition):
        second_project.create_job_batch(
            collection_id, concept_id, "generation", [("batch-c", {"seed": 30})]
        )
    assert second_project.get_concept(concept_id)["state"] == WorkflowState.GENERATING.value
    assert all(job_id for job_id in jobs)


def test_duplicate_batch_rolls_back_pending_state(setup_project):
    _, database, collection_id, concept_id, _ = setup_project
    database.transition_concept(concept_id, WorkflowState.PROMPTS_APPROVED, "Aprovação explícita")
    database.create_job(collection_id, concept_id, "generation", "existing-key", {"seed": 1})

    with pytest.raises(DuplicateJobError):
        database.create_job_batch(
            collection_id, concept_id, "generation", [("existing-key", {"seed": 2})]
        )

    assert database.get_concept(concept_id)["state"] == WorkflowState.PROMPTS_APPROVED.value
    assert len(database.list_recoverable_jobs(collection_id)) == 1


def test_inpainting_profile_requires_image_prompt_and_api_output():
    workflow = {
        "1": {"class_type": "LoadImage", "inputs": {"image": "input.png"}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "fix geometry"}},
        "3": {"class_type": "SaveImage", "inputs": {"images": ["4", 0]}},
    }
    with pytest.raises(ValidationError, match="prompt"):
        ComfyWorkflowProfile(
            kind="inpainting",
            workflow=workflow,
            mappings={"image": WorkflowInputRef(node_id="1", input_name="image")},
        )

    profile = ComfyWorkflowProfile(
        kind="inpainting",
        workflow=workflow,
        mappings={
            "image": WorkflowInputRef(node_id="1", input_name="image"),
            "prompt": WorkflowInputRef(node_id="2", input_name="text"),
        },
        output_node_ids=["3"],
    )
    assert profile.kind == "inpainting"
    assert profile.output_node_ids == ["3"]


def test_correction_job_uses_explicit_candidate_state_transition(setup_project):
    _, database, collection_id, concept_id, _ = setup_project
    database.transition_concept(concept_id, WorkflowState.PROMPTS_APPROVED, "Aprovado")
    database.transition_concept(concept_id, WorkflowState.GENERATING, "Geração autorizada")
    database.transition_concept(concept_id, WorkflowState.CANDIDATES_READY, "Candidatos prontos")

    job_ids = database.create_job_batch(
        collection_id, concept_id, "correction", [("correction-1", {"feedback": "fix geometry"})]
    )

    assert database.get_concept(concept_id)["state"] == WorkflowState.GENERATING.value
    assert database.get_job(job_ids[0])["kind"] == "correction"


def test_candidate_corrections_stop_at_two_without_contacting_comfy(setup_project):
    from flowapp.config import AppConfig
    from flowapp.orchestrator import Orchestrator

    tmp_path, _, _, _, plan = setup_project
    orchestrator = Orchestrator(tmp_path, AppConfig())
    brief = CollectionBrief(theme="Teste de correção", objective="Validar limite de versões")
    collection_id = orchestrator.database.create_collection("Correções", brief)
    concept_id = orchestrator.database.create_concept(collection_id, plan)
    orchestrator.database.transition_concept(
        concept_id, WorkflowState.PROMPTS_APPROVED, "Aprovação humana"
    )
    orchestrator.database.transition_concept(
        concept_id, WorkflowState.GENERATING, "Geração autorizada"
    )
    orchestrator.database.transition_concept(
        concept_id, WorkflowState.CANDIDATES_READY, "Candidatos prontos"
    )
    store = orchestrator.images
    candidate_bytes = io.BytesIO()
    Image.new("RGB", (100, 80), color=(80, 90, 100)).save(candidate_bytes, format="PNG")
    source_id, _ = store.register_candidate(
        collection_id, concept_id, candidate_bytes.getvalue(), "source.png"
    )
    for index, color in enumerate(((80, 90, 101), (80, 90, 102)), start=1):
        output = io.BytesIO()
        Image.new("RGB", (100, 80), color=color).save(output, format="PNG")
        correction_id, _ = store.register_correction(
            collection_id, concept_id, output.getvalue(), f"correction-{index}.png"
        )
        orchestrator.database.update_asset_metadata(correction_id, {
            "root_source_asset_id": source_id,
            "correction_iteration": index,
        })

    with pytest.raises(ValueError, match="duas correções"):
        orchestrator.correct_candidate(
            collection_id,
            orchestrator.database.get_concept(concept_id),
            orchestrator.database.get_asset(source_id),
            "Fix the leaf geometry and shadow",
        )
    orchestrator.close()


def test_visual_workflow_is_not_accepted_as_api_prompt(setup_project):
    with pytest.raises(ValidationError, match="workflow visual"):
        ComfyWorkflowProfile(
            kind="generation",
            workflow={"nodes": [], "links": []},
            mappings={"prompt": WorkflowInputRef(node_id="4", input_name="text")},
        )


def test_metadata_deduplicates_keywords_and_rejects_ai_title():
    metadata = MetadataSuggestion(
        title_en="Urban balcony greenhouse",
        keywords=["Urban garden", "urban  garden", "greenhouse"],
        analysis_source="description",
    )
    assert metadata.keywords == ["Urban garden", "greenhouse"]

    with pytest.raises(ValidationError, match="generative AI"):
        MetadataSuggestion(
            title_en="Generative AI garden",
            keywords=["garden"],
            analysis_source="description",
        )


def test_human_selection_and_srgb_export_preserve_original(setup_project):
    tmp_path, database, collection_id, concept_id, _ = setup_project
    database.transition_concept(concept_id, WorkflowState.PROMPTS_APPROVED, "Prompt aprovado")
    database.transition_concept(concept_id, WorkflowState.GENERATING, "Geração iniciada")
    database.transition_concept(concept_id, WorkflowState.CANDIDATES_READY, "Geração finalizada")

    raw_image = io.BytesIO()
    source_profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    Image.new("RGB", (240, 120), color=(40, 130, 70)).save(
        raw_image, format="PNG", icc_profile=source_profile,
    )
    store = ImageStore(tmp_path / "data", database)
    asset_id, _ = store.register_candidate(collection_id, concept_id, raw_image.getvalue(), "candidate.png")
    original = database.get_asset(asset_id)
    original_path = original["path"]
    selected_path = store.promote_selected(original)
    assert selected_path != original_path
    assert (tmp_path / "data" / "collections" / collection_id / "originals").is_dir()
    database.select_asset(concept_id, asset_id, "Composição limpa e espaço para texto")

    asset = database.get_asset(asset_id)
    original_hash = asset["sha256"]
    output_path, report = store.export_jpeg(
        asset,
        "Urban balcony greenhouse",
        ["greenhouse", "balcony garden"],
        ExportRules(technical_rules_verified=False),
    )

    with Image.open(output_path) as exported:
        assert exported.format == "JPEG"
        assert exported.info.get("icc_profile")
        assert exported.size == (240, 120)
    assert report["megapixels"] == pytest.approx(0.0288)
    assert report["warnings"]
    assert database.get_asset(asset_id)["sha256"] == original_hash
    assert output_path.is_relative_to(tmp_path / "data" / "collections" / collection_id / "exports")


def test_human_rejection_requires_reason_and_keeps_other_candidate_selectable(setup_project):
    tmp_path, database, collection_id, concept_id, _ = setup_project
    database.transition_concept(concept_id, WorkflowState.PROMPTS_APPROVED, "Prompt aprovado")
    database.transition_concept(concept_id, WorkflowState.GENERATING, "Geração iniciada")
    database.transition_concept(concept_id, WorkflowState.CANDIDATES_READY, "Candidatos prontos")
    store = ImageStore(tmp_path / "data", database)
    image_bytes = []
    for color in ((120, 40, 60), (40, 120, 60)):
        output = io.BytesIO()
        Image.new("RGB", (120, 80), color=color).save(output, format="PNG")
        image_bytes.append(output.getvalue())
    rejected_id, _ = store.register_candidate(collection_id, concept_id, image_bytes[0], "a.png")
    selected_id, _ = store.register_candidate(collection_id, concept_id, image_bytes[1], "b.png")

    with pytest.raises(ValueError, match="motivo"):
        database.reject_asset(concept_id, rejected_id, " ")
    database.reject_asset(concept_id, rejected_id, "Artefatos visíveis nas folhas")
    store.promote_selected(database.get_asset(selected_id))
    database.select_asset(concept_id, selected_id, "Composição mais limpa")

    assets = {asset["id"]: asset for asset in database.list_assets(concept_id)}
    assert assets[rejected_id]["status"] == "rejected"
    assert assets[rejected_id]["review"]["reason"] == "Artefatos visíveis nas folhas"
    assert assets[selected_id]["status"] == "selected"


def test_unselect_returns_chosen_and_passed_over_candidates_to_evaluation(setup_project):
    tmp_path, database, collection_id, concept_id, _ = setup_project
    database.transition_concept(concept_id, WorkflowState.PROMPTS_APPROVED, "Prompt aprovado")
    database.transition_concept(concept_id, WorkflowState.GENERATING, "Geração iniciada")
    database.transition_concept(concept_id, WorkflowState.CANDIDATES_READY, "Candidatos prontos")
    store = ImageStore(tmp_path / "data", database)
    ids = []
    for color in ((10, 20, 30), (30, 20, 10), (20, 30, 10)):
        output = io.BytesIO()
        Image.new("RGB", (120, 80), color=color).save(output, format="PNG")
        ids.append(store.register_candidate(collection_id, concept_id, output.getvalue(), "c.png")[0])
    rejected_id, chosen_id, other_id = ids
    database.reject_asset(concept_id, rejected_id, "Mãos deformadas")
    database.select_asset(concept_id, chosen_id, "Melhor luz")

    database.unselect_asset(concept_id, "Mudei de ideia")

    concept = database.get_concept(concept_id)
    assert concept["state"] == WorkflowState.CANDIDATES_READY.value
    assert concept["selected_asset_id"] is None
    statuses = {asset["id"]: asset["status"] for asset in database.list_assets(concept_id)}
    assert statuses == {rejected_id: "rejected", chosen_id: "candidate", other_id: "candidate"}
    with pytest.raises(InvalidTransition):
        database.unselect_asset(concept_id, "De novo")


def test_comfy_profile_checks_real_node_inputs_and_output(setup_project):
    profile = ComfyWorkflowProfile(
        kind="generation",
        workflow={
            "3": {"class_type": "KSampler", "inputs": {"seed": 11}},
            "11": {"class_type": "CLIPTextEncode", "inputs": {"text": "prompt"}},
            "13": {"class_type": "EmptyLatentImage", "inputs": {"width": 1024, "height": 1024}},
            "24": {"class_type": "SaveImage", "inputs": {"images": ["8", 0]}},
        },
        mappings={
            "prompt": WorkflowInputRef(node_id="11", input_name="text"),
            "seed": WorkflowInputRef(node_id="3", input_name="seed"),
            "width": WorkflowInputRef(node_id="13", input_name="width"),
            "height": WorkflowInputRef(node_id="13", input_name="height"),
        },
        output_node_ids=["24"],
    )
    client = ComfyUIClient("http://localhost:8188")
    client.object_info = lambda: {
        "KSampler": {"input": {"required": {"seed": ["INT"]}}},
        "EmptyLatentImage": {"input": {"required": {"width": ["INT"], "height": ["INT"]}}},
        "CLIPTextEncode": {"input": {"required": {"text": ["STRING"]}}},
        "SaveImage": {"input": {"required": {"images": ["IMAGE"]}}, "output_node": True},
    }

    assert client.validate_profile(profile) == []
    client.close()


def test_generation_requires_seed_mapping_and_applies_random_seed(setup_project):
    profile = ComfyWorkflowProfile(
        kind="generation",
        workflow={
            "3": {"class_type": "KSampler", "inputs": {"seed": 11}},
            "11": {"class_type": "CLIPTextEncode", "inputs": {"text": "prompt"}},
            "24": {"class_type": "SaveImage", "inputs": {"images": ["8", 0]}},
        },
        mappings={"prompt": WorkflowInputRef(node_id="11", input_name="text")},
        output_node_ids=["24"],
    )
    client = ComfyUIClient("http://localhost:8188")
    client.object_info = lambda: {
        "KSampler": {"input": {"required": {"seed": ["INT"]}}},
        "SaveImage": {"input": {"required": {"images": ["IMAGE"]}}, "output_node": True},
    }

    issues = client.validate_profile(profile)
    assert any("mapeie uma entrada seed" in issue.casefold() for issue in issues)
    client.close()

    seeded_profile = ComfyWorkflowProfile(
        kind="generation",
        workflow=profile.workflow,
        output_node_ids=profile.output_node_ids,
        mappings={
            "prompt": WorkflowInputRef(node_id="11", input_name="text"),
            "seed": WorkflowInputRef(node_id="3", input_name="seed"),
        },
    )
    result = client.build_workflow(seeded_profile, {"seed": 987654})
    assert result["3"]["inputs"]["seed"] == 987654


def test_recovery_uses_existing_prompt_id_without_resubmission(setup_project):
    _, database, collection_id, concept_id, _ = setup_project
    job_id = database.create_job(
        collection_id, concept_id, "generation", "recover-key", {"seed": 987},
    )
    recovered_database = Database(database.path)
    job = recovered_database.list_recoverable_jobs(collection_id)[0]
    assert job["id"] == job_id
    assert job["comfy_prompt_id"] == job_id

    profile = ComfyWorkflowProfile(
        kind="generation",
        workflow={"24": {"class_type": "SaveImage", "inputs": {}}},
        mappings={"prompt": WorkflowInputRef(node_id="24", input_name="text")},
        output_node_ids=["24"],
    )
    client = ComfyUIClient("http://localhost:8188")
    requests = []

    def respond(request):
        requests.append((request.method, request.url.path))
        if request.url.path == f"/history/{job_id}":
            return httpx.Response(200, json={job_id: {"outputs": {}}})
        return httpx.Response(200, json={"queue_running": [], "queue_pending": []})

    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(respond))
    client.submit_or_reconcile(job_id, profile, {})
    client.close()

    assert requests == [("GET", f"/history/{job_id}")]


def test_prepared_transfer_requires_explicit_human_state_and_metrics(setup_project):
    _, database, collection_id, concept_id, _ = setup_project
    with pytest.raises(InvalidTransition):
        database.transition_concept(concept_id, WorkflowState.TRANSFERRED, "pular aprovação")

    entry = PerformanceEntry(
        report_date="2026-09-30", download_count=3, revenue=1.25,
        revenue_currency="USD", moderation_status="pending",
        generation_seconds=14, upscale_seconds=8, manual_minutes=5,
        user_cost=0.40, cost_currency="USD",
    )
    database.record_performance(collection_id, entry, concept_id)

    report = database.list_performance(collection_id)
    comparison = database.performance_comparison()
    assert report[0]["download_count"] == 3
    assert report[0]["revenue"] == pytest.approx(1.25)
    assert comparison[0]["generation_seconds"] == pytest.approx(14)
    with pytest.raises(ValidationError):
        PerformanceEntry(report_date="2026-09-30", download_count=-1)


def test_metadata_agent_cannot_claim_image_analysis_without_image(setup_project):
    _, _, _, _, concept = setup_project
    client = LMStudioClient("http://localhost:1234")
    client._complete = lambda *args, **kwargs: json.dumps({
        "title_en": "Urban balcony greenhouse",
        "keywords": ["greenhouse", "balcony garden"],
        "category_suggestion": "Plants",
        "terms_to_confirm": [],
        "analysis_source": "image",
    })

    with pytest.raises(AgentError, match="origem declarada") as raised:
        client.suggest_metadata(
            "local-model", concept, "Greenhouse plants on an apartment balcony", max_attempts=1,
        )
    client.close()

    assert raised.value.attempts == 1


def test_cancel_marks_unsubmitted_batch_jobs_and_stops_future_submission(setup_project):
    from flowapp.config import AppConfig
    from flowapp.orchestrator import Orchestrator

    tmp_path, _, _, _, plan = setup_project
    orchestrator = Orchestrator(tmp_path, AppConfig())
    collection_id = orchestrator.database.create_collection(
        "Teste", CollectionBrief(theme="Teste urbano", objective="Validar cancelamento")
    )
    concept_id = orchestrator.database.create_concept(collection_id, plan)
    orchestrator.database.transition_concept(
        concept_id, WorkflowState.PROMPTS_APPROVED, "Prompt aprovado"
    )
    orchestrator.database.create_job_batch(
        collection_id,
        concept_id,
        "generation",
        [("batch-one", {"seed": 1}), ("batch-two", {"seed": 2})],
    )
    orchestrator.comfy.cancel = lambda prompt_id: "not_found"

    orchestrator.cancel_jobs(collection_id, concept_id)

    assert orchestrator.database.get_concept(concept_id)["state"] == WorkflowState.CANCELLED.value
    assert orchestrator.database.list_recoverable_jobs(collection_id) == []
    orchestrator.close()


def test_delete_collection_removes_only_its_records_and_local_files(tmp_path):
    from flowapp.config import AppConfig
    from flowapp.orchestrator import Orchestrator

    orchestrator = Orchestrator(tmp_path, AppConfig())
    brief = CollectionBrief(theme="Coleção removível", objective="Testar remoção completa")
    collection_id = orchestrator.database.create_collection("Removível", brief)
    collection_root = orchestrator.images.ensure_collection_dirs(collection_id)
    marker = collection_root / "originals" / "only-this-collection.bin"
    marker.write_bytes(b"owned by this collection")
    plan = ConceptPlan(
        concept="A test concept for deletion",
        commercial_application="Test application",
        main_subject="A test subject",
        composition_and_copy_space="Centered with open space",
        orientation="horizontal",
        prompt_en="A deliberately synthetic test subject",
        differentiation_rationale="A temporary test record",
    )
    concept_id = orchestrator.database.create_concept(collection_id, plan)
    orchestrator.database.record_agent_run(
        collection_id, concept_id, "planner", "mock", "1.0.0", "completed",
        {"ok": True}, "", 1, 1,
    )

    orchestrator.delete_collection(collection_id)

    assert orchestrator.database.get_collection(collection_id) is None
    assert not collection_root.exists()
    assert orchestrator.database.list_concepts(collection_id) == []
    assert orchestrator.database.list_history() == []
    orchestrator.close()


def test_delete_collection_is_blocked_by_pending_comfy_job(tmp_path):
    from flowapp.config import AppConfig
    from flowapp.orchestrator import Orchestrator

    orchestrator = Orchestrator(tmp_path, AppConfig())
    collection_id = orchestrator.database.create_collection(
        "Com fila", CollectionBrief(theme="Fila ativa", objective="Garantir bloqueio seguro")
    )
    collection_root = orchestrator.images.ensure_collection_dirs(collection_id)
    marker = collection_root / "originals" / "keep.bin"
    marker.write_bytes(b"do not delete while queued")
    concept_id = orchestrator.database.create_concept(
        collection_id,
        ConceptPlan(
            concept="Queued test concept",
            commercial_application="Test only",
            main_subject="Queue marker",
            composition_and_copy_space="Centered",
            orientation="square",
            prompt_en="A queue marker for a test",
            differentiation_rationale="A temporary test record",
        ),
    )
    orchestrator.database.transition_concept(
        concept_id, WorkflowState.PROMPTS_APPROVED, "Test approval"
    )
    orchestrator.database.create_job_batch(
        collection_id, concept_id, "generation", [("pending-delete-test", {"seed": 1})]
    )

    with pytest.raises(ValueError, match="trabalhos ComfyUI ativos"):
        orchestrator.delete_collection(collection_id)

    assert orchestrator.database.get_collection(collection_id) is not None
    assert marker.is_file()
    orchestrator.close()


def test_failed_upscale_releases_selection_for_retry(setup_project):
    from flowapp.config import AppConfig
    from flowapp.images import ImageValidationError
    from flowapp.orchestrator import Orchestrator
    from flowapp.profiles import save_profile

    tmp_path, _, _, _, plan = setup_project
    orchestrator = Orchestrator(tmp_path, AppConfig())
    database = orchestrator.database
    collection_id = database.create_collection("Upscale", CollectionBrief(theme="Teste", objective="Repetir upscale"))
    concept_id = database.create_concept(collection_id, plan)
    for state in (WorkflowState.PROMPTS_APPROVED, WorkflowState.GENERATING, WorkflowState.CANDIDATES_READY):
        database.transition_concept(concept_id, state, "Preparação do teste")
    raw = io.BytesIO()
    Image.new("RGB", (100, 80), color=(10, 20, 30)).save(raw, format="PNG")
    asset_id, _ = orchestrator.images.register_candidate(collection_id, concept_id, raw.getvalue(), "c.png")
    database.select_asset(concept_id, asset_id, "Melhor composição")
    save_profile(orchestrator.images.collection_path(collection_id), ComfyWorkflowProfile(
        kind="upscale",
        workflow={
            "1": {"class_type": "LoadImage", "inputs": {"image": "x.png"}},
            "9": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}},
        },
        mappings={"image": WorkflowInputRef(node_id="1", input_name="image")},
        output_node_ids=["9"],
    ))
    orchestrator.comfy.validate_profile = lambda profile: []
    orchestrator.comfy.upload_image = lambda path: "x.png"

    def failing_job(job, progress=None):
        database.update_job(job["id"], "failed", "Saída duplicada")
        raise ImageValidationError("Saída duplicada")

    orchestrator._execute_job = failing_job
    with pytest.raises(ImageValidationError):
        orchestrator.upscale_selected(
            collection_id, database.get_concept(concept_id), database.get_asset(asset_id)
        )
    assert database.get_concept(concept_id)["state"] == WorkflowState.SELECTED.value
    orchestrator.close()


def test_new_upscale_output_keeps_its_role_and_duplicate_keeps_original_role(setup_project):
    tmp_path, database, collection_id, concept_id, _ = setup_project
    store = ImageStore(tmp_path / "data", database)
    source = io.BytesIO()
    Image.new("RGB", (60, 40), color=(1, 2, 3)).save(source, format="PNG")
    store.register_candidate(collection_id, concept_id, source.getvalue(), "candidate.png")
    upscaled = io.BytesIO()
    Image.new("RGB", (120, 80), color=(1, 2, 3)).save(upscaled, format="PNG")

    _, new_asset = store._register(collection_id, concept_id, "upscaled", "upscaled", upscaled.getvalue(), "up.png")
    _, duplicate = store._register(collection_id, concept_id, "upscaled", "upscaled", source.getvalue(), "dup.png")

    assert new_asset["role"] == "upscaled"
    assert duplicate["role"] == "candidate"


def test_metadata_with_image_does_not_require_description(setup_project, tmp_path):
    _, _, _, _, concept = setup_project
    image_path = tmp_path / "final.png"
    Image.new("RGB", (32, 32), color=(9, 9, 9)).save(image_path)
    client = LMStudioClient("http://localhost:1234")
    sent = {}

    def fake_request_json(model_id, system, user, output_model, max_attempts, **kwargs):
        sent.update(user=user, images=kwargs.get("images"))
        return output_model(title_en="Balcony greenhouse", keywords=["greenhouse"], analysis_source="image"), 1, 1

    client.request_json = fake_request_json
    result, _, _ = client.suggest_metadata("vision-model", concept, "", max_attempts=1, image_path=image_path)
    with pytest.raises(AgentError, match="modelo com visão"):
        client.suggest_metadata("text-model", concept, "  ", max_attempts=1)
    client.close()

    assert result.analysis_source == "image"
    assert sent["images"] == [image_path]
    assert "<descricao>" not in sent["user"]


def test_prepare_package_exports_jpeg_without_extra_confirmation(setup_project):
    from flowapp.config import AppConfig
    from flowapp.orchestrator import Orchestrator

    tmp_path, _, _, _, plan = setup_project
    orchestrator = Orchestrator(tmp_path, AppConfig())
    database = orchestrator.database
    collection_id = database.create_collection("Pacotes", CollectionBrief(theme="Teste", objective="Exportar em lote"))
    concept_id = database.create_concept(collection_id, plan)
    for state in (WorkflowState.PROMPTS_APPROVED, WorkflowState.GENERATING, WorkflowState.CANDIDATES_READY):
        database.transition_concept(concept_id, state, "Preparação do teste")
    raw = io.BytesIO()
    Image.new("RGB", (80, 60), color=(30, 90, 60)).save(raw, format="PNG")
    asset_id, _ = orchestrator.images.register_candidate(collection_id, concept_id, raw.getvalue(), "c.png")
    database.select_asset(concept_id, asset_id, "Escolhida")
    database.transition_concept(concept_id, WorkflowState.UPSCALING, "teste")
    upscaled = io.BytesIO()
    Image.new("RGB", (160, 120), color=(30, 90, 60)).save(upscaled, format="PNG")
    upscaled_id, _ = orchestrator.images._register(collection_id, concept_id, "upscaled", "upscaled", upscaled.getvalue(), "u.png")
    database.transition_concept(concept_id, WorkflowState.FINAL_REVIEW, "teste")

    with pytest.raises(ValueError, match="Aprove os metadados"):
        orchestrator.prepare_package(database.get_concept(concept_id))

    database.update_asset_metadata(upscaled_id, {"title_en": "Urban greenhouse", "keywords": ["greenhouse", "balcony"]})
    database.transition_concept(concept_id, WorkflowState.METADATA_READY, "teste")
    output, report = orchestrator.prepare_package(database.get_concept(concept_id))
    orchestrator.close()

    assert output.suffix == ".jpg" and output.is_file()
    assert report["width"] == 160
    assert database.get_concept(concept_id)["state"] == WorkflowState.PREPARED_FOR_TRANSFER.value


def test_exported_jpeg_embeds_title_and_keywords_for_adobe(setup_project):
    import re

    from PIL import IptcImagePlugin

    from flowapp.adobe import adobe_csv, category_number

    tmp_path, database, collection_id, concept_id, _ = setup_project
    store = ImageStore(tmp_path / "data", database)
    raw = io.BytesIO()
    Image.new("RGB", (120, 80), color=(40, 130, 70)).save(raw, format="PNG")
    asset_id, _ = store.register_candidate(collection_id, concept_id, raw.getvalue(), "c.png")
    database.update_asset_metadata(asset_id, {"category_suggestion": "Plants and Flowers"})
    output, _ = store.export_jpeg(
        database.get_asset(asset_id), "Balcony greenhouse & herbs", ["greenhouse", "balcony garden"],
        ExportRules(technical_rules_verified=False),
    )

    with Image.open(output) as exported:
        exported.load()
        iptc = IptcImagePlugin.getiptcinfo(exported)
        xmp = exported.info["xmp"].decode("utf-8")
    assert iptc[(2, 5)] == b"Balcony greenhouse & herbs"
    assert iptc[(2, 25)] == [b"greenhouse", b"balcony garden"]
    assert "Balcony greenhouse &amp; herbs" in xmp
    assert re.findall(r"<rdf:li>([^<]*)</rdf:li>", xmp) == ["greenhouse", "balcony garden"]
    export = database.get_asset(asset_id)["export"]
    assert export["adobe_category"] == 14 and export["filename"] == output.name
    assert sorted(path.name for path in output.parent.iterdir()) == [output.name]

    content, warnings = adobe_csv([{
        "filename": output.name, "title": "Cup, coffee", "keywords": ["coffee", "cup"], "category": "Drinks",
    }])
    assert content.splitlines() == [
        "Filename,Title,Keywords,Category,Releases",
        f'{output.name},"Cup, coffee","coffee, cup",4,',
    ]
    assert warnings == []
    assert category_number("The Environment") == 5 and category_number("algo novo") is None


def test_prompt_revision_rejects_removed_terms_left_in_prompt():
    with pytest.raises(ValidationError, match="12:59"):
        PromptRevision(
            prompt_en="A countdown clock displays 12:59 over fireworks",
            removed_terms=["12:59"],
        )
    with pytest.raises(ValidationError, match="Countdown Clock"):
        PromptRevision(
            prompt_en="A countdown clock over fireworks", removed_terms=["Countdown Clock"],
        )
    revision = PromptRevision(
        prompt_en='A black LED number sign showing "2027" over fireworks',
        removed_terms=["12:59", "countdown clock", " "],
    )
    assert revision.removed_terms[0] == "12:59"


def test_every_agent_instruction_exists_for_current_version():
    from flowapp.agents import INSTRUCTION_VERSION, PROMPTS

    for name in ("planner", "prompt_refiner", "visual_reviewer", "metadata", "correction_writer"):
        assert PROMPTS.path(name, INSTRUCTION_VERSION).is_file(), name


def test_correction_feedback_becomes_short_edit_instruction(setup_project):
    from flowapp.config import AppConfig
    from flowapp.orchestrator import Orchestrator

    tmp_path, _, _, _, plan = setup_project
    orchestrator = Orchestrator(tmp_path, AppConfig())
    brief = CollectionBrief(theme="Teste de correção", objective="Validar instrução de edição")
    collection_id = orchestrator.database.create_collection("Correções", brief)
    concept_id = orchestrator.database.create_concept(collection_id, plan)
    concept = orchestrator.database.get_concept(concept_id)
    feedback = "The clock displays '20:27' instead of '2027'.\nremova os dois pontos do relógio"
    calls = []

    def fake_write(model_id, plan, text, attempts, progress=None):
        calls.append(text)
        return CorrectionInstruction(
            instruction_en='Change the display text to "2027", no colon. Keep everything else unchanged.',
        ), 10, 1

    orchestrator.lmstudio.write_correction = fake_write
    instruction = orchestrator._correction_instruction(collection_id, concept, feedback, "mock", None)
    assert instruction.startswith('Change the display text to "2027"')
    assert calls == [feedback]

    def failing_write(*args, **kwargs):
        raise AgentError("LM Studio fora do ar.", 2, 5)

    orchestrator.lmstudio.write_correction = failing_write
    assert orchestrator._correction_instruction(collection_id, concept, feedback, "mock", None) == feedback
    assert orchestrator._correction_instruction(collection_id, concept, feedback, "", None) == feedback
    orchestrator.close()
