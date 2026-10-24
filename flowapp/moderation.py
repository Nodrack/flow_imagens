from __future__ import annotations

from typing import Any

MODERATION_LABELS = {None: "Não enviada", "pending": "Pendente", "approved": "Aprovada", "rejected": "Rejeitada"}
MODERATION_BY_LABEL = {label: status for status, label in MODERATION_LABELS.items()}


def moderation_changes(
    original: dict[str, dict[str, Any]], edited: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compara a tabela editada com a original, por id, e devolve só as linhas alteradas.

    Cada linha traz `status` (None = não enviada), `reason` e `adobe_id`; a original também
    traz `title`. Cada item devolvido tem `id`, `title`, `status`, `reason`, `adobe_id`,
    `error` (impede o salvamento) e `warning` (a linha é ignorada).
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
        }
        if values == {key: before[key] for key in values}:
            continue
        title = before.get("title", "")
        item = {"id": row_id, "title": title, **values, "error": None, "warning": None}
        if values["status"] is None and before["status"] is not None:
            item["error"] = f"{title}: o envio já foi registrado e não pode voltar para 'Não enviada'."
        elif values["status"] is None:
            item["warning"] = (
                f"{title}: escolha o resultado (Pendente, Aprovada ou Rejeitada) para salvar motivo ou ID."
            )
        changes.append(item)
    return changes
