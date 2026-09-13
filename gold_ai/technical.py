"""Análise técnica multi-timeframe (Diretriz §17, §18).

Implementação em Python puro (sem dependências) dos indicadores exigidos:
EMA 9/21/50/200, RSI, MACD, ATR, ADX, Bollinger, VWAP e estrutura de mercado.
Nenhum indicador isolado decide: a nota do timeframe é a média ponderada de
várias evidências, e a nota global é a média ponderada dos timeframes.
"""

from __future__ import annotations

from typing import Optional, Sequence

from .config import TIMEFRAME_WEIGHTS
from .models import Candle, TechnicalReading


def _clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def ema(values: Sequence[float], period: int) -> list[float]:
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
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


def rsi(closes: Sequence[float], period: int = 14) -> Optional[float]:
    if len(closes) <= period:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_gain, avg_loss = gains / period, losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(d, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0.0)) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9) -> tuple[list[float], list[float], list[float]]:
    if len(closes) < slow + signal:
        return [], [], []
    line = [f - s for f, s in zip(ema(closes, fast), ema(closes, slow))]
    sig = ema(line, signal)
    hist = [a - b for a, b in zip(line, sig)]
    return line, sig, hist


def true_ranges(candles: Sequence[Candle]) -> list[float]:
    trs: list[float] = []
    for i, c in enumerate(candles):
        if i == 0:
            trs.append(c.high - c.low)
        else:
            pc = candles[i - 1].close
            trs.append(max(c.high - c.low, abs(c.high - pc), abs(c.low - pc)))
    return trs


def atr(candles: Sequence[Candle], period: int = 14) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    trs = true_ranges(candles)
    a = sum(trs[1 : period + 1]) / period
    for tr in trs[period + 1 :]:
        a = (a * (period - 1) + tr) / period
    return a


def adx(candles: Sequence[Candle], period: int = 14) -> Optional[float]:
    if len(candles) < 2 * period + 1:
        return None
    plus_dm, minus_dm, trs = [], [], true_ranges(candles)[1:]
    for i in range(1, len(candles)):
        up = candles[i].high - candles[i - 1].high
        down = candles[i - 1].low - candles[i].low
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)

    def wilder(xs: list[float]) -> list[float]:
        out = [sum(xs[:period])]
        for x in xs[period:]:
            out.append(out[-1] - out[-1] / period + x)
        return out

    tr_s, pdm_s, mdm_s = wilder(trs), wilder(plus_dm), wilder(minus_dm)
    dx: list[float] = []
    for t, p, m in zip(tr_s, pdm_s, mdm_s):
        if t == 0:
            dx.append(0.0)
            continue
        pdi, mdi = 100 * p / t, 100 * m / t
        dx.append(100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) else 0.0)
    if len(dx) < period:
        return None
    a = sum(dx[:period]) / period
    for x in dx[period:]:
        a = (a * (period - 1) + x) / period
    return a


def bollinger(closes: Sequence[float], period: int = 20, mult: float = 2.0) -> Optional[tuple[float, float, float]]:
    if len(closes) < period:
        return None
    window = closes[-period:]
    mid = sum(window) / period
    var = sum((c - mid) ** 2 for c in window) / period
    sd = var ** 0.5
    return mid - mult * sd, mid, mid + mult * sd


def vwap(candles: Sequence[Candle]) -> Optional[float]:
    vol = sum(c.volume for c in candles)
    if vol <= 0:
        return None
    return sum(((c.high + c.low + c.close) / 3) * c.volume for c in candles) / vol


def swing_levels(candles: Sequence[Candle], lookback: int = 20) -> tuple[Optional[float], Optional[float]]:
    """Suporte/resistência simples: mínima/máxima do lookback."""
    if not candles:
        return None, None
    win = candles[-lookback:]
    return min(c.low for c in win), max(c.high for c in win)


def analyze_timeframe(tf: str, candles: Sequence[Candle]) -> TechnicalReading:
    """Nota técnica -1..+1 de um timeframe combinando múltiplas evidências."""
    closes = [c.close for c in candles]
    reading = TechnicalReading(timeframe=tf, score=0.0, trend="LATERAL")
    if len(closes) < 30:
        reading.notes.append("dados insuficientes")
        return reading

    close = closes[-1]
    e9, e21, e50 = ema(closes, 9)[-1], ema(closes, 21)[-1], ema(closes, 50)[-1]
    e200 = ema(closes, 200)[-1] if len(closes) >= 200 else None
    _atr = atr(candles) or (max(closes) - min(closes)) / 10 or 1.0
    reading.atr = _atr

    evidences: list[tuple[float, float]] = []  # (valor -1..1, peso)

    # 1) Alinhamento de EMAs (estrutura de tendência)
    align = 0.0
    align += 1 if close > e9 else -1
    align += 1 if e9 > e21 else -1
    align += 1 if e21 > e50 else -1
    if e200 is not None:
        align += 1 if close > e200 else -1
        align /= 4
    else:
        align /= 3
    reading.ema_alignment = align
    evidences.append((align, 0.30))

    # 2) RSI (momentum; extremos penalizam a continuação)
    r = rsi(closes)
    reading.rsi = r
    if r is not None:
        rs = (r - 50) / 25  # 25→-1, 75→+1
        if r > 75:
            rs = 0.4  # sobrecomprado: continuação menos provável
            reading.notes.append("RSI sobrecomprado")
        elif r < 25:
            rs = -0.4
            reading.notes.append("RSI sobrevendido")
        evidences.append((_clip(rs), 0.15))

    # 3) MACD (momentum direcional + divergência simples)
    _, _, hist = macd(closes)
    if hist:
        reading.macd_hist = hist[-1]
        h_norm = _clip(hist[-1] / (_atr * 0.5))
        evidences.append((h_norm, 0.15))
        # perda de momentum: histograma encolhendo enquanto preço estende
        if len(hist) >= 4 and abs(hist[-1]) < abs(hist[-3]) and abs(hist[-1]) < abs(hist[-4]):
            reading.notes.append("momentum perdendo força (MACD)")

    # 4) ADX (força da tendência amplifica ou reduz a leitura direcional)
    a = adx(candles)
    reading.adx = a
    strength = 1.0
    if a is not None:
        strength = 0.6 + 0.4 * _clip((a - 15) / 25, 0.0, 1.0)
        if a < 18:
            reading.notes.append("mercado sem tendência (ADX baixo)")

    # 5) VWAP (posição relativa)
    v = vwap(candles[-60:])
    if v is not None and _atr:
        pos = (close - v) / _atr
        reading.vwap_position = pos
        evidences.append((_clip(pos / 2), 0.15))

    # 6) Bollinger (posição na banda)
    bb = bollinger(closes)
    if bb:
        lo, mid, hi = bb
        width = (hi - lo) or 1.0
        pos = (close - mid) / (width / 2)
        evidences.append((_clip(pos), 0.10))
        if close > hi:
            reading.notes.append("acima da banda superior")
        elif close < lo:
            reading.notes.append("abaixo da banda inferior")

    # 7) Estrutura: máximas/mínimas e rompimentos
    sup, res = swing_levels(candles[:-1])
    reading.support, reading.resistance = sup, res
    if sup is not None and res is not None:
        if close > res:
            evidences.append((0.8, 0.15))
            reading.notes.append("rompimento de resistência")
        elif close < sup:
            evidences.append((-0.8, 0.15))
            reading.notes.append("perda de suporte")
        else:
            rng = (res - sup) or 1.0
            evidences.append((_clip((close - sup) / rng * 2 - 1) * 0.5, 0.15))

    # 8) Falso rompimento: máxima acima da resistência mas fechamento abaixo
    last = candles[-1]
    if res is not None and last.high > res and last.close < res:
        evidences.append((-0.6, 0.10))
        reading.notes.append("falso rompimento (rejeição na resistência)")
    if sup is not None and last.low < sup and last.close > sup:
        evidences.append((0.6, 0.10))
        reading.notes.append("falso rompimento (absorção no suporte)")

    total_w = sum(w for _, w in evidences) or 1.0
    score = sum(v * w for v, w in evidences) / total_w * strength
    reading.score = _clip(score)
    reading.trend = "ALTA" if score > 0.2 else "BAIXA" if score < -0.2 else "LATERAL"
    return reading


def analyze_multi_timeframe(candles_by_tf: dict[str, Sequence[Candle]]) -> tuple[float, list[TechnicalReading]]:
    """Retorna (nota técnica global -1..+1, leituras por timeframe)."""
    readings: list[TechnicalReading] = []
    num, den = 0.0, 0.0
    for tf, w in TIMEFRAME_WEIGHTS.items():
        cs = candles_by_tf.get(tf)
        if not cs:
            continue
        r = analyze_timeframe(tf, cs)
        readings.append(r)
        if "dados insuficientes" in r.notes:
            continue
        num += r.score * w
        den += w
    return (num / den if den else 0.0), readings


def volume_profile_signals(candles: Sequence[Candle], lookback: int = 20) -> dict[str, float]:
    """Sinais de acumulação/distribuição por volume × preço (Diretriz §15, §16).

    Retorna dicionário com:
      volume_ratio   — volume recente / média anterior
      range_ratio    — amplitude recente / amplitude anterior (lateral = < 1)
      absorption     — +1 volume alto com preço lateral (acumulação/distribuição)
      divergence     — preço subindo com volume caindo (>0 = distribuição), inverso <0
    """
    if len(candles) < lookback * 2:
        return {}
    recent, prev = candles[-lookback:], candles[-2 * lookback : -lookback]
    v_recent = sum(c.volume for c in recent) / lookback
    v_prev = sum(c.volume for c in prev) / lookback or 1.0
    r_recent = max(c.high for c in recent) - min(c.low for c in recent)
    r_prev = (max(c.high for c in prev) - min(c.low for c in prev)) or 1.0
    volume_ratio = v_recent / v_prev
    range_ratio = r_recent / r_prev
    absorption = 1.0 if volume_ratio > 1.2 and range_ratio < 0.8 else 0.0
    price_move = recent[-1].close - recent[0].open
    divergence = 0.0
    if price_move > 0 and volume_ratio < 0.8:
        divergence = 1.0
    elif price_move < 0 and volume_ratio < 0.8:
        divergence = -1.0
    return {
        "volume_ratio": volume_ratio,
        "range_ratio": range_ratio,
        "absorption": absorption,
        "divergence": divergence,
    }
