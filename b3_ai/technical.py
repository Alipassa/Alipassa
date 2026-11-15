"""Tendência (dia · semana · mês) e extremos intraday com sinal de reversão — tudo com o acerto medido no histórico."""

from __future__ import annotations

import math
from typing import Optional

from . import stats as st


# ============================================================================= tendência
def _trend(score: int, eff: float) -> str:
    if eff < 0.18 and abs(score) < 3:
        return "LATERAL"
    return "ALTA" if score >= 2 else "BAIXA" if score <= -2 else "LATERAL"


def _eff(closes: list[float]) -> float:
    """Eficiência do movimento (0 = vai e volta, 1 = linha reta): |deslocamento| ÷ caminho percorrido."""
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    return abs(closes[-1] - closes[0]) / path if path else 0.0


def trends(daily: list[tuple], bars5: list[tuple]) -> list[dict]:
    """Tendência do dia (intraday), da semana (≈5 pregões) e do mês (≈21 pregões), com os critérios usados."""
    out = []
    c = [b[4] for b in daily]
    if bars5 and len(bars5) > 40:
        i = len(bars5) - 1
        while i > 0 and bars5[i][0] - bars5[i - 1][0] <= 3600:
            i -= 1
        sess = bars5[i:]
        cs = [b[4] for b in bars5[-200:]]
        vv = sum(b[5] or 1 for b in sess)
        vwap = sum((b[2] + b[3] + b[4]) / 3 * (b[5] or 1) for b in sess) / vv
        e9, e21 = st.ema(cs, 9)[-1], st.ema(cs, 21)[-1]
        px, op = sess[-1][4], sess[0][1]
        crit = [("preço × abertura", px > op), ("preço × VWAP", px > vwap), ("EMA 9 × EMA 21 (5 min)", e9 > e21)]
        score = sum(1 if ok else -1 for _, ok in crit)
        out.append({"prazo": "Dia", "tendencia": _trend(score, _eff([b[4] for b in sess]) if len(sess) > 5 else 1), "score": score,
                    "var": (px / op - 1) * 100, "criterios": [{"nome": n, "alta": ok} for n, ok in crit]})
    for nome, n, fast, slow in (("Semana", 5, 5, 10), ("Mês", 21, 20, 50)):
        if len(c) < slow + 6:
            continue
        mf, ms = st.mean(c[-fast:]), st.mean(c[-slow:])
        mf_prev = st.mean(c[-fast - 3:-3])
        crit = [(f"preço × média de {fast}", c[-1] > mf), (f"média de {fast} × média de {slow}", mf > ms),
                (f"média de {fast} subindo", mf > mf_prev), (f"variação em {n} pregões", c[-1] > c[-1 - n])]
        score = sum(1 if ok else -1 for _, ok in crit)
        out.append({"prazo": nome, "tendencia": _trend(score, _eff(c[-n - 1:])), "score": score, "var": (c[-1] / c[-1 - n] - 1) * 100,
                    "criterios": [{"nome": x, "alta": ok} for x, ok in crit]})
    return out


# ============================================================================= extremos e reversão (barras de 5 min)
def extremes(bars: list[tuple]) -> dict:
    """Para cada barra: RSI, posição nas bandas de Bollinger, desvio do VWAP, estado de extremo e gatilho de reversão.

    Extremo de COMPRA (sobrecomprado) = pelo menos 2 de: RSI ≥ 70 · fechamento acima da banda superior · preço ≥ 2 desvios acima do VWAP.
    Extremo de VENDA (sobrevendido)   = o espelho.
    SINAL DE REVERSÃO = houve extremo nas últimas 3 barras E a barra atual fecha além da mínima (ou máxima) da anterior,
    contra o movimento, com o RSI virando. Um sinal por episódio (espera de 6 barras).
    """
    n = len(bars)
    c = [b[4] for b in bars]
    rsi = st.rsi(c, 14)
    state = [0] * n
    sig = [0] * n
    zv = [0.0] * n
    bbp = [0.5] * n
    pv = vv = 0.0
    devs: list[float] = []
    last_sig = -99
    for i in range(n):
        if i == 0 or bars[i][0] - bars[i - 1][0] > 3600:
            pv = vv = 0.0
            devs = []
        vol = bars[i][5] or 1.0
        pv += (bars[i][2] + bars[i][3] + bars[i][4]) / 3 * vol
        vv += vol
        dev = c[i] - pv / vv
        devs.append(dev)
        sd_v = st.std(devs) if len(devs) >= 12 else 0.0
        zv[i] = dev / sd_v if sd_v else 0.0
        if i < 20:
            continue
        w = c[i - 19:i + 1]
        mid, sd = st.mean(w), st.std(w) or 1e-9
        up, lo = mid + 2 * sd, mid - 2 * sd
        bbp[i] = (c[i] - lo) / (up - lo)
        ob = (rsi[i] >= 70) + (c[i] >= up) + (zv[i] >= 2)
        os_ = (rsi[i] <= 30) + (c[i] <= lo) + (zv[i] <= -2)
        state[i] = 1 if ob >= 2 else -1 if os_ >= 2 else 0
        if i - last_sig < 6 or bars[i][0] - bars[i - 1][0] > 3600:
            continue
        recent = state[i - 3:i]
        if 1 in recent and c[i] < bars[i - 1][3] and c[i] < bars[i][1] and rsi[i] < rsi[i - 1]:
            sig[i], last_sig = -1, i        # topo: reversão para BAIXO
        elif -1 in recent and c[i] > bars[i - 1][2] and c[i] > bars[i][1] and rsi[i] > rsi[i - 1]:
            sig[i], last_sig = 1, i         # fundo: reversão para CIMA
    return {"rsi": rsi, "state": state, "signal": sig, "vwap_z": zv, "bb_pos": bbp}


def backtest(bars: list[tuple], sig: list[int], horizons: tuple[int, ...] = (6, 12)) -> dict:
    """O que o preço fez depois de cada sinal de reversão (mesmo pregão): acerto e ganho médio na direção do sinal."""
    out: dict = {}
    for h in horizons:
        res = []
        for i, s in enumerate(sig):
            if s and i + h < len(bars) and bars[i + h][0] - bars[i][0] <= h * 300 + 900:
                res.append(s * (bars[i + h][4] / bars[i][4] - 1))
        out[f"{h * 5} min"] = {"n": len(res), "acerto": sum(1 for r in res if r > 0) / len(res) if res else None,
                               "media_pct": st.mean(res) * 100 if res else None}
    out["sinais"] = sum(1 for s in sig if s)
    return out


def reversal_read(bars: list[tuple], decimals: int = 0) -> Optional[dict]:
    if len(bars) < 60:
        return None
    x = extremes(bars)
    i = len(bars) - 1
    bt = backtest(bars, x["signal"])
    st_now, sg_now = x["state"][i], next((x["signal"][j] for j in range(i, max(i - 3, 0), -1) if x["signal"][j]), 0)
    if sg_now:
        label = "SINAL DE REVERSÃO PARA CIMA (fundo)" if sg_now > 0 else "SINAL DE REVERSÃO PARA BAIXO (topo)"
    elif st_now:
        label = "EXTREMO DE COMPRA — sobrecomprado, aguardando gatilho" if st_now > 0 else "EXTREMO DE VENDA — sobrevendido, aguardando gatilho"
    else:
        label = "Sem extremo no momento"
    recent = [{"ts": bars[j][0], "lado": x["signal"][j], "preco": bars[j][4]} for j in range(max(0, i - 400), i + 1) if x["signal"][j]][-8:]
    return {"rotulo": label, "estado": st_now, "sinal": sg_now, "rsi": x["rsi"][i], "bb_pos": x["bb_pos"][i], "vwap_z": x["vwap_z"][i],
            "historico": bt, "recentes": recent, "ultimo_ts": bars[i][0]}
