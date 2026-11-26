"""Indicadores do gráfico (mesmos do painel): SuperTrend, LSMA, Keltner, Parabolic SAR, ZigZag/CHoCH, EMA, RSI, ATR.

Todos recebem listas de candles {"time","open","high","low","close"} e devolvem listas do mesmo tamanho
(None onde ainda não há dados suficientes). Sem dependências externas.
"""

from __future__ import annotations

from typing import Optional

Series = list[Optional[float]]


def _c(c, k):
    return float(c[k])


def sma(values: list[float], n: int) -> Series:
    out: Series = [None] * len(values)
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= n:
            s -= values[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(values: list[float], n: int) -> Series:
    out: Series = [None] * len(values)
    k = 2.0 / (n + 1)
    prev = None
    for i, v in enumerate(values):
        if i < n - 1:
            continue
        prev = sum(values[i - n + 1:i + 1]) / n if prev is None else v * k + prev * (1 - k)
        out[i] = prev
    return out


def true_range(candles: list[dict]) -> list[float]:
    tr = []
    for i, c in enumerate(candles):
        h, l = _c(c, "high"), _c(c, "low")
        if i == 0:
            tr.append(h - l)
        else:
            pc = _c(candles[i - 1], "close")
            tr.append(max(h - l, abs(h - pc), abs(l - pc)))
    return tr


def atr(candles: list[dict], n: int = 14) -> Series:
    """ATR de Wilder (RMA do true range)."""
    tr = true_range(candles)
    out: Series = [None] * len(tr)
    prev = None
    for i, v in enumerate(tr):
        if i < n - 1:
            continue
        prev = sum(tr[:n]) / n if prev is None else (prev * (n - 1) + v) / n
        out[i] = prev
    return out


def lsma(values: list[float], n: int = 25, offset: int = 0) -> Series:
    """Least Squares Moving Average (regressão linear dos últimos n, avaliada no fim − offset)."""
    out: Series = [None] * len(values)
    xs = list(range(n))
    mx = (n - 1) / 2.0
    sxx = sum((x - mx) ** 2 for x in xs)
    for i in range(n - 1, len(values)):
        w = values[i - n + 1:i + 1]
        my = sum(w) / n
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, w)) / sxx
        out[i] = my + slope * ((n - 1 - offset) - mx)
    return out


def rsi(values: list[float], n: int = 14) -> Series:
    out: Series = [None] * len(values)
    if len(values) <= n:
        return out
    gains = [max(values[i] - values[i - 1], 0.0) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], 0.0) for i in range(1, len(values))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(values)):
        if i > n:
            ag = (ag * (n - 1) + gains[i - 1]) / n
            al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    return out


def supertrend(candles: list[dict], n: int = 10, mult: float = 3.0) -> tuple[Series, list[int]]:
    """Devolve (linha, direção) — direção +1 alta / −1 baixa / 0 indefinida."""
    a = atr(candles, n)
    line: Series = [None] * len(candles)
    dirs = [0] * len(candles)
    fu = fl = None
    d = 1
    for i, c in enumerate(candles):
        if a[i] is None:
            continue
        hl2 = (_c(c, "high") + _c(c, "low")) / 2
        ub, lb = hl2 + mult * a[i], hl2 - mult * a[i]
        pc = _c(candles[i - 1], "close") if i else _c(c, "close")
        fu = ub if fu is None or ub < fu or pc > fu else fu
        fl = lb if fl is None or lb > fl or pc < fl else fl
        cl = _c(c, "close")
        if d == 1 and cl < fl:
            d = -1
        elif d == -1 and cl > fu:
            d = 1
        dirs[i] = d
        line[i] = fl if d == 1 else fu
    return line, dirs


def keltner(candles: list[dict], n: int = 20, mult: float = 1.0) -> tuple[Series, Series, Series]:
    """(superior, meio, inferior) — meio = EMA(n) do fechamento, largura = mult × ATR(n)."""
    mid = ema([_c(c, "close") for c in candles], n)
    a = atr(candles, n)
    up: Series = [None] * len(candles)
    lo: Series = [None] * len(candles)
    for i in range(len(candles)):
        if mid[i] is not None and a[i] is not None:
            up[i], lo[i] = mid[i] + mult * a[i], mid[i] - mult * a[i]
    return up, mid, lo


def parabolic_sar(candles: list[dict], step: float = 0.02, inc: float = 0.02, max_af: float = 0.2) -> tuple[Series, list[int]]:
    n = len(candles)
    sar: Series = [None] * n
    dirs = [0] * n
    if n < 2:
        return sar, dirs
    up = _c(candles[1], "close") >= _c(candles[0], "close")
    af = step
    ep = _c(candles[0], "high") if up else _c(candles[0], "low")
    s = _c(candles[0], "low") if up else _c(candles[0], "high")
    for i in range(1, n):
        h, l = _c(candles[i], "high"), _c(candles[i], "low")
        s = s + af * (ep - s)
        if up:
            s = min(s, _c(candles[i - 1], "low"), _c(candles[max(i - 2, 0)], "low"))
            if l < s:
                up, s, ep, af = False, ep, l, step
            elif h > ep:
                ep, af = h, min(af + inc, max_af)
        else:
            s = max(s, _c(candles[i - 1], "high"), _c(candles[max(i - 2, 0)], "high"))
            if h > s:
                up, s, ep, af = True, ep, h, step
            elif l < ep:
                ep, af = l, min(af + inc, max_af)
        sar[i] = s
        dirs[i] = 1 if up else -1
    return sar, dirs


def zigzag_pivots(candles: list[dict], depth: int = 5) -> list[dict]:
    """Pivôs de swing confirmados: máxima/mínima maior/menor que `depth` candles de cada lado.
    Cada pivô: {"i", "confirm" (índice em que fica conhecido), "time", "price", "kind": "H"|"L"}. Pivôs do mesmo tipo seguidos ficam só com o extremo."""
    piv: list[dict] = []
    for i in range(depth, len(candles) - depth):
        h, l = _c(candles[i], "high"), _c(candles[i], "low")
        win = candles[i - depth:i + depth + 1]
        kind = None
        if h >= max(_c(c, "high") for c in win):
            kind = "H"
        elif l <= min(_c(c, "low") for c in win):
            kind = "L"
        if kind is None:
            continue
        p = {"i": i, "confirm": i + depth, "time": candles[i]["time"], "price": h if kind == "H" else l, "kind": kind}
        if piv and piv[-1]["kind"] == kind:
            if (kind == "H" and p["price"] > piv[-1]["price"]) or (kind == "L" and p["price"] < piv[-1]["price"]):
                piv[-1] = p
            continue
        piv.append(p)
    return piv


def choch(candles: list[dict], pivots: list[dict]) -> list[dict]:
    """Change of Character: preço fecha além do último pivô contra a estrutura vigente.
    Devolve eventos {"i","time","price","dir": +1 (CHoCH ↑) | −1 (CHoCH ↓)}."""
    events: list[dict] = []
    trend = 0
    last_h = last_l = None
    pi = 0
    for i, c in enumerate(candles):
        while pi < len(pivots) and pivots[pi]["confirm"] <= i:   # só pivô já confirmado (sem olhar o futuro)
            p = pivots[pi]
            if p["kind"] == "H":
                last_h = p
            else:
                last_l = p
            pi += 1
        cl = _c(c, "close")
        if last_h is not None and cl > last_h["price"]:
            if trend <= 0:
                events.append({"i": i, "time": c["time"], "price": last_h["price"], "dir": 1})
            trend, last_h = 1, None
        elif last_l is not None and cl < last_l["price"]:
            if trend >= 0:
                events.append({"i": i, "time": c["time"], "price": last_l["price"], "dir": -1})
            trend, last_l = -1, None
    return events


def support_resistance(pivots: list[dict], price: float, k: int = 2) -> dict:
    """Níveis S/R mais próximos acima e abaixo do preço a partir dos pivôs recentes."""
    highs = sorted({round(p["price"], 10) for p in pivots[-30:] if p["price"] > price})
    lows = sorted({round(p["price"], 10) for p in pivots[-30:] if p["price"] < price}, reverse=True)
    return {"resistance": highs[:k], "support": lows[:k]}


def adx(candles: list[dict], n: int = 14) -> Series:
    """ADX de Wilder: força da tendência (0–100), sem dizer a direção. < 20 costuma ser mercado sem tendência."""
    m = len(candles)
    out: Series = [None] * m
    if m < 2 * n + 1:
        return out
    tr, pdm, ndm = [0.0] * m, [0.0] * m, [0.0] * m
    for i in range(1, m):
        h, l, ph, pl, pc = (_c(candles[i], "high"), _c(candles[i], "low"), _c(candles[i - 1], "high"),
                            _c(candles[i - 1], "low"), _c(candles[i - 1], "close"))
        up, dn = h - ph, pl - l
        pdm[i] = up if up > dn and up > 0 else 0.0
        ndm[i] = dn if dn > up and dn > 0 else 0.0
        tr[i] = max(h - l, abs(h - pc), abs(l - pc))
    atr_s, p_s, n_s = sum(tr[1:n + 1]), sum(pdm[1:n + 1]), sum(ndm[1:n + 1])
    dx: list[float] = []
    for i in range(n + 1, m):
        atr_s = atr_s - atr_s / n + tr[i]
        p_s = p_s - p_s / n + pdm[i]
        n_s = n_s - n_s / n + ndm[i]
        pdi = 100 * p_s / atr_s if atr_s else 0.0
        ndi = 100 * n_s / atr_s if atr_s else 0.0
        dx.append(100 * abs(pdi - ndi) / (pdi + ndi) if (pdi + ndi) else 0.0)
        if len(dx) == n:
            val = sum(dx) / n
            out[i] = val
        elif len(dx) > n:
            val = (out[i - 1] * (n - 1) + dx[-1]) / n
            out[i] = val
    return out


def choppiness(candles: list[dict], n: int = 14) -> Series:
    """Choppiness Index: 100·log10(Σ TR / (máx − mín)) / log10(n). Alto (> 61,8) = lateral; baixo (< 38,2) = tendência."""
    import math
    tr = true_range(candles)
    out: Series = [None] * len(candles)
    for i in range(n, len(candles)):
        hi = max(_c(c, "high") for c in candles[i - n + 1:i + 1])
        lo = min(_c(c, "low") for c in candles[i - n + 1:i + 1])
        s = sum(tr[i - n + 1:i + 1])
        if hi > lo and s > 0:
            out[i] = 100 * math.log10(s / (hi - lo)) / math.log10(n)
    return out


def efficiency_ratio(values: list[float], n: int = 20) -> Series:
    """Efficiency Ratio de Kaufman: |deslocamento em n| ÷ soma dos movimentos. 1 = linha reta; perto de 0 = zigue-zague."""
    out: Series = [None] * len(values)
    for i in range(n, len(values)):
        path = sum(abs(values[j] - values[j - 1]) for j in range(i - n + 1, i + 1))
        out[i] = abs(values[i] - values[i - n]) / path if path else 0.0
    return out
