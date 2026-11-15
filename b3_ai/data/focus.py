"""Boletim Focus (Banco Central) — mediana das projeções de ~140 instituições para Selic, IPCA, PIB e câmbio."""

from __future__ import annotations

import urllib.parse
from datetime import date

from .http import DataError, get_json

BASE = "https://olinda.bcb.gov.br/olinda/servico/Expectativas/versao/v1/odata/ExpectativasMercadoAnuais"
INDICADORES = {"Selic": "Selic fim do ano (% a.a.)", "IPCA": "IPCA no ano (%)", "PIB Total": "PIB no ano (%)", "Câmbio": "Dólar fim do ano (R$)"}


def fetch_focus() -> dict:
    """{indicador: [{ano, mediana, ha_4_semanas, revisao, data, n}]} para o ano corrente e o seguinte."""
    years = [str(date.today().year), str(date.today().year + 1)]
    out: dict = {}
    for ind in INDICADORES:
        q = {"$top": "400", "$format": "json", "$orderby": "Data desc", "$filter": f"Indicador eq '{ind}'"}
        try:
            rows = get_json(BASE + "?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote), timeout=25, retries=1).get("value") or []
        except DataError:
            continue
        rows = [r for r in rows if r.get("baseCalculo") == 0 and r.get("Mediana") is not None]
        dates = sorted({r["Data"] for r in rows}, reverse=True)
        if not dates:
            continue
        old = dates[min(20, len(dates) - 1)]          # ~4 semanas (20 dias úteis) atrás
        items = []
        for y in years:
            now = next((r for r in rows if r["Data"] == dates[0] and r["DataReferencia"] == y), None)
            if not now:
                continue
            prev = next((r for r in rows if r["Data"] == old and r["DataReferencia"] == y), None)
            items.append({"ano": y, "mediana": now["Mediana"], "ha_4_semanas": prev["Mediana"] if prev else None,
                          "revisao": (now["Mediana"] - prev["Mediana"]) if prev else None, "data": dates[0],
                          "n": now.get("numeroRespondentes")})
        if items:
            out[ind] = {"nome": INDICADORES[ind], "itens": items}
    return out
