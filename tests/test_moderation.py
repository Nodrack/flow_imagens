import pytest

from flowapp.agents import adobe_lessons
from flowapp.database import Database, InvalidTransition
from flowapp.schemas import CollectionBrief, ConceptPlan, WorkflowState


def _plan(name: str) -> ConceptPlan:
    return ConceptPlan(
        concept=name,
        commercial_application="Campanha digital",
        main_subject="Assunto",
        composition_and_copy_space="Assunto à direita, espaço livre à esquerda",
        orientation="horizontal",
        prompt_en=f"Detailed photo of {name}, soft daylight",
        differentiation_rationale="Ângulo distinto",
    )


@pytest.fixture
def env(tmp_path):
    database = Database(tmp_path / "flow.sqlite3")
    collection_id = database.create_collection(
        "Jardim", CollectionBrief(theme="Jardim eletrônico", objective="Imagens comerciais"),
    )
    return database, collection_id


def _concept(database, collection_id, name, state):
    concept_id = database.create_concept(collection_id, _plan(name))
    with database.connect() as connection:
        connection.execute("UPDATE concepts SET state = ? WHERE id = ?", (state.value, concept_id))
    return concept_id


def _legacy_transition(database, concept_id, from_state, to_state, reason, created_at):
    with database.connect() as connection:
        connection.execute(
            "INSERT INTO transitions(concept_id, from_state, to_state, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?)", (concept_id, from_state.value, to_state.value, reason, created_at),
        )


def _records(database):
    with database.connect() as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM moderation_records")]


def test_backfill_creates_records_from_legacy_concepts_and_is_idempotent(tmp_path):
    database = Database(tmp_path / "flow.sqlite3")
    collection_id = database.create_collection(
        "Jardim", CollectionBrief(theme="Jardim eletrônico", objective="Imagens comerciais"),
    )
    approved = _concept(database, collection_id, "Aprovada", WorkflowState.ADOBE_APPROVED)
    rejected = _concept(database, collection_id, "Rejeitada", WorkflowState.ADOBE_REJECTED)
    pending = _concept(database, collection_id, "Pendente", WorkflowState.SUBMITTED)
    _concept(database, collection_id, "Preparada", WorkflowState.PREPARED_FOR_TRANSFER)
    _legacy_transition(
        database, rejected, WorkflowState.SUBMITTED, WorkflowState.ADOBE_REJECTED,
        "Rejeição confirmada no portal: Similar content", "2026-01-02T10:00:00+00:00",
    )
    _legacy_transition(
        database, pending, WorkflowState.TRANSFERRED, WorkflowState.SUBMITTED,
        "Submissão", "2026-01-01T10:00:00+00:00",
    )

    Database(database.path)
    Database(database.path)

    records = {record["concept_id"]: record for record in _records(database)}
    assert len(records) == 3
    assert records[approved]["status"] == "approved"
    assert records[rejected]["status"] == "rejected"
    assert records[rejected]["reason"] == "Similar content"
    assert records[rejected]["decided_at"] == "2026-01-02T10:00:00+00:00"
    assert records[rejected]["title"] == "Rejeitada" and records[rejected]["source"] == "flow"
    assert records[pending]["status"] == "pending"
    assert records[pending]["submitted_at"] == "2026-01-01T10:00:00+00:00"
    feedback = database.adobe_feedback()
    assert (feedback["approved_count"], feedback["rejected_count"], feedback["pending_count"]) == (1, 1, 1)


def test_set_moderation_advances_state_chain_and_records_transitions(env):
    database, collection_id = env
    concept_id = _concept(database, collection_id, "Ingrediente", WorkflowState.PREPARED_FOR_TRANSFER)

    database.set_moderation(concept_id, "approved", adobe_id="123")

    assert database.get_concept(concept_id)["state"] == WorkflowState.ADOBE_APPROVED.value
    history = database.transition_history(concept_id)[-3:]
    assert [item["to_state"] for item in history] == ["transferred", "submitted", "adobe_approved"]
    assert all(item["reason"] == "Registrado na tela de moderação" for item in history)
    record = _records(database)[0]
    assert record["status"] == "approved" and record["adobe_id"] == "123"


def test_set_moderation_rejects_unprepared_concepts(env):
    database, collection_id = env
    concept_id = _concept(database, collection_id, "Cedo demais", WorkflowState.METADATA_READY)
    with pytest.raises(InvalidTransition):
        database.set_moderation(concept_id, "approved")
    assert _records(database) == []


def test_rejection_requires_reason(env):
    database, collection_id = env
    concept_id = _concept(database, collection_id, "Ingrediente", WorkflowState.PREPARED_FOR_TRANSFER)
    with pytest.raises(ValueError, match="motivo"):
        database.set_moderation(concept_id, "rejected", "  ")
    with pytest.raises(ValueError, match="motivo"):
        database.add_external_moderation("Foto", "rejected")
    assert database.get_concept(concept_id)["state"] == WorkflowState.PREPARED_FOR_TRANSFER.value
    assert _records(database) == []


def test_decision_can_be_corrected(env):
    database, collection_id = env
    concept_id = _concept(database, collection_id, "Ingrediente", WorkflowState.TRANSFERRED)
    database.set_moderation(concept_id, "approved")
    database.set_moderation(concept_id, "rejected", "Similar content")

    assert database.get_concept(concept_id)["state"] == WorkflowState.ADOBE_REJECTED.value
    record = _records(database)[0]
    assert record["status"] == "rejected" and record["reason"] == "Similar content"
    assert len(_records(database)) == 1

    database.set_moderation(concept_id, "pending")
    record = _records(database)[0]
    assert record["status"] == "pending" and record["reason"] == "" and record["decided_at"] is None
    assert database.get_concept(concept_id)["state"] == WorkflowState.SUBMITTED.value


def test_external_records_feed_feedback_and_counts(env):
    database, collection_id = env
    database.add_external_moderation("Foto avulsa", "rejected", collection_id, reason="Poor quality")
    database.add_external_moderation("Outra foto", "approved")
    pending_id = database.add_external_moderation("Aguardando", "pending")

    feedback = database.adobe_feedback()
    assert (feedback["approved_count"], feedback["rejected_count"], feedback["pending_count"]) == (1, 1, 1)
    rejected = feedback["rejected"][0]
    assert rejected["concept"] == "Foto avulsa" and rejected["prompt_en"] == ""
    assert rejected["theme"] == "Jardim eletrônico" and rejected["reason"] == "Poor quality"
    assert rejected["created_at"]
    assert feedback["approved"][0]["theme"] == ""
    assert len(database.list_moderation()) == 3
    assert len(database.list_moderation(collection_id)) == 1
    assert len(database.list_moderation(None)) == 2

    database.update_moderation_record(pending_id, status="approved", adobe_id="9")
    assert database.adobe_feedback()["approved_count"] == 2
    with pytest.raises(ValueError):
        database.update_moderation_record(pending_id, status="rejected")


def test_adobe_feedback_keeps_format(env):
    database, collection_id = env
    concept_id = _concept(database, collection_id, "Ingrediente", WorkflowState.SUBMITTED)
    database.transition_concept(concept_id, WorkflowState.ADOBE_REJECTED, "Rejeição confirmada no portal: Similar: x")
    feedback = database.adobe_feedback()
    assert set(feedback) == {"rejected", "approved", "rejected_count", "approved_count", "pending_count"}
    assert feedback["rejected"][0] == {
        "collection_id": collection_id, "theme": "Jardim eletrônico", "concept": "Ingrediente",
        "prompt_en": "Detailed photo of Ingrediente, soft daylight", "reason": "Similar: x",
        "created_at": feedback["rejected"][0]["created_at"],
    }


def test_export_tab_transitions_create_and_update_records(env):
    database, collection_id = env
    concept_id = _concept(database, collection_id, "Ingrediente", WorkflowState.TRANSFERRED)

    database.transition_concept(concept_id, WorkflowState.SUBMITTED, "Submissão confirmada")
    assert _records(database)[0]["status"] == "pending"
    assert _records(database)[0]["submitted_at"]

    database.transition_concept(
        concept_id, WorkflowState.ADOBE_REJECTED, "Rejeição confirmada no portal: Similar content",
    )
    record = _records(database)[0]
    assert record["status"] == "rejected" and record["reason"] == "Similar content"

    database.transition_concept(concept_id, WorkflowState.ADOBE_APPROVED, "Aprovação confirmada no portal")
    assert _records(database)[0]["status"] == "approved" and _records(database)[0]["reason"] == ""
    assert len(_records(database)) == 1


def test_moderation_candidates_report_status(env):
    database, collection_id = env
    _concept(database, collection_id, "Preparada", WorkflowState.PREPARED_FOR_TRANSFER)
    sent = _concept(database, collection_id, "Enviada", WorkflowState.TRANSFERRED)
    _concept(database, collection_id, "Fora do fluxo", WorkflowState.SELECTED)
    database.set_moderation(sent, "pending")

    candidates = database.moderation_candidates(collection_id)
    assert [(item["concept"], item["status"]) for item in candidates] == [
        ("Preparada", None), ("Enviada", "pending"),
    ]


def test_adobe_lessons_mentions_pending():
    feedback = {
        "rejected": [], "approved": [{"concept": "Ok"}], "approved_count": 1,
        "rejected_count": 0, "pending_count": 3,
    }
    assert "Pendentes: 3" in adobe_lessons(feedback, examples=True)
    feedback["pending_count"] = 0
    assert "Pendentes" not in adobe_lessons(feedback, examples=True)


def test_set_moderation_preserves_dates_when_status_unchanged(env):
    database, collection_id = env
    concept_id = _concept(database, collection_id, "Ingrediente", WorkflowState.PREPARED_FOR_TRANSFER)
    database.set_moderation(concept_id, "approved")
    first = _records(database)[0]
    assert first["submitted_at"] and first["decided_at"]

    with database.connect() as connection:
        connection.execute("UPDATE moderation_records SET decided_at = '2026-01-01T00:00:00+00:00'")
    database.set_moderation(concept_id, "approved", adobe_id="77", notes="ok")
    again = _records(database)[0]
    assert again["decided_at"] == "2026-01-01T00:00:00+00:00"
    assert again["submitted_at"] == first["submitted_at"] and again["adobe_id"] == "77"


def test_set_moderation_rolls_back_when_a_step_fails(env, monkeypatch):
    database, collection_id = env
    concept_id = _concept(database, collection_id, "Ingrediente", WorkflowState.PREPARED_FOR_TRANSFER)
    original = Database._move
    calls = []

    def flaky(connection, *args):
        calls.append(args)
        if len(calls) == 2:
            raise RuntimeError("falha no meio")
        original(connection, *args)

    monkeypatch.setattr(Database, "_move", staticmethod(flaky))
    with pytest.raises(RuntimeError):
        database.set_moderation(concept_id, "approved")

    assert database.get_concept(concept_id)["state"] == WorkflowState.PREPARED_FOR_TRANSFER.value
    assert len(database.transition_history(concept_id)) == 1
    assert _records(database) == []


def test_backfill_skips_concepts_already_recorded_by_transition_concept(env):
    database, collection_id = env
    concept_id = _concept(database, collection_id, "Ingrediente", WorkflowState.TRANSFERRED)
    database.transition_concept(concept_id, WorkflowState.SUBMITTED, "Submissão")
    Database(database.path)
    assert len(_records(database)) == 1


def test_rejection_via_transition_requires_reason_and_batch_note_is_saved(env):
    database, collection_id = env
    concept_id = _concept(database, collection_id, "Ingrediente", WorkflowState.SUBMITTED)
    with pytest.raises(ValueError):
        database.transition_concept(concept_id, WorkflowState.ADOBE_REJECTED, "Rejeição confirmada no portal: ")
    assert database.get_concept(concept_id)["state"] == WorkflowState.SUBMITTED.value
    database.transition_concept(
        concept_id, WorkflowState.ADOBE_APPROVED, "Aprovação confirmada no portal: lote de teste",
    )
    assert _records(database)[0]["notes"] == "lote de teste"


def test_delete_collection_keeps_unlinked_records_with_snapshot(env):
    database, collection_id = env
    concept_id = _concept(database, collection_id, "Ingrediente", WorkflowState.PREPARED_FOR_TRANSFER)
    database.set_moderation(concept_id, "rejected", "Similar content")
    database.add_external_moderation("Avulsa", "approved", collection_id)

    database.delete_collection_records(collection_id)

    records = _records(database)
    assert len(records) == 2
    assert all(record["collection_id"] is None and record["concept_id"] is None for record in records)
    assert len(database.list_moderation(None)) == 2
    feedback = database.adobe_feedback()
    assert (feedback["approved_count"], feedback["rejected_count"]) == (1, 1)
    assert feedback["rejected"][0]["theme"] == "Jardim eletrônico"
    assert feedback["rejected"][0]["prompt_en"] == "Detailed photo of Ingrediente, soft daylight"
    assert feedback["approved"][0]["theme"] == "Jardim eletrônico"
    orphan = next(record for record in records if record["status"] == "rejected")
    database.update_moderation_record(orphan["id"], status="approved")
    assert database.adobe_feedback()["approved_count"] == 2


def test_moderation_changes_diff_by_id():
    from flowapp.moderation import moderation_changes

    original = {
        "a": {"title": "A", "status": None, "reason": "", "adobe_id": ""},
        "b": {"title": "B", "status": "approved", "reason": "", "adobe_id": ""},
        "c": {"title": "C", "status": "pending", "reason": "", "adobe_id": "1"},
        "d": {"title": "D", "status": None, "reason": "", "adobe_id": ""},
    }
    edited = {
        "d": {"status": None, "reason": "", "adobe_id": "9"},
        "c": {"status": "pending", "reason": "", "adobe_id": "1"},
        "b": {"status": None, "reason": "", "adobe_id": ""},
        "a": {"status": "rejected", "reason": " Similar ", "adobe_id": ""},
    }
    changes = {item["id"]: item for item in moderation_changes(original, edited)}
    assert set(changes) == {"a", "b", "d"}
    assert changes["a"]["status"] == "rejected" and changes["a"]["reason"] == "Similar"
    assert changes["a"]["error"] is None and changes["a"]["warning"] is None
    assert "não pode voltar" in changes["b"]["error"]
    assert changes["d"]["warning"] and changes["d"]["error"] is None
    assert moderation_changes(original, {}) == []
    assert moderation_changes(original, {key: {**value} for key, value in original.items()}) == []
