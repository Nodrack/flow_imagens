import json

from streamlit.testing.v1 import AppTest


CREATION = '''
import app
import streamlit as st
from pathlib import Path
from unittest.mock import patch
from flowapp.config import AppConfig
from flowapp.orchestrator import Orchestrator
if "orch" not in st.session_state:
    st.session_state.orch = Orchestrator(Path(ROOT_PATH), AppConfig(planner_model="fake"))
    st.session_state.pipeline_calls = []
orch = st.session_state.orch
def start(*args, **kwargs):
    st.session_state.pipeline_calls.append(args[1])
with patch.object(app, "model_list", return_value=[]), patch.object(app, "load_default_profile", return_value=True), patch.object(app, "start_pipeline", side_effect=start):
    if MANUAL:
        app.render_manual_collection_creation(orch.database, orch)
    else:
        app.render_collection_creation(orch.database, orch, orch.config)
'''


def screen(tmp_path, manual=False):
    at = AppTest.from_string(CREATION.replace('ROOT_PATH', repr(str(tmp_path))).replace('MANUAL', repr(manual))).run(timeout=15)
    assert not at.exception
    return at


def button(at, label):
    return next(value for value in at.button if value.label == label)


def test_selecting_template_does_not_apply_or_start_production_and_fields_stay_editable(tmp_path):
    at = screen(tmp_path)
    assert at.text_input(key='new_brief_theme').value == ''
    at.selectbox(key='new_brief_template').select('Ingredientes e comida').run()
    assert at.text_input(key='new_brief_theme').value == ''
    assert not at.session_state.pipeline_calls
    button(at, 'Aplicar modelo ao briefing').click().run()
    assert not at.exception
    assert 'Ingredientes' in at.text_input(key='new_brief_theme').value
    assert at.text_input(key='new_brief_commercial_application').value
    assert not at.session_state.pipeline_calls
    at.text_area(key='new_brief_objective').input('Meu objetivo comercial revisado')
    button(at, 'Criar coleção e acionar agentes').click().run()
    assert not at.exception and not at.error
    assert len(at.session_state.pipeline_calls) == 1
    collection = at.session_state.orch.database.get_collection(at.session_state['active_collection'])
    assert collection['brief']['objective'] == 'Meu objetivo comercial revisado'
    assert collection['brief']['composition_constraints']
    assert collection['brief']['orientation'] == 'mixed'


def test_manual_creation_applies_brief_without_approving_or_creating_a_prompt(tmp_path):
    at = screen(tmp_path, manual=True)
    at.selectbox(key='manual_brief_template').select('Ambientes').run()
    button(at, 'Aplicar modelo ao briefing').click().run()
    assert not at.exception
    assert 'Ambientes' in at.text_input(key='manual_brief_theme').value
    assert not next(value for value in at.checkbox if 'Revisei e aprovo' in value.label).value
    assert not next(value for value in at.text_area if value.label == 'Prompt em inglês para o ComfyUI').value
    assert at.session_state.orch.database.list_collections() == []


SETTINGS = '''
import app
from pathlib import Path
from unittest.mock import patch
from flowapp.config import load_config
path = Path(CONFIG_FILE)
with patch.object(app, "CONFIG_PATH", path), patch.object(app, "model_list", return_value=[]):
    app.render_settings(load_config(path))
'''


def test_custom_templates_save_and_invalid_template_does_not_replace_config(tmp_path):
    path = tmp_path / 'config.json'
    at = AppTest.from_string(SETTINGS.replace('CONFIG_FILE', repr(str(path)))).run(timeout=15)
    at.session_state['commercial_templates_editor'] = {
        'edited_rows': {'0': {'Objetivo': 'Objetivo personalizado e revisado'}},
        'added_rows': [], 'deleted_rows': [],
    }
    button(at, 'Salvar configuração').click().run()
    assert not at.exception and not at.error
    assert json.loads(path.read_text(encoding='utf-8'))['commercial_briefings']['templates'][0]['objective'] == 'Objetivo personalizado e revisado'
    before = path.read_bytes()
    at.session_state['commercial_templates_editor'] = {
        'edited_rows': {'0': {'Tema': 'x'}}, 'added_rows': [], 'deleted_rows': [],
    }
    button(at, 'Salvar configuração').click().run()
    assert not at.exception and at.error
    assert path.read_bytes() == before


def test_empty_template_list_keeps_editor_columns_and_accepts_a_custom_model(tmp_path):
    from flowapp.briefings import CommercialBriefings
    from flowapp.config import AppConfig, save_config
    path = tmp_path / 'config.json'
    save_config(path, AppConfig(commercial_briefings=CommercialBriefings(templates=[])))
    at = AppTest.from_string(SETTINGS.replace('CONFIG_FILE', repr(str(path)))).run(timeout=15)
    assert not at.exception and len(at.dataframe[1].value.columns) == 7
    at.session_state['commercial_templates_editor'] = {
        'edited_rows': {}, 'deleted_rows': [],
        'added_rows': [{'Modelo': 'Meu modelo', 'Tema': 'Campanha local', 'Objetivo': 'Criar cenas para uma campanha'}],
    }
    button(at, 'Salvar configuração').click().run()
    assert not at.exception and not at.error
    saved = json.loads(path.read_text(encoding='utf-8'))['commercial_briefings']['templates']
    assert len(saved) == 1 and saved[0]['label'] == 'Meu modelo'
