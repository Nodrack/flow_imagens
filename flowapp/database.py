from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from types import EllipsisType
from typing import Any, get_args

from flowapp.moderation import local_date, local_noon_utc, today_local
from flowapp.sales_import import to_micros, with_occurrences
from flowapp.schemas import (
    LOCAL_TZ, CollectionBrief, CollectionCostEntry, ConceptPlan, ModerationStatus, MonthlySalesEntry, PerformanceEntry,
    STATE_TRANSITIONS, WorkflowState,
)


MAX_WORK_MINUTES = 24 * 60


EXTERNAL_COLLECTION_NAME = "Sem coleção"
logger = logging.getLogger(__name__)


class InvalidTransition(ValueError):
    pass


_ADOBE_SALES_DDL = """
    CREATE TABLE IF NOT EXISTS adobe_sales (
        id TEXT PRIMARY KEY,
        moderation_record_id TEXT NOT NULL REFERENCES moderation_records(id),
        sale_date TEXT NOT NULL, amount REAL NOT NULL, amount_micros INTEGER NOT NULL,
        currency TEXT NOT NULL DEFAULT 'USD', occurrence INTEGER NOT NULL DEFAULT 1,
        source_file TEXT NOT NULL DEFAULT '', imported_at TEXT NOT NULL,
        UNIQUE(moderation_record_id, sale_date, amount_micros, occurrence)
    )
"""


class ImportedMonthError(ValueError):
    """Editar à mão um lançamento mensal vindo de importação exige confirmação."""


class TimerTooLong(ValueError):
    """Cronômetro acima do limite: provavelmente esquecido; pede os minutos reais."""

    def __init__(self, minutes: float, max_minutes: float) -> None:
        self.minutes = minutes
        self.max_minutes = max_minutes
        super().__init__(
            f"O cronômetro mediu {minutes / 60:.1f} h, acima do limite de {max_minutes / 60:.1f} h. "
            "Informe os minutos realmente trabalhados para registrar."
        )


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


APPROVAL_PREFIX = "Aprovação confirmada no portal"
REJECTION_PREFIX = "Rejeição confirmada no portal"
MODERATION_PREFIXES = (f"{REJECTION_PREFIX}:", "Registrado na tela de moderação:")
MODERATION_NOTE = "Registrado na tela de moderação"
_STATE_TO_STATUS = {
    WorkflowState.SUBMITTED.value: "pending",
    WorkflowState.ADOBE_APPROVED.value: "approved",
    WorkflowState.ADOBE_REJECTED.value: "rejected",
}
_STATUS_TO_STATE = {
    "pending": WorkflowState.SUBMITTED,
    "approved": WorkflowState.ADOBE_APPROVED,
    "rejected": WorkflowState.ADOBE_REJECTED,
}
# Cadeia válida até a moderação; a partir de submitted/adobe_* a correção é direta.
_MODERATION_CHAIN = (
    WorkflowState.PREPARED_FOR_TRANSFER, WorkflowState.TRANSFERRED, WorkflowState.SUBMITTED,
)


def _clean_reason(reason: str) -> str:
    """Remove o prefixo gravado nas transições, deixando só o motivo do portal."""
    text = (reason or "").strip()
    for prefix in MODERATION_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix):].strip()
    return text


def _check_moderation(status: str, reason: str) -> str:
    if status not in get_args(ModerationStatus):
        raise ValueError("Resultado de moderação inválido.")
    reason = _clean_reason(reason)
    if status == "rejected" and not reason:
        raise ValueError("Informe o motivo da rejeição.")
    return reason


def _as_date(value: Any, label: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise ValueError(f"{label} inválida.")


def _validate_dates(submitted: str | None, decided: str | None) -> None:
    """Envio <= decisão <= hoje (datas locais de Madri)."""
    today = today_local()
    sent, decision = local_date(submitted), local_date(decided)
    if sent and sent > today:
        raise ValueError("A data de envio não pode estar no futuro.")
    if decision and decision > today:
        raise ValueError("A data da decisão não pode estar no futuro.")
    if sent and decision and sent > decision:
        raise ValueError("A data de envio não pode ser posterior à data da decisão.")


def _first_sale(connection: sqlite3.Connection, record_id: str | None) -> str | None:
    if not record_id:
        return None
    return connection.execute(
        "SELECT MIN(sale_date) AS first FROM adobe_sales WHERE moderation_record_id = ?", (record_id,)
    ).fetchone()["first"]


def _moderation_dates(
    status: str, existing: sqlite3.Row | dict[str, Any] | None, default_submitted: str | None,
    timestamp: str, submitted_on: date | None, decided_on: date | None, first_sale: str | None = None,
) -> tuple[str | None, str | None]:
    """Datas finais (envio, decisão) de um registro, com a validação envio <= decisão <= hoje.

    Datas informadas são locais de Madri (gravadas ao meio-dia de Madri em UTC). Sem data de envio
    informada, preserva a existente; só pendente usa hoje. Sem data da decisão, preserva a existente
    quando o resultado não muda; senão, hoje. A validação só roda quando o usuário informou uma data;
    a decisão informada também não pode ser posterior à primeira venda importada (`first_sale`).
    """
    if submitted_on is not None:
        submitted_on = _as_date(submitted_on, "Data de envio")
    if decided_on is not None:
        decided_on = _as_date(decided_on, "Data da decisão")
        if status == "pending":
            raise ValueError("Um registro pendente não tem data da decisão.")
    existing_submitted = existing["submitted_at"] if existing else None
    submitted = local_noon_utc(submitted_on) if submitted_on else (existing_submitted or default_submitted)
    if status == "pending":
        decided = None
    elif decided_on:
        decided = local_noon_utc(decided_on)
    elif existing and existing["status"] == status and existing["decided_at"]:
        decided = existing["decided_at"]
    else:
        decided = timestamp
    if submitted_on or decided_on:
        _validate_dates(submitted, decided)
        if decided_on and first_sale and decided_on > date.fromisoformat(first_sale):
            raise ValueError(
                f"A data da decisão não pode ser posterior à primeira venda importada ({first_sale})."
            )
    return submitted, decided


def _upsert_moderation(
    connection: sqlite3.Connection, concept_id: str, status: str, reason: str, timestamp: str,
    *, submitted_at: str | None = None, adobe_id: str | None = None, notes: str | None = None,
    submitted_on: date | None = None, decided_on: date | None = None,
) -> None:
    """Cria ou atualiza o registro de moderação de um conceito do fluxo (na transação do chamador).

    Datas já registradas são preservadas quando o resultado não muda; `submitted_at` é só o valor
    padrão do envio. `submitted_on`/`decided_on` (datas locais) sobrescrevem, com validação.
    """
    concept = connection.execute(
        "SELECT c.collection_id, c.data_json, col.brief_json FROM concepts c "
        "JOIN collections col ON col.id = c.collection_id WHERE c.id = ?", (concept_id,)
    ).fetchone()
    if concept is None:
        raise KeyError("Conceito não encontrado.")
    reason = _clean_reason(reason)
    existing = connection.execute(
        "SELECT id, status, submitted_at, decided_at FROM moderation_records WHERE concept_id = ?", (concept_id,)
    ).fetchone()
    submitted, decided = _moderation_dates(
        status, existing, submitted_at, timestamp, submitted_on, decided_on,
        _first_sale(connection, existing["id"] if existing else None),
    )
    if existing is None:
        data = json.loads(concept["data_json"])
        connection.execute(
            "INSERT INTO moderation_records(id, collection_id, concept_id, title, status, reason, "
            "adobe_id, submitted_at, decided_at, source, notes, theme, prompt_en, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'flow', ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), concept["collection_id"], concept_id, data.get("concept", "") or "Sem título",
             status, reason, adobe_id or "", submitted, decided,
             notes or "", json.loads(concept["brief_json"]).get("theme", ""), data.get("prompt_en", ""),
             timestamp, timestamp),
        )
        return
    connection.execute(
        "UPDATE moderation_records SET status = ?, reason = ?, decided_at = ?, "
        "submitted_at = ?, adobe_id = COALESCE(?, adobe_id), "
        "notes = COALESCE(?, notes), updated_at = ? WHERE id = ?",
        (status, reason, decided, submitted, adobe_id, notes, timestamp, existing["id"]),
    )


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
                CREATE TABLE IF NOT EXISTS work_sessions (
                    id TEXT PRIMARY KEY, collection_id TEXT NOT NULL REFERENCES collections(id),
                    work_date TEXT NOT NULL, started_at TEXT, ended_at TEXT, minutes REAL,
                    source TEXT NOT NULL CHECK (source IN ('manual', 'timer')),
                    notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_work_sessions_collection ON work_sessions(collection_id, work_date);
                CREATE UNIQUE INDEX IF NOT EXISTS ux_work_sessions_open_timer
                    ON work_sessions((1)) WHERE source = 'timer' AND ended_at IS NULL;
                CREATE TABLE IF NOT EXISTS monthly_sales (
                    id TEXT PRIMARY KEY,
                    collection_id TEXT REFERENCES collections(id),
                    collection_name TEXT NOT NULL DEFAULT '',
                    month TEXT NOT NULL, downloads INTEGER NOT NULL CHECK (downloads >= 0),
                    revenue REAL NOT NULL CHECK (revenue >= 0), currency TEXT NOT NULL,
                    notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS archived_work_minutes (
                    detached_from TEXT PRIMARY KEY, collection_name TEXT NOT NULL,
                    minutes REAL NOT NULL, manual_minutes REAL NOT NULL,
                    revenue_json TEXT NOT NULL DEFAULT '{}', archived_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS collection_costs (
                    id TEXT PRIMARY KEY, collection_id TEXT REFERENCES collections(id),
                    detached_from TEXT, collection_name TEXT NOT NULL DEFAULT '',
                    cost_date TEXT NOT NULL, amount_micros INTEGER NOT NULL CHECK (amount_micros > 0),
                    currency TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS moderation_records (
                    id TEXT PRIMARY KEY,
                    collection_id TEXT REFERENCES collections(id),
                    concept_id TEXT UNIQUE REFERENCES concepts(id),
                    title TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected')),
                    reason TEXT NOT NULL DEFAULT '', adobe_id TEXT NOT NULL DEFAULT '',
                    submitted_at TEXT, decided_at TEXT,
                    source TEXT NOT NULL CHECK (source IN ('flow', 'external')),
                    notes TEXT NOT NULL DEFAULT '',
                    theme TEXT NOT NULL DEFAULT '', prompt_en TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_moderation_collection ON moderation_records(collection_id);
                CREATE TABLE IF NOT EXISTS similarity_references (
                    moderation_record_id TEXT NOT NULL REFERENCES moderation_records(id),
                    path TEXT NOT NULL, sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (moderation_record_id, sha256)
                );
                CREATE TABLE IF NOT EXISTS agency_exports (
                    id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, collection_id TEXT,
                    collection_name TEXT NOT NULL, profile_id TEXT NOT NULL,
                    report_json TEXT NOT NULL, created_at TEXT NOT NULL
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
            if "kind" not in columns:
                connection.execute(
                    "ALTER TABLE collections ADD COLUMN kind TEXT NOT NULL DEFAULT 'normal' "
                    "CHECK (kind IN ('normal', 'external'))"
                )
                # Banco anterior: uma "Sem coleção" criada à mão e ainda sem conceitos vira a coleção
                # das imagens externas; se tiver conceitos é uma coleção de verdade e fica normal.
                connection.execute(
                    "UPDATE collections SET kind = 'external' WHERE id = ("
                    "SELECT c.id FROM collections c WHERE c.name = ? AND NOT EXISTS "
                    "(SELECT 1 FROM concepts k WHERE k.collection_id = c.id) ORDER BY c.created_at LIMIT 1)",
                    (EXTERNAL_COLLECTION_NAME,),
                )
            asset_columns = {row["name"] for row in connection.execute("PRAGMA table_info(assets)")}
            if "export_json" not in asset_columns:
                # Registro do JPEG exportado (arquivo, dimensões, avisos); separado dos metadados
                # para que editar título/palavras-chave não apague o registro do pacote.
                connection.execute(
                    "ALTER TABLE assets ADD COLUMN export_json TEXT NOT NULL DEFAULT '{}'"
                )
            moderation_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(moderation_records)")
            }
            for column in ("theme", "prompt_en"):
                # Cópia do tema e do prompt para o registro sobreviver à remoção da coleção.
                if column not in moderation_columns:
                    connection.execute(
                        f"ALTER TABLE moderation_records ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                    )
            adobe_columns = {row["name"] for row in connection.execute("PRAGMA table_info(adobe_sales)")}
            if adobe_columns and "amount_micros" not in adobe_columns:
                # Versão anterior (UNIQUE em REAL): recria a tabela com a chave em micro-dólares.
                connection.execute("ALTER TABLE adobe_sales RENAME TO adobe_sales_old")
                connection.execute(_ADOBE_SALES_DDL)
                connection.execute(
                    "INSERT INTO adobe_sales(id, moderation_record_id, sale_date, amount, amount_micros, "
                    "currency, occurrence, source_file, imported_at) SELECT id, moderation_record_id, "
                    "sale_date, amount, CAST(ROUND(amount * 1000000) AS INTEGER), currency, occurrence, "
                    "source_file, imported_at FROM adobe_sales_old"
                )
                connection.execute("DROP TABLE adobe_sales_old")
            connection.execute(_ADOBE_SALES_DDL)
            for table, extra in (
                ("archived_work_minutes", (("cost_json", "TEXT NOT NULL DEFAULT '{}'"),)),
                ("monthly_sales", (
                    ("detached_from", "TEXT"),
                    ("source", "TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'import'))"),
                )),
                ("moderation_records", (("detached_from", "TEXT"), ("detached_name", "TEXT NOT NULL DEFAULT ''"))),
            ):
                existing_columns = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
                for column, definition in extra:
                    # Origem (id e nome) de registros que sobrevivem à remoção da coleção.
                    if column not in existing_columns:
                        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            # Um lançamento por coleção (ou por coleção removida, via detached_from) e mês.
            connection.execute("DROP INDEX IF EXISTS ux_monthly_sales_key")
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_monthly_sales_month ON monthly_sales("
                "COALESCE(collection_id, detached_from, ''), month)"
            )
            self._backfill_moderation(connection)
            self._migrate_unlinked_to_external(connection)

    @staticmethod
    def _backfill_moderation(connection: sqlite3.Connection) -> None:
        """Cria o registro dos conceitos já enviados antes da tabela existir (idempotente)."""
        legacy = connection.execute(
            "SELECT c.id, c.collection_id, c.data_json, c.state, c.updated_at, col.brief_json "
            "FROM concepts c JOIN collections col ON col.id = c.collection_id "
            "WHERE c.state IN (?, ?, ?) AND NOT EXISTS "
            "(SELECT 1 FROM moderation_records m WHERE m.concept_id = c.id)",
            tuple(_STATE_TO_STATUS),
        ).fetchall()
        for concept in legacy:
            status = _STATE_TO_STATUS[concept["state"]]

            def last(state: WorkflowState) -> sqlite3.Row | None:
                return connection.execute(
                    "SELECT reason, created_at FROM transitions WHERE concept_id = ? AND to_state = ? "
                    "ORDER BY id DESC LIMIT 1", (concept["id"], state.value),
                ).fetchone()

            submitted = last(WorkflowState.SUBMITTED)
            decision = last(_STATUS_TO_STATE[status]) if status != "pending" else None
            reason = _clean_reason(decision["reason"]) if decision and status == "rejected" else ""
            reference = decision or submitted
            created = reference["created_at"] if reference else concept["updated_at"]
            data = json.loads(concept["data_json"])
            connection.execute(
                "INSERT OR IGNORE INTO moderation_records(id, collection_id, concept_id, title, status, "
                "reason, submitted_at, decided_at, source, theme, prompt_en, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'flow', ?, ?, ?, ?)",
                (str(uuid.uuid4()), concept["collection_id"], concept["id"],
                 data.get("concept", "") or "Sem título", status, reason,
                 submitted["created_at"] if submitted else None,
                 decision["created_at"] if decision else None,
                 json.loads(concept["brief_json"]).get("theme", ""), data.get("prompt_en", ""),
                 created, created),
            )

    @staticmethod
    def _decode(row: sqlite3.Row | None, json_fields: tuple[str, ...] = ()) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for field in json_fields:
            result[field.removesuffix("_json")] = json.loads(result.pop(field))
        return result

    def create_collection(self, name: str, brief: CollectionBrief, kind: str = "normal") -> str:
        collection_id = str(uuid.uuid4())
        timestamp = utc_now()
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO collections(id, name, brief_json, kind, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (collection_id, name.strip(), brief.model_dump_json(), kind, timestamp, timestamp),
            )
        return collection_id

    @staticmethod
    def _ensure_external(connection: sqlite3.Connection) -> str:
        """Id da coleção externa na transação do chamador; cria se não existir."""
        row = connection.execute(
            "SELECT id FROM collections WHERE kind = 'external' ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row:
            return row["id"]
        name = EXTERNAL_COLLECTION_NAME
        if connection.execute("SELECT 1 FROM collections WHERE name = ?", (name,)).fetchone():
            name = f"{EXTERNAL_COLLECTION_NAME} (imagens externas)"  # nome ocupado por uma coleção normal
        collection_id = str(uuid.uuid4())
        timestamp = utc_now()
        brief = CollectionBrief(
            theme="Imagens enviadas fora do fluxo",
            objective="Reunir as imagens enviadas à Adobe sem passar pelos agentes do app",
            concept_count=1,
        )
        connection.execute(
            "INSERT INTO collections(id, name, brief_json, kind, created_at, updated_at) "
            "VALUES (?, ?, ?, 'external', ?, ?)",
            (collection_id, name, brief.model_dump_json(), timestamp, timestamp),
        )
        return collection_id

    def external_collection_id(self) -> str:
        """Id da coleção das imagens enviadas fora do fluxo; cria "Sem coleção" se ainda não existir.

        A leitura é sem trava; só quando não existe abre a transação e confere de novo.
        """
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id FROM collections WHERE kind = 'external' ORDER BY created_at LIMIT 1"
            ).fetchone()
            if row:
                return row["id"]
            connection.execute("BEGIN IMMEDIATE")
            return self._ensure_external(connection)

    def _migrate_unlinked_to_external(self, connection: sqlite3.Connection) -> None:
        """Move para a coleção externa os registros que nunca tiveram coleção (idempotente).

        São os registros de moderação externos e os lançamentos mensais com collection_id e
        detached_from NULL. Num mês que já exista na externa, lançamentos manuais da mesma moeda
        são somados (com nota); moeda diferente, ou linha importada, mantém o lançamento sem
        vínculo no lugar (aviso no log).
        """
        loose_records = connection.execute(
            "SELECT COUNT(*) AS n FROM moderation_records WHERE concept_id IS NULL "
            "AND collection_id IS NULL AND detached_from IS NULL"
        ).fetchone()["n"]
        loose_sales = connection.execute(
            "SELECT * FROM monthly_sales WHERE collection_id IS NULL AND detached_from IS NULL "
            "AND source = 'manual'"
        ).fetchall()
        if not loose_records and not loose_sales:
            return
        external_id = self._ensure_external(connection)
        connection.execute(
            "UPDATE moderation_records SET collection_id = ? WHERE concept_id IS NULL "
            "AND collection_id IS NULL AND detached_from IS NULL", (external_id,),
        )
        for sale in loose_sales:
            twin = connection.execute(
                "SELECT id, source, currency, downloads, revenue, notes FROM monthly_sales "
                "WHERE collection_id = ? AND month = ?", (external_id, sale["month"]),
            ).fetchone()
            if twin is None:
                connection.execute(
                    "UPDATE monthly_sales SET collection_id = ?, collection_name = '' WHERE id = ?",
                    (external_id, sale["id"]),
                )
            elif twin["source"] == "manual" and twin["currency"] == sale["currency"]:
                note = f"somado de lançamento sem vínculo: {sale['downloads']} downloads, {sale['revenue']:.2f} {sale['currency']}"
                connection.execute(
                    "UPDATE monthly_sales SET downloads = ?, revenue = ?, notes = ?, updated_at = ? WHERE id = ?",
                    (twin["downloads"] + sale["downloads"], twin["revenue"] + sale["revenue"],
                     "; ".join(part for part in (twin["notes"], note) if part), utc_now(), twin["id"]),
                )
                connection.execute("DELETE FROM monthly_sales WHERE id = ?", (sale["id"],))
            else:
                logger.warning(
                    "Lançamento mensal sem vínculo de %s não foi movido para a coleção externa: "
                    "já existe lançamento do mesmo mês com outra moeda ou importado.", sale["month"],
                )
        self._recalculate_import_sales(connection)

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
                "SELECT id, name FROM collections WHERE id = ?", (collection_id,)
            ).fetchone()
            if collection is None:
                raise KeyError("Coleção não encontrada.")
            if connection.execute(
                "SELECT 1 FROM collections WHERE id = ? AND kind = 'external'", (collection_id,)
            ).fetchone():
                raise ValueError("A coleção das imagens enviadas fora do fluxo não pode ser removida.")
            active_job = connection.execute(
                "SELECT id FROM jobs WHERE collection_id = ? "
                "AND status IN ('queued', 'running') LIMIT 1",
                (collection_id,),
            ).fetchone()
            if active_job:
                raise ValueError("Não é possível remover uma coleção com trabalhos ComfyUI ativos.")
            if connection.execute(
                "SELECT 1 FROM work_sessions WHERE collection_id = ? "
                "AND source = 'timer' AND ended_at IS NULL LIMIT 1", (collection_id,),
            ).fetchone():
                raise ValueError(
                    "Não é possível remover uma coleção com cronômetro aberto. "
                    "Pare ou descarte o cronômetro antes."
                )
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
            self._archive_work(connection, collection_id, collection["name"])
            connection.execute(
                "UPDATE collection_costs SET detached_from = collection_id, collection_name = ?, "
                "collection_id = NULL WHERE collection_id = ?", (collection["name"], collection_id),
            )
            connection.execute(
                "DELETE FROM performance_records WHERE collection_id = ?", (collection_id,)
            )
            connection.execute("DELETE FROM work_sessions WHERE collection_id = ?", (collection_id,))
            # Lançamentos mensais são histórico financeiro: ficam sem vínculo, com origem e nome.
            connection.execute(
                "UPDATE monthly_sales SET detached_from = collection_id, collection_name = ?, "
                "collection_id = NULL WHERE collection_id = ?", (collection["name"], collection_id),
            )
            connection.execute("DELETE FROM agent_runs WHERE collection_id = ?", (collection_id,))
            connection.execute("DELETE FROM jobs WHERE collection_id = ?", (collection_id,))
            connection.execute(
                "DELETE FROM transitions WHERE concept_id IN "
                "(SELECT id FROM concepts WHERE collection_id = ?)",
                (collection_id,),
            )
            # O histórico de moderação é preservado (sem vínculo) para continuar alimentando os agentes.
            connection.execute(
                "UPDATE moderation_records SET concept_id = NULL, detached_from = collection_id, "
                "detached_name = ?, collection_id = NULL WHERE collection_id = ?",
                (collection["name"], collection_id),
            )
            self._recalculate_import_sales(connection)
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
        if target == WorkflowState.ADOBE_REJECTED:
            _check_moderation("rejected", reason)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state FROM concepts WHERE id = ?", (concept_id,)).fetchone()
            if row is None:
                raise KeyError("Conceito não encontrado.")
            current = WorkflowState(row["state"])
            if target not in STATE_TRANSITIONS[current]:
                raise InvalidTransition(f"Transição não permitida: {current.value} -> {target.value}.")
            timestamp = utc_now()
            self._move(connection, concept_id, current, target, reason, timestamp)
            if target.value in _STATE_TO_STATUS:
                status = _STATE_TO_STATUS[target.value]
                note = None
                if status == "approved" and reason.startswith(APPROVAL_PREFIX):
                    note = reason[len(APPROVAL_PREFIX):].lstrip(":").strip() or None
                _upsert_moderation(
                    connection, concept_id, status, reason if status == "rejected" else "", timestamp,
                    submitted_at=timestamp if status == "pending" else None, notes=note,
                )

    @staticmethod
    def _move(
        connection: sqlite3.Connection, concept_id: str, current: WorkflowState,
        target: WorkflowState, reason: str, timestamp: str,
    ) -> None:
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

    def set_moderation(
        self, concept_id: str, status: str, reason: str = "",
        adobe_id: str | None = None, notes: str | None = None,
        submitted_on: date | None = None, decided_on: date | None = None,
    ) -> None:
        """Registra o resultado da Adobe de um conceito, avançando o estado pela cadeia válida.

        `submitted_on`/`decided_on` são datas locais (Madri) opcionais; mudar só a data não muda o estado.
        """
        reason = _check_moderation(status, reason)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._set_moderation(
                connection, concept_id, status, reason, adobe_id, notes, submitted_on, decided_on,
            )

    def _set_moderation(
        self, connection: sqlite3.Connection, concept_id: str, status: str, reason: str,
        adobe_id: str | None, notes: str | None,
        submitted_on: date | None = None, decided_on: date | None = None,
    ) -> None:
        """Corpo de set_moderation; exige uma transação já aberta pelo chamador."""
        target = _STATUS_TO_STATE[status]
        allowed = {*_MODERATION_CHAIN, WorkflowState.ADOBE_APPROVED, WorkflowState.ADOBE_REJECTED}
        row = connection.execute("SELECT state FROM concepts WHERE id = ?", (concept_id,)).fetchone()
        if row is None:
            raise KeyError("Conceito não encontrado.")
        current = WorkflowState(row["state"])
        if current not in allowed:
            raise InvalidTransition(
                f"A moderação só pode ser registrada após a preparação do pacote (estado atual: {current.value})."
            )
        if current in _MODERATION_CHAIN:
            path = list(_MODERATION_CHAIN[_MODERATION_CHAIN.index(current) + 1:])
            if target != WorkflowState.SUBMITTED:
                path.append(target)
        else:
            path = [target] if target != current else []
        timestamp = utc_now()
        for step in path:
            if step not in STATE_TRANSITIONS[current]:
                raise InvalidTransition(f"Transição não permitida: {current.value} -> {step.value}.")
            step_reason = MODERATION_NOTE + (f": {reason}" if step == WorkflowState.ADOBE_REJECTED else "")
            self._move(connection, concept_id, current, step, step_reason, timestamp)
            current = step
        _upsert_moderation(
            connection, concept_id, status, reason, timestamp,
            submitted_at=timestamp if status == "pending" else None,
            adobe_id=adobe_id, notes=notes, submitted_on=submitted_on, decided_on=decided_on,
        )

    @staticmethod
    def _insert_external(
        connection: sqlite3.Connection, title: str, status: str, collection_id: str | None,
        reason: str, adobe_id: str, submitted_at: str | None, decided_at: str | None, notes: str,
    ) -> str:
        """Cria o registro externo na transação do chamador."""
        title = title.strip()
        if not title:
            raise ValueError("Informe o título da imagem.")
        reason = _check_moderation(status, reason)
        record_id = str(uuid.uuid4())
        timestamp = utc_now()
        if collection_id is not None and connection.execute(
            "SELECT 1 FROM collections WHERE id = ?", (collection_id,)
        ).fetchone() is None:
            raise KeyError("Coleção não encontrada.")
        theme = ""
        if collection_id is not None:
            theme = json.loads(connection.execute(
                "SELECT brief_json FROM collections WHERE id = ?", (collection_id,)
            ).fetchone()["brief_json"]).get("theme", "")
        connection.execute(
            "INSERT INTO moderation_records(id, collection_id, concept_id, title, status, reason, "
            "adobe_id, submitted_at, decided_at, source, notes, theme, created_at, updated_at) "
            "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, 'external', ?, ?, ?, ?)",
            (record_id, collection_id, title, status, reason, adobe_id.strip(), submitted_at,
             None if status == "pending" else (decided_at or timestamp),
             notes.strip(), theme, timestamp, timestamp),
        )
        return record_id

    def add_external_moderation(
        self, title: str, status: str, collection_id: str | None = None, reason: str = "",
        adobe_id: str = "", submitted_at: str | None = None, decided_at: str | None = None,
        notes: str = "", submitted_on: date | None = None, decided_on: date | None = None,
    ) -> str:
        """Registra uma imagem enviada à Adobe fora do fluxo do app.

        `submitted_on`/`decided_on` são datas locais (Madri) gravadas ao meio-dia de Madri em UTC e
        validadas (envio <= decisão <= hoje); `submitted_at`/`decided_at` são timestamps já prontos.
        """
        if decided_on is not None and status == "pending":
            raise ValueError("Um registro pendente não tem data da decisão.")
        if submitted_on is not None:
            submitted_at = local_noon_utc(_as_date(submitted_on, "Data de envio"))
        if decided_on is not None:
            decided_at = local_noon_utc(_as_date(decided_on, "Data da decisão"))
        if submitted_on is not None or decided_on is not None:
            _validate_dates(submitted_at, None if status == "pending" else (decided_at or utc_now()))
        with self.connect() as connection:
            return self._insert_external(
                connection, title, status, collection_id, reason, adobe_id, submitted_at, decided_at, notes,
            )

    def update_moderation_record(
        self, record_id: str, *, status: str, reason: str = "", adobe_id: str = "", notes: str = "",
        submitted_on: date | None = None, decided_on: date | None = None,
    ) -> None:
        reason = _check_moderation(status, reason)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = connection.execute(
                "SELECT concept_id, status, submitted_at, decided_at FROM moderation_records WHERE id = ?",
                (record_id,),
            ).fetchone()
            if record is None:
                raise KeyError("Registro de moderação não encontrado.")
            if record["concept_id"]:
                self._set_moderation(
                    connection, record["concept_id"], status, reason, adobe_id.strip(), notes.strip(),
                    submitted_on, decided_on,
                )
                return
            timestamp = utc_now()
            submitted, decided = _moderation_dates(
                status, record, timestamp if status == "pending" else None, timestamp, submitted_on, decided_on,
                _first_sale(connection, record_id),
            )
            connection.execute(
                "UPDATE moderation_records SET status = ?, reason = ?, adobe_id = ?, notes = ?, "
                "submitted_at = ?, decided_at = ?, updated_at = ? WHERE id = ?",
                (status, reason, adobe_id.strip(), notes.strip(), submitted, decided, timestamp, record_id),
            )

    def list_moderation(self, collection_id: str | None | EllipsisType = ...) -> list[dict[str, Any]]:
        """Registros de uma coleção (None = sem coleção); sem argumento, todos."""
        query = (
            "SELECT m.*, COALESCE(col.name, m.detached_name) AS collection_name FROM moderation_records m "
            "LEFT JOIN collections col ON col.id = m.collection_id"
        )
        params: tuple[Any, ...] = ()
        if collection_id is None:
            query += " WHERE m.collection_id IS NULL"
        elif collection_id is not ...:
            query += " WHERE m.collection_id = ?"
            params = (collection_id,)
        with self.connect() as connection:
            rows = connection.execute(
                query + " ORDER BY COALESCE(m.decided_at, m.updated_at) DESC, m.created_at DESC", params
            ).fetchall()
        return [dict(row) for row in rows]

    def moderation_candidates(self, collection_id: str) -> list[dict[str, Any]]:
        """Conceitos da coleção já preparados/enviados, com o resultado registrado (None = não enviada)."""
        states = (
            WorkflowState.PREPARED_FOR_TRANSFER.value, WorkflowState.TRANSFERRED.value,
            *_STATE_TO_STATUS,
        )
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT c.id AS concept_id, c.data_json, c.state, m.id AS record_id, m.status, "
                "m.reason, m.adobe_id, m.notes, m.submitted_at, m.decided_at FROM concepts c "
                "LEFT JOIN moderation_records m ON m.concept_id = c.id "
                f"WHERE c.collection_id = ? AND c.state IN ({','.join('?' * len(states))}) "
                "ORDER BY c.created_at", (collection_id, *states),
            ).fetchall()
        return [
            {
                "concept_id": row["concept_id"],
                "record_id": row["record_id"],
                "concept": json.loads(row["data_json"]).get("concept", ""),
                "state": row["state"],
                "status": row["status"] or _STATE_TO_STATUS.get(row["state"]),
                "reason": row["reason"] or "",
                "adobe_id": row["adobe_id"] or "",
                "notes": row["notes"] or "",
                "submitted_at": row["submitted_at"],
                "decided_at": row["decided_at"],
            }
            for row in rows
        ]

    def adobe_feedback(self, limit: int = 20) -> dict[str, Any]:
        """Resultados da moderação da Adobe em todas as coleções, para os agentes aprenderem."""
        query = (
            "SELECT m.title, m.reason, m.decided_at, m.updated_at, m.collection_id, "
            "m.theme AS snapshot_theme, m.prompt_en AS snapshot_prompt, "
            "c.data_json, col.brief_json FROM moderation_records m "
            "LEFT JOIN concepts c ON c.id = m.concept_id "
            "LEFT JOIN collections col ON col.id = m.collection_id "
            "WHERE m.status = ? ORDER BY COALESCE(m.decided_at, m.updated_at) DESC LIMIT ?"
        )
        with self.connect() as connection:
            rejected = connection.execute(query, ("rejected", limit)).fetchall()
            approved = connection.execute(query, ("approved", limit)).fetchall()
            counts = {
                row["status"]: row["total"] for row in connection.execute(
                    "SELECT status, COUNT(*) AS total FROM moderation_records GROUP BY status"
                )
            }

        def entry(row: sqlite3.Row) -> dict[str, Any]:
            data = json.loads(row["data_json"]) if row["data_json"] else {}
            brief = json.loads(row["brief_json"]) if row["brief_json"] else {}
            return {
                "collection_id": row["collection_id"],
                "theme": brief.get("theme", "") or row["snapshot_theme"],
                "concept": row["title"],
                "prompt_en": data.get("prompt_en", "") or row["snapshot_prompt"],
            }

        return {
            "rejected": [
                {**entry(row), "reason": row["reason"], "created_at": row["decided_at"] or row["updated_at"]}
                for row in rejected
            ],
            "approved": [entry(row) for row in approved],
            "rejected_count": counts.get("rejected", 0),
            "approved_count": counts.get("approved", 0),
            "pending_count": counts.get("pending", 0),
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

    def record_agency_export(self, asset: dict, profile_id: str, report: dict) -> str:
        record_id = str(uuid.uuid4())
        collection = self.get_collection(asset["collection_id"])
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO agency_exports(id, asset_id, collection_id, collection_name, profile_id, report_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (record_id, asset["id"], asset["collection_id"], (collection or {}).get("name", ""), profile_id,
                 json.dumps(report, ensure_ascii=False), utc_now()),
            )
        return record_id

    def list_agency_exports(self, collection_id: str, profile_id: str) -> list[dict]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM agency_exports WHERE collection_id = ? AND profile_id = ? "
                                      "ORDER BY created_at DESC, rowid DESC", (collection_id, profile_id)).fetchall()
        return [{**dict(row), "report": json.loads(row["report_json"])} for row in rows]

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

    @staticmethod
    def _archive_work(connection: sqlite3.Connection, collection_id: str, name: str) -> None:
        """Guarda horas e receita avulsa da coleção antes de apagá-las, para a receita/hora total
        não inflar ao remover (os lançamentos mensais continuam, vinculados por detached_from)."""
        minutes = connection.execute(
            "SELECT COALESCE(SUM(minutes), 0) AS total FROM work_sessions "
            "WHERE collection_id = ? AND minutes IS NOT NULL", (collection_id,),
        ).fetchone()["total"]
        manual = connection.execute(
            "SELECT COALESCE(SUM(manual_minutes), 0) AS total FROM performance_records "
            "WHERE collection_id = ?", (collection_id,),
        ).fetchone()["total"]
        revenue = {
            row["revenue_currency"]: row["total"] for row in connection.execute(
                "SELECT revenue_currency, SUM(revenue) AS total FROM performance_records "
                "WHERE collection_id = ? GROUP BY revenue_currency", (collection_id,),
            )
        }
        costs = {
            row["cost_currency"]: row["total"] for row in connection.execute(
                "SELECT cost_currency, SUM(user_cost) AS total FROM performance_records "
                "WHERE collection_id = ? AND user_cost > 0 GROUP BY cost_currency", (collection_id,),
            )
        }
        if minutes or manual or revenue or costs:
            connection.execute(
                "INSERT INTO archived_work_minutes(detached_from, collection_name, minutes, "
                "manual_minutes, revenue_json, cost_json, archived_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (collection_id, name, minutes, manual, json.dumps(revenue), json.dumps(costs), utc_now()),
            )

    def list_archived_work(self) -> list[dict[str, Any]]:
        """Horas e receita avulsa de coleções removidas (uma linha por coleção)."""
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM archived_work_minutes").fetchall()
        return [
            {**dict(row), "revenue": json.loads(row["revenue_json"] or "{}"),
             "costs": json.loads(row["cost_json"] or "{}")} for row in rows
        ]

    # --- Downloads e receita mensais -----------------------------------------------------

    def upsert_monthly_sales(
        self, collection_id: str | None, month: str, downloads: int, revenue: float,
        currency: str, notes: str = "", confirm_import_override: bool = False,
    ) -> str:
        """Lança o mês; repetir o mês da mesma coleção substitui o valor, inclusive a moeda (correção).

        Uma linha vinda de importação só é editada com `confirm_import_override`; ela vira manual.
        """
        entry = MonthlySalesEntry(
            month=month, downloads=downloads, revenue=revenue, currency=currency, notes=notes.strip(),
        )
        timestamp = utc_now()
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if collection_id is not None and connection.execute(
                "SELECT 1 FROM collections WHERE id = ?", (collection_id,)
            ).fetchone() is None:
                raise KeyError("Coleção não encontrada.")
            if collection_id is None:
                existing = connection.execute(
                    "SELECT id, source FROM monthly_sales WHERE collection_id IS NULL "
                    "AND detached_from IS NULL AND month = ?", (entry.month,),
                ).fetchone()
            else:
                existing = connection.execute(
                    "SELECT id, source FROM monthly_sales WHERE collection_id = ? AND month = ?",
                    (collection_id, entry.month),
                ).fetchone()
            if existing:
                if existing["source"] == "import" and not confirm_import_override:
                    raise ImportedMonthError(
                        "Este mês foi importado dos ganhos da Adobe. Confirme para substituí-lo por um "
                        "lançamento manual (a linha passa a ser manual)."
                    )
                connection.execute(
                    "UPDATE monthly_sales SET downloads = ?, revenue = ?, currency = ?, notes = ?, "
                    "source = 'manual', updated_at = ? WHERE id = ?",
                    (entry.downloads, entry.revenue, entry.currency, entry.notes, timestamp, existing["id"]),
                )
                return existing["id"]
            sale_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO monthly_sales(id, collection_id, collection_name, month, downloads, "
                "revenue, currency, notes, created_at, updated_at) VALUES (?, ?, '', ?, ?, ?, ?, ?, ?, ?)",
                (sale_id, collection_id, entry.month, entry.downloads, entry.revenue, entry.currency,
                 entry.notes, timestamp, timestamp),
            )
        return sale_id

    def list_monthly_sales(self, collection_id: str | None | EllipsisType = ...) -> list[dict[str, Any]]:
        """Lançamentos de uma coleção (None = sem coleção/removidas); sem argumento, todos."""
        query = (
            "SELECT s.id, s.collection_id, COALESCE(c.name, s.collection_name) AS collection_name, "
            "s.detached_from, s.month, s.downloads, s.revenue, s.currency, s.notes, s.source FROM monthly_sales s "
            "LEFT JOIN collections c ON c.id = s.collection_id"
        )
        params: tuple[Any, ...] = ()
        if collection_id is None:
            query += " WHERE s.collection_id IS NULL"
        elif collection_id is not ...:
            query += " WHERE s.collection_id = ?"
            params = (collection_id,)
        with self.connect() as connection:
            rows = connection.execute(query + " ORDER BY s.month DESC, collection_name", params).fetchall()
        return [dict(row) for row in rows]

    def delete_monthly_sales(self, sale_id: str) -> None:
        """Exclusão explícita (UI com confirmação).

        Roda o recálculo: uma linha 'import' excluída volta a refletir as vendas importadas, e um
        mês manual que tenha vendas importadas ganha de volta a linha 'import'.
        """
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("DELETE FROM monthly_sales WHERE id = ?", (sale_id,)).rowcount == 0:
                raise KeyError("Lançamento mensal não encontrado.")
            self._recalculate_import_sales(connection)

    # --- Importação do CSV de ganhos da Adobe ------------------------------------------------

    @staticmethod
    def _record_key(connection: sqlite3.Connection, record_id: str) -> tuple[str | None, str | None]:
        record = connection.execute(
            "SELECT collection_id, detached_from FROM moderation_records WHERE id = ?", (record_id,)
        ).fetchone()
        if record is None:
            raise KeyError("Registro de moderação não encontrado.")
        return record["collection_id"], record["detached_from"]

    def _manual_months(
        self, connection: sqlite3.Connection, key: tuple[str | None, str | None], months: list[str],
    ) -> list[dict[str, Any]]:
        collection_id, detached_from = key
        if collection_id:
            where, params = "collection_id = ?", (collection_id,)
        elif detached_from:
            where, params = "collection_id IS NULL AND detached_from = ?", (detached_from,)
        else:
            where, params = "collection_id IS NULL AND detached_from IS NULL", ()
        rows = [
            dict(row) for row in connection.execute(
                f"SELECT id, month, downloads, revenue, currency FROM monthly_sales "
                f"WHERE source = 'manual' AND {where} ORDER BY month", params,
            )
        ]
        return [row for row in rows if row["month"] in months]

    def import_conflicts(
        self, months: list[str], *, record_id: str | None = None, collection_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Lançamentos manuais que a importação tocaria (mesma coleção da imagem e mesmo mês)."""
        with self.connect() as connection:
            key = self._record_key(connection, record_id) if record_id else (collection_id, None)
            return self._manual_months(connection, key, months)

    def count_existing_adobe_sales(self, record_id: str, sales: list[tuple[str, float]]) -> int:
        """Quantas vendas do arquivo já estão importadas para a imagem (prévia da reimportação)."""
        with self.connect() as connection:
            existing = {
                (row["sale_date"], row["amount_micros"], row["occurrence"]) for row in connection.execute(
                    "SELECT sale_date, amount_micros, occurrence FROM adobe_sales "
                    "WHERE moderation_record_id = ?", (record_id,),
                )
            }
        return sum(
            1 for sale_date, amount, occurrence in with_occurrences(sales)
            if (sale_date, to_micros(amount), occurrence) in existing
        )

    def find_moderation_by_adobe_id(self, adobe_id: str) -> dict[str, Any] | None:
        adobe_id = adobe_id.strip()
        if not adobe_id:
            return None
        with self.connect() as connection:
            return self._find_by_adobe_id(connection, adobe_id)

    @staticmethod
    def _find_by_adobe_id(connection: sqlite3.Connection, adobe_id: str) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT id, title, status, collection_id FROM moderation_records WHERE adobe_id = ? "
            "ORDER BY created_at LIMIT 1", (adobe_id,),
        ).fetchone()
        return dict(row) if row else None

    def import_adobe_sales(
        self, record_id: str | None, sales: list[tuple[str, float]], source_file: str = "",
        replace_manual: bool | set[str] = False, new_image: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Importa as vendas de uma imagem e atualiza os lançamentos mensais 'import', numa transação.

        `new_image` ({title, adobe_id, collection_id, submitted_at}) cria o registro externo aprovado
        na mesma transação, reutilizando um registro com o mesmo ID Adobe (busca refeita dentro dela).
        Idempotente. A imagem fica aprovada e o decided_at nunca fica depois da primeira venda.
        `replace_manual` (True = todos, ou um conjunto de meses 'AAAA-MM') troca lançamentos manuais
        do mesmo mês pelos dados importados; do contrário eles ficam como estão.
        """
        if not sales:
            raise ValueError("O arquivo não tem vendas válidas para importar.")
        timestamp = utc_now()
        first_date = min(sale_date for sale_date, _ in sales)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            created = False
            if record_id is None:
                if not new_image:
                    raise ValueError("Informe a imagem das vendas.")
                reused = self._find_by_adobe_id(connection, (new_image.get("adobe_id") or "").strip()) \
                    if (new_image.get("adobe_id") or "").strip() else None
                if reused:
                    record_id = reused["id"]
                else:
                    submitted = new_image.get("submitted_at")
                    if new_image.get("submitted_on"):
                        submitted = local_noon_utc(_as_date(new_image["submitted_on"], "Data de envio"))
                    decided = f"{first_date}T00:00:00+00:00"
                    if submitted:
                        _validate_dates(submitted, decided)
                    record_id = self._insert_external(
                        connection, new_image["title"], "approved", new_image.get("collection_id"), "",
                        new_image.get("adobe_id") or "", submitted, decided, "",
                    )
                    created = True
            record = connection.execute(
                "SELECT concept_id, status FROM moderation_records WHERE id = ?", (record_id,)
            ).fetchone()
            if record is None:
                raise KeyError("Registro de moderação não encontrado.")
            new = 0
            for sale_date, amount, occurrence in with_occurrences(sales):
                new += connection.execute(
                    "INSERT OR IGNORE INTO adobe_sales(id, moderation_record_id, sale_date, amount, "
                    "amount_micros, currency, occurrence, source_file, imported_at) "
                    "VALUES (?, ?, ?, ?, ?, 'USD', ?, ?, ?)",
                    (str(uuid.uuid4()), record_id, sale_date, amount, to_micros(amount), occurrence,
                     source_file[:200], timestamp),
                ).rowcount
            first_sale = connection.execute(
                "SELECT MIN(sale_date) AS first FROM adobe_sales WHERE moderation_record_id = ?", (record_id,)
            ).fetchone()["first"]
            target = f"{first_sale}T00:00:00+00:00"
            marked = record["status"] != "approved"
            if marked:
                if record["concept_id"]:
                    self._set_moderation(connection, record["concept_id"], "approved", "", None, None)
                else:
                    connection.execute(
                        "UPDATE moderation_records SET status = 'approved', reason = '', decided_at = ?, "
                        "updated_at = ? WHERE id = ?", (target, timestamp, record_id),
                    )
            decided = connection.execute(
                "SELECT decided_at FROM moderation_records WHERE id = ?", (record_id,)
            ).fetchone()["decided_at"]
            if decided is None or decided > target:
                connection.execute(
                    "UPDATE moderation_records SET decided_at = ?, updated_at = ? WHERE id = ?",
                    (target, timestamp, record_id),
                )
            collection_id, detached_from = self._record_key(connection, record_id)
            months = sorted({sale_date[:7] for sale_date, _ in sales})
            conflicts = self._manual_months(connection, (collection_id, detached_from), months)
            if replace_manual is True:
                chosen = {row["month"] for row in conflicts}
            else:
                chosen = {row["month"] for row in conflicts} & set(replace_manual or ())
            replace = {(collection_id, detached_from, month) for month in chosen}
            self._recalculate_import_sales(connection, replace)
        return {
            "record_id": record_id, "created": created, "new": new, "existing": len(sales) - new,
            "marked_approved": marked, "conflict_months": [row["month"] for row in conflicts],
            "replaced_months": sorted(chosen), "replaced_manual": bool(chosen),
        }

    def delete_adobe_sales(self, record_id: str, source_file: str | None = None) -> int:
        """Desfaz importações: apaga as vendas da imagem (ou só as de um arquivo) e recalcula os meses.

        Não mexe no registro de moderação. Devolve quantas vendas foram removidas.
        """
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if source_file is None:
                removed = connection.execute(
                    "DELETE FROM adobe_sales WHERE moderation_record_id = ?", (record_id,)
                ).rowcount
            else:
                removed = connection.execute(
                    "DELETE FROM adobe_sales WHERE moderation_record_id = ? AND source_file = ?",
                    (record_id, source_file[:200]),
                ).rowcount
            self._recalculate_import_sales(connection)
        return removed

    def recalculate_import_sales(self) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._recalculate_import_sales(connection)

    @staticmethod
    def _recalculate_import_sales(
        connection: sqlite3.Connection,
        replace_manual: set[tuple[str | None, str | None, str]] = frozenset(),
    ) -> None:
        """Refaz as linhas 'import' de monthly_sales a partir das vendas e da coleção atual de cada imagem.

        Downloads = nº de vendas, receita = soma (USD) por (coleção da imagem, mês). Linhas manuais
        só mudam se a chave estiver em `replace_manual`; linhas 'import' sem vendas somem.
        """
        groups = connection.execute(
            "SELECT m.collection_id, m.detached_from, MAX(m.detached_name) AS detached_name, "
            "substr(a.sale_date, 1, 7) AS month, COUNT(*) AS n, SUM(a.amount) AS total "
            "FROM adobe_sales a JOIN moderation_records m ON m.id = a.moderation_record_id "
            "GROUP BY m.collection_id, m.detached_from, substr(a.sale_date, 1, 7)"
        ).fetchall()
        timestamp = utc_now()
        keep: set[str] = set()
        for group in groups:
            collection_id, detached_from = group["collection_id"], group["detached_from"]
            if collection_id:
                where, params = "collection_id = ?", (collection_id,)
            elif detached_from:
                where, params = "collection_id IS NULL AND detached_from = ?", (detached_from,)
            else:
                where, params = "collection_id IS NULL AND detached_from IS NULL", ()
            existing = connection.execute(
                f"SELECT id, source, downloads, revenue, currency FROM monthly_sales "
                f"WHERE {where} AND month = ?", (*params, group["month"]),
            ).fetchone()
            if existing:
                keep.add(existing["id"])
                if existing["source"] == "manual" and (collection_id, detached_from, group["month"]) not in replace_manual:
                    continue
                note = "Importado do CSV de ganhos da Adobe"
                if existing["source"] == "manual":
                    note += (
                        f"; manual anterior: {existing['downloads']} downloads, "
                        f"{existing['revenue']:.2f} {existing['currency']}"
                    )
                connection.execute(
                    "UPDATE monthly_sales SET downloads = ?, revenue = ?, currency = 'USD', "
                    "notes = ?, source = 'import', updated_at = ? WHERE id = ?",
                    (group["n"], group["total"], note, timestamp, existing["id"]),
                )
                continue
            sale_id = str(uuid.uuid4())
            keep.add(sale_id)
            connection.execute(
                "INSERT INTO monthly_sales(id, collection_id, collection_name, detached_from, month, "
                "downloads, revenue, currency, notes, source, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'USD', 'Importado do CSV de ganhos da Adobe', 'import', ?, ?)",
                (sale_id, collection_id, (group["detached_name"] or "") if detached_from and not collection_id else "",
                 detached_from if not collection_id else None, group["month"], group["n"], group["total"],
                 timestamp, timestamp),
            )
        for row in connection.execute("SELECT id FROM monthly_sales WHERE source = 'import'").fetchall():
            if row["id"] not in keep:
                connection.execute("DELETE FROM monthly_sales WHERE id = ?", (row["id"],))

    def list_adobe_sales_summary(self) -> list[dict[str, Any]]:
        """Vendas importadas por imagem, para conferência."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT m.id AS record_id, m.title, m.adobe_id, "
                "COALESCE(col.name, m.detached_name) AS collection_name, m.detached_from, "
                "COUNT(*) AS sales, SUM(a.amount) AS revenue, MIN(a.sale_date) AS first_sale, "
                "MAX(a.sale_date) AS last_sale FROM adobe_sales a "
                "JOIN moderation_records m ON m.id = a.moderation_record_id "
                "LEFT JOIN collections col ON col.id = m.collection_id "
                "GROUP BY m.id ORDER BY revenue DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    # --- Registro de horas por coleção -------------------------------------------------

    def add_work_session(
        self, collection_id: str, work_date: str, minutes: float, notes: str = "",
    ) -> str:
        """Lançamento manual de tempo trabalhado (minutos > 0 e até 24 h)."""
        if not 0 < minutes <= MAX_WORK_MINUTES:
            raise ValueError("Informe um tempo maior que zero e de no máximo 24 horas por lançamento.")
        try:
            work_date = date.fromisoformat(str(work_date)).isoformat()
        except ValueError as error:
            raise ValueError("Data do lançamento inválida.") from error
        if len(notes) > 1000:
            raise ValueError("A nota do lançamento deve ter no máximo 1000 caracteres.")
        session_id = str(uuid.uuid4())
        timestamp = utc_now()
        with self.connect() as connection:
            if connection.execute(
                "SELECT 1 FROM collections WHERE id = ?", (collection_id,)
            ).fetchone() is None:
                raise KeyError("Coleção não encontrada.")
            connection.execute(
                "INSERT INTO work_sessions(id, collection_id, work_date, started_at, ended_at, "
                "minutes, source, notes, created_at, updated_at) "
                "VALUES (?, ?, ?, NULL, NULL, ?, 'manual', ?, ?, ?)",
                (session_id, collection_id, work_date, float(minutes), notes.strip(), timestamp, timestamp),
            )
        return session_id

    def start_timer(self, collection_id: str) -> str:
        timestamp = utc_now()
        session_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM collections WHERE id = ?", (collection_id,)
            ).fetchone() is None:
                raise KeyError("Coleção não encontrada.")
            running = connection.execute(
                "SELECT w.id, c.name FROM work_sessions w JOIN collections c ON c.id = w.collection_id "
                "WHERE w.source = 'timer' AND w.ended_at IS NULL LIMIT 1"
            ).fetchone()
            if running:
                raise ValueError(
                    f"Já existe um cronômetro aberto na coleção \"{running['name']}\". "
                    "Pare ou descarte esse cronômetro antes de iniciar outro."
                )
            try:
                connection.execute(
                    "INSERT INTO work_sessions(id, collection_id, work_date, started_at, ended_at, "
                    "minutes, source, notes, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, NULL, NULL, 'timer', '', ?, ?)",
                    (session_id, collection_id, date.today().isoformat(), timestamp, timestamp, timestamp),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError(
                    "Já existe um cronômetro aberto. Pare ou descarte-o antes de iniciar outro."
                ) from error
        return session_id

    def open_timer(self) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT w.id, w.collection_id, c.name AS collection_name, w.started_at "
                "FROM work_sessions w JOIN collections c ON c.id = w.collection_id "
                "WHERE w.source = 'timer' AND w.ended_at IS NULL LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def stop_timer(
        self, session_id: str, minutes_override: float | None = None,
        max_minutes: float = 12 * 60,
    ) -> float | None:
        """Fecha o cronômetro e devolve os minutos gravados.

        Acima de `max_minutes` (e sem `minutes_override`) levanta TimerTooLong e nada é gravado.
        Com menos de 1 minuto medido (e sem override) a sessão é descartada e devolve None.
        """
        if minutes_override is not None and not 0 < minutes_override <= MAX_WORK_MINUTES:
            raise ValueError("Informe um tempo maior que zero e de no máximo 24 horas por lançamento.")
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT started_at FROM work_sessions "
                "WHERE id = ? AND source = 'timer' AND ended_at IS NULL", (session_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Não há cronômetro aberto com este identificador.")
            ended = utc_now()
            seconds = (datetime.fromisoformat(ended) - datetime.fromisoformat(row["started_at"])).total_seconds()
            measured = round(max(seconds, 0) / 60, 2)
            if minutes_override is None:
                if measured > max_minutes:
                    raise TimerTooLong(measured, max_minutes)
                if measured < 1:
                    connection.execute("DELETE FROM work_sessions WHERE id = ?", (session_id,))
                    return None
                minutes = measured
            else:
                minutes = round(float(minutes_override), 2)
            connection.execute(
                "UPDATE work_sessions SET ended_at = ?, minutes = ?, updated_at = ? WHERE id = ?",
                (ended, minutes, ended, session_id),
            )
        return minutes

    def discard_timer(self, session_id: str) -> None:
        """Descarta um cronômetro ainda aberto; sessões fechadas nunca são apagadas por aqui."""
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM work_sessions WHERE id = ? AND source = 'timer' AND ended_at IS NULL",
                (session_id,),
            )
            if cursor.rowcount == 0:
                raise ValueError("Só é possível descartar um cronômetro que ainda está aberto.")

    def list_work_sessions(self, collection_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, work_date, started_at, ended_at, minutes, source, notes "
                "FROM work_sessions WHERE collection_id = ? "
                "ORDER BY work_date DESC, created_at DESC", (collection_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_work_session(self, session_id: str) -> None:
        """Exclusão explícita (UI com confirmação). Cronômetro aberto deve ser descartado."""
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM work_sessions WHERE id = ? AND minutes IS NOT NULL", (session_id,),
            )
            if cursor.rowcount == 0:
                raise KeyError("Lançamento não encontrado (ou cronômetro ainda aberto).")

    def work_minutes_by_collection(self) -> dict[str, float]:
        """Minutos por coleção, só de sessões fechadas (manuais e cronômetros parados)."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT collection_id, SUM(minutes) AS minutes FROM work_sessions "
                "WHERE minutes IS NOT NULL GROUP BY collection_id"
            ).fetchall()
        return {row["collection_id"]: row["minutes"] or 0.0 for row in rows}

    def list_similarity_references(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM similarity_references")]

    def add_similarity_reference(self, record_id: str, path: Path, sha256: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO similarity_references "
                "(moderation_record_id, path, sha256, created_at) VALUES (?, ?, ?, ?)",
                (record_id, str(path), sha256, utc_now()),
            )

    def add_collection_cost(self, collection_id: str, entry: CollectionCostEntry) -> str:
        entry = CollectionCostEntry.model_validate(entry.model_dump())
        micros = to_micros(entry.amount)
        if micros <= 0:
            raise ValueError("O valor do custo deve ser de pelo menos 0,000001.")
        cost_id = str(uuid.uuid4())
        with self.connect() as connection:
            if not connection.execute("SELECT 1 FROM collections WHERE id = ?", (collection_id,)).fetchone():
                raise KeyError("Coleção não encontrada.")
            connection.execute(
                "INSERT INTO collection_costs(id, collection_id, cost_date, amount_micros, currency, notes, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (cost_id, collection_id, entry.cost_date.isoformat(), micros, entry.currency, entry.notes, utc_now()),
            )
        return cost_id

    def list_collection_costs(self, collection_id: str | EllipsisType = ...) -> list[dict[str, Any]]:
        query = (
            "SELECT cost.*, COALESCE(col.name, cost.collection_name) AS name FROM collection_costs cost "
            "LEFT JOIN collections col ON col.id = cost.collection_id"
        )
        params = ()
        if collection_id is not ...:
            query += " WHERE cost.collection_id = ?"
            params = (collection_id,)
        with self.connect() as connection:
            rows = connection.execute(query + " ORDER BY cost_date DESC, created_at DESC", params).fetchall()
        return [{**dict(row), "amount": row["amount_micros"] / 1_000_000, "source": "manual"} for row in rows]

    def delete_collection_cost(self, cost_id: str, collection_id: str) -> None:
        with self.connect() as connection:
            cursor = connection.execute("DELETE FROM collection_costs WHERE id = ? AND collection_id = ?",
                                        (cost_id, collection_id))
            if not cursor.rowcount:
                raise KeyError("Custo não encontrado nesta coleção.")

    def costs_for_metrics(self) -> list[dict[str, Any]]:
        rows = self.list_collection_costs()
        with self.connect() as connection:
            legacy = connection.execute(
                "SELECT collection_id, cost_currency AS currency, SUM(user_cost) AS amount "
                "FROM performance_records WHERE user_cost > 0 GROUP BY collection_id, cost_currency"
            ).fetchall()
        rows.extend({**dict(row), "source": "legacy"} for row in legacy)
        for archive in self.list_archived_work():
            rows.extend({"collection_id": None, "detached_from": archive["detached_from"],
                         "collection_name": archive["collection_name"], "currency": currency,
                         "amount": amount, "source": "legacy_archived"}
                        for currency, amount in archive["costs"].items())
        return rows
