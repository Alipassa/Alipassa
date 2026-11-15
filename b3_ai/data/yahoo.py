"""Yahoo Finance (chart v8) — reserva para o histórico diário quando o feed da TradingView não responde."""

from __future__ import annotations

import urllib.parse

from .http import DataError, get_json

BASE = "https://query1.finance.yahoo.com/v8/finance/chart/"


def parse_chart(payload: dict) -> list[tuple]:
    try:
        res = payload["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        raise DataError("resposta Yahoo inválida")
    ts = res.get("timestamp") or []
    q = (res.get("indicators", {}).get("quote") or [{}])[0]
    out = []
    for i, t in enumerate(ts):
        try:
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        except (KeyError, IndexError):
            break
        if None in (o, h, l, c):
            continue
        v = (q.get("volume") or [0] * len(ts))[i] or 0
        out.append((float(t), float(o), float(h), float(l), float(c), float(v)))
    return out


def fetch_daily(symbol: str, rng: str = "5y") -> list[tuple]:
    url = f"{BASE}{urllib.parse.quote(symbol)}?range={rng}&interval=1d"
    return parse_chart(get_json(url))
