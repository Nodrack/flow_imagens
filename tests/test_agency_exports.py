import csv
import io
import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from PIL import Image

from flowapp.agency_exports import AgencyExportProfile, AgencyExportsConfig, default_profiles, metadata_reference
from flowapp.config import AppConfig, load_config, save_config
from flowapp.images import ImageValidationError
from flowapp.moderation import today_local
from flowapp.orchestrator import Orchestrator
from flowapp.schemas import CollectionBrief, ConceptPlan, ExportRules, WorkflowState


def custom_profile(**changes):
    values = dict(id='example', name='Agência de teste', enabled=True, ai_policy='conditional', policy_verified=True,
                  policy_source='https://example.org/ai', policy_checked_on=today_local(),
                  policy_notes='Conferir o conteúdo manualmente', ai_title_suffix=' [AI-generated]',
                  rules=ExportRules(technical_rules_verified=True, source='https://example.org/technical',
                                    checked_at=today_local().isoformat()))
    return AgencyExportProfile(**(values | changes))


@pytest.fixture
def project(tmp_path):
    profile = custom_profile()
    config = AppConfig(agency_exports=AgencyExportsConfig(profiles=[default_profiles()[0], profile]),
                       export_rules=ExportRules(technical_rules_verified=True),
                       quality_rules={'min_sharpness': 0, 'max_noise': 100,
                                      'min_local_sharpness_p25': 0, 'max_local_detail_ratio': 100})
    orch = Orchestrator(tmp_path, config)
    db = orch.database
    collection = db.create_collection('Exportações', CollectionBrief(theme='Teste', objective='Exportar finais'))
    plan = ConceptPlan(concept='Plantas em uma mesa', commercial_application='Banner', main_subject='Plantas',
        composition_and_copy_space='Assunto à direita e espaço à esquerda', orientation='horizontal',
        prompt_en='Green plants on a table with empty space on the left', differentiation_rationale='Nova composição')
    cid = db.create_concept(collection, plan)
    for state in [WorkflowState.PROMPTS_APPROVED, WorkflowState.GENERATING, WorkflowState.CANDIDATES_READY]:
        db.transition_concept(cid, state, 'Teste')
    raw = io.BytesIO()
    Image.new('RGB', (100, 80), (60, 90, 70)).save(raw, format='PNG')
    candidate, _ = orch.images.register_candidate(collection, cid, raw.getvalue(), 'candidate.png')
    db.select_asset(cid, candidate, 'Escolhida')
    db.transition_concept(cid, WorkflowState.UPSCALING, 'Teste')
    raw = io.BytesIO()
    Image.new('RGBA', (200, 160), (60, 90, 70, 77)).save(raw, format='PNG')
    aid, _ = orch.images.register_upscaled(collection, cid, raw.getvalue(), 'final.png')
    db.update_asset_metadata(aid, {'title_en': 'Plants on a table', 'keywords': ['plants', 'table']})
    db.transition_concept(cid, WorkflowState.FINAL_REVIEW, 'Teste')
    db.transition_concept(cid, WorkflowState.METADATA_READY, 'Teste')
    yield orch, collection, cid, aid, profile
    orch.close()


def test_seeded_profiles_record_sources_and_dates_and_keep_unverified_destinations_disabled():
    profiles = {profile.id: profile for profile in default_profiles()}
    assert profiles['dreamstime'].enabled and profiles['dreamstime'].rules.min_megapixels == 3
    assert profiles['dreamstime'].allowed_formats == ['JPEG']
    assert all(profile.policy_source.startswith('https://') and profile.policy_checked_on == date(2026, 10, 25)
               for profile in profiles.values())
    for key in ['shutterstock', 'getty', 'freepik']:
        assert not profiles[key].enabled
        with pytest.raises(ValueError):
            AgencyExportProfile.model_validate(profiles[key].model_dump() | {'enabled': True})


def test_activation_requires_reviewed_policy_technical_source_and_valid_dates():
    profile = custom_profile()
    for change in [dict(policy_verified=False), dict(policy_checked_on=None), dict(policy_checked_on=today_local()+timedelta(days=1)),
                   dict(ai_policy='not_allowed'), dict(policy_source=''), dict(rules=ExportRules()),
                   dict(allowed_formats=['PNG']), dict(id='../escape')]:
        with pytest.raises(ValueError):
            AgencyExportProfile.model_validate(profile.model_dump() | change)
    with pytest.raises(ValueError):
        AgencyExportsConfig(profiles=[profile])


def test_preview_has_no_writes_and_export_needs_policy_review(project):
    orch, collection, cid, _, profile = project
    _, report = orch.prepare_agency_package(orch.database.get_concept(cid), profile.id, preview=True)
    assert report['title'] == 'Plants on a table [AI-generated]'
    assert not (orch.images.collection_path(collection) / 'agency_exports').exists()
    with pytest.raises(ValueError, match='condições'):
        orch.prepare_agency_package(orch.database.get_concept(cid), profile.id)
    assert not orch.database.list_agency_exports(collection, profile.id)


def test_non_adobe_export_keeps_adobe_package_states_originals_and_metadata(project):
    orch, collection, cid, aid, profile = project
    db = orch.database
    original = Path(db.get_asset(aid)['path']).read_bytes()
    adobe_path, adobe_report = orch.prepare_package(db.get_concept(cid))
    metadata = db.get_asset(aid)['metadata']
    output, report = orch.prepare_agency_package(db.get_concept(cid), profile.id, conditions_reviewed=True)
    assert db.get_concept(cid)['state'] == 'prepared_for_transfer'
    assert db.get_asset(aid)['export'] == adobe_report and db.get_asset(aid)['metadata'] == metadata
    assert Path(db.get_asset(aid)['path']).read_bytes() == original
    assert adobe_path.parent.name == 'exports' and output.parent.name == profile.id
    assert output.parent.parent.name == 'agency_exports'
    reference = json.loads(Path(report['metadata_path']).read_text(encoding='utf-8'))
    assert reference['ai_generated'] and reference['title'].endswith('[AI-generated]')
    assert 'path' not in reference and 'similarity' not in reference
    with Image.open(output) as image:
        assert image.format == 'JPEG' and image.info.get('icc_profile')
        assert 'AI-generated' in image.info['xmp'].decode()
    records = db.list_agency_exports(collection, profile.id)
    assert len(records) == 1 and records[0]['report']['agency_profile']['policy_source'] == profile.policy_source
    second, _ = orch.prepare_agency_package(db.get_concept(cid), profile.id, conditions_reviewed=True)
    assert second != output and output.exists() and len(db.list_agency_exports(collection, profile.id)) == 2
    assert db.list_agency_exports(collection, profile.id)[0]['report']['path'] == str(second)


def test_resize_png_alpha_and_csv_reference_are_profile_specific(project):
    orch, collection, cid, _, profile = project
    replacement = custom_profile(file_format='PNG', metadata_format='csv', max_width=100, max_height=100,
                                 required_keywords=['AI', 'plants'])
    orch.config.agency_exports = AgencyExportsConfig(profiles=[default_profiles()[0], replacement])
    output, report = orch.prepare_agency_package(orch.database.get_concept(cid), profile.id, conditions_reviewed=True)
    with Image.open(output) as image:
        assert image.format == 'PNG' and image.size == (100, 80)
        assert image.getchannel('A').getextrema() == (77, 77)
        assert 'AI-generated' in image.info['Title'] and 'XML:com.adobe.xmp' in image.info
    assert report['keywords'] == ['plants', 'table', 'AI']
    rows = list(csv.DictReader(io.StringIO(Path(report['metadata_path']).read_text(encoding='utf-8'))))
    assert rows[0]['filename'] == output.name and rows[0]['ai_generated'] == 'true'
    assert not orch.database.get_asset(orch.database.list_assets(cid)[-1]['id'])['export']


@pytest.mark.parametrize('limits', [dict(min_megapixels=0.01), dict(max_file_bytes=10), dict(min_short_edge=90)])
def test_limits_block_after_resizing_and_cannot_be_overridden(project, limits):
    orch, collection, cid, _, _ = project
    short_edge = limits.get('min_short_edge')
    rules = ExportRules(technical_rules_verified=True, source='https://example.org/technical',
                        checked_at=today_local().isoformat(), **limits)
    profile = custom_profile(max_width=100, max_height=100, min_short_edge=short_edge, rules=rules)
    orch.config.agency_exports = AgencyExportsConfig(profiles=[default_profiles()[0], profile])
    _, preview = orch.prepare_agency_package(orch.database.get_concept(cid), profile.id, preview=True)
    assert preview['blocked']
    with pytest.raises(ImageValidationError, match='bloqueada'):
        orch.prepare_agency_package(orch.database.get_concept(cid), profile.id, conditions_reviewed=True, allow_warnings=True)
    assert not (orch.images.collection_path(collection) / 'agency_exports').exists()


def test_metadata_csv_is_a_reference_and_neutralizes_spreadsheet_formulas():
    profile = custom_profile(metadata_format='csv')
    content, suffix = metadata_reference({'filename': 'test.jpg', 'title': '=2+2', 'keywords': ['plants']}, profile)
    rows = list(csv.DictReader(io.StringIO(content)))
    assert suffix == '.metadata.csv' and rows[0]['title'] == "'=2+2"


def test_profile_config_roundtrip(tmp_path):
    config = AppConfig(agency_exports=AgencyExportsConfig(profiles=[default_profiles()[0], custom_profile(max_width=400)]))
    path = tmp_path / 'config.json'
    save_config(path, config)
    assert load_config(path).agency_exports == config.agency_exports
