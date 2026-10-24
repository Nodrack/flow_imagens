import io
import json

import httpx
import pytest
from PIL import Image

from flowapp.comfy import ComfyError
from flowapp.config import AppConfig, load_config, save_config
from flowapp.orchestrator import Orchestrator
from flowapp.profiles import save_profile
from flowapp.schemas import CollectionBrief, ComfyWorkflowProfile, ConceptPlan, WorkflowInputRef, WorkflowState


def png(size):
    buffer = io.BytesIO()
    Image.new('RGB', size, (30, 90, 60)).save(buffer, format='PNG')
    return buffer.getvalue()


@pytest.fixture
def routed(tmp_path):
    orch = Orchestrator(tmp_path, AppConfig(comfy_base_url='http://legacy.test',
        comfy_generation_url='http://generation.test', comfy_upscale_url='https://upscale.test',
        free_vram_between_phases=False))
    collection = orch.database.create_collection('Nuvem', CollectionBrief(theme='Teste', objective='Testar duas GPUs'))
    plan = ConceptPlan(concept='Plantas em uma mesa', commercial_application='Banner', main_subject='Plantas',
        composition_and_copy_space='Assunto à direita e espaço à esquerda', orientation='horizontal',
        prompt_en='Green plants on a table with empty space on the left', differentiation_rationale='Nova composição')
    cid = orch.database.create_concept(collection, plan)
    orch.database.transition_concept(cid, WorkflowState.PROMPTS_APPROVED, 'Teste')
    root = orch.images.ensure_collection_dirs(collection)
    gen = ComfyWorkflowProfile(kind='generation', workflow={
        '1': {'class_type': 'Generator', 'inputs': {'text': '', 'seed': 0, 'width': 256, 'height': 256}},
        '9': {'class_type': 'SaveImage', 'inputs': {}}},
        mappings={key: WorkflowInputRef(node_id='1', input_name=name)
                  for key, name in [('prompt', 'text'), ('seed', 'seed'), ('width', 'width'), ('height', 'height')]},
        output_node_ids=['9'])
    up = ComfyWorkflowProfile(kind='upscale', workflow={
        '1': {'class_type': 'LoadImage', 'inputs': {'image': ''}},
        '9': {'class_type': 'SaveImage', 'inputs': {}}},
        mappings={'image': WorkflowInputRef(node_id='1', input_name='image')}, output_node_ids=['9'])
    save_profile(root, gen)
    save_profile(root, up)
    calls, histories = [], {}
    flags = {'timeout_once': False, 'offline': False}

    def respond(request):
        host, path = request.url.host, request.url.path
        calls.append((host, request.method, path))
        if host == 'upscale.test' and flags['offline']:
            raise httpx.ConnectError('offline', request=request)
        if path == '/object_info':
            return httpx.Response(200, json={
                'Generator': {'input': {'required': {'text': ['STRING'], 'seed': ['INT'], 'width': ['INT'], 'height': ['INT']}}},
                'LoadImage': {'input': {'required': {'image': ['STRING']}}},
                'SaveImage': {'input': {}, 'output_node': True},
            })
        if path.startswith('/history/'):
            pid = path.rsplit('/', 1)[1]
            output = histories.get((host, pid))
            return httpx.Response(200, json={pid: output} if output else {})
        if path == '/queue':
            return httpx.Response(200, json={'queue_running': [], 'queue_pending': []})
        if path == '/upload/image':
            assert host == 'upscale.test'
            assert png((256, 256)) in request.content
            return httpx.Response(200, json={'name': 'uploaded.png'})
        if path == '/prompt':
            payload = json.loads(request.content)
            pid = payload['prompt_id']
            histories[(host, pid)] = {'outputs': {'9': {'images': [{'filename': 'out.png', 'type': 'output'}]}}}
            if host == 'upscale.test' and flags['timeout_once']:
                flags['timeout_once'] = False
                raise httpx.ReadTimeout('response lost after acceptance', request=request)
            return httpx.Response(200, json={'prompt_id': pid})
        if path == '/view':
            return httpx.Response(200, content=png((512, 512) if host == 'upscale.test' else (256, 256)))
        if path == '/free':
            return httpx.Response(200, json={})
        raise AssertionError((host, path))

    for client in orch._comfy_clients.values():
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(respond))
    yield orch, collection, cid, calls, flags
    orch.close()


def generate_and_select(orch, collection, cid):
    orch.generate_candidates(collection, orch.database.get_concept(cid), 1, 256, 256)
    asset = orch.database.list_assets(cid)[0]
    orch.database.select_asset(cid, asset['id'], 'Escolhida')
    return asset


def test_generation_and_upscale_validate_upload_execute_and_fetch_on_correct_servers(routed):
    orch, collection, cid, calls, _ = routed
    asset = generate_and_select(orch, collection, cid)
    original = png((256, 256))
    final = orch.upscale_selected(collection, orch.database.get_concept(cid), asset)
    from pathlib import Path
    assert Path(asset['path']).read_bytes() == original
    assert final['width'] == 512 and final['role'] == 'upscaled'
    assert ('generation.test', 'POST', '/prompt') in calls
    assert ('upscale.test', 'POST', '/upload/image') in calls
    assert ('upscale.test', 'POST', '/prompt') in calls
    assert ('upscale.test', 'GET', '/object_info') in calls
    assert ('upscale.test', 'GET', '/view') in calls
    with orch.database.connect() as connection:
        jobs = connection.execute('SELECT kind, payload_json FROM jobs').fetchall()
    assert {row['kind']: json.loads(row['payload_json'])['comfy_base_url'] for row in jobs} == {
        'generation': 'http://generation.test', 'upscale': 'https://upscale.test'}


def test_resume_keeps_original_endpoint_and_prompt_id_after_configuration_change(routed):
    orch, collection, cid, calls, flags = routed
    asset = generate_and_select(orch, collection, cid)
    flags['timeout_once'] = True
    with pytest.raises(ComfyError):
        orch.upscale_selected(collection, orch.database.get_concept(cid), asset)
    job = orch.database.list_recoverable_jobs(collection)[0]
    assert job['payload']['comfy_base_url'] == 'https://upscale.test'
    orch.config.comfy_upscale_url = 'https://replacement.test'
    flags['offline'] = True
    with pytest.raises(ComfyError):
        orch.resume_jobs(collection)
    assert not any(host == 'replacement.test' for host, _, _ in calls)
    flags['offline'] = False
    restarted = Orchestrator(orch.root, orch.config.model_copy(deep=True))
    original = orch.job_comfy(job)
    resumed = restarted.job_comfy(job)
    assert resumed.base_url == 'https://upscale.test'
    resumed._client.close()
    resumed._client = httpx.Client(transport=original._client._transport)
    try:
        restarted.resume_jobs(collection)
    finally:
        restarted.close()
    assert orch.database.get_job(job['id'])['status'] == 'completed'
    assert sum(host == 'upscale.test' and path == '/prompt' for host, _, path in calls) == 1
    assert orch.database.get_concept(cid)['state'] == 'final_review'


def test_cancel_and_legacy_jobs_resolve_their_original_server(routed):
    orch, collection, cid, _, _ = routed
    job_id = orch.database.create_job(collection, cid, 'upscale', 'cancel-cloud',
        {'comfy_base_url': 'https://upscale.test'})
    cancelled = []
    orch.comfy_for('upscale').cancel = lambda pid: cancelled.append(pid) or 'not_found'
    orch.config.comfy_upscale_url = 'https://replacement.test'
    orch.cancel_jobs(collection, cid)
    assert cancelled == [job_id]
    assert orch.database.get_job(job_id)['status'] == 'cancelled'
    assert orch.job_comfy({'kind': 'generation', 'payload': {}}).base_url == 'http://legacy.test'


def test_vram_release_attempts_every_destination_and_respects_toggle(routed):
    orch, _, _, calls, flags = routed
    orch.gpu._switch('comfy', 'llm')
    assert not calls
    orch.gpu.free_vram = True
    flags['offline'] = True
    orch.gpu._switch('comfy', 'llm')
    assert ('generation.test', 'POST', '/free') in calls
    assert ('upscale.test', 'POST', '/free') in calls
    assert orch.gpu.last_switch_error


def test_urls_default_to_shared_service_and_roundtrip_env(tmp_path, monkeypatch):
    config = AppConfig()
    assert config.comfy_url_for('generation') == config.comfy_url_for('upscale') == config.comfy_base_url
    path = tmp_path / 'config.json'
    save_config(path, config)
    monkeypatch.setenv('FLOW_COMFY_UPSCALE_URL', 'https://cloud.example/comfy/')
    assert load_config(path).comfy_url_for('upscale') == 'https://cloud.example/comfy'
    for url in ['ftp://host', 'not a URL', 'https://user:secret@host', 'https://host?token=secret', 'http://host:invalid']:
        with pytest.raises(ValueError):
            AppConfig(comfy_upscale_url=url)
