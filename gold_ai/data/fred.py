"""Coletor FRED (CSV público, sem chave): juros reais, breakeven, spreads, Fed Funds.

Séries: DFII10 (TIPS 10Y real, %), T10YIE (breakeven 10Y, %), DGS10, DGS2 (%),
BAMLH0A0HYM2 (HY OAS, %), DFF (Fed Funds efetiva, %). Frequência diária, com defasagem
de um dia útil — usar para nível/tendência; variações intraday vêm do Yahoo.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

from .http import DataError, HttpClient

FRED_BASE = "https://fred.stlouisfed.org/graph/fredgraph.csv?id="


def parse_csv(text: str) -> list[tuple[date, float]]:
    out: list[tuple[date, float]] = []
    for ln in text.strip().splitlines()[1:]:
        parts = ln.split(",")
        if len(parts) < 2 or parts[1].strip() in (".", ""):
            continue
        try:
            out.append((date.fromisoformat(parts[0].strip()), float(parts[1])))
        except ValueError:
            continue
    return out


class FredCollector:
    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def series(self, series_id: str, ttl: int = 3600) -> list[tuple[date, float]]:
        text = self.http.get_text(FRED_BASE + series_id, ttl)
        data = parse_csv(text)
        if not data:
            raise DataError(f"série FRED vazia: {series_id}")
        return data

    def last_and_change(self, series_id: str, lookback: int = 1) -> tuple[Optional[float], Optional[float]]:
        """(último valor, variação vs `lookback` observações atrás) na unidade da série."""
        data = self.series(series_id)
        if len(data) <= lookback:
            return (data[-1][1] if data else None), None
        return data[-1][1], data[-1][1] - data[-1 - lookback][1]
