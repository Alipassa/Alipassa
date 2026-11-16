"""Simulação com custos: as previsões viram operações de 1 contrato, com corretagem/emolumentos e escorregamento.

    python -m b3_ai simulacao            # (com --fonte mt5 usa o histórico do MetaTrader)

Responde "isso dá lucro?", que é diferente de "isso acerta?". Cada estratégia usa só previsões fora da amostra (o modelo
nunca viu o período em que opera) e sai com: nº de operações, acerto, ganho médio líquido, total, maior sequência de perdas,
pior queda acumulada e um intervalo de 95 % do ganho médio — só há "lucro comprovado" quando o limite inferior é positivo.

Premissas (ajuste no .env): 1 mini contrato; valor do ponto WIN R$ 0,20 e WDO R$ 10; escorregamento de 1 tick na entrada e 1
na saída (WIN 5 pontos, WDO 0,5 ponto); custo fixo por operação completa B3AI_CUSTO_WIN (padrão R$ 0,60) e B3AI_CUSTO_WDO
(R$ 2,60). Preços de séries contínuas ajustadas: o resultado em reais é aproximado ao nível de preço atual.
"""

from __future__ import annotations

import math
import os
from datetime import datetime

from . import model as M, stats as st
from .engine import Engine
from .model import BRT

POINT = {"WIN": 0.20, "WDO": 10.0}
TICK = {"WIN": 5.0, "WDO": 0.5}
FEE = {"WIN": float(os.environ.get("B3AI_CUSTO_WIN", "0.60")), "WDO": float(os.environ.get("B3AI_CUSTO_WDO", "2.60"))}


def cost(asset: str) -> float:
    """Custo em reais de uma operação completa (entrada + saída) de 1 contrato."""
    return 2 * TICK[asset] * POINT[asset] + FEE[asset]


def summarize(name: str, asset: str, gross_pts: list[float], note: str = "") -> dict:
    """gross_pts = resultado bruto de cada operação em pontos (já com o sinal da operação)."""
    n = len(gross_pts)
    if n == 0:
        return {"nome": name, "ativo": asset, "n": 0, "nota": note or "nenhuma operação no período"}
    c = cost(asset)
    net = [g * POINT[asset] - c for g in gross_pts]
    mean, sd = st.mean(net), st.std(net)
    se = sd / math.sqrt(n) if n > 1 else 0.0
    lo, hi = mean - 1.96 * se, mean + 1.96 * se
    eq = peak = dd = 0.0
    streak = worst = 0
    for x in net:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        streak = streak + 1 if x < 0 else 0
        worst = max(worst, streak)
    wins, losses = sum(x for x in net if x > 0), -sum(x for x in net if x < 0)
    verdict = ("✅ lucro comprovado" if lo > 0 and n >= 30 else "❌ prejuízo comprovado" if hi < 0 and n >= 30
               else "⚪ não distinguível de zero")
    return {"nome": name, "ativo": asset, "n": n, "acerto_bruto": sum(1 for g in gross_pts if g > 0) / n,
            "acerto_liquido": sum(1 for x in net if x > 0) / n, "bruto_medio_pts": st.mean(gross_pts), "custo": c,
            "liq_medio": mean, "ic": (lo, hi), "total": sum(net), "pior_queda": dd, "seq_perdas": worst,
            "fator_lucro": wins / losses if losses else None, "veredito": verdict, "nota": note}


def daily_strategy(asset: str, res: dict, closes: list[float], conv: float) -> list[float]:
    """Compra no fechamento quando p ≥ 50 % + conv, vende quando ≤ 50 % − conv; sai no fechamento seguinte."""
    m = res["models"][1]
    if not m or "t" not in m:
        return []
    out = []
    for j, p in enumerate(m["oos_p"]):
        t = m["t"][m["oos_start"] + j]
        side = 1 if p >= 0.5 + conv else -1 if p <= 0.5 - conv else 0
        if side and t + 1 < len(closes):
            out.append(side * (closes[t + 1] - closes[t]) * closes[-1] / closes[t])   # em pontos ao nível de preço atual
    return out


def intraday_strategy(model: dict, close: list[float], conv: float) -> list[float]:
    """Entra no fechamento da barra quando há convicção e sai h barras depois (sem sobrepor operações)."""
    if not model or "t" not in model:
        return []
    out, busy_until = [], -1
    h = model["h"]
    for j, p in enumerate(model["oos_p"]):
        i = model["t"][model["oos_start"] + j]
        side = 1 if p >= 0.5 + conv else -1 if p <= 0.5 - conv else 0
        if side and i > busy_until and i + h < len(close):
            out.append(side * (close[i + h] - close[i]))
            busy_until = i + h
    return out


def gap_fade(bars: list[tuple], stop_sigma: float = 0.0, min_sessions: int = 40) -> list[float]:
    """Gap grande na abertura (≥ 0,75 desvio dos gaps dos 60 pregões anteriores): entra CONTRA o gap na abertura e sai no
    fechamento. Com `stop_sigma` > 0, sai antes se o preço andar esse múltiplo do desvio diário contra a posição."""
    ss = [x for x in M.sessions_gap(bars, 2 * 3600) if len(x) >= 5]
    out = []
    for k in range(min_sessions, len(ss)):
        gaps = [ss[j][0][1] / ss[j - 1][-1][4] - 1 for j in range(k - min_sessions + 1, k)]
        dayr = [ss[j][-1][4] / ss[j - 1][-1][4] - 1 for j in range(k - min_sessions + 1, k)]
        gsd, dsd = st.std(gaps) or 1e-9, st.std(dayr) or 1e-9
        op, cl = ss[k][0][1], ss[k][-1][4]
        gap = op / ss[k - 1][-1][4] - 1
        if abs(gap) < 0.75 * gsd:
            continue
        side = -1 if gap > 0 else 1
        res = side * (cl - op)
        if stop_sigma > 0:
            stop = stop_sigma * dsd * op
            for b in ss[k]:
                adverse = (b[2] - op) if side < 0 else (op - b[3])
                if adverse >= stop:
                    res = -stop
                    break
        out.append(res)
    return out


def run(engine: Engine) -> str:
    s = engine.cycle()
    dres, ires, intr = engine._daily_cache["res"], engine._intra_cache["res"], engine._sim_intr
    rows = []
    for k in ("WDO", "WIN"):
        if k in dres:
            closes = [b[4] for b in engine._audit_bars[k]]
            rows.append(summarize(f"{k} diário — só com convicção (≥ 55/45)", k, daily_strategy(k, dres[k], closes, 0.05),
                                  "entra no fechamento, sai no fechamento seguinte"))
            rows.append(summarize(f"{k} diário — todos os dias", k, daily_strategy(k, dres[k], closes, 0.0)))
    for k in ("WDO", "WIN"):
        for tf, names in (("5", ("5 min", "10 min")), ("15", ("30 min", "1 hora"))):
            ir = ires.get((k, tf))
            if not ir:
                continue
            for name in names:
                m = ir["models"].get(name)
                rows.append(summarize(f"{k} {name} — só com convicção (≥ 55/45)", k, intraday_strategy(m, ir["f"]["close"], 0.05)))
    for k in ("WIN", "WDO"):
        b = intr.get((k, "15")) or []
        rows.append(summarize(f"{k} devolução de gap grande — sem stop", k, gap_fade(b), "entra na abertura contra o gap, sai no fechamento"))
        rows.append(summarize(f"{k} devolução de gap grande — stop de 0,5 desvio diário", k, gap_fade(b, 0.5)))
    when = datetime.fromtimestamp(s["atualizado"], tz=BRT).strftime("%d/%m/%Y %H:%M")
    r = lambda v: f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")  # noqa: E731
    L = ["# Simulação com custos — B3 AI", "", f"Gerada em {when} (Brasília) · {s['atraso']}", "",
         "1 mini contrato por operação · previsões fora da amostra · custo por operação completa: "
         f"WIN {r(cost('WIN'))} · WDO {r(cost('WDO'))} (2 ticks de escorregamento + custo fixo).", "",
         "| Estratégia | Operações | Acerto bruto | Acerto líquido | Ganho médio líquido | Intervalo 95% | Total | Pior queda | Maior sequência de perdas | Conclusão |",
         "| --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: | --- |"]
    for x in rows:
        if not x["n"]:
            L.append(f"| {x['nome']} | 0 | — | — | — | — | — | — | — | {x['nota']} |")
            continue
        L.append(f"| {x['nome']} | {x['n']} | {x['acerto_bruto'] * 100:.1f}% | {x['acerto_liquido'] * 100:.1f}% | **{r(x['liq_medio'])}** | "
                 f"{r(x['ic'][0])} a {r(x['ic'][1])} | {r(x['total'])} | {r(x['pior_queda'])} | {x['seq_perdas']} | {x['veredito']} |")
    L += ["", "## Como ler", "",
          "- **Ganho médio líquido**: quanto cada operação rendeu em média, por contrato, depois dos custos.",
          "- **Intervalo 95 %**: se ele inclui zero, o resultado não se distingue de sorte. Só ✅ é lucro com evidência.",
          "- **Pior queda**: o maior prejuízo acumulado no caminho — é o que a conta precisa aguentar.",
          "- A simulação não inclui imposto de renda, margem, nem a diferença entre operar day trade e carregar posição.",
          "- Resultado passado não garante resultado futuro. Teste em conta de simulação antes de usar dinheiro de verdade.", ""]
    return "\n".join(L)
