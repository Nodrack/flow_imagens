from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass
class ParsedEarnings:
    """Vendas lidas do CSV de ganhos da Adobe (uma por linha: AAAA-MM-DD,valor)."""

    sales: list[tuple[str, float]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        return sum(amount for _, amount in self.sales)

    @property
    def months(self) -> list[str]:
        return sorted({sale_date[:7] for sale_date, _ in self.sales})


def parse_adobe_earnings(content: bytes | str) -> ParsedEarnings:
    """Lê o CSV do portal (sem cabeçalho, sem ID). Linhas inválidas viram erros listados.

    Tolera BOM, CRLF, linhas vazias e um cabeçalho (primeira linha cuja 1ª coluna não é data).
    """
    text = content.decode("utf-8-sig", errors="replace") if isinstance(content, bytes) else content
    text = text.lstrip("﻿")
    result = ParsedEarnings()
    seen_content = False
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        first_content = not seen_content
        seen_content = True
        columns = [column.strip().strip('"').strip() for column in line.split(",")]
        if not _DATE.match(columns[0]):
            if first_content:
                continue  # cabeçalho
            result.errors.append(f"Linha {number}: data inválida ({columns[0][:30]!r}).")
            continue
        try:
            date.fromisoformat(columns[0])
        except ValueError:
            result.errors.append(f"Linha {number}: data inexistente ({columns[0]}).")
            continue
        if len(columns) != 2:
            result.errors.append(f"Linha {number}: esperado data e valor separados por vírgula.")
            continue
        try:
            amount = float(columns[1])
        except ValueError:
            result.errors.append(f"Linha {number}: valor inválido ({columns[1][:30]!r}).")
            continue
        if not math.isfinite(amount) or amount < 0:
            result.errors.append(f"Linha {number}: o valor deve ser um número maior ou igual a zero.")
            continue
        result.sales.append((columns[0], amount))
    return result


def to_micros(amount: float) -> int:
    """Valor em micro-dólares: chave estável para a unicidade (REAL não é boa chave)."""
    return round(amount * 1_000_000)


def with_occurrences(sales: list[tuple[str, float]]) -> list[tuple[str, float, int]]:
    """Numera as repetições de (data, valor) dentro do arquivo: vendas iguais no mesmo dia não colapsam."""
    counts: dict[tuple[str, int], int] = {}
    numbered = []
    for sale_date, amount in sales:
        key = (sale_date, to_micros(amount))
        counts[key] = counts.get(key, 0) + 1
        numbered.append((sale_date, amount, counts[key]))
    return numbered
