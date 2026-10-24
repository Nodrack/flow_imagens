from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from flowapp.schemas import LOCAL_TZ


def collection_key(row: dict[str, Any]) -> str | None:
    """Chave de agrupamento: id da coleção; coleção removida vira "removida:<id original>"; senão None."""
    if row.get("collection_id"):
        return row["collection_id"]
    if row.get("detached_from"):
        return f"removida:{row['detached_from']}"
    return None


def revenue_per_hour(
    performance_rows: list[dict[str, Any]], work_minutes: dict[str, float],
    sales_rows: list[dict[str, Any]] = (), archived_rows: list[dict[str, Any]] = (),
) -> dict[str, Any]:
    """Receita por hora, por coleção e no total, sem converter moedas.

    `performance_rows` tem uma linha por coleção e moeda (como `performance_comparison()`),
    com `collection_id`, `revenue_currency`, `revenue` e `manual_minutes`. `work_minutes` vem
    das sessões de trabalho. Horas = sessões + tempo manual lançado nos relatórios (as duas
    fontes são somadas). A receita soma os registros avulsos (`performance_rows`) e os
    lançamentos mensais (`sales_rows`: `collection_id` ou None, `currency`, `revenue`).
    `archived_rows` traz as horas (`minutes`, `manual_minutes`) e a receita avulsa (`revenue`,
    {moeda: valor}) de coleções removidas, guardadas na remoção (`detached_from`), para o total
    não mudar ao remover uma coleção. Lançamentos e arquivos de coleção removida são agrupados
    por `detached_from` (chave "removida:<id>"); sem vínculo algum a chave é None, sem horas.
    Sem horas, `per_hour` é None (nunca divide por zero).
    """
    minutes: dict[str, float] = {}
    revenue: dict[str, dict[str, float]] = {}
    for row in performance_rows:
        cid = row["collection_id"]
        minutes[cid] = minutes.get(cid, 0.0) + float(row.get("manual_minutes") or 0)
        currency = row.get("revenue_currency")
        if currency:
            by_currency = revenue.setdefault(cid, {})
            by_currency[currency] = by_currency.get(currency, 0.0) + float(row.get("revenue") or 0)
    for row in sales_rows:
        cid = collection_key(row)
        minutes.setdefault(cid, 0.0)
        by_currency = revenue.setdefault(cid, {})
        by_currency[row["currency"]] = by_currency.get(row["currency"], 0.0) + float(row["revenue"] or 0)
    for row in archived_rows:
        cid = collection_key({"detached_from": row["detached_from"]})
        minutes[cid] = (
            minutes.get(cid, 0.0) + float(row.get("minutes") or 0) + float(row.get("manual_minutes") or 0)
        )
        by_currency = revenue.setdefault(cid, {})
        for currency, amount in (row.get("revenue") or {}).items():
            by_currency[currency] = by_currency.get(currency, 0.0) + float(amount or 0)
    for cid, value in work_minutes.items():
        minutes[cid] = minutes.get(cid, 0.0) + float(value or 0)

    def per_hour(amount: float, hours: float) -> float | None:
        return amount / hours if hours > 0 else None

    collections: dict[str, Any] = {}
    totals: dict[str, float] = {}
    for cid, total_minutes in minutes.items():
        hours = total_minutes / 60
        collections[cid] = {
            "hours": hours,
            "by_currency": {
                currency: {"revenue": amount, "per_hour": per_hour(amount, hours)}
                for currency, amount in revenue.get(cid, {}).items()
            },
        }
        for currency, amount in revenue.get(cid, {}).items():
            totals[currency] = totals.get(currency, 0.0) + amount
    total_hours = sum(minutes.values()) / 60
    return {
        "collections": collections,
        "total": {
            "hours": total_hours,
            "by_currency": {
                currency: {"revenue": amount, "per_hour": per_hour(amount, total_hours)}
                for currency, amount in sorted(totals.items())
            },
        },
    }


def format_hours(hours: float) -> str:
    total_minutes = round(hours * 60)
    return f"{total_minutes // 60}h{total_minutes % 60:02d}"


def result_after_costs(hours_metrics: dict[str, Any], cost_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Revenue minus reported costs in each currency; no exchange rates or consolidated profit."""
    costs: dict[str | None, dict[str, float]] = {}
    for row in cost_rows:
        currencies = costs.setdefault(collection_key(row), {})
        currency = row["currency"]
        currencies[currency] = currencies.get(currency, 0.0) + float(row["amount"])

    def cell(revenue, cost, hours):
        net = revenue - cost
        return {"revenue": revenue, "cost": cost, "result": net,
                "per_hour": net/hours if hours > 0 else None}

    collections, totals = {}, {}
    for key in hours_metrics["collections"].keys() | costs.keys():
        gross = hours_metrics["collections"].get(key, {"hours": 0, "by_currency": {}})
        by_currency = {}
        for currency in gross["by_currency"].keys() | costs.get(key, {}).keys():
            revenue = gross["by_currency"].get(currency, {}).get("revenue", 0.0)
            cost = costs.get(key, {}).get(currency, 0.0)
            by_currency[currency] = cell(revenue, cost, gross["hours"])
            total = totals.setdefault(currency, {"revenue": 0.0, "cost": 0.0})
            total["revenue"] += revenue
            total["cost"] += cost
        collections[key] = {"hours": gross["hours"], "by_currency": by_currency}
    total_hours = hours_metrics["total"]["hours"]
    return {"collections": collections, "total": {"hours": total_hours,
            "by_currency": {currency: cell(values["revenue"], values["cost"], total_hours)
                            for currency, values in sorted(totals.items())}}}


def format_per_hour(value: float | None, currency: str) -> str:
    return "-" if value is None else f"{value:.2f} {currency}/h"


# --- Indicadores de moderação e vendas mensais (dados informados; sem conversão nem projeção) ---

def _local(timestamp: str | None) -> datetime | None:
    """Timestamp ISO do banco (UTC) no fuso de Madri."""
    if not timestamp:
        return None
    moment = datetime.fromisoformat(timestamp)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(LOCAL_TZ)


def _rate(approved: int, rejected: int) -> float | None:
    decided = approved + rejected
    return approved / decided if decided else None


def approval_rate(moderation_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aprovadas / (aprovadas + rejeitadas), pendentes fora; por coleção (None = sem coleção) e total."""
    counts: dict[str | None, dict[str, int]] = {}
    for row in moderation_rows:
        item = counts.setdefault(collection_key(row), {"approved": 0, "rejected": 0, "pending": 0})
        if row["status"] in item:
            item[row["status"]] += 1
    collections = {cid: {**item, "rate": _rate(item["approved"], item["rejected"])} for cid, item in counts.items()}
    total = {key: sum(item[key] for item in counts.values()) for key in ("approved", "rejected", "pending")}
    return {"collections": collections, "total": {**total, "rate": _rate(total["approved"], total["rejected"])}}


def _approved_at(row: dict[str, Any]) -> datetime | None:
    """Quando a imagem foi aprovada (Madri): decided_at; sem ele, submitted_at; sem ele, updated_at."""
    if row["status"] != "approved":
        return None
    return _local(row.get("decided_at") or row.get("submitted_at") or row.get("updated_at"))


def approved_per_week(
    moderation_rows: list[dict[str, Any]], now: datetime, weeks: int = 12,
) -> dict[str, Any]:
    """Aprovadas por semana ISO (segunda a domingo, em Madri) nas últimas `weeks` semanas.

    A série inclui semanas com zero e a semana atual (`complete` False). A média usa só semanas
    completas, a partir da semana da primeira aprovação, e é None sem nenhuma.
    """
    today = now.astimezone(LOCAL_TZ).date()
    current_monday = today - timedelta(days=today.weekday())
    starts = [current_monday - timedelta(weeks=offset) for offset in range(weeks - 1, -1, -1)]
    counts = dict.fromkeys(starts, 0)
    moments = [moment for moment in map(_approved_at, moderation_rows) if moment is not None]
    for moment in moments:
        monday = moment.date() - timedelta(days=moment.weekday())
        if monday in counts:
            counts[monday] += 1
    series = [
        {"week": f"{start.isocalendar().year}-S{start.isocalendar().week:02d}", "start": start,
         "approved": count, "complete": start < current_monday}
        for start, count in counts.items()
    ]
    average = None
    if moments:
        first = min(moments).date()
        first_monday = first - timedelta(days=first.weekday())
        complete = [item["approved"] for item in series if item["complete"] and item["start"] >= first_monday]
        average = sum(complete) / len(complete) if complete else None
    return {"series": series, "average": average}


def _month_end(month: str) -> date:
    year, number = int(month[:4]), int(month[5:])
    return (date(year + number // 12, number % 12 + 1, 1)) - timedelta(days=1)


def downloads_per_approved(
    sales_rows: list[dict[str, Any]], moderation_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Para cada mês lançado: downloads do mês / imagens aprovadas acumuladas até o fim dele.

    Há um lançamento por coleção e mês (a moeda só rotula a receita). Retorna `collections` (chave None = sem coleção/removidas)
    e `total`, cada um como {mês: {"downloads", "approved", "per_image"}}; sem aprovadas, per_image None.
    """
    approvals: dict[str | None, list[date]] = {}
    for row in moderation_rows:
        decided = _approved_at(row)
        if decided is not None:
            approvals.setdefault(collection_key(row), []).append(decided.date())

    def cell(downloads: int, dates: list[date], month: str) -> dict[str, Any]:
        end = _month_end(month)
        approved = sum(1 for item in dates if item <= end)
        return {"downloads": downloads, "approved": approved,
                "per_image": downloads / approved if approved else None}

    by_collection: dict[str | None, dict[str, int]] = {}
    by_month: dict[str, int] = {}
    for row in sales_rows:
        month_totals = by_collection.setdefault(collection_key(row), {})
        month_totals[row["month"]] = int(row["downloads"] or 0)
        by_month[row["month"]] = by_month.get(row["month"], 0) + int(row["downloads"] or 0)
    everything = [item for dates in approvals.values() for item in dates]
    return {
        "collections": {
            cid: {month: cell(value, approvals.get(cid, []), month) for month, value in sorted(months.items())}
            for cid, months in by_collection.items()
        },
        "total": {month: cell(value, everything, month) for month, value in sorted(by_month.items())},
    }
