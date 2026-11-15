"""Mercado à vista da B3: todas as empresas listadas (ações, units), BDRs, FIIs e ETFs — amplitude, setores, destaques."""

from __future__ import annotations

import re

from .universe import SECTOR_PT

MIN_LIQ = 3_000_000      # R$ negociados no pregão para entrar em destaques e amplitude


def _kind(r: dict) -> str:
    t, s = r.get("type"), r.get("subtype") or ""
    if t == "stock":
        return "Ação"
    if t == "dr":
        return "BDR"
    if s == "etf":
        return "ETF"
    return "FII/Fundo"


def process(rows: list[dict]) -> dict:
    names = {r.get("name") for r in rows}
    out = []
    for r in rows:
        tk = r.get("name") or ""
        if r.get("close") is None or not tk:
            continue
        if re.fullmatch(r"[A-Z0-9]{4}\d{1,2}F", tk) and tk[:-1] in names:
            continue   # mercado fracionário: duplicata do lote padrão
        sec = r.get("sector")
        out.append({
            "t": tk, "n": r.get("description") or tk, "k": _kind(r), "s": SECTOR_PT.get(sec or "", sec or "—"),
            "p": r.get("close"), "c": r.get("change"), "v": r.get("Value.Traded") or 0, "mc": r.get("market_cap_basic"),
            "rsi": r.get("RSI"), "w": r.get("Perf.W"), "m": r.get("Perf.1M"), "y": r.get("Perf.YTD"),
            "pe": r.get("price_earnings_ttm"), "dy": r.get("dividend_yield_recent"),
            "a50": (r["close"] > r["SMA50"]) if r.get("SMA50") else None,
            "a200": (r["close"] > r["SMA200"]) if r.get("SMA200") else None,
            "rec": r.get("Recommend.All"), "rv": r.get("relative_volume_10d_calc"),
            # consenso de analistas (casas que cobrem o papel): compra · neutro · venda
            "ab": (r.get("recommendation_buy") or 0) + (r.get("recommendation_over") or 0), "ah": r.get("recommendation_hold") or 0,
            "av": (r.get("recommendation_under") or 0) + (r.get("recommendation_sell") or 0), "at": r.get("recommendation_total") or 0,
        })
    stocks = [x for x in out if x["k"] == "Ação"]
    liquid = [x for x in stocks if x["v"] >= MIN_LIQ and x["c"] is not None]
    up = sum(1 for x in liquid if x["c"] > 0.05)
    down = sum(1 for x in liquid if x["c"] < -0.05)
    tot_v = sum(x["v"] for x in liquid) or 1.0
    a50 = [x for x in liquid if x["a50"] is not None]
    a200 = [x for x in liquid if x["a200"] is not None]
    sectors: dict[str, dict] = {}
    for x in liquid:
        s = sectors.setdefault(x["s"], {"setor": x["s"], "n": 0, "altas": 0, "v": 0.0, "_cv": 0.0})
        s["n"] += 1
        s["altas"] += 1 if x["c"] > 0.05 else 0
        s["v"] += x["v"]
        s["_cv"] += x["c"] * x["v"]
    sec_list = []
    for s in sectors.values():
        s["var"] = s.pop("_cv") / s["v"] if s["v"] else 0.0
        if s["n"] >= 2:
            sec_list.append(s)
    sec_list.sort(key=lambda s: -s["var"])
    by_c = sorted(liquid, key=lambda x: x["c"])
    pick = lambda xs: [{"t": x["t"], "n": x["n"], "c": x["c"], "p": x["p"], "v": x["v"]} for x in xs]  # noqa: E731
    counts: dict[str, int] = {}
    for x in out:
        counts[x["k"]] = counts.get(x["k"], 0) + 1
    cov = [x for x in stocks if x["at"] >= 3]
    tot_rec = sum(x["at"] for x in cov) or 1
    share = lambda x: x["ab"] / x["at"]  # noqa: E731
    apick = lambda xs: [{"t": x["t"], "n": x["n"], "c": x["c"], "ab": x["ab"], "ah": x["ah"], "av": x["av"], "at": x["at"]} for x in xs]  # noqa: E731
    deep = [x for x in cov if x["at"] >= 5]
    analysts = {
        "cobertas": len(cov), "recomendacoes": sum(x["at"] for x in cov),
        "pct_compra": sum(x["ab"] for x in cov) / tot_rec, "pct_neutro": sum(x["ah"] for x in cov) / tot_rec,
        "pct_venda": sum(x["av"] for x in cov) / tot_rec,
        "preferidas": apick(sorted(deep, key=lambda x: (-share(x), -x["at"]))[:8]),
        "evitadas": apick(sorted(deep, key=lambda x: (share(x) - x["av"] / x["at"], -x["at"]))[:8]),
    } if cov else None
    summary = {
        "analistas": analysts,
        "total": len(out), "por_tipo": counts, "acoes": len(stocks), "liquidas": len(liquid),
        "altas": up, "quedas": down, "estaveis": len(liquid) - up - down,
        "pct_altas": up / len(liquid) if liquid else None,
        "var_media_pond": sum(x["c"] * x["v"] for x in liquid) / tot_v if liquid else None,
        "acima_mm50": sum(1 for x in a50 if x["a50"]) / len(a50) if a50 else None,
        "acima_mm200": sum(1 for x in a200 if x["a200"]) / len(a200) if a200 else None,
        "volume_total": sum(x["v"] for x in stocks),
        "setores": sec_list, "maiores_altas": pick(by_c[::-1][:8]), "maiores_quedas": pick(by_c[:8]),
        "mais_negociadas": pick(sorted(liquid, key=lambda x: -x["v"])[:8]),
    }
    return {"resumo": summary, "papeis": out}
