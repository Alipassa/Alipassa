"""Qual prazo acerta mais? Teste de 5 min, 10 min, 30 min e 1 hora, no mini índice e no mini dólar.

Para cada prazo, compara o modelo do painel com regras simples, todas em barras de 5 min e só com informação conhecida no
fechamento da barra:
  - momento:  aposta que o movimento das últimas barras continua;
  - S&P líder: aposta na direção do S&P das últimas barras (mini índice: mesmo lado; mini dólar: lado oposto);
  - VWAP:     posição do preço em relação ao VWAP do dia.
Cada regra pode funcionar "a favor" ou "ao contrário" (reversão): o sentido é ESCOLHIDO na 1ª metade do histórico e o
resultado mostrado é o da 2ª metade, que a escolha não viu. As previsões são contadas sem sobreposição (uma a cada prazo)
para o intervalo de confiança não sair estreito demais, e nunca atravessam a noite. Ao lado do acerto vai o ganho médio por
contrato depois dos custos — acerto e lucro não são a mesma coisa.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

from . import model as M, simulate as SIM, stats as st
from .model import BRT

HORIZONTES = {"5 min": 1, "10 min": 2, "30 min": 6, "1 hora": 12}


def _align(ts: list[float], bars: list[tuple]) -> list[Optional[float]]:
    out, j, last = [], 0, None
    for t in ts:
        while j < len(bars) and bars[j][0] <= t:
            last = bars[j][4]
            j += 1
        out.append(last)
    return out


def _signals(bars: list[tuple], lead: list[tuple], h: int, asset: str) -> dict[str, list[Optional[int]]]:
    """Sinal (+1 alta, −1 queda, None sem sinal) por barra, para cada regra, olhando h barras para trás."""
    n = len(bars)
    ts = [b[0] for b in bars]
    c = [b[4] for b in bars]
    es = _align(ts, lead) if lead else [None] * n
    new = [i == 0 or ts[i] - ts[i - 1] > 3600 for i in range(n)]
    sess_start, vwap = [0] * n, [0.0] * n
    pv = vv = 0.0
    s0 = 0
    for i in range(n):
        if new[i]:
            pv = vv = 0.0
            s0 = i
        sess_start[i] = s0
        vol = bars[i][5] or 1.0
        pv += (bars[i][2] + bars[i][3] + bars[i][4]) / 3 * vol
        vv += vol
        vwap[i] = pv / vv
    sg = lambda x: 1 if x > 0 else -1 if x < 0 else None  # noqa: E731
    mom, lid, vw = [None] * n, [None] * n, [None] * n
    for i in range(n):
        if i - h >= sess_start[i]:
            mom[i] = sg(c[i] - c[i - h])
            if es[i] and es[i - h]:
                s = sg(es[i] - es[i - h])
                lid[i] = (s if asset == "WIN" else -s) if s else None
        if i - sess_start[i] >= 6:
            vw[i] = sg(c[i] - vwap[i])
    return {"momento": mom, "S&P líder": lid, "VWAP": vw}


def _evaluate(bars: list[tuple], sig: list[Optional[int]], h: int, asset: str, idx: list[int]) -> dict:
    """Acerto e ganho líquido nas barras `idx` (já sem sobreposição e sem atravessar a noite)."""
    hits = n = 0
    gross = []
    for i in idx:
        s = sig[i]
        if not s:
            continue
        move = bars[i + h][4] - bars[i][4]
        if move == 0:
            continue
        n += 1
        hits += 1 if s * move > 0 else 0
        gross.append(s * move)
    if not n:
        return {"n": 0}
    p = hits / n
    half = 1.96 * math.sqrt(p * (1 - p) / n)
    net = st.mean(gross) * SIM.POINT[asset] - SIM.cost(asset)
    return {"n": n, "acerto": p, "ic": (p - half, p + half), "liq": net}


def _verdict(r: dict) -> str:
    if not r.get("n"):
        return "sem previsões"
    lo, hi = r["ic"]
    return "✅ acima do acaso" if lo > 0.5 else "❌ abaixo do acaso" if hi < 0.5 else "⚪ não se distingue do acaso"


def study(asset: str, bars: list[tuple], lead: list[tuple], model_eval: dict[str, dict]) -> list[dict]:
    rows = []
    n = len(bars)
    ts = [b[0] for b in bars]
    for name, h in HORIZONTES.items():
        ok = [i for i in range(0, n - h, h) if ts[i + h] - ts[i] <= h * 300 + 900]      # não atravessa a noite
        half = len(ok) // 2
        tr, te = ok[:half], ok[half:]
        me = model_eval.get(name)
        if me:
            rows.append({"ativo": asset, "prazo": name, "regra": "modelo do painel", "sentido": "—", **me})
        for rule, sig in _signals(bars, lead, h, asset).items():
            a = _evaluate(bars, sig, h, asset, tr)
            if not a.get("n"):
                continue
            inv = a["acerto"] < 0.5                                   # escolhido na 1ª metade
            s2 = [(-x if (inv and x) else x) for x in sig]
            r = _evaluate(bars, s2, h, asset, te)
            rows.append({"ativo": asset, "prazo": name, "regra": rule, "sentido": "ao contrário (reversão)" if inv else "a favor",
                         "acerto_1a": a["acerto"] if not inv else 1 - a["acerto"], **r})
    for r in rows:
        r["veredito"] = _verdict(r)
    return rows


def run(engine) -> str:
    s = engine.cycle()
    intr, ires = engine._sim_intr, engine._intra_cache["res"]
    when = datetime.fromtimestamp(s["atualizado"], tz=BRT).strftime("%d/%m/%Y %H:%M")
    rp = lambda v: "—" if v is None else f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")  # noqa: E731
    pc = lambda v: f"{v * 100:.1f}%".replace(".", ",")  # noqa: E731
    L = ["# Qual prazo acerta mais? 5 min × 10 min × 30 min × 1 hora", "", f"Gerado em {when} (Brasília) · {s['atraso']}", "",
         "Previsões sem sobreposição e sem atravessar a noite; sentido de cada regra escolhido na 1ª metade, resultado da 2ª metade. "
         f"Custo por operação: mini índice {rp(SIM.cost('WIN'))}, mini dólar {rp(SIM.cost('WDO'))}.", ""]
    best: dict = {}
    for k in ("WIN", "WDO"):
        bars = intr.get((k, "5")) or []
        if len(bars) < 1000:
            L += [f"## {k}: histórico de 5 min insuficiente ({len(bars)} barras)", ""]
            continue
        model_eval = {}
        for (kk, tf), ir in ires.items():
            if kk == k and ir:
                for nm, m in ir["models"].items():
                    if m:
                        ev = m["eval"]
                        p, nn = ev["hit"], ev["n"]
                        half = 1.96 * math.sqrt(p * (1 - p) / nn) if nn else 0
                        model_eval[nm] = {"n": nn, "acerto": p, "ic": (p - half, p + half), "liq": None}
        rows = study(k, bars, intr.get(("ES", "5")) or [], model_eval)
        days = len({int(b[0] // 86400) for b in bars})
        L += [f"## {'Mini índice (WIN)' if k == 'WIN' else 'Mini dólar (WDO)'} — {len(bars)} barras de 5 min (~{days} pregões)", "",
              "| Prazo | Regra | Sentido | Previsões (2ª metade) | Acerto | Intervalo 95% | Ganho líquido por operação | Conclusão |",
              "| --- | --- | --- | ---: | ---: | --- | ---: | --- |"]
        for r in rows:
            if not r.get("n"):
                continue
            L.append(f"| {r['prazo']} | {r['regra']} | {r['sentido']} | {r['n']} | **{pc(r['acerto'])}** | {pc(r['ic'][0])} a {pc(r['ic'][1])} | "
                     f"{rp(r.get('liq'))} | {r['veredito']} |")
            b = best.setdefault((k, r["prazo"]), r)
            if r["acerto"] > b["acerto"]:
                best[(k, r["prazo"])] = r
        L.append("")
    L += ["## Melhor regra em cada prazo", "", "| Ativo | Prazo | Melhor regra | Acerto | Intervalo 95% | Ganho líquido | Conclusão |",
          "| --- | --- | --- | ---: | --- | ---: | --- |"]
    for (k, prazo), r in best.items():
        L.append(f"| {k} | {prazo} | {r['regra']}{' (' + r['sentido'] + ')' if r['sentido'] != '—' else ''} | **{pc(r['acerto'])}** | "
                 f"{pc(r['ic'][0])} a {pc(r['ic'][1])} | {rp(r.get('liq'))} | {r['veredito']} |")
    L += ["", "## Como ler", "",
          "- Só ✅ é acerto acima do acaso. Escolher a melhor linha entre muitas favorece a sorte: desconfie de vantagens pequenas.",
          "- O ganho líquido mostra se o acerto paga os custos: 53 % de acerto com movimento pequeno ainda perde dinheiro.",
          "- A linha \"modelo do painel\" usa o walk-forward do próprio modelo (com sobreposição nas previsões) e não tem ganho calculado.", ""]
    return "\n".join(L)
