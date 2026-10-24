import pytest

from flowapp.database import Database, ImportedMonthError
from flowapp.metrics import approval_rate, downloads_per_approved, revenue_per_hour
from flowapp.sales_import import parse_adobe_earnings, with_occurrences
from flowapp.schemas import CollectionBrief

BRIEF = CollectionBrief(theme="Jardim eletrônico", objective="Imagens comerciais")
SALES = [("2026-08-14", 1.04489902433), ("2026-08-20", 0.5), ("2026-09-02", 2.0)]


@pytest.fixture
def env(tmp_path):
    database = Database(tmp_path / "flow.sqlite3")
    cid = database.create_collection("Jardim", BRIEF)
    pending = database.add_external_moderation("Rosa", "pending", cid, adobe_id="111")
    return database, cid, pending


def test_parser_tolerates_bom_crlf_blank_lines_and_header():
    content = "﻿data,valor\r\n2024-04-14,1.04489902433\r\n\r\n2024-04-15,0.25\r\n".encode("utf-8")
    result = parse_adobe_earnings(content)
    assert result.errors == []
    assert result.sales == [("2024-04-14", 1.04489902433), ("2024-04-15", 0.25)]
    assert result.months == ["2024-04"]
    assert result.total == pytest.approx(1.29489902433)


def test_parser_bytes_with_bom_and_empty():
    assert parse_adobe_earnings(b"\xef\xbb\xbf2024-04-14,1.5\n").sales == [("2024-04-14", 1.5)]
    empty = parse_adobe_earnings("")
    assert empty.sales == [] and empty.errors == []
    assert parse_adobe_earnings("\n\r\n  \n").sales == []


def test_parser_lists_invalid_lines_without_aborting():
    result = parse_adobe_earnings(
        "2024-04-14,1.0\nabc,1.0\n2024-13-40,1.0\n2024-04-16,x\n2024-04-17,-2\n2024-04-18,nan\n2024-04-19\n2024-04-20,3.0\n"
    )
    assert result.sales == [("2024-04-14", 1.0), ("2024-04-20", 3.0)]
    assert len(result.errors) == 6
    assert result.errors[0].startswith("Linha 2")


def test_with_occurrences_numbers_repeats():
    assert with_occurrences([("2026-08-14", 1.0), ("2026-08-14", 1.0), ("2026-08-14", 2.0)]) == [
        ("2026-08-14", 1.0, 1), ("2026-08-14", 1.0, 2), ("2026-08-14", 2.0, 1)]


def test_import_is_idempotent_and_counts(env):
    database, cid, record = env
    first = database.import_adobe_sales(record, SALES, "earnings.csv")
    assert (first["new"], first["existing"]) == (3, 0)
    again = database.import_adobe_sales(record, SALES, "earnings (1).csv")
    assert (again["new"], again["existing"]) == (0, 3)
    assert database.count_existing_adobe_sales(record, SALES) == 3
    summary = database.list_adobe_sales_summary()
    assert summary[0]["sales"] == 3 and summary[0]["revenue"] == pytest.approx(3.54489902433)


def test_repeated_sales_same_day_are_kept(env):
    database, cid, record = env
    sales = [("2026-08-14", 1.0), ("2026-08-14", 1.0), ("2026-08-14", 1.0)]
    assert database.import_adobe_sales(record, sales)["new"] == 3
    assert database.import_adobe_sales(record, sales)["new"] == 0
    month = database.list_monthly_sales(cid)[0]
    assert (month["downloads"], month["revenue"]) == (3, 3.0)


def test_monthly_rows_sum_two_images_in_same_collection(env):
    database, cid, record = env
    other = database.add_external_moderation("Tulipa", "approved", cid, adobe_id="222")
    database.import_adobe_sales(record, [("2026-08-14", 1.0), ("2026-08-15", 2.0)])
    database.import_adobe_sales(other, [("2026-08-20", 4.0), ("2026-09-01", 1.0)])
    rows = {row["month"]: row for row in database.list_monthly_sales(cid)}
    assert (rows["2026-08"]["downloads"], rows["2026-08"]["revenue"]) == (3, 7.0)
    assert (rows["2026-09"]["downloads"], rows["2026-09"]["currency"], rows["2026-09"]["source"]) == (1, "USD", "import")


def test_conflict_with_manual_keep_then_replace(env):
    database, cid, record = env
    database.upsert_monthly_sales(cid, "2026-08", 99, 50.0, "EUR", "conferido")
    assert [row["month"] for row in database.import_conflicts(["2026-08", "2026-09"], record_id=record)] == ["2026-08"]
    assert [row["month"] for row in database.import_conflicts(["2026-08"], collection_id=cid)] == ["2026-08"]
    kept = database.import_adobe_sales(record, SALES)
    assert kept["conflict_months"] == ["2026-08"] and not kept["replaced_manual"]
    rows = {row["month"]: row for row in database.list_monthly_sales(cid)}
    assert (rows["2026-08"]["downloads"], rows["2026-08"]["source"]) == (99, "manual")
    assert rows["2026-09"]["source"] == "import"
    replaced = database.import_adobe_sales(record, SALES, replace_manual=True)
    assert replaced["replaced_manual"]
    rows = {row["month"]: row for row in database.list_monthly_sales(cid)}
    assert (rows["2026-08"]["downloads"], rows["2026-08"]["source"], rows["2026-08"]["currency"]) == (2, "import", "USD")


def test_editing_imported_month_needs_confirmation_and_becomes_manual(env):
    database, cid, record = env
    database.import_adobe_sales(record, SALES)
    with pytest.raises(ImportedMonthError):
        database.upsert_monthly_sales(cid, "2026-09", 5, 5.0, "USD")
    database.upsert_monthly_sales(cid, "2026-09", 5, 5.0, "USD", confirm_import_override=True)
    row = next(row for row in database.list_monthly_sales(cid) if row["month"] == "2026-09")
    assert (row["downloads"], row["source"]) == (5, "manual")
    database.import_adobe_sales(record, SALES)  # manual não é sobrescrito em silêncio
    row = next(row for row in database.list_monthly_sales(cid) if row["month"] == "2026-09")
    assert row["downloads"] == 5


def test_import_approves_pending_record_with_decided_at_on_first_sale(env):
    database, cid, record = env
    result = database.import_adobe_sales(record, SALES)
    assert result["marked_approved"]
    row = next(row for row in database.list_moderation() if row["id"] == record)
    assert row["status"] == "approved" and row["decided_at"] == "2026-08-14T00:00:00+00:00"
    again = database.import_adobe_sales(record, SALES)
    assert not again["marked_approved"]


def test_import_keeps_earlier_decided_at_of_approved_record(env):
    database, cid, _ = env
    record = database.add_external_moderation("Lírio", "approved", cid, decided_at="2026-07-01T10:00:00+00:00")
    database.import_adobe_sales(record, SALES)
    row = next(row for row in database.list_moderation() if row["id"] == record)
    assert row["decided_at"] == "2026-07-01T10:00:00+00:00"


def test_find_moderation_by_adobe_id(env):
    database, cid, record = env
    assert database.find_moderation_by_adobe_id(" 111 ")["id"] == record
    assert database.find_moderation_by_adobe_id("") is None
    assert database.find_moderation_by_adobe_id("999") is None


def test_removed_collection_keeps_imported_sales_on_removed_row(env):
    database, cid, record = env
    database.import_adobe_sales(record, SALES)
    database.delete_collection_records(cid)
    rows = database.list_monthly_sales(None)
    assert {row["month"] for row in rows} == {"2026-08", "2026-09"}
    assert all(row["detached_from"] == cid and row["collection_name"] == "Jardim" and row["source"] == "import" for row in rows)
    summary = database.list_adobe_sales_summary()
    assert summary[0]["sales"] == 3 and summary[0]["collection_name"] == "Jardim"
    # nova importação para a imagem desvinculada continua na linha da removida
    database.import_adobe_sales(record, SALES + [("2026-08-30", 1.0)])
    august = next(row for row in database.list_monthly_sales(None) if row["month"] == "2026-08")
    assert august["downloads"] == 3 and august["detached_from"] == cid


def test_metrics_reflect_imported_sales(env):
    database, cid, record = env
    database.import_adobe_sales(record, SALES)  # aprovada em 2026-08-14
    database.add_work_session(cid, "2026-08-15", 60)
    moderation = database.list_moderation()
    sales = database.list_monthly_sales()
    per_image = downloads_per_approved(sales, moderation)
    assert per_image["collections"][cid]["2026-08"] == {"downloads": 2, "approved": 1, "per_image": 2}
    assert approval_rate(moderation)["total"]["rate"] == 1
    result = revenue_per_hour(
        database.performance_comparison(), database.work_minutes_by_collection(), sales,
        database.list_archived_work(),
    )
    assert result["total"]["by_currency"]["USD"]["revenue"] == pytest.approx(3.54489902433)
    assert result["total"]["by_currency"]["USD"]["per_hour"] == pytest.approx(3.54489902433)


def test_import_rejects_empty_and_unknown_record(env):
    database, _, record = env
    with pytest.raises(ValueError):
        database.import_adobe_sales(record, [])
    with pytest.raises(KeyError):
        database.import_adobe_sales("nope", SALES)


def test_import_marks_flow_concept_approved_through_state_chain(env):
    from flowapp.schemas import ConceptPlan, WorkflowState

    database, cid, _ = env
    plan = ConceptPlan(
        concept="Flor do jardim", commercial_application="Campanha digital", main_subject="Assunto",
        composition_and_copy_space="Assunto à direita, espaço livre à esquerda", orientation="horizontal",
        prompt_en="Detailed photo of flower, soft daylight", differentiation_rationale="Ângulo distinto",
    )
    concept_id = database.create_concept(cid, plan)
    with database.connect() as connection:
        connection.execute(
            "UPDATE concepts SET state = ? WHERE id = ?", (WorkflowState.PREPARED_FOR_TRANSFER.value, concept_id))
    database.set_moderation(concept_id, "pending")
    record = next(row for row in database.list_moderation() if row["concept_id"] == concept_id)
    result = database.import_adobe_sales(record["id"], SALES)
    assert result["marked_approved"]
    assert database.get_concept(concept_id)["state"] == WorkflowState.ADOBE_APPROVED.value
    row = next(row for row in database.list_moderation() if row["concept_id"] == concept_id)
    assert row["status"] == "approved" and row["decided_at"] == "2026-08-14T00:00:00+00:00"


def _count_sales(database):
    with database.connect() as connection:
        return connection.execute("SELECT COUNT(*) FROM adobe_sales").fetchone()[0]


def _prepared_flow_record(database, cid, name="Flor do jardim", reject=False):
    from flowapp.schemas import ConceptPlan, WorkflowState

    plan = ConceptPlan(
        concept=name, commercial_application="Campanha digital", main_subject="Assunto",
        composition_and_copy_space="Assunto à direita, espaço livre à esquerda", orientation="horizontal",
        prompt_en="Detailed photo of flower, soft daylight", differentiation_rationale="Ângulo distinto",
    )
    concept_id = database.create_concept(cid, plan)
    with database.connect() as connection:
        connection.execute(
            "UPDATE concepts SET state = ? WHERE id = ?", (WorkflowState.PREPARED_FOR_TRANSFER.value, concept_id))
    if reject:
        database.set_moderation(concept_id, "rejected", reason="Conteúdo similar")
    else:
        database.set_moderation(concept_id, "pending")
    record = next(row for row in database.list_moderation() if row["concept_id"] == concept_id)
    return concept_id, record["id"]


def test_decided_at_moves_back_to_first_sale_even_if_already_approved(env):
    database, cid, _ = env
    record = database.add_external_moderation("Hoje", "approved", cid)  # decided_at = agora
    database.import_adobe_sales(record, [("2024-04-14", 1.0), ("2024-05-01", 1.0)])
    row = next(row for row in database.list_moderation() if row["id"] == record)
    assert row["status"] == "approved" and row["decided_at"] == "2024-04-14T00:00:00+00:00"


def test_decided_at_empty_on_approved_is_filled(env):
    database, cid, _ = env
    record = database.add_external_moderation("Sem data", "approved", cid)
    with database.connect() as connection:
        connection.execute("UPDATE moderation_records SET decided_at = NULL WHERE id = ?", (record,))
    database.import_adobe_sales(record, SALES)
    row = next(row for row in database.list_moderation() if row["id"] == record)
    assert row["decided_at"] == "2026-08-14T00:00:00+00:00"


def test_rejected_becomes_approved_for_external_and_flow(env):
    database, cid, _ = env
    external = database.add_external_moderation("Rej", "rejected", cid, reason="Similar")
    database.import_adobe_sales(external, SALES)
    _, flow = _prepared_flow_record(database, cid, reject=True)
    database.import_adobe_sales(flow, SALES)
    statuses = {row["id"]: row for row in database.list_moderation()}
    assert statuses[external]["status"] == "approved" and statuses[external]["reason"] == ""
    assert statuses[flow]["status"] == "approved"
    assert statuses[flow]["decided_at"] == "2026-08-14T00:00:00+00:00"


def test_rollback_when_state_transition_fails(env):
    from flowapp.database import InvalidTransition

    database, cid, _ = env
    concept_id, record = _prepared_flow_record(database, cid)
    with database.connect() as connection:
        connection.execute("UPDATE concepts SET state = 'concepts_ready' WHERE id = ?", (concept_id,))
    with pytest.raises(InvalidTransition):
        database.import_adobe_sales(record, SALES)
    assert _count_sales(database) == 0
    assert database.list_monthly_sales(cid) == []


def test_new_image_and_import_are_atomic_and_reuse_adobe_id(env):
    database, cid, _ = env
    image = {"title": "Nova", "adobe_id": "777", "collection_id": cid, "submitted_at": None}
    first = database.import_adobe_sales(None, SALES, "a.csv", new_image=image)
    assert first["created"] and first["new"] == 3
    second = database.import_adobe_sales(None, SALES, "a.csv", new_image=image)
    assert not second["created"] and second["record_id"] == first["record_id"] and second["new"] == 0
    assert len([row for row in database.list_moderation() if row["adobe_id"] == "777"]) == 1
    before = len(database.list_moderation())
    with pytest.raises(KeyError):
        database.import_adobe_sales(None, SALES, new_image={**image, "adobe_id": "888", "collection_id": "x"})
    with pytest.raises(ValueError):
        database.import_adobe_sales(None, SALES, new_image={**image, "adobe_id": "", "title": "  "})
    assert len(database.list_moderation()) == before
    row = next(row for row in database.list_moderation() if row["id"] == first["record_id"])
    assert row["decided_at"] == "2026-08-14T00:00:00+00:00" and row["source"] == "external"


def test_reimport_with_new_and_repeated_sales_in_between(env):
    database, cid, record = env
    database.import_adobe_sales(record, [("2026-08-14", 1.0), ("2026-08-16", 2.0)])
    result = database.import_adobe_sales(
        record, [("2026-08-14", 1.0), ("2026-08-15", 3.0), ("2026-08-16", 2.0), ("2026-08-16", 2.0)])
    assert (result["new"], result["existing"]) == (2, 2)
    month = database.list_monthly_sales(cid)[0]
    assert (month["downloads"], month["revenue"]) == (4, 8.0)


def test_amount_micros_ignores_float_noise(env):
    database, cid, record = env
    database.import_adobe_sales(record, [("2026-08-14", 0.1 + 0.2)])
    again = database.import_adobe_sales(record, [("2026-08-14", 0.3)])
    assert again["new"] == 0 and database.count_existing_adobe_sales(record, [("2026-08-14", 0.3)]) == 1


def test_two_images_same_date_and_value_both_count(env):
    database, cid, record = env
    other = database.add_external_moderation("Outra", "approved", cid)
    database.import_adobe_sales(record, [("2026-08-14", 1.0)])
    database.import_adobe_sales(other, [("2026-08-14", 1.0)])
    assert database.list_monthly_sales(cid)[0]["downloads"] == 2


def test_no_collection_import_and_conflict(env):
    database, _, _ = env
    loose = database.add_external_moderation("Solta", "pending", None)
    database.upsert_monthly_sales(None, "2026-08", 10, 5.0, "USD")
    assert [row["month"] for row in database.import_conflicts(["2026-08"], record_id=loose)] == ["2026-08"]
    assert [row["month"] for row in database.import_conflicts(["2026-08"], collection_id=None)] == ["2026-08"]
    database.import_adobe_sales(loose, SALES)
    rows = {row["month"]: row for row in database.list_monthly_sales(None)}
    assert rows["2026-08"]["source"] == "manual" and rows["2026-09"]["source"] == "import"
    database.import_adobe_sales(loose, SALES, replace_manual={"2026-08"})
    august = next(row for row in database.list_monthly_sales(None) if row["month"] == "2026-08")
    assert august["source"] == "import" and "manual anterior: 10 downloads, 5.00 USD" in august["notes"]


def test_single_decision_for_two_files_same_collection_and_month(env):
    database, cid, record = env
    other = database.add_external_moderation("Outra", "approved", cid)
    database.upsert_monthly_sales(cid, "2026-08", 10, 5.0, "USD")
    first = database.import_adobe_sales(record, [("2026-08-14", 1.0)], replace_manual={"2026-08"})
    second = database.import_adobe_sales(other, [("2026-08-20", 2.0)], replace_manual={"2026-08"})
    assert first["replaced_months"] == ["2026-08"] and second["conflict_months"] == []
    month = database.list_monthly_sales(cid)[0]
    assert (month["downloads"], month["revenue"], month["source"]) == (2, 3.0, "import")
    # só os meses escolhidos são trocados
    database.upsert_monthly_sales(cid, "2026-09", 7, 7.0, "USD")
    kept = database.import_adobe_sales(record, [("2026-09-02", 1.0)], replace_manual=set())
    assert kept["replaced_months"] == [] and kept["conflict_months"] == ["2026-09"]


def test_delete_adobe_sales_recalculates_and_keeps_moderation(env):
    database, cid, record = env
    database.import_adobe_sales(record, [("2026-08-14", 1.0)], "a.csv")
    database.import_adobe_sales(record, [("2026-09-02", 2.0)], "b.csv")
    assert database.delete_adobe_sales(record, "a.csv") == 1
    assert [row["month"] for row in database.list_monthly_sales(cid)] == ["2026-09"]
    assert database.delete_adobe_sales(record) == 1
    assert database.list_monthly_sales(cid) == [] and database.list_adobe_sales_summary() == []
    row = next(row for row in database.list_moderation() if row["id"] == record)
    assert row["status"] == "approved"  # moderação intacta


def test_delete_monthly_sales_recalculates_imports(env):
    database, cid, record = env
    database.import_adobe_sales(record, [("2026-08-14", 1.0)])
    imported = database.list_monthly_sales(cid)[0]
    database.delete_monthly_sales(imported["id"])
    again = database.list_monthly_sales(cid)
    assert len(again) == 1 and again[0]["source"] == "import" and again[0]["downloads"] == 1
    manual = database.upsert_monthly_sales(cid, "2026-09", 3, 3.0, "USD")
    database.import_adobe_sales(record, [("2026-09-02", 1.0)])  # conflito: manual mantido
    database.delete_monthly_sales(manual)
    september = next(row for row in database.list_monthly_sales(cid) if row["month"] == "2026-09")
    assert september["source"] == "import"


def test_removing_one_collection_does_not_touch_other_imports(env):
    database, cid, record = env
    other_cid = database.create_collection("Cidade", BRIEF)
    other = database.add_external_moderation("Rua", "approved", other_cid)
    database.import_adobe_sales(record, SALES)
    database.import_adobe_sales(other, [("2026-08-02", 9.0)])
    database.delete_collection_records(cid)
    live = database.list_monthly_sales(other_cid)
    assert [(row["month"], row["downloads"], row["revenue"]) for row in live] == [("2026-08", 1, 9.0)]
    assert {row["detached_from"] for row in database.list_monthly_sales(None)} == {cid}


def test_migration_adds_source_and_amount_micros(tmp_path):
    import sqlite3

    path = tmp_path / "old.sqlite3"
    database = Database(path)
    cid = database.create_collection("Jardim", BRIEF)
    record = database.add_external_moderation("Rosa", "approved", cid)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE adobe_sales")
        connection.execute(
            "CREATE TABLE adobe_sales (id TEXT PRIMARY KEY, moderation_record_id TEXT NOT NULL, "
            "sale_date TEXT NOT NULL, amount REAL NOT NULL, currency TEXT NOT NULL DEFAULT 'USD', "
            "occurrence INTEGER NOT NULL DEFAULT 1, source_file TEXT NOT NULL DEFAULT '', "
            "imported_at TEXT NOT NULL, UNIQUE(moderation_record_id, sale_date, amount, occurrence))")
        connection.execute(
            "INSERT INTO adobe_sales VALUES ('a', ?, '2026-08-14', 1.04489902433, 'USD', 1, 'x', 't')", (record,))
        connection.execute("DROP INDEX ux_monthly_sales_month")
        connection.execute("ALTER TABLE monthly_sales DROP COLUMN source")
    database.initialize()
    database.initialize()
    assert database.count_existing_adobe_sales(record, [("2026-08-14", 1.04489902433)]) == 1
    assert database.import_adobe_sales(record, [("2026-08-14", 1.04489902433)])["new"] == 0
    assert database.list_monthly_sales(cid)[0]["source"] == "import"
