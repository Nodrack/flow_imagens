import json

from streamlit.testing.v1 import AppTest


SCREEN = '''
import app
import io
import streamlit as st
from pathlib import Path
from PIL import Image
from flowapp.agency_exports import AgencyExportProfile, AgencyExportsConfig, default_profiles
from flowapp.config import AppConfig
from flowapp.orchestrator import Orchestrator
from flowapp.moderation import today_local
from flowapp.schemas import CollectionBrief, ConceptPlan, ExportRules, WorkflowState
if "orch" not in st.session_state:
    profile = AgencyExportProfile(id="example", name="Perfil de teste", enabled=True, ai_policy="conditional",
        policy_verified=True, policy_source="https://example.org/ai", policy_checked_on=today_local(),
        rules=ExportRules(technical_rules_verified=True, source="https://example.org/technical", checked_at=today_local().isoformat()))
    config = AppConfig(agency_exports=AgencyExportsConfig(profiles=[*default_profiles(), profile]),
        export_rules={"technical_rules_verified": True}, quality_rules={"min_sharpness": 0, "min_local_sharpness_p25": 0})
    orch = Orchestrator(Path(ROOT_PATH), config)
    db = orch.database
    collection = db.create_collection("Coleção", CollectionBrief(theme="Teste", objective="Exportar imagens"))
    plan = ConceptPlan(concept="Plantas na mesa", commercial_application="Banner", main_subject="Plantas",
        composition_and_copy_space="Espaço à esquerda", orientation="horizontal", prompt_en="Plants on a table with empty space on the left",
        differentiation_rationale="Cena nova")
    cid = db.create_concept(collection, plan)
    for state in [WorkflowState.PROMPTS_APPROVED, WorkflowState.GENERATING, WorkflowState.CANDIDATES_READY]:
        db.transition_concept(cid, state, "Teste")
    raw = io.BytesIO()
    Image.new("RGB", (100, 80), (50, 100, 70)).save(raw, format="PNG")
    aid, _ = orch.images.register_candidate(collection, cid, raw.getvalue(), "candidate.png")
    db.select_asset(cid, aid, "Escolhida")
    db.transition_concept(cid, WorkflowState.UPSCALING, "Teste")
    raw = io.BytesIO()
    Image.new("RGB", (200, 160), (50, 100, 70)).save(raw, format="PNG")
    aid, _ = orch.images.register_upscaled(collection, cid, raw.getvalue(), "final.png")
    db.update_asset_metadata(aid, {"title_en": "Plants on a table", "keywords": ["plants"]})
    db.transition_concept(cid, WorkflowState.FINAL_REVIEW, "Teste")
    db.transition_concept(cid, WorkflowState.METADATA_READY, "Teste")
    st.session_state.orch, st.session_state.collection, st.session_state.cid = orch, collection, cid
app.render_export(st.session_state.orch, st.session_state.collection)
'''


def test_other_agency_requires_review_and_does_not_advance_adobe_state(tmp_path):
    at = AppTest.from_string(SCREEN.replace('ROOT_PATH', repr(str(tmp_path)))).run(timeout=20)
    assert not at.exception
    next(value for value in at.selectbox if value.label == 'Agência de exportação').select('example').run()
    assert not at.exception
    button = next(value for value in at.button if value.label == 'Preparar pacote para Perfil de teste')
    assert button.disabled
    next(value for value in at.checkbox if value.label.startswith('Conferi os direitos')).check().run()
    next(value for value in at.button if value.label == 'Preparar pacote para Perfil de teste').click().run()
    assert not at.exception and not at.error
    db = at.session_state.orch.database
    assert db.get_concept(at.session_state.cid)['state'] == 'metadata_ready'
    assert len(db.list_agency_exports(at.session_state.collection, 'example')) == 1
    assert any(value.label == 'Baixar metadados de referência' for value in at.get('download_button'))


def test_non_accepting_profile_is_blocked_without_creating_package(tmp_path):
    at = AppTest.from_string(SCREEN.replace('ROOT_PATH', repr(str(tmp_path)))).run(timeout=20)
    next(value for value in at.selectbox if value.label == 'Agência de exportação').select('shutterstock').run()
    assert not at.exception
    assert any('não permite' in value.value for value in at.warning)
    assert not any(value.label.startswith('Preparar pacote') for value in at.button)
    assert not list(tmp_path.rglob('*.jpg'))


SETTINGS = '''
import app
from pathlib import Path
from unittest.mock import patch
from flowapp.config import load_config
path = Path(CONFIG_FILE)
with patch.object(app, "CONFIG_PATH", path):
    app.render_agency_profile_settings(load_config(path))
'''


def test_profile_settings_save_dimensions_and_refuse_disallowed_activation(tmp_path):
    path = tmp_path / 'config.json'
    at = AppTest.from_string(SETTINGS.replace('CONFIG_FILE', repr(str(path)))).run(timeout=20)
    next(value for value in at.selectbox if value.label == 'Perfil para editar').select('dreamstime').run()
    next(value for value in at.number_input if value.label.startswith('Largura máxima')).set_value(800)
    next(value for value in at.button if value.label == 'Salvar perfil de exportação').click().run()
    assert not at.exception and not at.error
    profiles = json.loads(path.read_text(encoding='utf-8'))['agency_exports']['profiles']
    assert next(row for row in profiles if row['id'] == 'dreamstime')['max_width'] == 800
    before = path.read_bytes()
    next(value for value in at.selectbox if value.label == 'Perfil para editar').select('shutterstock').run()
    next(value for value in at.checkbox if value.label == 'Ativar perfil de exportação').check()
    next(value for value in at.button if value.label == 'Salvar perfil de exportação').click().run()
    assert not at.exception and at.error
    assert path.read_bytes() == before


def test_saving_general_settings_preserves_custom_agency_profiles(tmp_path):
    from flowapp.agency_exports import AgencyExportProfile, AgencyExportsConfig, default_profiles
    from flowapp.config import AppConfig, save_config
    path = tmp_path / 'config.json'
    custom = AgencyExportProfile(id='draft', name='Perfil personalizado', max_width=1234)
    save_config(path, AppConfig(agency_exports=AgencyExportsConfig(profiles=[*default_profiles(), custom])))
    source = '''
import app
from pathlib import Path
from unittest.mock import patch
from flowapp.config import load_config
path = Path(CONFIG_FILE)
with patch.object(app, "CONFIG_PATH", path), patch.object(app, "model_list", return_value=[]):
    app.render_settings(load_config(path))
'''
    at = AppTest.from_string(source.replace('CONFIG_FILE', repr(str(path)))).run(timeout=20)
    next(value for value in at.button if value.label == 'Salvar configuração').click().run()
    assert not at.exception and not at.error
    profiles = json.loads(path.read_text(encoding='utf-8'))['agency_exports']['profiles']
    assert next(row for row in profiles if row['id'] == 'draft')['max_width'] == 1234
