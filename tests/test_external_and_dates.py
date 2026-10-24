import sqlite3
from datetime import UTC, date, datetime, timedelta

import pytest

from flowapp.config import AppConfig
from flowapp.database import Database
from flowapp.moderation import local_date, local_noon_utc, moderation_changes
from flowapp.orchestrator import Orchestrator
from flowapp.pipeline import AgentPipeline
from flowapp.schemas import LOCAL_TZ, CollectionBrief, ConceptPlan, WorkflowState

BRIEF = CollectionBrief(theme="Jardim eletrônico", objective="Imagens comerciais")


def _today():
    return datetime.now(LOCAL_TZ).date()


@pytest.fixture
def env(tmp_path):
    database = Database(tmp_path / "flow.sqlite3")
    return database, database.create_collection("Jardim", BRIEF)


def _prepared(database, cid, name="Flor do jardim"):
    plan = ConceptPlan(
        concept=name, commercial_application="Campanha digital", main_subject="Assunto",
        composition_and_copy_space="Assunto à direita, espaço livre à esquerda", orientation="horizontal",
        prompt_en="Detailed photo of flower, soft daylight", differentiation_rationale="Ângulo distinto",
    )
    concept_id = database.create_concept(cid, plan)
    with database.connect() as connection:
        connection.execute(
            "UPDATE concepts SET state = ? WHERE id = ?", (WorkflowState.PREPARED_FOR_TRANSFER.value, concept_id))
    return concept_id


def _record(database, concept_id=None, record_id=None):
    return next(
        row for row in database.list_moderation()
        if (row["concept_id"] == concept_id if concept_id else row["id"] == record_id)
    )


# ---------- coleção externa ----------

def test_kind_migration_marks_existing_sem_colecao_and_is_idempotent(tmp_path):
    path = tmp_path / "old.sqlite3"
    database = Database(path)
    manual = database.create_collection("Sem coleção", CollectionBrief(
        theme="Imagens enviadas fora do fluxo", objective="Criada à mão"))
    normal = database.create_collection("Jardim", BRIEF)
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE collections DROP COLUMN kind")
    database.initialize()
    database.initialize()
    kinds = {item["id"]: item["kind"] for item in database.list_collections()}
    assert kinds == {manual: "external", normal: "normal"}
    assert database.external_collection_id() == manual


def test_kind_migration_does_not_mark_second_when_external_exists(tmp_path):
    database = Database(tmp_path / "flow.sqlite3")
    first = database.external_collection_id()
    duplicate = database.create_collection("Sem coleção", BRIEF)
    database.initialize()
    kinds = {item["id"]: item["kind"] for item in database.list_collections()}
    assert kinds[first] == "external" and kinds[duplicate] == "normal"


def test_external_collection_id_creates_once_and_reuses(tmp_path):
    database = Database(tmp_path / "flow.sqlite3")
    assert database.list_collections() == []
    first = database.external_collection_id()
    assert database.external_collection_id() == first
    collection = database.get_collection(first)
    assert collection["name"] == "Sem coleção" and collection["kind"] == "external"
    assert collection["brief"]["theme"] == "Imagens enviadas fora do fluxo"
    assert len(database.list_collections()) == 1


def test_pipeline_refuses_external_collection(tmp_path):
    orchestrator = Orchestrator(tmp_path, AppConfig())
    try:
        pipeline = AgentPipeline(orchestrator)
        external = orchestrator.database.external_collection_id()
        normal = orchestrator.database.create_collection("Jardim", BRIEF)
        for mode in ("full", "new_image", "review"):
            with pytest.raises(ValueError, match="fora do fluxo"):
                pipeline.create(external, mode, "planner", "vision")
        assert pipeline.create(normal, "full", "planner", "vision")  # coleções normais seguem funcionando
    finally:
        orchestrator.close()


def test_external_collection_cannot_be_removed(tmp_path):
    orchestrator = Orchestrator(tmp_path, AppConfig())
    try:
        external = orchestrator.database.external_collection_id()
        with pytest.raises(ValueError, match="não pode ser removida"):
            orchestrator.delete_collection(external)
        with pytest.raises(ValueError, match="não pode ser removida"):
            orchestrator.database.delete_collection_records(external)
        assert orchestrator.database.get_collection(external) is not None
    finally:
        orchestrator.close()


# ---------- datas no registro ----------

def test_decide_from_unsent_does_not_record_today_as_submission(env):
    database, cid = env
    approved = _prepared(database, cid)
    database.set_moderation(approved, "approved")
    row = _record(database, approved)
    assert row["submitted_at"] is None and local_date(row["decided_at"]) == _today()
    pending = _prepared(database, cid, "Outra flor do jardim")
    database.set_moderation(pending, "pending")
    assert local_date(_record(database, pending)["submitted_at"]) == _today()


def test_explicit_dates_are_stored_as_madrid_noon(env):
    database, cid = env
    concept = _prepared(database, cid)
    sent, decided = _today() - timedelta(days=10), _today() - timedelta(days=3)
    database.set_moderation(concept, "approved", submitted_on=sent, decided_on=decided)
    row = _record(database, concept)
    assert row["submitted_at"] == local_noon_utc(sent) and row["decided_at"] == local_noon_utc(decided)
    assert (local_date(row["submitted_at"]), local_date(row["decided_at"])) == (sent, decided)


def test_date_validation_messages(env):
    database, cid = env
    concept = _prepared(database, cid)
    today = _today()
    with pytest.raises(ValueError, match="decisão não pode estar no futuro"):
        database.set_moderation(concept, "approved", decided_on=today + timedelta(days=1))
    with pytest.raises(ValueError, match="envio não pode estar no futuro"):
        database.set_moderation(concept, "pending", submitted_on=today + timedelta(days=1))
    with pytest.raises(ValueError, match="posterior à data da decisão"):
        database.set_moderation(
            concept, "approved", submitted_on=today - timedelta(days=1), decided_on=today - timedelta(days=5))
    assert database.get_concept(concept)["state"] == WorkflowState.PREPARED_FOR_TRANSFER.value  # validação desfez tudo
    assert database.list_moderation() == []
    external = database.add_external_moderation("Ext", "approved", cid)
    with pytest.raises(ValueError, match="posterior à data da decisão"):
        database.update_moderation_record(
            external, status="approved", submitted_on=today, decided_on=today - timedelta(days=2))


def test_changing_only_the_date_keeps_state_and_other_dates_flow(env):
    database, cid = env
    concept = _prepared(database, cid)
    database.set_moderation(concept, "approved", submitted_on=_today() - timedelta(days=9))
    before = _record(database, concept)
    steps = len(database.transition_history(concept))
    new_decision = _today() - timedelta(days=4)
    database.set_moderation(concept, "approved", decided_on=new_decision)
    after = _record(database, concept)
    assert after["decided_at"] == local_noon_utc(new_decision) and after["submitted_at"] == before["submitted_at"]
    assert database.get_concept(concept)["state"] == WorkflowState.ADOBE_APPROVED.value
    assert len(database.transition_history(concept)) == steps
    # data de envio posterior à decisão existente é recusada
    with pytest.raises(ValueError, match="posterior"):
        database.set_moderation(concept, "approved", submitted_on=_today() - timedelta(days=1))


def test_changing_only_the_date_external(env):
    database, cid = env
    external = database.add_external_moderation("Ext", "rejected", cid, reason="Similar")
    day = _today() - timedelta(days=6)
    database.update_moderation_record(external, status="rejected", reason="Similar", decided_on=day)
    row = _record(database, record_id=external)
    assert row["decided_at"] == local_noon_utc(day) and row["status"] == "rejected"
    database.update_moderation_record(external, status="rejected", reason="Similar", adobe_id="9")
    assert _record(database, record_id=external)["decided_at"] == local_noon_utc(day)  # preservada


def test_pending_external_without_submission_uses_today_and_has_no_decision(env):
    database, cid = env
    external = database.add_external_moderation("Ext", "approved", cid)
    database.update_moderation_record(external, status="pending")
    row = _record(database, record_id=external)
    assert row["decided_at"] is None and local_date(row["submitted_at"]) == _today()


def test_moderation_candidates_expose_dates(env):
    database, cid = env
    concept = _prepared(database, cid)
    database.set_moderation(concept, "approved", decided_on=_today() - timedelta(days=2))
    candidate = database.moderation_candidates(cid)[0]
    assert candidate["submitted_at"] is None and local_date(candidate["decided_at"]) == _today() - timedelta(days=2)


# ---------- diff da tabela ----------

def test_moderation_changes_detects_date_only_edits_and_blocks_clearing():
    day = date(2026, 10, 1)
    base = {"title": "A", "status": "approved", "reason": "", "adobe_id": "", "submitted_on": day, "decided_on": day}
    original = {"a": base, "b": {**base, "title": "B"}, "c": {**base, "title": "C"}, "d": {**base, "title": "D", "status": None,
               "submitted_on": None, "decided_on": None}}
    edited = {
        "a": {**base, "decided_on": date(2026, 10, 5)},        # só a data da decisão
        "b": {**base, "submitted_on": None},                   # apagar data já registrada
        "c": dict(base),                                        # igual
        "d": {"status": None, "reason": "", "adobe_id": "", "submitted_on": day, "decided_on": None},
    }
    changes = {item["id"]: item for item in moderation_changes(original, edited)}
    assert set(changes) == {"a", "b", "d"}
    assert changes["a"]["decided_changed"] and not changes["a"]["submitted_changed"]
    assert changes["a"]["error"] is None and changes["a"]["decided_on"] == date(2026, 10, 5)
    assert "não pode ser apagada" in changes["b"]["error"]
    assert changes["d"]["warning"] and changes["d"]["error"] is None


def test_moderation_changes_without_date_keys_still_works():
    original = {"a": {"title": "A", "status": None, "reason": "", "adobe_id": ""}}
    edited = {"a": {"status": "approved", "reason": "", "adobe_id": ""}}
    [change] = moderation_changes(original, edited)
    assert change["status"] == "approved" and not change["submitted_changed"] and not change["decided_changed"]


def test_utc_helpers_roundtrip_across_dst():
    for day in (date(2026, 3, 29), date(2026, 10, 25), date(2027, 1, 1), date(2026, 7, 14)):
        assert local_date(local_noon_utc(day)) == day
    assert local_noon_utc(date(2026, 7, 14)) == datetime(2026, 7, 14, 10, 0, tzinfo=UTC).isoformat(timespec="seconds")


# ---------- revisão: conversão de células, escopos e migrações ----------

def test_to_date_accepts_str_timestamp_nat_none_and_rejects_other_types():
    import pandas as pd

    from flowapp.moderation import to_date

    assert to_date("2026-10-01") == date(2026, 10, 1)
    assert to_date("2026-10-01T10:00:00+00:00") == date(2026, 10, 1)
    assert to_date("  ") is None and to_date("") is None
    assert to_date(pd.Timestamp("2026-10-02")) == date(2026, 10, 2)
    assert to_date(pd.NaT) is None and to_date(None) is None and to_date(float("nan")) is None
    assert to_date(date(2026, 10, 3)) == date(2026, 10, 3)
    assert to_date(datetime(2026, 10, 4, 15, 0)) == date(2026, 10, 4)
    with pytest.raises(TypeError):
        to_date(5)
    with pytest.raises(ValueError):
        to_date("não é data")


def test_all_empty_date_columns_become_datetime_dtype():
    import pandas as pd

    column = pd.to_datetime(pd.Series([None, None], dtype=object))
    assert str(column.dtype).startswith("datetime64") and column.isna().all()


def test_moderation_scopes_dedupes_and_keeps_orphans_last():
    from flowapp.moderation import moderation_scopes

    assert moderation_scopes("a", "ext") == ["a", "ext", None]
    assert moderation_scopes("ext", "ext") == ["ext", None]
    assert moderation_scopes(None, "ext") == ["ext", None]
    assert moderation_scopes("", "ext") == ["ext", None]


def test_non_date_types_become_value_errors(env):
    database, cid = env
    concept = _prepared(database, cid)
    with pytest.raises(ValueError, match="inválida"):
        database.set_moderation(concept, "approved", decided_on="2026-10-01")
    with pytest.raises(ValueError, match="inválida"):
        database.add_external_moderation("Ext", "approved", cid, submitted_on="2026-10-01")


def test_pending_with_decision_date_is_an_error(env):
    database, cid = env
    concept = _prepared(database, cid)
    with pytest.raises(ValueError, match="pendente não tem data da decisão"):
        database.set_moderation(concept, "pending", decided_on=_today())
    external = database.add_external_moderation("Ext", "approved", cid)
    with pytest.raises(ValueError, match="pendente não tem data da decisão"):
        database.update_moderation_record(external, status="pending", decided_on=_today())
    with pytest.raises(ValueError, match="pendente não tem data da decisão"):
        database.add_external_moderation("Ext2", "pending", cid, decided_on=_today())


def test_back_to_pending_clears_decision_and_is_not_a_diff_error(env):
    database, cid = env
    external = database.add_external_moderation("Ext", "approved", cid, decided_on=_today() - timedelta(days=3))
    database.update_moderation_record(external, status="pending")
    row = _record(database, record_id=external)
    assert row["decided_at"] is None and row["status"] == "pending"
    day = date(2026, 10, 1)
    original = {"a": {"title": "A", "status": "approved", "reason": "", "adobe_id": "",
                      "submitted_on": None, "decided_on": day}}
    edited = {"a": {"status": "pending", "reason": "", "adobe_id": "", "submitted_on": None, "decided_on": None}}
    [change] = moderation_changes(original, edited)
    assert change["error"] is None and change["warning"] is None
    still_approved = {"a": {**edited["a"], "status": "approved"}}
    [bad] = moderation_changes(original, still_approved)
    assert "não pode ser apagada" in bad["error"]


def test_external_registration_validates_and_stores_madrid_noon(env):
    database, cid = env
    sent, decided = _today() - timedelta(days=8), _today() - timedelta(days=2)
    record = database.add_external_moderation("Ext", "approved", cid, submitted_on=sent, decided_on=decided)
    row = _record(database, record_id=record)
    assert row["submitted_at"] == local_noon_utc(sent) and row["decided_at"] == local_noon_utc(decided)
    with pytest.raises(ValueError, match="posterior à data da decisão"):
        database.add_external_moderation("Ext", "approved", cid, submitted_on=decided, decided_on=sent)
    with pytest.raises(ValueError, match="envio não pode estar no futuro"):
        database.add_external_moderation("Ext", "pending", cid, submitted_on=_today() + timedelta(days=1))
    with pytest.raises(ValueError, match="decisão não pode estar no futuro"):
        database.add_external_moderation("Ext", "approved", cid, decided_on=_today() + timedelta(days=1))
    assert len(database.list_moderation()) == 1


SALES = [("2026-08-14", 1.0), ("2026-09-02", 2.0)]


def test_edited_decision_cannot_be_after_first_imported_sale(env):
    database, cid = env
    record = database.add_external_moderation("Ext", "pending", cid)
    database.import_adobe_sales(record, SALES)
    with pytest.raises(ValueError, match="primeira venda importada"):
        database.update_moderation_record(record, status="approved", decided_on=date(2026, 9, 1))
    database.update_moderation_record(record, status="approved", decided_on=date(2026, 8, 10))
    assert local_date(_record(database, record_id=record)["decided_at"]) == date(2026, 8, 10)
    concept = _prepared(database, cid, "Outra flor do jardim")
    database.set_moderation(concept, "pending", submitted_on=date(2026, 8, 1))
    flow_record = _record(database, concept)["id"]
    database.import_adobe_sales(flow_record, SALES)
    with pytest.raises(ValueError, match="primeira venda importada"):
        database.set_moderation(concept, "approved", decided_on=date(2026, 9, 1))


def test_import_new_image_validates_submission_date(env):
    database, cid = env
    image = {"title": "Nova", "adobe_id": "1", "collection_id": cid, "submitted_on": date(2026, 9, 30)}
    with pytest.raises(ValueError, match="posterior à data da decisão"):
        database.import_adobe_sales(None, SALES, new_image=image)
    assert database.list_moderation() == []
    result = database.import_adobe_sales(None, SALES, new_image={**image, "submitted_on": date(2026, 8, 1)})
    row = _record(database, record_id=result["record_id"])
    assert row["submitted_at"] == local_noon_utc(date(2026, 8, 1))


def test_external_collection_works_for_monthly_sales_and_import(tmp_path):
    database = Database(tmp_path / "flow.sqlite3")
    external = database.external_collection_id()
    database.upsert_monthly_sales(external, "2026-07", 5, 3.0, "USD")
    result = database.import_adobe_sales(
        None, SALES, "a.csv",
        new_image={"title": "Nova", "adobe_id": "9", "collection_id": external, "submitted_on": None},
    )
    assert result["created"]
    months = {row["month"]: row for row in database.list_monthly_sales(external)}
    assert set(months) == {"2026-07", "2026-08", "2026-09"}
    assert months["2026-07"]["source"] == "manual" and months["2026-08"]["source"] == "import"
    database.delete_adobe_sales(result["record_id"])
    assert [row["month"] for row in database.list_monthly_sales(external)] == ["2026-07"]


def test_old_database_with_loose_rows_moves_to_external_collection(tmp_path):
    database = Database(tmp_path / "old.sqlite3")
    loose = database.add_external_moderation("Solta", "approved", None)
    database.upsert_monthly_sales(None, "2026-08", 4, 2.0, "USD", "antigo")
    database.upsert_monthly_sales(None, "2026-09", 1, 1.0, "EUR")
    loose_import = database.add_external_moderation("Com ganhos", "pending", None)
    database.import_adobe_sales(loose_import, [("2026-10-20", 3.0)])
    external = database.external_collection_id()
    database.upsert_monthly_sales(external, "2026-08", 10, 5.0, "USD")   # mesmo mês e moeda: soma
    database.upsert_monthly_sales(external, "2026-09", 7, 7.0, "USD")    # moeda diferente: não move
    detached = database.create_collection("Removida", BRIEF)
    kept = database.add_external_moderation("Antiga", "approved", detached)
    database.delete_collection_records(detached)
    database.initialize()
    database.initialize()  # idempotente
    moderation = {row["id"]: row for row in database.list_moderation()}
    assert moderation[loose]["collection_id"] == external and moderation[loose_import]["collection_id"] == external
    assert moderation[kept]["collection_id"] is None and moderation[kept]["detached_from"] == detached
    months = {row["month"]: row for row in database.list_monthly_sales(external)}
    assert months["2026-08"]["source"] == "manual" and months["2026-08"]["downloads"] == 14
    assert months["2026-08"]["revenue"] == pytest.approx(7.0)
    assert "somado de lançamento sem vínculo" in months["2026-08"]["notes"]
    assert months["2026-09"]["downloads"] == 7  # a linha da externa ficou como estava
    assert months["2026-10"]["source"] == "import"  # recalculada na coleção externa
    left = [row for row in database.list_monthly_sales(None) if row["detached_from"] is None]
    assert [(row["month"], row["currency"]) for row in left] == [("2026-09", "EUR")]


def test_database_without_loose_rows_creates_no_external_collection(tmp_path):
    database = Database(tmp_path / "flow.sqlite3")
    database.create_collection("Jardim", BRIEF)
    database.initialize()
    assert [item["kind"] for item in database.list_collections()] == ["normal"]


def test_sem_colecao_with_concepts_is_not_marked_external(tmp_path):
    path = tmp_path / "old.sqlite3"
    database = Database(path)
    real = database.create_collection("Sem coleção", BRIEF)
    _prepared(database, real)
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE collections DROP COLUMN kind")
    database.initialize()
    assert database.get_collection(real)["kind"] == "normal"
    external = database.external_collection_id()
    assert external != real
    created = database.get_collection(external)
    assert created["kind"] == "external" and created["name"] == "Sem coleção (imagens externas)"


def test_external_collection_id_does_not_need_the_write_lock_when_it_exists(tmp_path):
    database = Database(tmp_path / "flow.sqlite3")
    external = database.external_collection_id()
    blocker = sqlite3.connect(database.path, timeout=0.1)
    blocker.execute("BEGIN IMMEDIATE")  # outra transação de escrita aberta
    try:
        assert database.external_collection_id() == external
    finally:
        blocker.rollback()
        blocker.close()


def test_agents_are_blocked_on_external_collection(tmp_path):
    orchestrator = Orchestrator(tmp_path, AppConfig())
    try:
        database = orchestrator.database
        external = database.external_collection_id()
        concept = {"state": "prompts_approved", "id": "x", "data": {}}
        asset = {"role": "candidate", "status": "candidate"}
        with pytest.raises(ValueError, match="fora do fluxo"):
            orchestrator.generate_candidates(external, concept, 1, 512, 512)
        with pytest.raises(ValueError, match="fora do fluxo"):
            orchestrator.upscale_selected(external, concept, asset)
        with pytest.raises(ValueError, match="fora do fluxo"):
            orchestrator.correct_candidate(external, concept, asset, "corrigir")
        with pytest.raises(ValueError, match="fora do fluxo"):
            orchestrator.resume_jobs(external)
        normal = database.create_collection("Jardim", BRIEF)
        assert orchestrator.resume_jobs(normal) == []  # coleção normal segue funcionando
    finally:
        orchestrator.close()


def test_pipeline_execute_and_recover_ignore_external_runs(tmp_path):
    orchestrator = Orchestrator(tmp_path, AppConfig())
    try:
        pipeline = AgentPipeline(orchestrator)
        external = orchestrator.database.external_collection_id()
        # execução antiga, criada antes de a coleção virar externa (contorna o create protegido)
        run_id = pipeline.pipeline.create("full", subject=external, subject_label="Sem coleção", params={})
        with pytest.raises(ValueError, match="fora do fluxo"):
            pipeline.execute(run_id)
        assert pipeline.status(run_id)["cancel_requested"]
        other = pipeline.pipeline.create("full", subject=external, subject_label="Sem coleção", params={})
        pipeline.recover()
        status = pipeline.status(other)
        assert status["cancel_requested"] or status["status"] not in ("queued", "running")
    finally:
        orchestrator.close()
