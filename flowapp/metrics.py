from __future__ import annotations

from typing import Any


def revenue_per_hour(
    performance_rows: list[dict[str, Any]], work_minutes: dict[str, float],
) -> dict[str, Any]:
    """Receita por hora, por coleção e no total, sem converter moedas.

    `performance_rows` tem uma linha por coleção e moeda (como `performance_comparison()`),
    com `collection_id`, `revenue_currency`, `revenue` e `manual_minutes`. `work_minutes` vem
    das sessões de trabalho. Horas = sessões + tempo manual lançado nos relatórios (as duas
    fontes são somadas). Sem horas, `per_hour` é None (nunca divide por zero).
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


def format_per_hour(value: float | None, currency: str) -> str:
    return "-" if value is None else f"{value:.2f} {currency}/h"
