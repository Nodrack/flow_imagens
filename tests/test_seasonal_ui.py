from streamlit.testing.v1 import AppTest


def test_dashboard_separates_upcoming_and_expired_and_handles_disabled_calendar():
    at = AppTest.from_string('''
import app
from datetime import date
from flowapp.seasonal import SeasonalCalendar, SeasonalEvent
calendar = SeasonalCalendar(events=[
    SeasonalEvent(name="Carnaval de teste", rule="once", on_date=date(2027, 2, 9), lead_days=90),
    SeasonalEvent(name="Natal de teste", rule="once", on_date=date(2026, 12, 25), lead_days=90),
])
app.render_seasonal_calendar(calendar, date(2026, 10, 24))
app.render_seasonal_calendar(SeasonalCalendar(enabled=False), date(2026, 10, 24))
''').run(timeout=15)
    assert not at.exception
    assert len(at.dataframe) == 2
    assert at.dataframe[0].value['Evento'].tolist() == ['Carnaval de teste']
    assert at.dataframe[1].value['Evento'].tolist() == ['Natal de teste']
    assert at.dataframe[1].value['Situação'].tolist() == ['Prazo encerrado']
    assert any('Calendário desativado' in caption.value for caption in at.caption)


SETTINGS = '''
import app
from pathlib import Path
from unittest.mock import patch
from flowapp.config import load_config
path = Path(CONFIG_FILE)
with patch.object(app, "CONFIG_PATH", path), patch.object(app, "model_list", return_value=[]):
    app.render_settings(load_config(path))
'''


def settings(tmp_path):
    path = tmp_path / 'config.json'
    at = AppTest.from_string(SETTINGS.replace('CONFIG_FILE', repr(str(path)))).run(timeout=15)
    assert not at.exception
    return at, path


def test_settings_saves_calendar_edits_and_keeps_other_settings(tmp_path):
    import json
    at, path = settings(tmp_path)
    at.session_state['seasonal_events_editor'] = {
        'edited_rows': {'0': {'Antecedência (dias)': 42}},
        'added_rows': [], 'deleted_rows': [],
    }
    next(value for value in at.number_input if value.label == 'Horizonte dos prazos de envio (dias)').set_value(120)
    next(value for value in at.button if value.label == 'Salvar configuração').click().run()
    assert not at.exception and not at.error
    saved = json.loads(path.read_text(encoding='utf-8'))
    assert saved['seasonal_calendar']['horizon_days'] == 120
    assert saved['seasonal_calendar']['events'][0]['lead_days'] == 42
    assert saved['similarity_rules']['one_per_concept'] is True
    assert saved['quality_rules']['region_size'] == 128


def test_invalid_calendar_does_not_overwrite_existing_config(tmp_path):
    from flowapp.config import AppConfig, save_config
    path = tmp_path / 'config.json'
    save_config(path, AppConfig())
    before = path.read_bytes()
    at, _ = settings(tmp_path)
    at.session_state['seasonal_events_editor'] = {
        'edited_rows': {'0': {'Mês': 2, 'Dia': 30}},
        'added_rows': [], 'deleted_rows': [],
    }
    next(value for value in at.button if value.label == 'Salvar configuração').click().run()
    assert not at.exception
    assert any('Calendário inválido' in error.value for error in at.error)
    assert path.read_bytes() == before


def test_empty_calendar_still_allows_adding_events(tmp_path):
    import json
    from flowapp.config import AppConfig, save_config
    from flowapp.seasonal import SeasonalCalendar
    path = tmp_path / 'config.json'
    save_config(path, AppConfig(seasonal_calendar=SeasonalCalendar(events=[])))
    at, _ = settings(tmp_path)
    assert len(at.dataframe[0].value.columns) == 8
    at.session_state['seasonal_events_editor'] = {
        'edited_rows': {}, 'deleted_rows': [],
        'added_rows': [{'Nome': 'Campanha nova', 'Mês': 9, 'Dia': 7, 'Antecedência (dias)': 30}],
    }
    next(value for value in at.button if value.label == 'Salvar configuração').click().run()
    assert not at.exception and not at.error
    events = json.loads(path.read_text(encoding='utf-8'))['seasonal_calendar']['events']
    assert len(events) == 1 and events[0]['name'] == 'Campanha nova'
