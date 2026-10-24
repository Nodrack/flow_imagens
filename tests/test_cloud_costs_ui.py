import json

from streamlit.testing.v1 import AppTest


SETTINGS = '''
import app
from pathlib import Path
from unittest.mock import patch
from flowapp.config import load_config
path = Path(CONFIG_FILE)
with patch.object(app, "CONFIG_PATH", path), patch.object(app, "model_list", return_value=[]):
    app.render_settings(load_config(path))
'''


def button(at, label):
    return next(value for value in at.button if value.label == label)


def test_per_stage_urls_save_and_invalid_address_does_not_overwrite_config(tmp_path):
    path = tmp_path / 'config.json'
    at = AppTest.from_string(SETTINGS.replace('CONFIG_FILE', repr(str(path)))).run(timeout=15)
    next(value for value in at.text_input if value.label.startswith('ComfyUI para ampliação')).input('https://cloud.example/comfy/')
    button(at, 'Salvar configuração').click().run()
    assert not at.exception and not at.error
    values = json.loads(path.read_text(encoding='utf-8'))
    assert values['comfy_upscale_url'] == 'https://cloud.example/comfy'
    assert values['comfy_generation_url'] == ''
    before = path.read_bytes()
    next(value for value in at.text_input if value.label.startswith('ComfyUI para ampliação')).input('ftp://invalid')
    button(at, 'Salvar configuração').click().run()
    assert not at.exception and at.error
    assert path.read_bytes() == before


COSTS = '''
import app
import streamlit as st
from pathlib import Path
from flowapp.config import AppConfig
from flowapp.orchestrator import Orchestrator
from flowapp.schemas import CollectionBrief, PerformanceEntry
from flowapp.metrics import revenue_per_hour
if "orch" not in st.session_state:
    st.session_state.orch = Orchestrator(Path(ROOT_PATH), AppConfig())
    db = st.session_state.orch.database
    st.session_state.cid = db.create_collection("Coleção de teste", CollectionBrief(theme="Teste", objective="Testar custos"))
    db.add_work_session(st.session_state.cid, "2026-10-01", 60)
    db.record_performance(st.session_state.cid, PerformanceEntry(report_date="2026-10-01", revenue=10))
db = st.session_state.orch.database
metrics = revenue_per_hour(db.performance_comparison(), db.work_minutes_by_collection(), db.list_monthly_sales(), db.list_archived_work())
app.render_collection_costs(st.session_state.orch, st.session_state.cid, metrics)
'''


def test_cost_entry_updates_result_and_deletion_requires_confirmation(tmp_path):
    at = AppTest.from_string(COSTS.replace('ROOT_PATH', repr(str(tmp_path)))).run(timeout=15)
    assert not at.exception
    next(value for value in at.number_input if value.label == 'Valor do custo').set_value(1.25)
    next(value for value in at.text_input if value.label == 'Descrição ou fornecedor do custo').input('Aluguel GPU')
    button(at, 'Registrar custo').click().run()
    assert not at.exception and not at.error
    rows = at.session_state.orch.database.list_collection_costs(at.session_state.cid)
    assert len(rows) == 1 and rows[0]['amount'] == 1.25
    assert at.dataframe[-1].value['Resultado após custos'].tolist() == [8.75]
    assert button(at, 'Excluir custo').disabled
    next(value for value in at.checkbox if value.label == 'Confirmo a exclusão deste custo').check().run()
    button(at, 'Excluir custo').click().run()
    assert not at.exception
    assert not at.session_state.orch.database.list_collection_costs(at.session_state.cid)


def test_zero_cost_is_rejected_without_writing_data(tmp_path):
    at = AppTest.from_string(COSTS.replace('ROOT_PATH', repr(str(tmp_path)))).run(timeout=15)
    button(at, 'Registrar custo').click().run()
    assert not at.exception and at.error
    assert not at.session_state.orch.database.list_collection_costs(at.session_state.cid)


def test_workflow_import_uses_server_for_selected_stage(tmp_path):
    source = '''
import app
import streamlit as st
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
if "calls" not in st.session_state:
    st.session_state.calls = []
workflow = {"1": {"class_type": "LoadImage", "inputs": {"image": "input.png"}},
            "9": {"class_type": "SaveImage", "inputs": {"images": ["1", 0]}}}
class Upload:
    size = 300
    def getvalue(self): return json.dumps(workflow).encode()
class Client:
    def __init__(self, stage): self.stage = stage
    def object_info(self):
        st.session_state.calls.append((self.stage, "nodes"))
        return {"LoadImage": {"input": {"required": {"image": ["STRING"]}}}, "SaveImage": {"output_node": True}}
    def validate_profile(self, profile):
        st.session_state.calls.append((self.stage, "validate"))
        return []
orch = SimpleNamespace(comfy_for=lambda stage: Client(stage))
with patch.object(app, "ROOT", Path(ROOT_PATH)), patch.object(app, "DATA_ROOT", Path(ROOT_PATH)/"data"), patch.object(app.st, "file_uploader", return_value=Upload()):
    app.render_workflow_import(orch, None)
'''
    at = AppTest.from_string(source.replace('ROOT_PATH', repr(str(tmp_path)))).run(timeout=15)
    assert not at.exception
    next(value for value in at.selectbox if value.label == 'Etapa').select('upscale').run()
    button(at, 'Validar e salvar workflow').click().run()
    assert not at.exception
    assert ('upscale', 'nodes') in at.session_state.calls
    assert at.session_state.calls[-1] == ('upscale', 'validate')
    assert (tmp_path / 'data' / 'workflows' / 'upscale.json').is_file()
