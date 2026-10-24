import io
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from flowapp.config import AppConfig, load_config, save_config
from flowapp.database import Database
from flowapp.images import ImageValidationError
from flowapp.orchestrator import Orchestrator
from flowapp.schemas import CollectionBrief, ConceptPlan, WorkflowState
from flowapp.similarity import SimilarityChecker, SimilarityRules, compare_items, fingerprint


def write_pattern(path, seed=5, size=(128, 96)):
    rng = np.random.default_rng(seed)
    # Broad shapes survive resizing/re-encoding, unlike random pixel noise.
    small = Image.fromarray(rng.integers(20, 235, (8, 8, 3), dtype=np.uint8))
    image = small.resize(size, Image.Resampling.BICUBIC)
    image.save(path)
    return path


def item(path, id='a', concept='c'):
    return dict(id=id, path=str(path), concept_id=concept, title=id, source='export')


def test_phash_survives_resizing_and_jpeg_but_rejects_unrelated_pattern(tmp_path):
    path = write_pattern(tmp_path / 'a.png')
    other = tmp_path / 'b.jpg'
    with Image.open(path) as image:
        image.resize((256, 192)).save(other, quality=90)
    rules = SimilarityRules(compare_palette=False)
    match = compare_items(item(path), item(other, 'b', 'd'), rules)
    assert match and match['phash_distance'] <= rules.max_phash_distance
    assert compare_items(item(path), item(write_pattern(tmp_path / 'c.png', seed=37), 'c', 'e'), rules) is None
    assert compare_items(item(path), item(path), rules) is None


def test_palette_is_separate_advisory_and_can_be_disabled(tmp_path):
    path = write_pattern(tmp_path / 'a.png')
    other = tmp_path / 'b.png'
    with Image.open(path) as image:
        image.transpose(Image.Transpose.ROTATE_180).save(other)
    rules = SimilarityRules(max_phash_distance=0, min_palette_overlap=0.99)
    match = compare_items(item(path), item(other, 'b', 'd'), rules)
    assert match and match['palette_overlap'] > 0.99 and not match['blocked']
    assert match['phash_distance'] > 0
    assert any('Paleta' in reason for reason in match['reasons'])
    assert compare_items(item(path), item(other, 'b', 'd'), rules.model_copy(update={'compare_palette': False})) is None


def test_concept_limit_blocks_even_when_visual_signals_are_disabled(tmp_path):
    a = item(write_pattern(tmp_path / 'a.png'))
    b = item(write_pattern(tmp_path / 'b.png', seed=37), 'b')
    rules = SimilarityRules(compare_palette=False, max_phash_distance=0)
    assert compare_items(a, b, rules, enforce_concept=True)['blocked']
    assert compare_items(a, b, rules, enforce_concept=False) is None
    assert compare_items(a, b, rules.model_copy(update={'one_per_concept': False}), enforce_concept=True) is None


def test_cache_invalidates_after_file_changes_and_uses_exif_orientation(tmp_path):
    path = write_pattern(tmp_path / 'a.png')
    rules = SimilarityRules()
    before = fingerprint(path, rules)[0]
    write_pattern(path, seed=37, size=(132, 100))
    assert fingerprint(path, rules)[0] != before
    rotated = tmp_path / 'rotated.png'
    exif = Image.Exif()
    exif[274] = 6
    with Image.open(path) as image:
        image.transpose(Image.Transpose.ROTATE_90).save(rotated, exif=exif)
    assert fingerprint(path, rules)[0] == fingerprint(rotated, rules)[0]


@pytest.fixture
def project(tmp_path):
    config = AppConfig(quality_rules={'min_sharpness': 0, 'min_local_sharpness_p25': 0,
                                     'max_local_detail_ratio': 100, 'max_noise': 100},
                       export_rules={'technical_rules_verified': True})
    orch = Orchestrator(tmp_path, config)
    collection = orch.database.create_collection('Teste', CollectionBrief(theme='Teste', objective='Comparar imagens'))
    plan = ConceptPlan(concept='Plantas em circuitos', commercial_application='Campanha comercial',
        main_subject='Plantas', composition_and_copy_space='Espaço vazio à esquerda', orientation='horizontal',
        prompt_en='Green plants and golden circuits on a dark background', differentiation_rationale='Nova composição')
    cid = orch.database.create_concept(collection, plan)
    for state in [WorkflowState.PROMPTS_APPROVED, WorkflowState.GENERATING, WorkflowState.CANDIDATES_READY]:
        orch.database.transition_concept(cid, state, 'Teste')
    path = write_pattern(tmp_path / 'original.png')
    aid, _ = orch.images.register_candidate(collection, cid, path.read_bytes(), 'original.png')
    orch.database.select_asset(cid, aid, 'Teste')
    orch.database.transition_concept(cid, WorkflowState.UPSCALING, 'Teste')
    path = write_pattern(tmp_path / 'final.png', size=(256, 192))
    aid, _ = orch.images.register_upscaled(collection, cid, path.read_bytes(), 'final.png')
    orch.database.update_asset_metadata(aid, {'title_en': 'Test plants', 'keywords': ['plants', 'circuits']})
    orch.database.transition_concept(cid, WorkflowState.FINAL_REVIEW, 'Teste')
    orch.database.transition_concept(cid, WorkflowState.METADATA_READY, 'Teste')
    yield orch, collection, cid, aid
    orch.close()


def test_missing_history_is_explicit_then_attachment_is_persistent_and_idempotent(project):
    orch, collection, cid, aid = project
    record = orch.database.add_external_moderation('Imagem externa', status='approved')
    checker = orch.similarity_checker()
    report = checker.report(collection)
    assert report['missing_history'] == [{'id': record, 'title': 'Imagem externa'}]
    with pytest.raises(ImageValidationError, match='similaridade'):
        orch.prepare_package(orch.database.get_concept(cid))
    source = Path(orch.database.get_asset(aid)['path']).read_bytes()
    path = checker.attach_reference(record, source)
    checker.attach_reference(record, source)
    assert path.is_file() and len(orch.database.list_similarity_references()) == 1
    report = SimilarityChecker(Database(orch.database.path), orch.data_root, orch.config.similarity_rules).report(collection)
    assert not report['missing_history']
    assert len(report['matches']) == 1 and report['matches'][0]['right']['moderation'] == 'approved'
    with pytest.raises(ImageValidationError, match='similaridade'):
        orch.prepare_package(orch.database.get_concept(cid))
    output, saved = orch.prepare_package(orch.database.get_concept(cid), allow_warnings=True)
    assert output.exists() and saved['similarity']['warnings_confirmed']
    assert saved['similarity']['matches']
    assert orch.database.get_asset(aid)['export']['similarity'] == saved['similarity']


def test_invalid_reference_is_not_written_and_missing_file_never_passes_silently(project):
    orch, collection, _, aid = project
    checker = orch.similarity_checker()
    record = orch.database.add_external_moderation('Imagem externa', status='pending')
    with pytest.raises(ValueError, match='não encontrado'):
        checker.attach_reference('../not-a-record', b'bad')
    with pytest.raises(OSError):
        checker.attach_reference(record, b'bad')
    assert not orch.database.list_similarity_references()
    Path(orch.database.get_asset(aid)['path']).write_bytes(b'broken')
    report = checker.report(collection)
    assert report['unreadable'][0]['id'] == aid
    assert report['compared_images'] == 0 and report['missing_history']


def test_backend_checks_concept_rule_even_after_warning_confirmation(project, monkeypatch):
    orch, _, cid, _ = project
    real = orch.similarity_checker()
    report = real.report(orch.database.get_concept(cid)['collection_id'])
    asset = orch.database.list_assets(cid)[-1]
    duplicate = item(Path(asset['path']), 'different-final', cid)
    match = compare_items(item(Path(asset['path']), asset['id'], cid), duplicate,
                          orch.config.similarity_rules, enforce_concept=True)
    report['matches'] = [match]
    monkeypatch.setattr(real, 'report', lambda *args, **kwargs: report)
    monkeypatch.setattr(orch, 'similarity_checker', lambda: real)
    with pytest.raises(ImageValidationError, match='uma imagem por conceito'):
        orch.prepare_package(orch.database.get_concept(cid), allow_warnings=True)
    assert orch.database.get_concept(cid)['state'] == 'metadata_ready'
    assert not list(orch.data_root.rglob('*.jpg'))


def test_similarity_config_roundtrip(tmp_path):
    config = AppConfig(similarity_rules={'one_per_concept': False, 'min_palette_overlap': 0.95})
    path = tmp_path / 'config.json'
    save_config(path, config)
    assert load_config(path).similarity_rules == config.similarity_rules
    with pytest.raises(ValueError):
        SimilarityRules(max_phash_distance=64)


def test_candidates_compare_with_each_other_and_all_historical_statuses(project, tmp_path):
    orch, collection, cid, _ = project
    other_collection = orch.database.create_collection('Histórico', CollectionBrief(theme='Outros', objective='Histórico de envios'))
    second_path = write_pattern(tmp_path / 'another-candidate.png', size=(192, 144))
    second_id, _ = orch.images.register_candidate(collection, cid, second_path.read_bytes(), 'variation.png')
    checker = orch.similarity_checker()
    for status in ['pending', 'approved', 'rejected']:
        record = orch.database.add_external_moderation(
            f'Referência {status}', status=status, collection_id=other_collection,
            reason='Conteúdo semelhante' if status == 'rejected' else '',
        )
        checker.attach_reference(record, second_path.read_bytes())
    report = checker.report(collection, 'candidates')
    assert report['compared_images'] == 2 and report['history_images'] == 3
    assert not report['missing_history']
    assert any(m['left']['id'] != m['right']['id'] and m['right']['source'] == 'candidates'
               for m in report['matches'])
    assert {m['right']['moderation'] for m in report['matches'] if m['right']['source'] == 'history'} == {
        'pending', 'approved', 'rejected'}
    assert not any(m['blocked'] for m in report['matches'])


def test_original_does_not_warn_against_its_own_submitted_upscale(project):
    orch, collection, cid, _ = project
    orch.prepare_package(orch.database.get_concept(cid))
    orch.database.transition_concept(cid, WorkflowState.TRANSFERRED, 'Transferência manual')
    orch.database.transition_concept(cid, WorkflowState.SUBMITTED, 'Submissão manual')
    report = orch.similarity_checker().report(collection, 'candidates')
    assert report['history_images'] == 1 and report['compared_images'] == 1
    assert not report['matches']
