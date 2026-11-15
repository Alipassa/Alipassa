"""Estatística em Python puro: médias, normal, ridge, regressão logística (IRLS), indicadores técnicos."""

from __future__ import annotations

import math
from typing import Optional, Sequence


def mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def std(xs: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def rolling_std(xs: Sequence[float], w: int) -> list[float]:
    """out[i] = desvio-padrão amostral de xs[i-w:i] (0 enquanto i < w), em tempo linear por somas acumuladas."""
    n = len(xs)
    out = [0.0] * n
    s1 = s2 = 0.0
    for i in range(n):
        if i >= w:
            var = (s2 - s1 * s1 / w) / (w - 1)
            out[i] = math.sqrt(var) if var > 0 else 0.0
            old = xs[i - w]
            s1 -= old
            s2 -= old * old
        v = xs[i]
        s1 += v
        s2 += v * v
    return out


def phi(z: float) -> float:
    """CDF da normal padrão."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def phi_inv(p: float) -> float:
    """Inversa da CDF normal (Acklam), |erro| < 1e-8."""
    p = min(max(p, 1e-9), 1 - 1e-9)
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02, 1.383577518672690e+02,
         -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02, 6.680131188771972e+01,
         -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00, -2.549732539343734e+00,
         4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00, 3.754408661907416e+00]
    if p < 0.02425:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p > 1 - 0.02425:
        return -phi_inv(1 - p)
    q = p - 0.5
    r = q * q
    return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
           (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)


def clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def corr(xs: Sequence[float], ys: Sequence[float]) -> Optional[float]:
    n = min(len(xs), len(ys))
    if n < 10:
        return None
    xs, ys = xs[-n:], ys[-n:]
    mx, my = mean(xs), mean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def solve(a: list[list[float]], b: list[float]) -> list[float]:
    """Resolve A·x = b por eliminação de Gauss com pivô parcial (A pequena e bem condicionada pelo ridge)."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for i in range(n):
        piv = max(range(i, n), key=lambda r: abs(m[r][i]))
        if abs(m[piv][i]) < 1e-12:
            m[piv][i] = 1e-12
        m[i], m[piv] = m[piv], m[i]
        inv = 1.0 / m[i][i]
        for r in range(i + 1, n):
            f = m[r][i] * inv
            if f:
                mr, mi = m[r], m[i]
                for c in range(i, n + 1):
                    mr[c] -= f * mi[c]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        x[i] = (m[i][n] - sum(m[i][c] * x[c] for c in range(i + 1, n))) / m[i][i]
    return x


def ridge(xs: list[list[float]], ys: list[float], lam: float = 1.0) -> list[float]:
    """Mínimos quadrados com penalidade L2, sem intercepto (retornos já são ~centrados)."""
    k = len(xs[0])
    a = [[0.0] * k for _ in range(k)]
    b = [0.0] * k
    for row, y in zip(xs, ys):
        for i in range(k):
            ri = row[i]
            if ri:
                b[i] += ri * y
                ai = a[i]
                for j in range(i, k):
                    ai[j] += ri * row[j]
    for i in range(k):
        for j in range(i):
            a[i][j] = a[j][i]
        a[i][i] += lam
    return solve(a, b)


def logistic_fit(xs: list[list[float]], ys: list[int], lam: float = 5.0, iters: int = 7) -> list[float]:
    """Regressão logística por Newton/IRLS com L2. Devolve [intercepto, w1..wk] (intercepto sem penalidade)."""
    n, k = len(xs), len(xs[0]) + 1
    w = [0.0] * k
    base = clamp(mean(ys), 0.02, 0.98)
    w[0] = math.log(base / (1 - base))
    for _ in range(iters):
        h = [[0.0] * k for _ in range(k)]
        g = [0.0] * k
        for row, y in zip(xs, ys):
            z = w[0]
            for j, v in enumerate(row):
                z += w[j + 1] * v
            p = 1.0 / (1.0 + math.exp(-clamp(z, -30, 30)))
            e, s = y - p, max(p * (1 - p), 1e-6)
            g[0] += e
            h[0][0] += s
            h0 = h[0]
            for j, v in enumerate(row):
                g[j + 1] += e * v
                sv = s * v
                h0[j + 1] += sv
                hj = h[j + 1]
                for m in range(j, k - 1):
                    hj[m + 1] += sv * row[m]
        for i in range(k):
            for j in range(i):
                h[i][j] = h[j][i]
        for i in range(1, k):
            h[i][i] += lam
            g[i] -= lam * w[i]
        step = solve(h, g)
        w = [wi + clamp(si, -5, 5) for wi, si in zip(w, step)]
        if max(abs(s) for s in step) < 1e-6:
            break
    return w


def logistic_predict(w: list[float], row: list[float]) -> float:
    z = w[0] + sum(wi * v for wi, v in zip(w[1:], row))
    return 1.0 / (1.0 + math.exp(-clamp(z, -30, 30)))


# ----------------------------------------------------------------------------- indicadores técnicos
def ema(values: Sequence[float], period: int) -> list[float]:
    out: list[float] = []
    k = 2.0 / (period + 1)
    for i, v in enumerate(values):
        out.append(v if i == 0 else out[-1] + k * (v - out[-1]))
    return out


def sma(values: Sequence[float], period: int) -> list[Optional[float]]:
    out: list[Optional[float]] = []
    s = 0.0
    for i, v in enumerate(values):
        s += v
        if i >= period:
            s -= values[i - period]
        out.append(s / period if i >= period - 1 else None)
    return out


def rsi(values: Sequence[float], period: int = 14) -> list[float]:
    """RSI de Wilder; 50 enquanto não há histórico suficiente."""
    out = [50.0] * len(values)
    if len(values) <= period:
        return out
    gain = loss = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        gain += max(d, 0.0)
        loss += max(-d, 0.0)
    gain /= period
    loss /= period
    for i in range(period, len(values)):
        if i > period:
            d = values[i] - values[i - 1]
            gain = (gain * (period - 1) + max(d, 0.0)) / period
            loss = (loss * (period - 1) + max(-d, 0.0)) / period
        out[i] = 100.0 if loss == 0 else 100.0 - 100.0 / (1.0 + gain / loss)
    return out


def atr(bars: Sequence[tuple], period: int = 14) -> float:
    """ATR simples das últimas `period` barras (ts, o, h, l, c, v)."""
    trs = []
    for i in range(max(1, len(bars) - period), len(bars)):
        h, l, pc = bars[i][2], bars[i][3], bars[i - 1][4]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return mean(trs)
