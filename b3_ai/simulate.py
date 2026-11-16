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

from typing import TYPE_CHECKING, Optional

from . import model as M, stats as st
from .model import BRT

if TYPE_CHECKING:
    from .engine import Engine

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


GAP_LIMIAR = 0.75        # gap "grande": ≥ 0,75 desvio dos gaps dos 40 pregões anteriores


def gap_state(bars15: list[tuple], is_open: bool, min_sessions: int = 40) -> Optional[dict]:
    """Gap do pregão de hoje (se aberto) em relação ao histórico recente, e o lado da operação de devolução."""
    ss = [x for x in M.sessions_gap(bars15, 2 * 3600) if len(x) >= 1]
    done = [x for x in (ss[:-1] if is_open else ss) if len(x) >= 5]
    if not is_open or len(done) < min_sessions or not ss:
        return None
    gaps = [done[j][0][1] / done[j - 1][-1][4] - 1 for j in range(len(done) - min_sessions + 2, len(done))]
    gsd = st.std(gaps) or 1e-9
    op, prev = ss[-1][0][1], done[-1][-1][4]
    gap = op / prev - 1
    z = gap / gsd
    return {"gap_pct": gap * 100, "z": z, "abertura": op, "fech_ant": prev, "grande": abs(z) >= GAP_LIMIAR,
            "lado": (-1 if gap > 0 else 1) if abs(z) >= GAP_LIMIAR else 0, "barras_hoje": len(ss[-1])}


def run(engine: "Engine") -> str:
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


# ============================================================================= extremos com filtro (treino × teste)
def _context(bars: list[tuple]) -> dict:
    """Por barra: sessão, abertura, VWAP, eficiência do dia até ali, fechamento/máx./mín. do pregão anterior e desvio diário."""
    n = len(bars)
    sess = [0] * n
    s = -1
    for i in range(n):
        if i == 0 or bars[i][0] - bars[i - 1][0] > 3600:
            s += 1
        sess[i] = s
    first = {}
    last = {}
    for i in range(n):
        first.setdefault(sess[i], i)
        last[sess[i]] = i
    closes = [bars[last[k]][4] for k in range(s + 1)]
    highs = [max(b[2] for b in bars[first[k]:last[k] + 1]) for k in range(s + 1)]
    lows = [min(b[3] for b in bars[first[k]:last[k] + 1]) for k in range(s + 1)]
    dsd = [0.0] * (s + 1)
    for k in range(21, s + 1):
        dsd[k] = st.std([closes[j] / closes[j - 1] - 1 for j in range(k - 20, k)])
    vwap, eff, pv, vv, path = [0.0] * n, [1.0] * n, 0.0, 0.0, 0.0
    for i in range(n):
        if i == first[sess[i]]:
            pv = vv = path = 0.0
        else:
            path += abs(bars[i][4] - bars[i - 1][4])
        vol = bars[i][5] or 1.0
        pv += (bars[i][2] + bars[i][3] + bars[i][4]) / 3 * vol
        vv += vol
        vwap[i] = pv / vv
        op = bars[first[sess[i]]][1]
        eff[i] = abs(bars[i][4] - op) / path if path else 1.0
    return {"sess": sess, "first": first, "last": last, "closes": closes, "highs": highs, "lows": lows, "dsd": dsd, "vwap": vwap, "eff": eff}


def _exit_fixed(bars: list[tuple], ctx: dict, i: int, side: int, hold: int) -> tuple[float, int]:
    j = min(i + hold, ctx["last"][ctx["sess"][i]])
    return side * (bars[j][4] - bars[i][4]), j


def extremes_lateral(bars: list[tuple], ctx: dict, sig: list[int], lo: int, hi: int, eff_max: float, hold: int) -> list[float]:
    """Sinal de reversão só quando o dia está LATERAL (eficiência do pregão até ali abaixo de eff_max)."""
    out, busy = [], -1
    for i in range(max(lo, 1), hi):
        if sig[i] and i > busy and ctx["eff"][i] < eff_max and i - ctx["first"][ctx["sess"][i]] >= 12:
            r, busy = _exit_fixed(bars, ctx, i, sig[i], hold)
            out.append(r)
    return out


def extremes_band(bars: list[tuple], ctx: dict, lo: int, hi: int, mult: float) -> list[float]:
    """Preço sai da faixa diária (mult × 1,28 desvio do dia): entra contra, sai na volta ao VWAP ou no fechamento;
    stop a mais 0,6 desvio diário contra. Uma operação por pregão."""
    out, done = [], set()
    for i in range(lo, hi):
        k = ctx["sess"][i]
        sd = ctx["dsd"][k]
        if k in done or k < 22 or not sd:
            continue
        ref = ctx["closes"][k - 1]
        r = bars[i][4] / ref - 1
        if abs(r) < mult * 1.2816 * sd:
            continue
        done.add(k)
        side = -1 if r > 0 else 1
        entry, stop = bars[i][4], 0.6 * sd * ref
        res = side * (bars[ctx["last"][k]][4] - entry)
        for j in range(i + 1, ctx["last"][k] + 1):
            adverse = (bars[j][2] - entry) if side < 0 else (entry - bars[j][3])
            if adverse >= stop:
                res = -stop
                break
            if (side < 0 and bars[j][3] <= ctx["vwap"][j]) or (side > 0 and bars[j][2] >= ctx["vwap"][j]):
                res = side * (ctx["vwap"][j] - entry)
                break
        out.append(res)
    return out


def extremes_pivot(bars: list[tuple], ctx: dict, sig: list[int], lo: int, hi: int, level: int, hold: int) -> list[float]:
    """Sinal de reversão só perto de suporte/resistência do pregão anterior (level 1 = S1/R1 ou além; 2 = S2/R2 ou além)."""
    out, busy = [], -1
    for i in range(max(lo, 1), hi):
        k = ctx["sess"][i]
        if not sig[i] or i <= busy or k < 1:
            continue
        h, l, c = ctx["highs"][k - 1], ctx["lows"][k - 1], ctx["closes"][k - 1]
        p = (h + l + c) / 3
        sup = (2 * p - h) if level == 1 else p - (h - l)
        res_ = (2 * p - l) if level == 1 else p + (h - l)
        tol = 0.15 * (h - l)
        px = bars[i][4]
        if (sig[i] > 0 and px <= sup + tol) or (sig[i] < 0 and px >= res_ - tol):
            r, busy = _exit_fixed(bars, ctx, i, sig[i], hold)
            out.append(r)
    return out


def extremes_study(asset: str, bars: list[tuple]) -> list[dict]:
    """Três filtros para o sinal de extremo. O parâmetro de cada um é escolhido na 1ª metade do histórico (maior ganho médio
    líquido, mínimo de 15 operações) e o resultado mostrado é o da 2ª metade — que o filtro nunca viu."""
    from . import technical as T
    if len(bars) < 3000:
        return []
    ctx, sig = _context(bars), T.extremes(bars)["signal"]
    half = len(bars) // 2
    c = cost(asset)

    def pick(fn, grid: list[tuple], label) -> dict:
        best, best_m = None, -1e18
        for params in grid:
            tr = fn(0, half, *params)
            if len(tr) >= 15:
                m = st.mean(tr) * POINT[asset] - c
                if m > best_m:
                    best, best_m = params, m
        if best is None:
            return {"nome": f"{asset} {label(grid[0]).split(' (')[0]}", "ativo": asset, "n": 0, "nota": "operações insuficientes na 1ª metade para escolher o filtro"}
        out = summarize(f"{asset} {label(best)}", asset, fn(half, len(bars), *best))
        out["treino"] = best_m
        return out

    base = summarize(f"{asset} extremo sem filtro (referência, 2ª metade)", asset,
                     extremes_lateral(bars, ctx, sig, half, len(bars), 9.9, 6))
    base["treino"] = None
    return [
        base,
        pick(lambda lo, hi, e, hd: extremes_lateral(bars, ctx, sig, lo, hi, e, hd), [(e, hd) for e in (0.15, 0.25, 0.35) for hd in (6, 12)],
             lambda p: f"extremo só em dia lateral (eficiência < {p[0]:.2f}, saída em {p[1] * 5} min)"),
        pick(lambda lo, hi, m: extremes_band(bars, ctx, lo, hi, m), [(0.8,), (1.0,), (1.2,)],
             lambda p: f"preço fora da faixa diária ({p[0]:.1f} × faixa de 80%), saída no VWAP"),
        pick(lambda lo, hi, lv, hd: extremes_pivot(bars, ctx, sig, lo, hi, lv, hd), [(lv, hd) for lv in (1, 2) for hd in (6, 12)],
             lambda p: f"extremo em {'S1/R1' if p[0] == 1 else 'S2/R2'} (saída em {p[1] * 5} min)"),
    ]


def run_extremes(engine: "Engine") -> str:
    s = engine.cycle()
    intr = engine._sim_intr
    when = datetime.fromtimestamp(s["atualizado"], tz=BRT).strftime("%d/%m/%Y %H:%M")
    r = lambda v: "—" if v is None else f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")  # noqa: E731
    L = ["# Extremos de compra e venda como entrada — teste com filtros e custos", "", f"Gerado em {when} (Brasília) · {s['atraso']}", "",
         "Entrada contra o movimento quando o preço está num extremo (RSI, bandas, VWAP) e dá o gatilho de virada. Cada filtro tem o "
         "parâmetro escolhido na 1ª metade do histórico; os números abaixo são da 2ª metade, que o filtro não viu. 1 mini contrato, "
         f"custo por operação: WIN {r(cost('WIN'))} · WDO {r(cost('WDO'))}.", ""]
    for k in ("WIN", "WDO"):
        bars = intr.get((k, "5")) or []
        rows = extremes_study(k, bars)
        n_s = len({b[0] // 86400 for b in bars})
        L += [f"## {'Mini índice' if k == 'WIN' else 'Mini dólar'} — {len(bars)} barras de 5 min (~{n_s} pregões)", "",
              "| Filtro | Ganho médio na 1ª metade | Operações (2ª metade) | Acerto bruto | Ganho médio líquido | Intervalo 95% | Total | Pior queda | Conclusão |",
              "| --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | --- |"]
        for x in rows:
            if not x["n"]:
                L.append(f"| {x['nome']} | — | 0 | — | — | — | — | — | {x.get('nota', 'sem operações')} |")
                continue
            L.append(f"| {x['nome']} | {r(x.get('treino'))} | {x['n']} | {x['acerto_bruto'] * 100:.1f}% | **{r(x['liq_medio'])}** | "
                     f"{r(x['ic'][0])} a {r(x['ic'][1])} | {r(x['total'])} | {r(x['pior_queda'])} | {x['veredito']} |")
        L.append("")
    L += ["## Como ler", "",
          "- Um filtro só vale se der ✅ na 2ª metade. Bom na 1ª e ruim na 2ª = ajuste ao passado, não vantagem.",
          "- Com poucas operações o intervalo fica largo: \"não distinguível de zero\" significa que ainda não há prova, nem a favor nem contra.",
          "- Resultado passado não garante resultado futuro.", ""]
    return "\n".join(L)
