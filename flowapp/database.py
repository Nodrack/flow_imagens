from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from flowapp.schemas import (
    CollectionBrief, ConceptPlan, PerformanceEntry, STATE_TRANSITIONS, WorkflowState,
)


class InvalidTransition(ValueError):
    pass


class DuplicateJobError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _merge_review(review_json: str, decision: str, reason: str) -> str:
    """Registra a decisão humana sem apagar o parecer do revisor visual."""
    try:
        review = json.loads(review_json or "{}")
    except json.JSONDecodeError:
        review = {}
    if not isinstance(review, dict):
        review = {}
    review.update({"human_decision": decision, "reason": reason.strip()})
    return json.dumps(review, ensure_ascii=False)



class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS collections (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, brief_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS concepts (
                    id TEXT PRIMARY KEY, collection_id TEXT NOT NULL REFERENCES collections(id),
                    data_json TEXT NOT NULL, state TEXT NOT NULL, selected_asset_id TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assets (
                    id TEXT PRIMARY KEY, collection_id TEXT NOT NULL REFERENCES collections(id),
                    concept_id TEXT NOT NULL REFERENCES concepts(id), role TEXT NOT NULL,
                    status TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL UNIQUE,
                    width INTEGER NOT NULL, height INTEGER NOT NULL, size_bytes INTEGER NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}', review_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, idempotency_key TEXT NOT NULL UNIQUE,
                    collection_id TEXT NOT NULL REFERENCES collections(id),
                    concept_id TEXT NOT NULL REFERENCES concepts(id), kind TEXT NOT NULL,
                    status TEXT NOT NULL, comfy_prompt_id TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
                    duration_ms INTEGER, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_runs (
                    id TEXT PRIMARY KEY, collection_id TEXT NOT NULL REFERENCES collections(id),
                    concept_id TEXT, agent_name TEXT NOT NULL, model_id TEXT NOT NULL,
                    instruction_version TEXT NOT NULL, status TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
                    duration_ms INTEGER NOT NULL, attempts INTEGER NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    concept_id TEXT NOT NULL REFERENCES concepts(id),
                    from_state TEXT NOT NULL, to_state TEXT NOT NULL, reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS performance_records (
                    id TEXT PRIMARY KEY, collection_id TEXT NOT NULL REFERENCES collections(id),
                    concept_id TEXT REFERENCES concepts(id), report_date TEXT NOT NULL,
                    download_count INTEGER NOT NULL, revenue REAL NOT NULL,
                    revenue_currency TEXT NOT NULL, moderation_status TEXT NOT NULL,
                    generation_seconds REAL NOT NULL, upscale_seconds REAL NOT NULL,
                    manual_minutes REAL NOT NULL, user_cost REAL NOT NULL,
                    cost_currency TEXT NOT NULL, notes TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_concepts_collection ON concepts(collection_id, created_at);
                CREATE INDEX IF NOT EXISTS ix_assets_concept ON assets(concept_id, created_at);
                CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(status, created_at);
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(collections)")}
            if "planning_json" not in columns:
                connection.execute(
                    "ALTER TABLE collections ADD COLUMN planning_json TEXT NOT NULL DEFAULT '{}'"
                )
            asset_columns = {row["name"] for row in connection.execute("PRAGMA table_info(assets)")}
            if "export_json" not in asset_columns:
                # Registro do JPEG exportado (arquivo, dimensões, avisos); separado dos metadados
                # para que editar título/palavras-chave não apague o registro do pacote.
                connection.execute(
                    "ALTER TABLE assets ADD COLUMN export_json TEXT NOT NULL DEFAULT '{}'"
                )

    @staticmethod
    def _decode(row: sqlite3.Row | None, json_fields: tuple[str, ...] = ()) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for field in json_fields:
            result[field.removesuffix("_json")] = json.loads(result.pop(field))
        return result

    def create_collection(self, name: str, brief: CollectionBrief) -> str:
        collection_id = str(uuid.uuid4())
        timestamp = utc_now()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO collections(id, name, brief_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (collection_id, name.strip(), brief.model_dump_json(), timestamp, timestamp),
            )
        return collection_id

    def list_collections(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM collections ORDER BY created_at DESC").fetchall()
        return [self._decode(row, ("brief_json", "planning_json")) or {} for row in rows]

    def get_collection(self, collection_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM collections WHERE id = ?", (collection_id,)).fetchone()
        return self._decode(row, ("brief_json", "planning_json"))

    def update_collection_planning(self, collection_id: str, planning: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE collections SET planning_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(planning, ensure_ascii=False), utc_now(), collection_id),
            )

    def delete_collection_records(self, collection_id: str) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            collection = connection.execute(
                "SELECT id FROM collections WHERE id = ?", (collection_id,)
            ).fetchone()
            if collection is None:
                raise KeyError("Coleção não encontrada.")
            active_job = connection.execute(
                "SELECT id FROM jobs WHERE collection_id = ? "
                "AND status IN ('queued', 'running') LIMIT 1",
                (collection_id,),
            ).fetchone()
            if active_job:
                raise ValueError("Não é possível remover uma coleção com trabalhos ComfyUI ativos.")
            # Tabelas de agentes de versões anteriores (hoje no banco central do agentkit).
            existing = {
                row["name"] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            if "pipeline_runs" in existing:
                for table in ("pipeline_events", "agent_status"):
                    if table in existing:
                        connection.execute(
                            f"DELETE FROM {table} WHERE run_id IN "
                            "(SELECT id FROM pipeline_runs WHERE collection_id = ?)",
                            (collection_id,),
                        )
                connection.execute("DELETE FROM pipeline_runs WHERE collection_id = ?", (collection_id,))
            connection.execute(
                "DELETE FROM performance_records WHERE collection_id = ?", (collection_id,)
            )
            connection.execute("DELETE FROM agent_runs WHERE collection_id = ?", (collection_id,))
            connection.execute("DELETE FROM jobs WHERE collection_id = ?", (collection_id,))
            connection.execute(
                "DELETE FROM transitions WHERE concept_id IN "
                "(SELECT id FROM concepts WHERE collection_id = ?)",
                (collection_id,),
            )
            connection.execute("DELETE FROM assets WHERE collection_id = ?", (collection_id,))
            connection.execute("DELETE FROM concepts WHERE collection_id = ?", (collection_id,))
            connection.execute("DELETE FROM collections WHERE id = ?", (collection_id,))

    def create_concept(self, collection_id: str, plan: ConceptPlan) -> str:
        concept_id = str(uuid.uuid4())
        timestamp = utc_now()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO concepts(id, collection_id, data_json, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (concept_id, collection_id, plan.model_dump_json(), WorkflowState.CONCEPTS_READY.value,
                 timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO transitions(concept_id, from_state, to_state, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (concept_id, WorkflowState.DRAFT.value, WorkflowState.CONCEPTS_READY.value,
                 "Conceito criado e validado", timestamp),
            )
        return concept_id

    def get_concept(self, concept_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM concepts WHERE id = ?", (concept_id,)).fetchone()
        return self._decode(row, ("data_json",))

    def list_concepts(self, collection_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM concepts WHERE collection_id = ? ORDER BY created_at", (collection_id,)
            ).fetchall()
        return [self._decode(row, ("data_json",)) or {} for row in rows]

    def update_concept_plan(self, concept_id: str, plan: ConceptPlan) -> None:
        with self.connect() as connection:
            row = connection.execute("SELECT state FROM concepts WHERE id = ?", (concept_id,)).fetchone()
            if row is None:
                raise KeyError("Conceito não encontrado.")
            if row["state"] not in {WorkflowState.DRAFT.value, WorkflowState.CONCEPTS_READY.value}:
                raise InvalidTransition("O prompt não pode ser editado após aprovação.")
            connection.execute(
                "UPDATE concepts SET data_json = ?, updated_at = ? WHERE id = ?",
                (plan.model_dump_json(), utc_now(), concept_id),
            )

    def revise_concept_prompt(self, concept_id: str, plan: ConceptPlan) -> None:
        """Atualiza o prompt de um conceito ainda em geração/escolha (recriação por agente)."""
        editable = {
            WorkflowState.DRAFT.value, WorkflowState.CONCEPTS_READY.value,
            WorkflowState.PROMPTS_APPROVED.value, WorkflowState.CANDIDATES_READY.value,
            WorkflowState.FAILED.value, WorkflowState.CANCELLED.value,
        }
        with self.connect() as connection:
            row = connection.execute("SELECT state FROM concepts WHERE id = ?", (concept_id,)).fetchone()
            if row is None:
                raise KeyError("Conceito não encontrado.")
            if row["state"] not in editable:
                raise InvalidTransition("O prompt não pode ser revisado após a seleção da imagem.")
            connection.execute(
                "UPDATE concepts SET data_json = ?, updated_at = ? WHERE id = ?",
                (plan.model_dump_json(), utc_now(), concept_id),
            )

    def transition_concept(self, concept_id: str, target: WorkflowState, reason: str) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state FROM concepts WHERE id = ?", (concept_id,)).fetchone()
            if row is None:
                raise KeyError("Conceito não encontrado.")
            current = WorkflowState(row["state"])
            if target not in STATE_TRANSITIONS[current]:
                raise InvalidTransition(f"Transição não permitida: {current.value} -> {target.value}.")
            timestamp = utc_now()
            connection.execute(
                "UPDATE concepts SET state = ?, updated_at = ? WHERE id = ?",
                (target.value, timestamp, concept_id),
            )
            connection.execute(
                "INSERT INTO transitions(concept_id, from_state, to_state, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (concept_id, current.value, target.value, reason.strip(), timestamp),
            )

    def transition_history(self, concept_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT from_state, to_state, reason, created_at FROM transitions "
                "WHERE concept_id = ? ORDER BY id", (concept_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def adobe_feedback(self, limit: int = 20) -> dict[str, Any]:
        """Resultados da moderação da Adobe em todas as coleções, para os agentes aprenderem."""
        with self.connect() as connection:
            rejected = connection.execute(
                "SELECT t.reason, t.created_at, c.collection_id, c.data_json, col.brief_json "
                "FROM transitions t JOIN concepts c ON c.id = t.concept_id "
                "JOIN collections col ON col.id = c.collection_id "
                "WHERE t.to_state = ? ORDER BY t.id DESC LIMIT ?",
                (WorkflowState.ADOBE_REJECTED.value, limit),
            ).fetchall()
            approved = connection.execute(
                "SELECT c.collection_id, c.data_json, col.brief_json FROM concepts c "
                "JOIN collections col ON col.id = c.collection_id "
                "WHERE c.state = ? ORDER BY c.updated_at DESC LIMIT ?",
                (WorkflowState.ADOBE_APPROVED.value, limit),
            ).fetchall()
            counts = {
                row["state"]: row["total"] for row in connection.execute(
                    "SELECT state, COUNT(*) AS total FROM concepts WHERE state IN (?, ?) GROUP BY state",
                    (WorkflowState.ADOBE_APPROVED.value, WorkflowState.ADOBE_REJECTED.value),
                )
            }

        def entry(row: sqlite3.Row) -> dict[str, Any]:
            data = json.loads(row["data_json"])
            return {
                "collection_id": row["collection_id"],
                "theme": json.loads(row["brief_json"]).get("theme", ""),
                "concept": data.get("concept", ""),
                "prompt_en": data.get("prompt_en", ""),
            }

        return {
            # O motivo é gravado como "Rejeição confirmada no portal: <motivo>".
            "rejected": [
                {**entry(row), "reason": row["reason"].split(":", 1)[-1].strip(), "created_at": row["created_at"]}
                for row in rejected
            ],
            "approved": [entry(row) for row in approved],
            "rejected_count": counts.get(WorkflowState.ADOBE_REJECTED.value, 0),
            "approved_count": counts.get(WorkflowState.ADOBE_APPROVED.value, 0),
        }

    def create_job(
        self, collection_id: str, concept_id: str, kind: str,
        idempotency_key: str, payload: dict[str, Any],
    ) -> str:
        job_id = str(uuid.uuid4())
        timestamp = utc_now()
        try:
            with self.connect() as connection:
                connection.execute(
                    "INSERT INTO jobs(id, idempotency_key, collection_id, concept_id, kind, status, "
                    "comfy_prompt_id, payload_json, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
                    (job_id, idempotency_key, collection_id, concept_id, kind, job_id,
                     json.dumps(payload, ensure_ascii=False), timestamp, timestamp),
                )
        except sqlite3.IntegrityError as error:
            raise DuplicateJobError("Este trabalho já existe; confira o histórico antes de repetir.") from error
        return job_id

    def create_job_batch(
        self,
        collection_id: str,
        concept_id: str,
        kind: str,
        jobs: list[tuple[str, dict[str, Any]]],
    ) -> list[str]:
        if kind not in {"generation", "upscale", "correction"} or not jobs:
            raise ValueError("Tipo ou conteúdo do lote de trabalho inválido.")
        target_state = WorkflowState.UPSCALING if kind == "upscale" else WorkflowState.GENERATING
        allowed_sources = {
            "generation": {WorkflowState.PROMPTS_APPROVED, WorkflowState.CANDIDATES_READY},
            "upscale": {WorkflowState.SELECTED},
            "correction": {WorkflowState.CANDIDATES_READY},
        }[kind]
        job_ids = [str(uuid.uuid4()) for _ in jobs]
        timestamp = utc_now()
        try:
            with self.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                concept = connection.execute(
                    "SELECT state FROM concepts WHERE id = ? AND collection_id = ?",
                    (concept_id, collection_id),
                ).fetchone()
                if concept is None:
                    raise KeyError("Conceito não encontrado nesta coleção.")
                current = WorkflowState(concept["state"])
                if current not in allowed_sources:
                    raise InvalidTransition(
                        f"Não é possível criar {kind} a partir de {current.value}."
                    )
                if target_state not in STATE_TRANSITIONS[current]:
                    raise InvalidTransition(
                        f"Transição não permitida: {current.value} -> {target_state.value}."
                    )
                connection.execute(
                    "UPDATE concepts SET state = ?, updated_at = ? WHERE id = ?",
                    (target_state.value, timestamp, concept_id),
                )
                connection.execute(
                    "INSERT INTO transitions(concept_id, from_state, to_state, reason, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (concept_id, current.value, target_state.value,
                     f"Lote {kind} autorizado pelo usuário", timestamp),
                )
                for job_id, (idempotency_key, payload) in zip(job_ids, jobs):
                    connection.execute(
                        "INSERT INTO jobs(id, idempotency_key, collection_id, concept_id, kind, "
                        "status, comfy_prompt_id, payload_json, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
                        (job_id, idempotency_key, collection_id, concept_id, kind, job_id,
                         json.dumps(payload, ensure_ascii=False), timestamp, timestamp),
                    )
        except sqlite3.IntegrityError as error:
            raise DuplicateJobError("Este lote ou trabalho já existe; confira o histórico antes de repetir.") from error
        return job_ids

    def update_job(
        self, job_id: str, status: str, error: str = "", duration_ms: int | None = None,
    ) -> None:
        if status not in {"queued", "running", "completed", "failed", "cancelled"}:
            raise ValueError("Estado de trabalho inválido.")
        with self.connect() as connection:
            connection.execute(
                "UPDATE jobs SET status = ?, error = ?, duration_ms = COALESCE(?, duration_ms), "
                "updated_at = ? WHERE id = ?",
                (status, error[:2000], duration_ms, utc_now(), job_id),
            )

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return self._decode(row, ("payload_json",))

    def list_recoverable_jobs(self, collection_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE collection_id = ? AND status IN ('queued', 'running') "
                "ORDER BY created_at", (collection_id,),
            ).fetchall()
        return [self._decode(row, ("payload_json",)) or {} for row in rows]

    def list_jobs(self, collection_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT kind, status, error, duration_ms, created_at FROM jobs "
                "WHERE collection_id = ? ORDER BY created_at DESC LIMIT ?",
                (collection_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_concept_jobs(self, concept_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE concept_id = ? ORDER BY created_at", (concept_id,)
            ).fetchall()
        return [self._decode(row, ("payload_json",)) or {} for row in rows]

    def add_asset(
        self, collection_id: str, concept_id: str, role: str, path: Path,
        sha256: str, width: int, height: int, size_bytes: int, status: str = "candidate",
    ) -> str:
        asset_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO assets(id, collection_id, concept_id, role, status, path, sha256, "
                "width, height, size_bytes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (asset_id, collection_id, concept_id, role, status, str(path), sha256,
                 width, height, size_bytes, utc_now()),
            )
        return asset_id

    def list_assets(self, concept_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM assets WHERE concept_id = ? ORDER BY created_at", (concept_id,)
            ).fetchall()
        return [self._decode(row, ("metadata_json", "review_json", "export_json")) or {} for row in rows]

    def get_asset(self, asset_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        return self._decode(row, ("metadata_json", "review_json", "export_json"))

    def select_asset(self, concept_id: str, asset_id: str, reason: str) -> None:
        if not reason.strip():
            raise ValueError("Informe o motivo da seleção humana.")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            concept = connection.execute(
                "SELECT collection_id, state FROM concepts WHERE id = ?", (concept_id,)
            ).fetchone()
            asset = connection.execute(
                "SELECT id, review_json FROM assets WHERE id = ? AND concept_id = ? "
                "AND role IN ('candidate', 'correction') AND status = 'candidate'",
                (asset_id, concept_id),
            ).fetchone()
            if concept is None or asset is None:
                raise ValueError("Candidato não pertence a este conceito.")
            current = WorkflowState(concept["state"])
            if WorkflowState.SELECTED not in STATE_TRANSITIONS[current]:
                raise InvalidTransition("O conceito ainda não está pronto para seleção.")
            connection.execute(
                "UPDATE assets SET status = 'not_selected' WHERE concept_id = ? "
                "AND role IN ('candidate', 'correction') AND status = 'candidate'",
                (concept_id,),
            )
            connection.execute(
                "UPDATE assets SET status = 'selected', review_json = ? WHERE id = ?",
                (_merge_review(asset["review_json"], "selected", reason), asset_id),
            )
            connection.execute(
                "UPDATE concepts SET selected_asset_id = ? WHERE id = ?", (asset_id, concept_id)
            )
            timestamp = utc_now()
            connection.execute(
                "UPDATE concepts SET state = ?, updated_at = ? WHERE id = ?",
                (WorkflowState.SELECTED.value, timestamp, concept_id),
            )
            connection.execute(
                "INSERT INTO transitions(concept_id, from_state, to_state, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (concept_id, current.value, WorkflowState.SELECTED.value, reason.strip(), timestamp),
            )

    def unselect_asset(self, concept_id: str, reason: str) -> None:
        """Desfaz a seleção antes do upscale: a escolhida e as preteridas voltam para avaliação."""
        if not reason.strip():
            raise ValueError("Informe o motivo para desfazer a seleção.")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            concept = connection.execute(
                "SELECT state, selected_asset_id FROM concepts WHERE id = ?", (concept_id,)
            ).fetchone()
            if concept is None:
                raise KeyError("Conceito não encontrado.")
            if concept["state"] != WorkflowState.SELECTED.value:
                raise InvalidTransition(
                    "Só é possível desfazer a seleção antes do upscale. "
                    "Se já ampliou, rejeite o upscale na aba Ampliação primeiro."
                )
            selected = connection.execute(
                "SELECT id, review_json FROM assets WHERE id = ?", (concept["selected_asset_id"] or "",)
            ).fetchone()
            if selected is not None:
                connection.execute(
                    "UPDATE assets SET status = 'candidate', review_json = ? WHERE id = ?",
                    (_merge_review(selected["review_json"], "unselected", reason), selected["id"]),
                )
            connection.execute(
                "UPDATE assets SET status = 'candidate' WHERE concept_id = ? "
                "AND role IN ('candidate', 'correction') AND status = 'not_selected'",
                (concept_id,),
            )
            timestamp = utc_now()
            connection.execute(
                "UPDATE concepts SET state = ?, selected_asset_id = NULL, updated_at = ? WHERE id = ?",
                (WorkflowState.CANDIDATES_READY.value, timestamp, concept_id),
            )
            connection.execute(
                "INSERT INTO transitions(concept_id, from_state, to_state, reason, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (concept_id, WorkflowState.SELECTED.value, WorkflowState.CANDIDATES_READY.value,
                 reason.strip(), timestamp),
            )

    def reject_asset(self, concept_id: str, asset_id: str, reason: str) -> None:
        if not reason.strip():
            raise ValueError("Informe o motivo da rejeição humana.")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            concept = connection.execute(
                "SELECT state FROM concepts WHERE id = ?", (concept_id,)
            ).fetchone()
            asset = connection.execute(
                "SELECT id, review_json FROM assets WHERE id = ? AND concept_id = ? "
                "AND role IN ('candidate', 'correction') AND status = 'candidate'",
                (asset_id, concept_id),
            ).fetchone()
            if concept is None or asset is None:
                raise ValueError("Candidato não encontrado ou já decidido.")
            if concept["state"] != WorkflowState.CANDIDATES_READY.value:
                raise InvalidTransition("Só é possível rejeitar candidatos antes da seleção.")
            connection.execute(
                "UPDATE assets SET status = 'rejected', review_json = ? WHERE id = ?",
                (_merge_review(asset["review_json"], "rejected", reason), asset_id),
            )

    def update_asset_review(self, asset_id: str, review: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE assets SET review_json = ? WHERE id = ?",
                (json.dumps(review, ensure_ascii=False), asset_id),
            )

    def update_asset_metadata(self, asset_id: str, metadata: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE assets SET metadata_json = ? WHERE id = ?",
                (json.dumps(metadata, ensure_ascii=False), asset_id),
            )

    def update_asset_export(self, asset_id: str, export: dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE assets SET export_json = ? WHERE id = ?",
                (json.dumps(export, ensure_ascii=False), asset_id),
            )

    def update_asset_status(self, asset_id: str, status: str, role: str | None = None) -> None:
        with self.connect() as connection:
            if role is None:
                connection.execute("UPDATE assets SET status = ? WHERE id = ?", (status, asset_id))
            else:
                connection.execute(
                    "UPDATE assets SET status = ?, role = ? WHERE id = ?", (status, role, asset_id)
                )

    def update_asset_path(self, asset_id: str, path: Path) -> None:
        with self.connect() as connection:
            connection.execute("UPDATE assets SET path = ? WHERE id = ?", (str(path), asset_id))

    def record_agent_run(
        self, collection_id: str, concept_id: str | None, agent_name: str,
        model_id: str, instruction_version: str, status: str,
        result: dict[str, Any] | None, error: str, duration_ms: int, attempts: int,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO agent_runs(id, collection_id, concept_id, agent_name, model_id, "
                "instruction_version, status, result_json, error, duration_ms, attempts, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), collection_id, concept_id, agent_name, model_id,
                 instruction_version, status, json.dumps(result or {}, ensure_ascii=False),
                 error[:2000], duration_ms, attempts, utc_now()),
            )

    def list_history(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            jobs = connection.execute(
                "SELECT kind, status, error, duration_ms, created_at FROM jobs "
                "ORDER BY created_at DESC LIMIT ?", (limit,),
            ).fetchall()
            agents = connection.execute(
                "SELECT agent_name AS kind, status, error, duration_ms, created_at "
                "FROM agent_runs ORDER BY created_at DESC LIMIT ?", (limit,),
            ).fetchall()
        return sorted(
            [dict(row) for row in (*jobs, *agents)],
            key=lambda item: item["created_at"], reverse=True,
        )[:limit]

    def record_performance(
        self, collection_id: str, entry: PerformanceEntry, concept_id: str | None = None,
    ) -> str:
        record_id = str(uuid.uuid4())
        values = entry.model_dump(mode="json")
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO performance_records(id, collection_id, concept_id, report_date, "
                "download_count, revenue, revenue_currency, moderation_status, generation_seconds, "
                "upscale_seconds, manual_minutes, user_cost, cost_currency, notes, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record_id, collection_id, concept_id, values["report_date"],
                    values["download_count"], values["revenue"], values["revenue_currency"],
                    values["moderation_status"], values["generation_seconds"],
                    values["upscale_seconds"], values["manual_minutes"], values["user_cost"],
                    values["cost_currency"], values["notes"], utc_now(),
                ),
            )
        return record_id

    def performance_comparison(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT c.id AS collection_id, c.name, c.created_at, p.revenue_currency, "
                "SUM(p.download_count) AS downloads, SUM(p.revenue) AS revenue, "
                "SUM(p.generation_seconds) AS generation_seconds, "
                "SUM(p.upscale_seconds) AS upscale_seconds, SUM(p.manual_minutes) AS manual_minutes, "
                "COUNT(DISTINCT p.id) AS reports, "
                "(SELECT COUNT(*) FROM assets a WHERE a.collection_id = c.id AND a.role = 'candidate') AS candidates, "
                "(SELECT COUNT(*) FROM assets a WHERE a.collection_id = c.id AND a.status = 'selected') AS selected "
                "FROM collections c LEFT JOIN performance_records p ON p.collection_id = c.id "
                "GROUP BY c.id, p.revenue_currency ORDER BY c.created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def list_performance(self, collection_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT report_date, download_count, revenue, revenue_currency, moderation_status, "
                "generation_seconds, upscale_seconds, manual_minutes, user_cost, cost_currency, notes "
                "FROM performance_records WHERE collection_id = ? ORDER BY report_date DESC LIMIT ?",
                (collection_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]
