"""Banco Central do Brasil (SGS) — Selic meta, CDI e PTAX. Sem chave."""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from .http import DataError, get_json

SGS = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{code}/dados/ultimos/{n}?formato=json"
SERIES = {"selic_meta": 432, "ptax_venda": 1, "cdi_dia": 12, "ipca_12m": 13522}


def last_value(code: int, n: int = 20) -> Optional[tuple[str, float]]:
    """Último valor com data ≤ hoje (a Selic meta é publicada com datas futuras até o próximo Copom)."""
    rows = get_json(SGS.format(code=code, n=n), timeout=12, retries=1)
    today = date.today()
    best = first = None
    for r in rows if isinstance(rows, list) else []:
        try:
            d = datetime.strptime(r["data"], "%d/%m/%Y").date()
            v = float(str(r["valor"]).replace(",", "."))
        except (KeyError, ValueError):
            continue
        first = first or (today.isoformat(), v)
        if d <= today:
            best = (d.isoformat(), v)
    return best or first   # série só com datas futuras (Selic meta entre reuniões): o valor vigente é o primeiro


def snapshot() -> dict:
    out: dict = {}
    for name, code in SERIES.items():
        try:
            v = last_value(code)
            if v:
                out[name] = {"data": v[0], "valor": v[1]}
        except DataError:
            continue
    return out
