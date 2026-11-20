"""Qual prazo acerta mais? Teste de 5 min, 10 min, 30 min e 1 hora, no mini índice e no mini dólar.

Para cada prazo, compara o modelo do painel com regras simples, todas em barras de 5 min e só com informação conhecida no
fechamento da barra:
  - momento:  aposta que o movimento das últimas barras continua;
  - S&P líder: aposta na direção do S&P das últimas barras (mini índice: mesmo lado; mini dólar: lado oposto);
  - VWAP:     posição do preço em relação ao VWAP do dia;
  - cruzamento de médias (EMA 9 × EMA 21) e MACD (12, 26, 9) cruzando a linha de sinal: sinal SÓ no candle do cruzamento,
    calculados no gráfico de 5 min e no de 15 min. Depois de um sinal, o próximo só conta depois de encerrado o prazo.
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
HORIZONTES_15 = {"15 min": 1, "30 min": 2, "1 hora": 4}
EVENTOS = ("cruzamento EMA 9×21", "MACD × sinal")


def _crosses(c: list[float]) -> dict[str, list[Optional[int]]]:
    """+1/−1 só no candle em que a EMA 9 cruza a EMA 21 (ou o MACD cruza a linha de sinal), None nos demais."""
    n = len(c)
    e9, e21 = st.ema(c, 9), st.ema(c, 21)
    macd = [a - b for a, b in zip(st.ema(c, 12), st.ema(c, 26))]
    sig = st.ema(macd, 9)
    out = {"cruzamento EMA 9×21": [None] * n, "MACD × sinal": [None] * n}
    for i in range(35, n):
        for name, a, b in (("cruzamento EMA 9×21", e9, e21), ("MACD × sinal", macd, sig)):
            d0, d1 = a[i - 1] - b[i - 1], a[i] - b[i]
            if d0 <= 0 < d1:
                out[name][i] = 1
            elif d0 >= 0 > d1:
                out[name][i] = -1
    return out


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
    return {"momento": mom, "S&P líder": lid, "VWAP": vw, **_crosses(c)}


def _event_idx(bars: list[tuple], sig: list[Optional[int]], h: int, span: int, lo: int, hi: int) -> list[int]:
    """Candles de sinal entre lo e hi, sem sobreposição (o próximo só depois de h candles) e sem atravessar a noite."""
    out, busy = [], -1
    for i in range(max(lo, 1), min(hi, len(bars) - h)):
        if sig[i] and i > busy and bars[i + h][0] - bars[i][0] <= h * span + 900:
            out.append(i)
            busy = i + h
    return out


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


def study(asset: str, bars: list[tuple], lead: list[tuple], model_eval: dict[str, dict],
          horizontes: Optional[dict] = None, tf: int = 5, so_eventos: bool = False) -> list[dict]:
    horizontes = horizontes or HORIZONTES
    rows = []
    n = len(bars)
    ts = [b[0] for b in bars]
    span = tf * 60
    grafico = f"gráfico de {tf} min"
    sigs = _signals(bars, lead, 1, asset) if so_eventos else None
    for name, h in horizontes.items():
        ok = [i for i in range(0, n - h, h) if ts[i + h] - ts[i] <= h * span + 900]      # não atravessa a noite
        half_i = n // 2
        tr, te = [i for i in ok if i < half_i], [i for i in ok if i >= half_i]
        me = model_eval.get(name)
        if me and not so_eventos:
            rows.append({"ativo": asset, "prazo": name, "regra": "modelo do painel", "sentido": "—", "grafico": grafico, **me})
        for rule, sig in (sigs or _signals(bars, lead, h, asset)).items():
            evento = rule in EVENTOS
            if so_eventos and not evento:
                continue
            idx_tr = _event_idx(bars, sig, h, span, 0, half_i) if evento else tr
            idx_te = _event_idx(bars, sig, h, span, half_i, n) if evento else te
            a = _evaluate(bars, sig, h, asset, idx_tr)
            if not a.get("n"):
                continue
            inv = a["acerto"] < 0.5                                   # escolhido na 1ª metade
            s2 = [(-x if (inv and x) else x) for x in sig]
            r = _evaluate(bars, s2, h, asset, idx_te)
            rows.append({"ativo": asset, "prazo": name, "regra": rule, "grafico": grafico,
                         "sentido": "ao contrário (reversão)" if inv else "a favor", "acerto_1a": a["acerto"] if not inv else 1 - a["acerto"], **r})
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
    cross_rows: list[dict] = []
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
        b15 = intr.get((k, "15")) or []
        if len(b15) > 500:
            rows += study(k, b15, [], {}, HORIZONTES_15, tf=15, so_eventos=True)
        days = len({int(b[0] // 86400) for b in bars})
        L += [f"## {'Mini índice (WIN)' if k == 'WIN' else 'Mini dólar (WDO)'} — {len(bars)} barras de 5 min (~{days} pregões)", "",
              "| Prazo | Regra | Gráfico | Sentido | Previsões (2ª metade) | Acerto | Intervalo 95% | Ganho líquido por operação | Conclusão |",
              "| --- | --- | --- | --- | ---: | ---: | --- | ---: | --- |"]
        for r in rows:
            if not r.get("n"):
                continue
            L.append(f"| {r['prazo']} | {r['regra']} | {r['grafico'].replace('gráfico de ', '')} | {r['sentido']} | {r['n']} | **{pc(r['acerto'])}** | {pc(r['ic'][0])} a {pc(r['ic'][1])} | "
                     f"{rp(r.get('liq'))} | {r['veredito']} |")
            if r["regra"] in EVENTOS:
                cross_rows.append(r)
            b = best.setdefault((k, r["prazo"]), r)
            if r["acerto"] > b["acerto"]:
                best[(k, r["prazo"])] = r
        L.append("")
    L += ["## Cruzamento de médias e MACD, por prazo", "", "| Ativo | Gráfico | Regra | Prazo | Previsões | Acerto | Ganho líquido | Conclusão |",
          "| --- | --- | --- | --- | ---: | ---: | ---: | --- |"]
    for r in sorted(cross_rows, key=lambda r: (r["ativo"], r["grafico"], r["regra"], r["prazo"])):
        L.append(f"| {r['ativo']} | {r['grafico'].replace('gráfico de ', '')} | {r['regra']} ({'reversão' if 'contrário' in r['sentido'] else 'a favor'}) | "
                 f"{r['prazo']} | {r['n']} | **{pc(r['acerto'])}** | {rp(r.get('liq'))} | {r['veredito']} |")
    L += ["", "## Melhor regra em cada prazo", "", "| Ativo | Prazo | Melhor regra | Acerto | Intervalo 95% | Ganho líquido | Conclusão |",
          "| --- | --- | --- | ---: | --- | ---: | --- |"]
    for (k, prazo), r in best.items():
        L.append(f"| {k} | {prazo} | {r['regra']}{' (' + r['sentido'] + ')' if r['sentido'] != '—' else ''}{', gráfico de 15 min' if r.get('grafico') == 'gráfico de 15 min' else ''} | **{pc(r['acerto'])}** | "
                 f"{pc(r['ic'][0])} a {pc(r['ic'][1])} | {rp(r.get('liq'))} | {r['veredito']} |")
    L += ["", "## Como ler", "",
          "- Só ✅ é acerto acima do acaso. Escolher a melhor linha entre muitas favorece a sorte: desconfie de vantagens pequenas.",
          "- O ganho líquido mostra se o acerto paga os custos: 53 % de acerto com movimento pequeno ainda perde dinheiro.",
          "- A linha \"modelo do painel\" usa o walk-forward do próprio modelo (com sobreposição nas previsões) e não tem ganho calculado.", ""]
    return "\n".join(L)
