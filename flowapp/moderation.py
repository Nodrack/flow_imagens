from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any

from flowapp.schemas import LOCAL_TZ

MODERATION_LABELS = {None: "Não enviada", "pending": "Pendente", "approved": "Aprovada", "rejected": "Rejeitada"}
MODERATION_BY_LABEL = {label: status for status, label in MODERATION_LABELS.items()}


def today_local() -> date:
    """Hoje no fuso de Madri (a mesma referência do banco e da interface)."""
    return datetime.now(LOCAL_TZ).date()


def to_date(value: Any) -> date | None:
    """Converte a célula de data de uma tabela editável em date.

    Aceita None/NaT (vazio), str ISO (o Streamlit devolve texto quando a coluna veio toda vazia),
    Timestamp/datetime e date. Qualquer outro tipo é um erro (TypeError).
    """
    if value is None or value != value:
        return None
    if isinstance(value, str):
        text = value.strip()
        return date.fromisoformat(text[:10]) if text else None
    if isinstance(value, datetime) or hasattr(value, "to_pydatetime"):
        return value.date()
    if isinstance(value, date):
        return value
    raise TypeError(f"Data inválida: {value!r}.")


def moderation_scopes(collection_id: str | None, external_id: str | None) -> list[str | None]:
    """Coleções cujos registros sem conceito aparecem na tabela de externos: a ativa, a externa e os órfãos (None)."""
    return [*dict.fromkeys(scope for scope in (collection_id, external_id) if scope), None]


def local_noon_utc(day: date) -> str:
    """Data local de Madri gravada como meio-dia de Madri em UTC (evita virar o dia no fuso)."""
    return datetime.combine(day, time(12, 0), tzinfo=LOCAL_TZ).astimezone(UTC).isoformat(timespec="seconds")


def local_date(timestamp: str | None) -> date | None:
    """Data local de Madri de um timestamp ISO do banco (UTC)."""
    if not timestamp:
        return None
    moment = datetime.fromisoformat(timestamp)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(LOCAL_TZ).date()


def moderation_changes(
    original: dict[str, dict[str, Any]], edited: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compara a tabela editada com a original, por id, e devolve só as linhas alteradas.

    Cada linha traz `status` (None = não enviada), `reason`, `adobe_id` e, opcionalmente,
    `submitted_on` e `decided_on` (datas locais ou None); a original também traz `title`.
    Cada item devolvido tem `id`, `title`, `status`, `reason`, `adobe_id`, `submitted_on`,
    `decided_on`, `submitted_changed`, `decided_changed`, `error` (impede o salvamento) e
    `warning` (a linha é ignorada). Mudar só uma data de uma linha já decidida conta como alteração.
    """
    changes = []
    for row_id, before in original.items():
        after = edited.get(row_id)
        if after is None:
            continue
        values = {
            "status": after["status"],
            "reason": (after.get("reason") or "").strip(),
            "adobe_id": (after.get("adobe_id") or "").strip(),
            "submitted_on": after.get("submitted_on"),
            "decided_on": after.get("decided_on"),
        }
        previous = {key: before.get(key) for key in values}
        if values == previous:
            continue
        title = before.get("title", "")
        item = {
            "id": row_id, "title": title, **values,
            "submitted_changed": values["submitted_on"] != previous["submitted_on"],
            "decided_changed": values["decided_on"] != previous["decided_on"],
            "error": None, "warning": None,
        }
        if values["status"] is None and before["status"] is not None:
            item["error"] = f"{title}: o envio já foi registrado e não pode voltar para 'Não enviada'."
        elif values["status"] is None:
            item["warning"] = (
                f"{title}: escolha o resultado (Pendente, Aprovada ou Rejeitada) para salvar motivo, ID ou datas."
            )
        elif (
            (item["submitted_changed"] and values["submitted_on"] is None)
            or (item["decided_changed"] and values["decided_on"] is None and values["status"] != "pending")
        ):
            item["error"] = f"{title}: uma data já registrada não pode ser apagada; escolha outra data."
        changes.append(item)
    return changes
