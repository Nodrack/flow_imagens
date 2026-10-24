import sqlite3
from datetime import UTC, date, datetime

import pytest

from flowapp.database import Database
from flowapp.metrics import (
    approval_rate, approved_per_week, downloads_per_approved, revenue_per_hour,
)
from flowapp.schemas import LOCAL_TZ, CollectionBrief, PerformanceEntry

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)  # quarta-feira; semana ISO começa em 26/10
BRIEF = CollectionBrief(theme="Jardim eletrônico", objective="Imagens comerciais")


@pytest.fixture
def env(tmp_path):
    database = Database(tmp_path / "flow.sqlite3")
    return database, database.create_collection("Jardim", BRIEF), database.create_collection("Cidade", BRIEF)


def _mod(cid, status, decided=None):
    return {"collection_id": cid, "status": status, "decided_at": decided}


def test_upsert_replaces_same_month_including_currency(env):
    database, cid, _ = env
    first = database.upsert_monthly_sales(cid, "2026-09", 10, 5.0, "USD", "a")
    second = database.upsert_monthly_sales(cid, "2026-09", 12, 6.5, "EUR", "corrigido")
    assert first == second
    rows = database.list_monthly_sales(cid)
    assert len(rows) == 1
    assert (rows[0]["downloads"], rows[0]["revenue"], rows[0]["currency"], rows[0]["notes"]) == (
        12, 6.5, "EUR", "corrigido")
    database.upsert_monthly_sales(cid, "2026-10", 1, 1.0, "USD")
    assert len(database.list_monthly_sales(cid)) == 2


def test_null_collection_duplicates_are_replaced_and_index_blocks_raw_insert(env):
    database, _, _ = env
    database.upsert_monthly_sales(None, "2026-09", 1, 1.0, "USD")
    database.upsert_monthly_sales(None, "2026-09", 2, 2.0, "USD")
    assert [row["downloads"] for row in database.list_monthly_sales(None)] == [2]
    with sqlite3.connect(database.path) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO monthly_sales(id, collection_id, month, downloads, revenue, currency, created_at, "
            "updated_at) VALUES ('x', NULL, '2026-09', 1, 1, 'USD', 'a', 'a')")


def test_validation(env):
    database, cid, _ = env
    current = datetime.now(LOCAL_TZ).strftime("%Y-%m")
    database.upsert_monthly_sales(cid, current, 1, 1.0, "USD")  # mês atual vale
    for month in ("2099-01", "2026-13", "setembro"):
        with pytest.raises(ValueError):
            database.upsert_monthly_sales(cid, month, 1, 1.0, "USD")
    with pytest.raises(ValueError):
        database.upsert_monthly_sales(cid, "2026-09", -1, 1.0, "USD")
    with pytest.raises(ValueError):
        database.upsert_monthly_sales(cid, "2026-09", 1, -1.0, "USD")
    with pytest.raises(KeyError):
        database.upsert_monthly_sales("x", "2026-09", 1, 1.0, "USD")


def test_delete_monthly_sales(env):
    database, cid, _ = env
    sale = database.upsert_monthly_sales(cid, "2026-09", 1, 1.0, "USD")
    database.delete_monthly_sales(sale)
    assert database.list_monthly_sales() == []
    with pytest.raises(KeyError):
        database.delete_monthly_sales(sale)


def test_delete_collection_keeps_and_detaches_sales(env):
    database, cid, other = env
    database.upsert_monthly_sales(cid, "2026-09", 10, 5.0, "USD")
    database.upsert_monthly_sales(other, "2026-09", 7, 3.0, "USD")  # outro nome, mesmo mês e moeda
    database.upsert_monthly_sales(None, "2026-09", 1, 1.0, "USD")
    database.delete_collection_records(cid)
    database.delete_collection_records(other)
    rows = {row["collection_name"]: row for row in database.list_monthly_sales(None)}
    assert set(rows) == {"Jardim", "Cidade", ""}
    assert rows["Jardim"]["downloads"] == 10 and rows["Jardim"]["collection_id"] is None


def test_two_removals_with_same_name_stay_separate(env):
    database, cid, _ = env
    database.upsert_monthly_sales(cid, "2026-09", 10, 5.0, "USD")
    database.delete_collection_records(cid)
    again = database.create_collection("Jardim", BRIEF)
    database.upsert_monthly_sales(again, "2026-09", 4, 2.5, "USD")
    database.delete_collection_records(again)
    rows = database.list_monthly_sales(None)
    assert sorted(row["downloads"] for row in rows) == [4, 10]
    assert {row["detached_from"] for row in rows} == {cid, again}
    assert all(row["collection_name"] == "Jardim" for row in rows)
    # lançamento sem coleção no mesmo mês continua possível e separado
    database.upsert_monthly_sales(None, "2026-09", 1, 1.0, "USD")
    assert len(database.list_monthly_sales(None)) == 3


def test_removing_collection_does_not_change_total_revenue_per_hour(env):
    database, a, b = env
    for cid in (a, b):
        database.upsert_monthly_sales(cid, "2026-09", 50, 100.0, "USD")
        database.add_work_session(cid, "2026-09-05", 600)

    def total():
        return revenue_per_hour(
            database.performance_comparison(), database.work_minutes_by_collection(),
            database.list_monthly_sales(), database.list_archived_work(),
        )

    before = total()["total"]["by_currency"]["USD"]
    assert before == {"revenue": 200.0, "per_hour": 10.0}
    database.delete_collection_records(a)
    after = total()
    assert after["total"]["by_currency"]["USD"] == {"revenue": 200.0, "per_hour": 10.0}
    removed = after["collections"][f"removida:{a}"]
    assert removed["hours"] == 10 and removed["by_currency"]["USD"]["per_hour"] == 10


def test_removed_collection_archives_manual_minutes_and_legacy_revenue(env):
    database, a, _ = env
    database.record_performance(a, PerformanceEntry(
        report_date=date(2026, 9, 1), revenue=30, manual_minutes=60, revenue_currency="EUR"))
    database.add_work_session(a, "2026-09-02", 60)
    database.delete_collection_records(a)
    archived = database.list_archived_work()
    assert len(archived) == 1
    assert (archived[0]["detached_from"], archived[0]["minutes"], archived[0]["manual_minutes"]) == (a, 60, 60)
    assert archived[0]["revenue"] == {"EUR": 30}
    result = revenue_per_hour([], {}, [], archived)
    assert result["collections"][f"removida:{a}"]["by_currency"]["EUR"]["per_hour"] == 15


def test_removed_collection_moderation_and_downloads_stay_coherent(env):
    database, a, _ = env
    database.add_external_moderation("img", "approved", a, decided_at="2026-08-15T10:00:00+00:00")
    database.upsert_monthly_sales(a, "2026-09", 40, 10.0, "USD")
    database.delete_collection_records(a)
    moderation = database.list_moderation()
    assert moderation[0]["detached_from"] == a and moderation[0]["collection_name"] == "Jardim"
    result = downloads_per_approved(database.list_monthly_sales(), moderation)
    assert result["collections"][f"removida:{a}"]["2026-09"] == {"downloads": 40, "approved": 1, "per_image": 40}
    assert approval_rate(moderation)["collections"][f"removida:{a}"]["rate"] == 1


def test_approved_falls_back_to_submitted_then_updated_at():
    rows = [
        {"collection_id": "a", "status": "approved", "decided_at": None,
         "submitted_at": "2026-10-28T09:00:00+00:00", "updated_at": "2026-01-01T00:00:00+00:00"},
        {"collection_id": "a", "status": "approved", "decided_at": None, "submitted_at": None,
         "updated_at": "2026-10-20T09:00:00+00:00"},
    ]
    series = approved_per_week(rows, NOW)["series"]
    assert series[-1]["approved"] == 1 and series[-2]["approved"] == 1 and series[-3]["approved"] == 0


def test_old_database_gets_new_columns(tmp_path):
    path = tmp_path / "old.sqlite3"
    database = Database(path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX ux_monthly_sales_month")
        connection.execute("ALTER TABLE monthly_sales DROP COLUMN detached_from")
        connection.execute("ALTER TABLE moderation_records DROP COLUMN detached_from")
        connection.execute("ALTER TABLE moderation_records DROP COLUMN detached_name")
    database.initialize()
    database.initialize()
    cid = database.create_collection("A", BRIEF)
    database.upsert_monthly_sales(cid, "2026-09", 1, 1.0, "USD")
    database.upsert_monthly_sales(cid, "2026-09", 2, 1.0, "USD")
    assert [row["downloads"] for row in database.list_monthly_sales(cid)] == [2]


def test_approval_rate_excludes_pending_and_handles_empty():
    rows = [_mod("a", "approved"), _mod("a", "approved"), _mod("a", "rejected"), _mod("a", "pending"),
            _mod("b", "pending"), _mod(None, "rejected")]
    result = approval_rate(rows)
    assert result["collections"]["a"]["rate"] == pytest.approx(2 / 3)
    assert result["collections"]["a"]["pending"] == 1
    assert result["collections"]["b"]["rate"] is None
    assert result["collections"][None]["rate"] == 0
    assert result["total"]["rate"] == pytest.approx(2 / 4)
    assert approval_rate([])["total"]["rate"] is None


def test_approved_per_week_has_zero_weeks_and_madrid_boundaries():
    rows = [
        _mod("a", "approved", "2026-10-28T09:00:00+00:00"),   # semana de 26/10
        _mod("a", "approved", "2026-10-25T22:30:00+00:00"),   # 23:30 de domingo em Madri: semana de 19/10
        _mod("a", "approved", "2026-10-25T23:30:00+00:00"),   # 00:30 de segunda em Madri: semana de 26/10
        _mod("a", "approved", "2026-01-01T10:00:00+00:00"),   # fora das 12 semanas
        _mod("a", "rejected", "2026-10-27T10:00:00+00:00"),
        _mod("a", "pending", None),
        _mod("a", "approved", None),                          # sem data: ignorada
    ]
    result = approved_per_week(rows, NOW)
    series = result["series"]
    assert len(series) == 12
    assert series[-1]["start"].isoformat() == "2026-10-26" and series[-1]["approved"] == 2
    assert series[-2]["approved"] == 1
    assert sum(item["approved"] == 0 for item in series) == 10
    # média: semanas completas desde a primeira aprovação (jan/2026 -> as 11 completas da janela)
    assert result["average"] == pytest.approx(1 / 11)
    assert approved_per_week([], NOW)["average"] is None


def test_downloads_per_approved_cumulative_by_month_in_madrid():
    moderation = [
        _mod("a", "approved", "2026-08-15T10:00:00+00:00"),
        _mod("a", "approved", "2026-09-30T22:30:00+00:00"),   # 01/10 em Madri: só conta em outubro
        _mod("b", "approved", "2026-09-10T10:00:00+00:00"),
        _mod("b", "rejected", "2026-09-10T10:00:00+00:00"),
    ]
    sales = [
        {"collection_id": "a", "month": "2026-09", "downloads": 30},
        {"collection_id": "a", "month": "2026-10", "downloads": 30},
        {"collection_id": "b", "month": "2026-09", "downloads": 5},
        {"collection_id": "c", "month": "2026-09", "downloads": 8},
        {"collection_id": "a", "month": "2026-07", "downloads": 4},    # antes de qualquer aprovada
    ]
    result = downloads_per_approved(sales, moderation)
    a = result["collections"]["a"]
    assert a["2026-09"] == {"downloads": 30, "approved": 1, "per_image": 30}
    assert a["2026-10"]["approved"] == 2 and a["2026-10"]["per_image"] == 15
    assert a["2026-07"]["per_image"] is None
    assert result["collections"]["c"]["2026-09"]["per_image"] is None
    assert result["total"]["2026-09"] == {"downloads": 43, "approved": 2, "per_image": 43 / 2}
    assert downloads_per_approved([], moderation) == {"collections": {}, "total": {}}


def test_revenue_per_hour_sums_performance_and_monthly_sales():
    performance = [{"collection_id": "a", "revenue_currency": "USD", "revenue": 10.0, "manual_minutes": 60}]
    sales = [
        {"collection_id": "a", "currency": "USD", "revenue": 20.0},
        {"collection_id": "a", "currency": "EUR", "revenue": 6.0},
        {"collection_id": None, "currency": "USD", "revenue": 10.0},
        {"collection_id": "b", "currency": "USD", "revenue": 4.0},
    ]
    result = revenue_per_hour(performance, {"a": 60}, sales)
    a = result["collections"]["a"]
    assert a["hours"] == 2
    assert a["by_currency"]["USD"] == {"revenue": 30.0, "per_hour": 15.0}
    assert a["by_currency"]["EUR"]["per_hour"] == 3
    assert result["collections"]["b"]["by_currency"]["USD"]["per_hour"] is None  # sem horas
    assert result["collections"][None]["by_currency"]["USD"]["per_hour"] is None
    assert result["total"]["by_currency"]["USD"] == {"revenue": 44.0, "per_hour": 22.0}
    assert revenue_per_hour([], {}, [])["total"]["by_currency"] == {}


def test_weekly_average_starts_at_first_approval_and_skips_current_week():
    rows = [_mod("a", "approved", "2026-10-13T10:00:00+00:00"), _mod("a", "approved", "2026-10-28T10:00:00+00:00")]
    result = approved_per_week(rows, NOW)
    # primeira aprovação na semana de 12/10: completas = 12/10, 19/10 -> 1 aprovada / 2 semanas
    assert result["average"] == pytest.approx(0.5)
    assert result["series"][-1]["complete"] is False and result["series"][-1]["approved"] == 1
    only_current = approved_per_week([_mod("a", "approved", "2026-10-28T10:00:00+00:00")], NOW)
    assert only_current["average"] is None


def test_approved_per_week_across_march_dst():
    now = datetime(2027, 3, 31, 12, 0, tzinfo=UTC)
    rows = [
        _mod("a", "approved", "2027-03-28T21:30:00+00:00"),   # 23:30 CEST de domingo: semana de 22/03
        _mod("a", "approved", "2027-03-28T22:30:00+00:00"),   # 00:30 CEST de segunda 29/03: semana de 29/03
    ]
    series = approved_per_week(rows, now)["series"]
    assert series[-1]["start"].isoformat() == "2027-03-29" and series[-1]["approved"] == 1
    assert series[-2]["start"].isoformat() == "2027-03-22" and series[-2]["approved"] == 1
