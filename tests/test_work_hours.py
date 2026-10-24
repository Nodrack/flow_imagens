import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

import flowapp.database as database_module
from flowapp.config import AppConfig
from flowapp.database import Database, TimerTooLong
from flowapp.metrics import format_hours, format_per_hour, revenue_per_hour
from flowapp.schemas import CollectionBrief, PerformanceEntry


@pytest.fixture
def env(tmp_path):
    database = Database(tmp_path / "flow.sqlite3")
    brief = CollectionBrief(theme="Jardim eletrônico", objective="Imagens comerciais")
    return database, database.create_collection("Jardim", brief), database.create_collection("Cidade", brief)


def test_manual_session_validation(env):
    database, cid, _ = env
    database.add_work_session(cid, "2026-10-01", 90, "edição")
    assert database.work_minutes_by_collection() == {cid: 90}
    for minutes in (0, -5, 24 * 60 + 1):
        with pytest.raises(ValueError, match="tempo"):
            database.add_work_session(cid, "2026-10-01", minutes)
    with pytest.raises(ValueError, match="Data"):
        database.add_work_session(cid, "ontem", 10)
    with pytest.raises(KeyError):
        database.add_work_session("x", "2026-10-01", 10)


def test_timer_start_stop_and_single_open(env):
    database, cid, other = env
    assert database.open_timer() is None
    session = database.start_timer(cid)
    assert database.open_timer()["collection_id"] == cid
    with pytest.raises(ValueError, match="Jardim"):
        database.start_timer(other)
    assert database.work_minutes_by_collection() == {}  # aberta não conta
    assert database.stop_timer(session, minutes_override=5) == 5
    assert database.open_timer() is None
    with pytest.raises(ValueError):
        database.stop_timer(session)
    database.start_timer(other)  # liberado depois de parar
    assert len(database.list_work_sessions(cid)) == 1


def test_discard_only_open(env):
    database, cid, _ = env
    session = database.start_timer(cid)
    database.discard_timer(session)
    assert database.open_timer() is None and database.list_work_sessions(cid) == []
    closed = database.start_timer(cid)
    database.stop_timer(closed, minutes_override=10)
    with pytest.raises(ValueError):
        database.discard_timer(closed)
    manual = database.add_work_session(cid, "2026-10-01", 30)
    with pytest.raises(ValueError):
        database.discard_timer(manual)


def test_delete_work_session(env):
    database, cid, _ = env
    manual = database.add_work_session(cid, "2026-10-01", 30)
    database.delete_work_session(manual)
    assert database.list_work_sessions(cid) == []
    with pytest.raises(KeyError):
        database.delete_work_session(manual)
    open_session = database.start_timer(cid)
    with pytest.raises(KeyError):
        database.delete_work_session(open_session)


def test_delete_collection_removes_sessions(env):
    database, cid, other = env
    database.add_work_session(cid, "2026-10-01", 30)
    database.add_work_session(other, "2026-10-01", 45)
    database.stop_timer(database.start_timer(cid), minutes_override=15)
    database.delete_collection_records(cid)
    assert database.open_timer() is None
    assert database.work_minutes_by_collection() == {other: 45}
    with sqlite3.connect(database.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM work_sessions").fetchone()[0] == 1


def _row(cid, currency, revenue, manual):
    return {"collection_id": cid, "revenue_currency": currency, "revenue": revenue, "manual_minutes": manual}


def test_revenue_per_hour_sums_sources_and_keeps_currencies():
    rows = [_row("a", "USD", 30.0, 60), _row("a", "EUR", 10.0, 0), _row("b", "USD", 20.0, 0), _row("c", None, None, None)]
    result = revenue_per_hour(rows, {"a": 60, "c": 0})
    a = result["collections"]["a"]
    assert a["hours"] == 2  # 60 manual + 60 sessões
    assert a["by_currency"]["USD"]["per_hour"] == 15
    assert a["by_currency"]["EUR"]["per_hour"] == 5
    assert result["collections"]["b"]["by_currency"]["USD"]["per_hour"] is None  # sem horas
    total = result["total"]
    assert total["hours"] == 2
    assert total["by_currency"]["USD"] == {"revenue": 50.0, "per_hour": 25.0}
    assert total["by_currency"]["EUR"]["per_hour"] == 5


def test_revenue_per_hour_without_any_hours():
    result = revenue_per_hour([_row("a", "USD", 5.0, 0)], {})
    assert result["total"]["by_currency"]["USD"]["per_hour"] is None


def test_metrics_with_database_rows(env):
    database, cid, _ = env
    database.record_performance(cid, PerformanceEntry(
        report_date=date(2026, 10, 1), revenue=12, manual_minutes=30))
    database.add_work_session(cid, "2026-10-02", 90)
    result = revenue_per_hour(database.performance_comparison(), database.work_minutes_by_collection())
    assert result["collections"][cid]["hours"] == 2
    assert result["collections"][cid]["by_currency"]["USD"]["per_hour"] == 6


def _age_timer(database, session_id, monkeypatch, minutes):
    """Faz o cronômetro parecer aberto há `minutes` (utc_now avança em vez de mexer no banco)."""
    start = datetime.fromisoformat(
        sqlite3.connect(database.path).execute(
            "SELECT started_at FROM work_sessions WHERE id = ?", (session_id,)).fetchone()[0]
    )
    monkeypatch.setattr(
        database_module, "utc_now",
        lambda: (start + timedelta(minutes=minutes)).isoformat(timespec="seconds"),
    )


def test_stop_timer_rounding_limit_override_and_short(env, monkeypatch):
    database, cid, _ = env
    session = database.start_timer(cid)
    _age_timer(database, session, monkeypatch, 90.5)
    assert database.stop_timer(session) == 90.5

    session = database.start_timer(cid)
    _age_timer(database, session, monkeypatch, 13 * 60)
    with pytest.raises(TimerTooLong) as info:
        database.stop_timer(session)
    assert info.value.minutes == 780
    assert database.open_timer() is not None  # nada foi gravado
    with pytest.raises(ValueError):
        database.stop_timer(session, minutes_override=0)
    with pytest.raises(ValueError):
        database.stop_timer(session, minutes_override=24 * 60 + 1)
    assert database.stop_timer(session, minutes_override=120) == 120
    assert database.work_minutes_by_collection() == {cid: 90.5 + 120}

    session = database.start_timer(cid)
    _age_timer(database, session, monkeypatch, 0.5)
    assert database.stop_timer(session) is None  # menos de 1 minuto: descartado
    assert database.open_timer() is None and len(database.list_work_sessions(cid)) == 2

    session = database.start_timer(cid)
    _age_timer(database, session, monkeypatch, 20 * 60)
    assert database.stop_timer(session, max_minutes=24 * 60) == 1200  # limite configurável


def test_unique_index_blocks_second_open_timer(env):
    database, cid, other = env
    database.start_timer(cid)
    with sqlite3.connect(database.path) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO work_sessions(id, collection_id, work_date, started_at, source, created_at, updated_at) "
            "VALUES ('x', ?, '2026-10-01', '2026-10-01T10:00:00+00:00', 'timer', 'a', 'a')", (other,))


def test_start_timer_converts_integrity_error(env, monkeypatch):
    database, cid, _ = env
    database.start_timer(cid)
    # Força o caminho do índice único: a checagem prévia é contornada com SQL que não vê o aberto.
    real_connect = database.connect

    class Blind:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, *args):
            if "SELECT w.id, c.name" in sql:
                return self.connection.execute("SELECT NULL WHERE 0")
            return self.connection.execute(sql, *args)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self.connection.__exit__(*exc)

    monkeypatch.setattr(database, "connect", lambda: Blind(real_connect()))
    with pytest.raises(ValueError, match="cronômetro aberto"):
        database.start_timer(env[2])


def test_initialize_twice_on_old_database(tmp_path):
    path = tmp_path / "old.sqlite3"
    database = Database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE work_sessions")  # banco anterior à funcionalidade
    database.initialize()
    database.initialize()
    cid = database.create_collection("A", CollectionBrief(theme="Tema longo", objective="Objetivo"))
    database.add_work_session(cid, "2026-10-01", 10)
    assert database.work_minutes_by_collection() == {cid: 10}


def test_delete_collection_blocked_by_open_timer(env):
    database, cid, _ = env
    session = database.start_timer(cid)
    with pytest.raises(ValueError, match="cronômetro aberto"):
        database.delete_collection_records(cid)
    database.discard_timer(session)
    database.delete_collection_records(cid)


def test_add_work_session_limits(env):
    database, cid, _ = env
    database.add_work_session(cid, "2026-10-01", 1440, "x" * 1000)
    with pytest.raises(ValueError, match="1000"):
        database.add_work_session(cid, "2026-10-01", 10, "x" * 1001)


def test_formatters():
    assert format_hours(59.6 / 60) == "1h00"
    assert format_hours(1.5) == "1h30"
    assert format_hours(0) == "0h00"
    assert format_per_hour(None, "USD") == "-"
    assert format_per_hour(12.5, "EUR") == "12.50 EUR/h"


def test_two_currencies_with_manual_minutes_from_database(env):
    database, cid, _ = env
    database.record_performance(cid, PerformanceEntry(
        report_date=date(2026, 10, 1), revenue=30, revenue_currency="USD", manual_minutes=60))
    database.record_performance(cid, PerformanceEntry(
        report_date=date(2026, 10, 2), revenue=10, revenue_currency="EUR", manual_minutes=60))
    database.add_work_session(cid, "2026-10-03", 60)
    rows = database.performance_comparison()
    result = revenue_per_hour(rows, database.work_minutes_by_collection())
    jardim = result["collections"][cid]
    assert jardim["hours"] == 3  # 60 + 60 manuais (somados uma vez por moeda) + 60 sessão
    assert jardim["by_currency"]["USD"]["per_hour"] == 10
    assert jardim["by_currency"]["EUR"]["per_hour"] == pytest.approx(10 / 3)
    assert result["total"]["hours"] == 3


def test_config_timer_max_hours():
    assert AppConfig().timer_max_hours == 12
    with pytest.raises(Exception):
        AppConfig(timer_max_hours=25)
