"""Coletor CFTC — Commitment of Traders (Disaggregated, Futures Only) via API pública Socrata.

Dataset 72hh-3qpy; ouro COMEX = código 088691. Campos usados:
m_money_positions_long_all / short_all (Managed Money), prod_merc_* + swap_* (Commercials).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from .http import DataError, HttpClient

# Dois relatórios públicos (Socrata): Disaggregated (commodities: ouro, petróleo…) e Traders in Financial Futures
# (moedas, índices, juros). O código do contrato só existe em UM deles — o coletor escolhe pelo código e tenta o outro se vier vazio.
CFTC_URL = ("https://publicreporting.cftc.gov/resource/72hh-3qpy.json?cftc_contract_market_code={code}"
       "&$order=report_date_as_yyyy_mm_dd%20DESC&$limit={limit}")
CFTC_TFF_URL = ("https://publicreporting.cftc.gov/resource/gpe5-46if.json?cftc_contract_market_code={code}"
                "&$order=report_date_as_yyyy_mm_dd%20DESC&$limit={limit}")
GOLD_CODE = "088691"
FINANCIAL_CODES = {"099741", "097741", "096742", "090741", "092741", "232741", "112741", "098662",   # EUR, JPY, GBP, CAD, CHF, AUD, NZD, DXY
                   "13874A", "13874+", "209742", "124603", "043602", "020601"}                          # ES, ES consolidado, NQ, YM, ZN, ZB


@dataclass
class CotReading:
    report_date: date
    managed_money_net: float
    managed_money_net_change: float
    managed_money_percentile: float  # 0..100 sobre a janela histórica
    commercial_net: float
    commercial_net_change: float
    history_weeks: int


def _f(row: dict, key: str) -> float:
    try:
        return float(row.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def parse_rows(rows: list[dict]) -> CotReading:
    if len(rows) < 2:
        raise DataError("COT: menos de duas semanas de dados")
    rows = sorted(rows, key=lambda r: r.get("report_date_as_yyyy_mm_dd", ""))
    if "lev_money_positions_long_all" in rows[-1]:      # Financial Futures: Leveraged Funds ≈ managed money; Dealer ≈ comercial
        mm = [_f(r, "lev_money_positions_long_all") - _f(r, "lev_money_positions_short_all") for r in rows]
        com = [_f(r, "dealer_positions_long_all") - _f(r, "dealer_positions_short_all") for r in rows]
    else:
        mm = [_f(r, "m_money_positions_long_all") - _f(r, "m_money_positions_short_all") for r in rows]
        com = [(_f(r, "prod_merc_positions_long_all") + _f(r, "swap_positions_long_all"))
               - (_f(r, "prod_merc_positions_short_all") + _f(r, "swap_positions_short_all")) for r in rows]
    last = mm[-1]
    pct = 100.0 * sum(1 for x in mm if x <= last) / len(mm)
    d = date.fromisoformat(rows[-1]["report_date_as_yyyy_mm_dd"][:10])
    return CotReading(d, last, mm[-1] - mm[-2], round(pct, 1), com[-1], com[-1] - com[-2], len(rows))


class CftcCollector:
    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def gold(self, weeks: int = 156, code: str = GOLD_CODE) -> CotReading:
        urls = [CFTC_TFF_URL, CFTC_URL] if code in FINANCIAL_CODES else [CFTC_URL, CFTC_TFF_URL]
        last_err: Optional[Exception] = None
        for url in urls:
            try:
                rows = self.http.get_json(url.format(code=code, limit=weeks), ttl=6 * 3600)
                if not isinstance(rows, list):
                    raise DataError("COT: resposta inesperada")
                return parse_rows(rows)
            except DataError as e:          # vazio/curto neste relatório → tenta o outro
                last_err = e
        raise DataError(f"COT {code}: {last_err}")
