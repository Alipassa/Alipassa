"""Coletor Yahoo Finance (chart API v8) — candles multi-timeframe e variações.

Sem chave. Símbolos úteis: GC=F (ouro futuro), XAUUSD=X (spot), DX-Y.NYB (DXY),
^TNX (10Y %), ^FVX (5Y), ^TYX (30Y), 2YY=F (2Y), ZQ=F (Fed Funds futuro),
^VIX, ^GSPC, SI=F (prata), CL=F (petróleo), BTC-USD, CNH=X (USD/CNH).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from ..models import Candle
from .http import DataError, HttpClient

YAHOO_BASE = "https://query1.finance.yahoo.com/v8/finance/chart/"

# timeframe interno → (intervalo Yahoo, range Yahoo)
TF_MAP: dict[str, tuple[str, str]] = {
    "M1": ("1m", "5d"),
    "M5": ("5m", "5d"),
    "M15": ("15m", "1mo"),
    "M30": ("30m", "1mo"),
    "H1": ("1h", "3mo"),
    "D1": ("1d", "2y"),
    "W1": ("1wk", "5y"),
}
TF_MINUTES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440, "W1": 10080}


def parse_chart(payload: dict) -> list[Candle]:
    """Converte a resposta da chart API em candles (ignora barras nulas)."""
    try:
        result = payload["chart"]["result"][0]
    except (KeyError, IndexError, TypeError) as e:
        err = (payload or {}).get("chart", {}).get("error") if isinstance(payload, dict) else None
        raise DataError(f"resposta Yahoo inválida: {err or e}")
    ts = result.get("timestamp") or []
    q = (result.get("indicators", {}).get("quote") or [{}])[0]
    out: list[Candle] = []
    for i, t in enumerate(ts):
        o, h, l, c = q.get("open", [None])[i], q.get("high", [None])[i], q.get("low", [None])[i], q.get("close", [None])[i]
        if None in (o, h, l, c):
            continue
        v = (q.get("volume") or [0] * len(ts))[i] or 0
        out.append(Candle(datetime.fromtimestamp(t, tz=timezone.utc), float(o), float(h), float(l), float(c), float(v)))
    return out


def resample(candles: list[Candle], minutes: int) -> list[Candle]:
    """Agrega candles em blocos de `minutes` alinhados à época (ex.: H1 → H4, D1 → W1)."""
    out: list[Candle] = []
    bucket: list[Candle] = []
    key: Optional[int] = None
    for c in candles:
        k = int(c.time.timestamp() // (minutes * 60))
        if key is not None and k != key and bucket:
            out.append(_merge(bucket))
            bucket = []
        key = k
        bucket.append(c)
    if bucket:
        out.append(_merge(bucket))
    return out


def _merge(b: list[Candle]) -> Candle:
    return Candle(b[0].time, b[0].open, max(c.high for c in b), min(c.low for c in b), b[-1].close, sum(c.volume for c in b))


class YahooCollector:
    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def candles(self, symbol: str, tf: str, ttl: int = 60) -> list[Candle]:
        if tf == "H4":
            return resample(self.candles(symbol, "H1", ttl), 240)
        interval, rng = TF_MAP[tf]
        url = f"{YAHOO_BASE}{symbol.replace('=', '%3D').replace('^', '%5E')}?interval={interval}&range={rng}&includePrePost=false"
        return parse_chart(self.http.get_json(url, ttl))

    def all_timeframes(self, symbol: str, tfs: tuple[str, ...] = ("M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1")) -> dict[str, list[Candle]]:
        out: dict[str, list[Candle]] = {}
        for tf in tfs:
            cs = self.candles(symbol, tf, ttl=60 if TF_MINUTES[tf] <= 60 else 900)
            if cs:
                out[tf] = cs
        return out

    def last_price(self, symbol: str) -> Optional[float]:
        cs = self.candles(symbol, "M5")
        return cs[-1].close if cs else None

    @staticmethod
    def change_over(candles: list[Candle], window_minutes: int, pct: bool = True) -> Optional[float]:
        """Variação do fechamento na janela (em % ou absoluta) usando o timestamp dos candles."""
        if len(candles) < 2:
            return None
        last = candles[-1]
        cutoff = last.time - timedelta(minutes=window_minutes)
        ref = None
        for c in reversed(candles[:-1]):
            if c.time <= cutoff:
                ref = c
                break
        if ref is None:
            ref = candles[0]
        if pct:
            return (last.close / ref.close - 1.0) * 100.0 if ref.close else None
        return last.close - ref.close
