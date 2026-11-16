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
    dayr = [done[j][-1][4] / done[j - 1][-1][4] - 1 for j in range(len(done) - min_sessions + 2, len(done))]
    gsd, dsd = st.std(gaps) or 1e-9, st.std(dayr) or 1e-9
    op, prev = ss[-1][0][1], done[-1][-1][4]
    gap = op / prev - 1
    z = gap / gsd
    side = (-1 if gap > 0 else 1) if abs(z) >= GAP_LIMIAR else 0
    # No histórico do MetaTrader (99 operações) nenhum stop de 0,5 a 1,5 desvio melhorou o resultado; o stop sugerido é só
    # de PROTEÇÃO, a 2 desvios diários — além da pior perda histórica — contra um dia fora do que já foi visto.
    stop_pts = 2.0 * dsd * op
    return {"gap_pct": gap * 100, "z": z, "abertura": op, "fech_ant": prev, "grande": abs(z) >= GAP_LIMIAR,
            "forca": "forte" if abs(z) >= 1.25 else "moderado" if abs(z) >= GAP_LIMIAR else "pequeno",
            "lado": side, "barras_hoje": len(ss[-1]), "stop_pts": stop_pts, "stop_preco": op - side * stop_pts if side else None,
            "meio_gap": (op + prev) / 2, "ultimo": ss[-1][-1][4],
            # contexto do gap (os dois filtros que melhoraram o resultado no estudo de 2 anos; confirme com o seu histórico)
            "fora_da_faixa": not (min(b[3] for b in done[-1]) <= op <= max(b[2] for b in done[-1])),
            "vespera_na_direcao": (prev / done[-2][-1][4] - 1) * gap > 0,
            "resultado_pts": side * (ss[-1][-1][4] - op) if side else None}


def gap_preopen(bars15: list[tuple], min_sessions: int = 40) -> Optional[dict]:
    """Com o pregão FECHADO: o que precisa acontecer na próxima abertura para haver sinal forte.

    Sinal forte = gap grande + abrir fora da faixa do último pregão + o último pregão ter fechado na direção do gap. Como a
    direção do último pregão já é conhecida, só UM lado pode dar sinal forte, e ele vira um preço de gatilho."""
    done = [x for x in M.sessions_gap(bars15, 2 * 3600) if len(x) >= 5]
    if len(done) < min_sessions:
        return None
    gaps = [done[j][0][1] / done[j - 1][-1][4] - 1 for j in range(len(done) - min_sessions + 2, len(done))]
    dayr = [done[j][-1][4] / done[j - 1][-1][4] - 1 for j in range(len(done) - min_sessions + 2, len(done))]
    gsd, dsd = st.std(gaps) or 1e-9, st.std(dayr) or 1e-9
    last = done[-1]
    close, hi, lo = last[-1][4], max(b[2] for b in last), min(b[3] for b in last)
    up_day = close > done[-2][-1][4]
    if close == done[-2][-1][4]:
        return None
    gatilho = max(hi, close * (1 + GAP_LIMIAR * gsd)) if up_day else min(lo, close * (1 - GAP_LIMIAR * gsd))
    return {"fechamento": close, "maxima": hi, "minima": lo, "dia_de_alta": up_day, "gatilho": gatilho,
            "gap_necessario_pct": (gatilho / close - 1) * 100, "lado": -1 if up_day else 1,
            "stop_pts": 2.0 * dsd * gatilho, "limite_gap_pct": GAP_LIMIAR * gsd * 100}


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


# ============================================================================= estudo da devolução de gap
def gap_trades(bars: list[tuple], min_sessions: int = 40, leader: Optional[list[tuple]] = None) -> list[dict]:
    """Uma ficha por pregão com gap grande: tudo o que é preciso para testar variações da estratégia.
    `leader`: barras do S&P (micro S&P da B3 ou contrato de 24 h) para medir quanto do gap o exterior explica."""
    ss = [x for x in M.sessions_gap(bars, 2 * 3600) if len(x) >= 5]
    span = min((bars[i][0] - bars[i - 1][0] for i in range(1, min(len(bars), 200))), default=900) or 900
    lspan = min((leader[i][0] - leader[i - 1][0] for i in range(1, min(len(leader), 200))), default=900) if leader else 900
    out = []
    for k in range(min_sessions, len(ss)):
        gaps = [ss[j][0][1] / ss[j - 1][-1][4] - 1 for j in range(k - min_sessions + 1, k)]
        dayr = [ss[j][-1][4] / ss[j - 1][-1][4] - 1 for j in range(k - min_sessions + 1, k)]
        gsd, dsd = st.std(gaps) or 1e-9, st.std(dayr) or 1e-9
        s = ss[k]
        op, prev = s[0][1], ss[k - 1][-1][4]
        gap = op / prev - 1
        if abs(gap) < GAP_LIMIAR * gsd:
            continue
        side = -1 if gap > 0 else 1
        closes20 = [ss[j][-1][4] for j in range(k - 20, k)]
        trend = 1 if prev > st.mean(closes20) else -1
        mae = max(((b[2] - op) if side < 0 else (op - b[3])) for b in s)                 # pior momento contra a posição (pontos)
        ph, pl = max(b[2] for b in ss[k - 1]), min(b[3] for b in ss[k - 1])
        lead = None
        if leader:
            a, b_ = M._at(leader, ss[k - 1][-1][0] + span, lspan), M._at(leader, s[0][0] + lspan, lspan)
            lead = (b_ / a - 1) if a and b_ else None
        out.append({"dentro": pl <= op <= ph, "confirma": side * (s[0][4] - op) > 0, "lider": lead,
                    "k": k, "ts": s[0][0], "z": abs(gap) / gsd, "gap": gap, "side": side, "op": op, "prev": prev, "dsd": dsd,
                    "bars": s, "mae": max(mae, 0.0), "trend": trend, "weekday": datetime.fromtimestamp(s[0][0], tz=BRT).weekday(),
                    "prev_ret": prev / ss[k - 2][-1][4] - 1 if k >= 2 else 0.0})
    return out


def gap_result(t: dict, entry_bar: int = 0, exit_bar: Optional[int] = None, stop_sigma: float = 0.0, target_fill: float = 0.0) -> float:
    """Resultado bruto (pontos) de uma ficha. entry_bar 0 = abertura; n = fechamento da n-ésima barra de 15 min.
    exit_bar None = fechamento do pregão. stop_sigma em desvios diários. target_fill: sai quando devolveu essa fração do gap."""
    s, side = t["bars"], t["side"]
    if entry_bar >= len(s) - 1:
        return 0.0
    entry = t["op"] if entry_bar == 0 else s[entry_bar - 1][4]
    last = len(s) - 1 if exit_bar is None else min(exit_bar, len(s) - 1)
    stop = stop_sigma * t["dsd"] * t["op"] if stop_sigma > 0 else None
    target = entry + side * target_fill * abs(t["op"] - t["prev"]) if target_fill > 0 else None
    for j in range(entry_bar, last + 1):
        b = s[j]
        if stop is not None and ((b[2] - entry) if side < 0 else (entry - b[3])) >= stop:
            return -stop
        if target is not None and ((b[3] <= target) if side < 0 else (b[2] >= target)):
            return side * (target - entry)
    return side * (s[last][4] - entry)


def _row(name: str, asset: str, trades: list[dict], **kw) -> dict:
    r = summarize(name, asset, [gap_result(t, **kw) for t in trades])
    half = len(trades) // 2
    r2 = summarize(name, asset, [gap_result(t, **kw) for t in trades[half:]])
    r["liq_2a"] = r2.get("liq_medio")
    return r


def gap_context(t: dict) -> int:
    """Quantos dos dois contextos favoráveis valem para a ficha: abriu FORA da faixa da véspera · véspera fechou na direção do gap."""
    return int(not t["dentro"]) + int(t["prev_ret"] * t["gap"] > 0)


def gap_tiers(asset: str, T: list[dict]) -> dict:
    """Resultado da regra base por contexto (0, 1 ou 2 contextos favoráveis; "algum" = 1 ou 2)."""
    names = {"dois": "os dois contextos (fora da faixa de ontem E ontem na mesma direção)", "um": "só um dos dois contextos",
             "nenhum": "nenhum contexto (dentro da faixa e ontem contra o gap)", "algum": "pelo menos um contexto (regra do sinal)"}
    sel = {"dois": lambda c: c == 2, "um": lambda c: c == 1, "nenhum": lambda c: c == 0, "algum": lambda c: c >= 1}
    out = {}
    for k, f in sel.items():
        sub = [t for t in T if f(gap_context(t))]
        r = summarize(names[k], asset, [gap_result(t) for t in sub])
        r["liq_2a"] = summarize(names[k], asset, [gap_result(t) for t in sub if t in T[len(T) // 2:]]).get("liq_medio")
        out[k] = r
    return out


def gap_filters(T: list[dict]) -> dict:
    """Filtros candidatos para melhorar a estratégia: {nome: (função que aceita a ficha, barra de entrada)}."""
    have = [t for t in T if t["lider"] is not None]
    sxx = sum(t["lider"] ** 2 for t in have)
    beta = sum(t["lider"] * t["gap"] for t in have) / sxx if sxx else 0.0      # quanto do gap o exterior costuma explicar

    def excess(t: dict) -> Optional[float]:
        return None if t["lider"] is None else t["gap"] - beta * t["lider"]

    return {
        "abriu DENTRO da faixa da véspera": (lambda t: t["dentro"], 0),
        "abriu FORA da faixa da véspera": (lambda t: not t["dentro"], 0),
        "primeiros 15 min a favor da devolução (entra depois deles)": (lambda t: t["confirma"], 1),
        "primeiros 15 min contra a devolução (entra depois deles)": (lambda t: not t["confirma"], 1),
        "gap maior do que o exterior justifica": (lambda t: excess(t) is not None and abs(excess(t)) >= 0.5 * abs(t["gap"]), 0),
        "gap que o exterior explica": (lambda t: excess(t) is not None and abs(excess(t)) < 0.5 * abs(t["gap"]), 0),
        "gap CONTRA o exterior (S&P foi para o outro lado)": (lambda t: t["lider"] is not None and t["lider"] * t["gap"] < 0, 0),
        "véspera fechou na direção do gap": (lambda t: t["prev_ret"] * t["gap"] > 0, 0),
        "véspera fechou contra o gap": (lambda t: t["prev_ret"] * t["gap"] < 0, 0),
    }


def gap_best_filter(asset: str, T: list[dict]) -> dict:
    """Escolhe UM filtro na 1ª metade (maior ganho médio líquido, ≥ 12 operações e melhor que a regra base) e mostra o
    resultado dele na 2ª metade, ao lado da regra base na mesma metade. Só vale adotar se melhorar nas duas."""
    half = len(T) // 2
    tr, te = T[:half], T[half:]
    F = gap_filters(T)
    c = cost(asset)

    def avg(trades: list[dict], f, eb: int) -> tuple[int, float]:
        r = [gap_result(t, entry_bar=eb) for t in trades if f(t)]
        return len(r), (st.mean(r) * POINT[asset] - c if r else 0.0)

    base_tr, base_te = avg(tr, lambda t: True, 0), avg(te, lambda t: True, 0)
    best, best_m = None, base_tr[1]
    for name, (f, eb) in F.items():
        n, m = avg(tr, f, eb)
        if n >= 12 and m > best_m:
            best, best_m = name, m
    out = {"base_1a": base_tr, "base_2a": base_te, "escolhido": best, "linhas": []}
    for name, (f, eb) in F.items():
        n1, m1 = avg(tr, f, eb)
        r2 = summarize(name, asset, [gap_result(t, entry_bar=eb) for t in te if f(t)])
        r2["n_1a"], r2["liq_1a"] = n1, m1
        out["linhas"].append(r2)
    if best:
        f, eb = F[best]
        out["resultado"] = summarize(best, asset, [gap_result(t, entry_bar=eb) for t in te if f(t)])
        out["melhorou"] = bool(out["resultado"]["n"] >= 10 and out["resultado"]["liq_medio"] > base_te[1])
    return out


def gap_study(asset: str, bars: list[tuple], leader: Optional[list[tuple]] = None) -> dict:
    T = gap_trades(bars, leader=leader)
    if len(T) < 30:
        return {"n": len(T)}
    S: dict = {"n": len(T)}
    S["filtros"] = gap_best_filter(asset, T)
    tiers = gap_tiers(asset, T)
    S["contexto"] = [tiers[k] for k in ("algum", "dois", "um", "nenhum")]
    S["base"] = [_row("regra base: entra na abertura, sai no fechamento, sem stop", asset, T)]
    S["tamanho"] = [_row(nm, asset, [t for t in T if lo <= t["z"] < hi]) for nm, lo, hi in
                    (("gap de 0,75 a 1,25 desvio", 0.75, 1.25), ("gap de 1,25 a 2 desvios", 1.25, 2.0), ("gap acima de 2 desvios", 2.0, 99))]
    S["direcao"] = [_row("gap de ALTA → vender", asset, [t for t in T if t["side"] < 0]),
                    _row("gap de QUEDA → comprar", asset, [t for t in T if t["side"] > 0])]
    S["tendencia"] = [_row("gap a favor da tendência de 20 pregões (operação contra a tendência)", asset, [t for t in T if t["side"] != t["trend"]]),
                      _row("gap contra a tendência de 20 pregões (operação a favor da tendência)", asset, [t for t in T if t["side"] == t["trend"]])]
    S["saida"] = [_row(nm, asset, T, exit_bar=b) for nm, b in
                  (("sair depois de 1 hora", 4), ("sair depois de 2 horas", 8), ("sair depois de 4 horas", 16))] + \
                 [_row("sair no fechamento", asset, T)] + \
                 [_row(f"sair quando devolver {int(f * 100)}% do gap (ou no fechamento)", asset, T, target_fill=f) for f in (0.5, 1.0)]
    S["stop"] = [_row("sem stop", asset, T)] + [_row(f"stop de {x:g} desvio diário", asset, T, stop_sigma=x) for x in (0.5, 0.75, 1.0, 1.5, 2.0, 3.0)]
    S["entrada"] = [_row(nm, asset, T, entry_bar=b) for nm, b in
                    (("entrar na abertura", 0), ("entrar depois de 15 min", 1), ("entrar depois de 30 min", 2), ("entrar depois de 1 hora", 4))]
    S["semana"] = [_row(nm, asset, [t for t in T if t["weekday"] == w]) for w, nm in enumerate(("segunda", "terça", "quarta", "quinta", "sexta"))]
    wins = sorted(t["mae"] * POINT[asset] for t in T if gap_result(t) > 0)
    loss = sorted(-gap_result(t) * POINT[asset] for t in T if gap_result(t) < 0)
    allm = sorted(t["mae"] * POINT[asset] for t in T)
    q = lambda v, p: v[min(len(v) - 1, int(p * len(v)))] if v else None  # noqa: E731
    S["calor"] = {"ganhadoras": len(wins), "mae_mediana": q(wins, 0.5), "mae_p80": q(wins, 0.8), "mae_p90": q(wins, 0.9),
                  "perda_mediana": q(loss, 0.5), "perda_p90": q(loss, 0.9), "pior_perda": loss[-1] if loss else None,
                  "mae_todas_p90": q(allm, 0.9), "desvio_diario_rs": st.mean([t["dsd"] * t["op"] for t in T]) * POINT[asset]}
    S["por_mes"] = round(len(T) / max(1, (T[-1]["ts"] - T[0]["ts"]) / (30 * 86400)), 1)
    return S


def run_gap(engine: "Engine") -> str:
    s = engine.cycle()
    intr = engine._sim_intr
    when = datetime.fromtimestamp(s["atualizado"], tz=BRT).strftime("%d/%m/%Y %H:%M")
    r = lambda v: "—" if v is None else f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")  # noqa: E731
    L = ["# Devolução de gap no mini índice — estudo de uso", "", f"Gerado em {when} (Brasília) · {s['atraso']}", "",
         "Regra base: o mini índice abre com gap grande (≥ 0,75 desvio dos gaps dos 40 pregões anteriores) → entra na abertura CONTRA o gap "
         f"e sai no fechamento. 1 mini contrato; custo por operação: WIN {r(cost('WIN'))} · WDO {r(cost('WDO'))}. Cada tabela muda UMA coisa da "
         "regra. A coluna \"2ª metade\" mostra o ganho médio só na metade mais recente: uma variação só é confiável se for boa nas duas.", ""]
    titles = [("base", "Regra base"), ("contexto", "Por contexto do gap (a regra do sinal no painel)"), ("tamanho", "Por tamanho do gap"), ("direcao", "Por direção do gap"),
              ("tendencia", "Em relação à tendência"), ("saida", "Quando sair"), ("stop", "Com stop"), ("entrada", "Quando entrar"),
              ("semana", "Por dia da semana")]
    for k in ("WIN", "WDO"):
        S = gap_study(k, intr.get((k, "15")) or [], leader=intr.get(("ES", "15")))
        L += [f"## {'Mini índice' if k == 'WIN' else 'Mini dólar'}", ""]
        if S.get("n", 0) < 30:
            L += [f"Só {S.get('n', 0)} gaps grandes no histórico disponível: insuficiente para o estudo.", ""]
            continue
        L += [f"{S['n']} gaps grandes no histórico — cerca de {S['por_mes']} por mês.", ""]
        for key, title in titles:
            L += [f"### {title}", "", "| Variação | Operações | Acerto | Ganho médio líquido | Intervalo 95% | 2ª metade | Total | Pior queda | Conclusão |",
                  "| --- | ---: | ---: | ---: | --- | ---: | ---: | ---: | --- |"]
            for x in S[key]:
                if not x["n"]:
                    L.append(f"| {x['nome']} | 0 | — | — | — | — | — | — | sem operações |")
                    continue
                L.append(f"| {x['nome']} | {x['n']} | {x['acerto_bruto'] * 100:.0f}% | **{r(x['liq_medio'])}** | {r(x['ic'][0])} a {r(x['ic'][1])} | "
                         f"{r(x.get('liq_2a'))} | {r(x['total'])} | {r(x['pior_queda'])} | {x['veredito']} |")
            L.append("")
        Fz = S["filtros"]
        L += ["### Filtros para acertar mais (escolhidos na 1ª metade, conferidos na 2ª)", "",
              f"Regra base: {r(Fz['base_1a'][1])} em {Fz['base_1a'][0]} operações na 1ª metade e {r(Fz['base_2a'][1])} em {Fz['base_2a'][0]} na 2ª.", "",
              "| Filtro | 1ª metade: operações | 1ª metade: ganho médio | 2ª metade: operações | Acerto | Ganho médio líquido | Intervalo 95% | Conclusão |",
              "| --- | ---: | ---: | ---: | ---: | ---: | --- | --- |"]
        for x in Fz["linhas"]:
            if not x["n"]:
                L.append(f"| {x['nome']} | {x['n_1a']} | {r(x['liq_1a']) if x['n_1a'] else '—'} | 0 | — | — | — | sem operações |")
                continue
            L.append(f"| {x['nome']} | {x['n_1a']} | {r(x['liq_1a'])} | {x['n']} | {x['acerto_bruto'] * 100:.0f}% | **{r(x['liq_medio'])}** | "
                     f"{r(x['ic'][0])} a {r(x['ic'][1])} | {x['veredito']} |")
        if Fz.get("escolhido"):
            res = Fz["resultado"]
            L += ["", f"**Filtro escolhido na 1ª metade: {Fz['escolhido']}.** Na 2ª metade: " +
                  (f"{res['n']} operações, {res['acerto_bruto'] * 100:.0f}% de acerto, {r(res['liq_medio'])} por operação, contra {r(Fz['base_2a'][1])} "
                   f"da regra base — {'MELHOROU' if Fz.get('melhorou') else 'NÃO melhorou'}." if res["n"] else "nenhuma operação."), ""]
        else:
            L += ["", "**Nenhum filtro superou a regra base na 1ª metade.** A regra simples continua sendo a melhor.", ""]
        c = S["calor"]
        L += ["### Quanto a operação vai contra antes de terminar (regra base)", "",
              f"- Desvio diário médio do período: {r(c['desvio_diario_rs'])} por contrato.",
              f"- Nas {c['ganhadoras']} operações que terminaram no lucro, o pior momento contra a posição foi de {r(c['mae_mediana'])} na mediana, "
              f"{r(c['mae_p80'])} em 80% delas e {r(c['mae_p90'])} em 90% delas.",
              f"- Nas que terminaram no prejuízo: perda mediana de {r(c['perda_mediana'])}, 90% perderam até {r(c['perda_p90'])}, e a pior perdeu {r(c['pior_perda'])}.",
              f"- Em 90% de TODAS as operações o pior momento não passou de {r(c['mae_todas_p90'])}.", ""]
    L += ["## Como ler", "",
          "- Poucas operações por linha = intervalo largo. Prefira variações com ✅ ou, pelo menos, positivas no total E na 2ª metade.",
          "- Escolher a melhor linha de cada tabela e juntar tudo é ajustar ao passado: mude no máximo uma ou duas coisas da regra base.",
          "- A simulação entra exatamente no preço de abertura com 1 tick de escorregamento; na prática o primeiro negócio pode sair pior.",
          "- Resultado passado não garante resultado futuro.", ""]
    return "\n".join(L)


# ============================================================================= devolução de gap em outros ativos
COST_PCT_STOCK = float(os.environ.get("B3AI_CUSTO_ACAO_PCT", "0.10"))      # % por operação completa em ações (taxas + 1 tick por lado)
FUTURES = {"IND": (["IND@", "IND$N", "IND$"], "BMFBOVESPA:IND1!", "Índice cheio (IND)", 0.01),
           "WSP": (["WSP@", "WSP$N", "WSP$"], "BMFBOVESPA:WSP1!", "Micro S&P 500 (WSP)", 0.02),
           "WIN": (["WIN@", "WIN$N", "WIN$"], "BMFBOVESPA:WIN1!", "Mini índice (WIN) — referência", 0.0075)}


def summarize_pct(name: str, rets: list[float], cost_pct: float) -> dict:
    """Como `summarize`, mas em % do preço (para comparar ativos de tamanhos diferentes). rets em fração (0,01 = 1 %)."""
    n = len(rets)
    if not n:
        return {"nome": name, "n": 0}
    net = [r * 100 - cost_pct for r in rets]
    mean, sd = st.mean(net), st.std(net)
    se = sd / math.sqrt(n) if n > 1 else 0.0
    lo, hi = mean - 1.96 * se, mean + 1.96 * se
    return {"nome": name, "n": n, "acerto": sum(1 for r in rets if r > 0) / n, "liq": mean, "ic": (lo, hi), "total": sum(net),
            "veredito": "✅ lucro comprovado" if lo > 0 and n >= 30 else "❌ prejuízo comprovado" if hi < 0 and n >= 30 else "⚪ não distinguível de zero"}


def asset_gap_rows(name: str, bars: list[tuple], cost_pct: float) -> Optional[dict]:
    """Fichas de gap de um ativo, com o resultado em % e o contexto (0, 1 ou 2)."""
    T = gap_trades(bars)
    if len(T) < 20:
        return None
    rows = [{"ts": t["ts"], "dia": int(t["ts"] // 86400), "ret": gap_result(t) / t["op"], "ctx": gap_context(t)} for t in T]
    half = rows[len(rows) // 2]["ts"]
    out = {"nome": name, "custo": cost_pct, "rows": rows, "corte": half}
    for key, f in (("todos", lambda c: True), ("forte", lambda c: c == 2), ("fraco", lambda c: c == 1), ("nenhum", lambda c: c == 0)):
        out[key] = summarize_pct(name, [r["ret"] for r in rows if f(r["ctx"])], cost_pct)
        out[key + "_1a"] = summarize_pct(name, [r["ret"] for r in rows if f(r["ctx"]) and r["ts"] < half], cost_pct)
        out[key + "_2a"] = summarize_pct(name, [r["ret"] for r in rows if f(r["ctx"]) and r["ts"] >= half], cost_pct)
    return out


def portfolio(assets: list[dict], pick, second_half_only: bool = False) -> dict:
    """Junta as operações de vários ativos. Vários ativos abrem com gap no MESMO dia e andam juntos, então a estatística é
    feita por DIA (média das operações do dia) — senão o intervalo de confiança sairia estreito demais."""
    by_day: dict[int, list[float]] = {}
    n_ops = 0
    for a in assets:
        for r in a["rows"]:
            if pick(r["ctx"]) and (not second_half_only or r["ts"] >= a["corte"]):
                by_day.setdefault(r["dia"], []).append(r["ret"] * 100 - a["custo"])
                n_ops += 1
    days = [st.mean(v) for _, v in sorted(by_day.items())]
    n = len(days)
    if not n:
        return {"dias": 0, "ops": 0}
    mean, sd = st.mean(days), st.std(days)
    se = sd / math.sqrt(n) if n > 1 else 0.0
    lo, hi = mean - 1.96 * se, mean + 1.96 * se
    return {"dias": n, "ops": n_ops, "acerto_dias": sum(1 for d in days if d > 0) / n, "liq": mean, "ic": (lo, hi),
            "veredito": "✅ lucro comprovado" if lo > 0 and n >= 30 else "❌ prejuízo comprovado" if hi < 0 and n >= 30 else "⚪ não distinguível de zero"}


def run_gap_assets(engine: "Engine", n_stocks: int = 30) -> str:
    """Devolução de gap nas ações mais negociadas, no índice cheio e no micro S&P — com o MetaTrader (--fonte mt5) ou a série pública."""
    from .data import tradingview as tv
    s = engine.cycle()
    liquid = sorted((p for p in engine.b3.get("papeis", []) if p["k"] == "Ação" and p["c"] is not None), key=lambda p: -p["v"])[:n_stocks]
    tickers = [p["t"] for p in liquid]
    series: list[tuple[str, list[tuple], float]] = []
    fonte = "MetaTrader 5" if engine.mt5 is not None and engine.rt else "série pública (15 min de atraso; ~180 pregões)"
    if engine.mt5 is not None and engine.rt:
        for key, (cands, _tv, nome, c) in FUTURES.items():
            for sym in cands:
                b = engine.mt5.series(sym, "15")
                if len(b) > 2000:
                    series.append((nome, b, c))
                    break
        for tk in tickers:
            b = engine.mt5.series(tk, "15")
            if len(b) > 2000:
                series.append((tk, b, COST_PCT_STOCK))
    else:
        reqs = [(v[1], "15", 5000) for v in FUTURES.values()] + [(f"BMFBOVESPA:{tk}", "15", 5000) for tk in tickers]
        h = tv.fetch_history(reqs)
        for key, (_c, sym, nome, c) in FUTURES.items():
            if len(h.get((sym, "15"), [])) > 2000:
                series.append((nome, h[(sym, "15")], c))
        for tk in tickers:
            b = h.get((f"BMFBOVESPA:{tk}", "15"), [])
            if len(b) > 2000:
                series.append((tk, b, COST_PCT_STOCK))
    A = [x for x in (asset_gap_rows(nm, b, c) for nm, b, c in series) if x]
    when = datetime.fromtimestamp(s["atualizado"], tz=BRT).strftime("%d/%m/%Y %H:%M")
    f2 = lambda v: "—" if v is None else f"{v:+.3f}%".replace(".", ",")  # noqa: E731
    L = ["# Devolução de gap em outros ativos", "", f"Gerado em {when} (Brasília) · fonte das barras: {fonte}", "",
         "Mesma regra do mini índice: gap grande na abertura (≥ 0,75 desvio dos 40 pregões anteriores) → entra contra o gap, sai no "
         f"fechamento. Resultado em % do preço por operação, já com custo (ações {COST_PCT_STOCK:.2f}% por operação completa — ajuste em "
         "B3AI_CUSTO_ACAO_PCT; futuros conforme o tick). \"Forte\" = abriu fora da faixa de ontem E ontem fechou na direção do gap.", "",
         f"{len(A)} ativos com histórico suficiente (de {len(series)} lidos).", "",
         "## Por ativo", "",
         "| Ativo | Gaps | Acerto | Média (todos) | Sinal forte: nº | Acerto | Média | 1ª metade | 2ª metade | Sem contexto: média |",
         "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for a in sorted(A, key=lambda a: -(a["forte"].get("liq") or -9)):
        t, fz, nn = a["todos"], a["forte"], a["nenhum"]
        L.append(f"| {a['nome']} | {t['n']} | {t['acerto'] * 100:.0f}% | {f2(t['liq'])} | {fz.get('n', 0)} | "
                 + (f"{fz['acerto'] * 100:.0f}% | **{f2(fz['liq'])}**" if fz.get("n") else "— | —")
                 + f" | {f2(a['forte_1a'].get('liq'))} | {f2(a['forte_2a'].get('liq'))} | {f2(nn.get('liq'))} |")
    stocks = [a for a in A if a["custo"] == COST_PCT_STOCK]
    L += ["", "## Carteira de ações (estatística por dia, para não contar em dobro dias em que várias ações abrem com gap)", "",
          "| Regra | Dias com operação | Operações | Dias no lucro | Média por dia | Intervalo 95% | Conclusão |", "| --- | ---: | ---: | ---: | ---: | --- | --- |"]
    for nome, pick in (("todo gap grande", lambda c: True), ("sinal forte (os dois contextos)", lambda c: c == 2),
                       ("sinal fraco (um contexto)", lambda c: c == 1), ("sem contexto", lambda c: c == 0)):
        P = portfolio(stocks, pick)
        L.append(f"| {nome} | {P['dias']} | {P['ops']} | " + (f"{P['acerto_dias'] * 100:.0f}% | **{f2(P['liq'])}** | {f2(P['ic'][0])} a {f2(P['ic'][1])} | {P['veredito']} |"
                                                              if P["dias"] else "— | — | — | sem operações |"))
    # escolha na 1ª metade → conferência na 2ª
    chosen = [a for a in stocks if a["forte_1a"].get("n", 0) >= 5 and a["forte_1a"]["liq"] > 0]
    P2 = portfolio(chosen, lambda c: c == 2, second_half_only=True)
    Pall = portfolio(stocks, lambda c: c == 2, second_half_only=True)
    L += ["", "## Teste honesto: ações escolhidas na 1ª metade, resultado na 2ª", "",
          f"Ações em que o sinal forte deu lucro na 1ª metade do histórico (≥ 5 operações): {', '.join(a['nome'] for a in chosen) or 'nenhuma'}.", ""]
    for nome, P in (("essas ações, só na 2ª metade", P2), ("todas as ações, sinal forte, só na 2ª metade", Pall)):
        L.append(f"- **{nome}**: " + (f"{P['dias']} dias, {P['ops']} operações, {P['acerto_dias'] * 100:.0f}% dos dias no lucro, média de {f2(P['liq'])} por dia "
                                      f"(intervalo {f2(P['ic'][0])} a {f2(P['ic'][1])}) — {P['veredito']}" if P["dias"] else "sem operações"))
    L += ["", "## Como ler", "",
          "- Em ações, 0,10% de 1 lote de R$ 4.500 são R$ 4,50: compare a média em % com esse custo e com o seu capital.",
          "- Data ex-dividendo cria um gap de queda que NÃO devolve; essas ocorrências estão misturadas aqui e pesam contra a estratégia.",
          "- Com dezenas de ativos testados, alguns vão parecer bons por sorte. Vale a carteira e o teste da 2ª metade, não a melhor linha.",
          "- Resultado passado não garante resultado futuro.", ""]
    return "\n".join(L)
