"""TradingView — dados reais sem chave.

* `scan_b3()`      : todas as ações/units/BDRs/FIIs/ETFs negociados na B3 numa única chamada (scanner).
* `scan_symbols()` : cotação atual de uma lista de símbolos de qualquer bolsa.
* `fetch_history()`: candles históricos (diário, 5 min...) pelo feed WebSocket público.

Cotações da B3 e das bolsas americanas chegam com ~15 min de atraso (dado público gratuito).
"""

from __future__ import annotations

import json
import re
import time
from typing import Iterable, Optional

from .http import DataError, post_json
from .ws import WebSocket, WsError

SCAN_URL = "https://scanner.tradingview.com/{market}/scan"
WS_URL = "wss://data.tradingview.com/socket.io/websocket"

B3_COLUMNS = ["name", "description", "close", "change", "volume", "Value.Traded", "market_cap_basic", "sector",
              "type", "subtype", "RSI", "Perf.W", "Perf.1M", "Perf.YTD", "price_earnings_ttm", "dividend_yield_recent",
              "SMA50", "SMA200", "Recommend.All", "relative_volume_10d_calc", "high", "low", "open",
              "recommendation_mark", "recommendation_buy", "recommendation_over", "recommendation_hold",
              "recommendation_under", "recommendation_sell", "recommendation_total"]

Bar = tuple  # (ts, open, high, low, close, volume)


def scan_b3(limit: int = 4000) -> list[dict]:
    payload = {"filter": [{"left": "type", "operation": "in_range", "right": ["stock", "dr", "fund"]}],
               "options": {"lang": "pt"}, "markets": ["brazil"], "columns": B3_COLUMNS,
               "sort": {"sortBy": "Value.Traded", "sortOrder": "desc"}, "range": [0, limit]}
    data = post_json(SCAN_URL.format(market="brazil"), payload)
    rows = data.get("data")
    if not isinstance(rows, list):
        raise DataError(f"scanner B3 sem dados: {str(data)[:200]}")
    return [dict(zip(B3_COLUMNS, r.get("d") or [])) for r in rows]


def scan_symbols(tickers: Iterable[str], columns: list[str]) -> dict[str, dict]:
    data = post_json(SCAN_URL.format(market="global"), {"symbols": {"tickers": list(tickers)}, "columns": columns})
    out: dict[str, dict] = {}
    for r in data.get("data") or []:
        out[r.get("s", "")] = dict(zip(columns, r.get("d") or []))
    return out


def _frame(m: str, p: list) -> str:
    s = json.dumps({"m": m, "p": p}, separators=(",", ":"))
    return f"~m~{len(s)}~m~{s}"


def _fetch_chunk(requests: list[tuple[str, str, int]], timeout: float = 45.0,
                  delays: Optional[dict[str, int]] = None) -> dict[tuple[str, str], list[Bar]]:
    """requests = [(símbolo, timeframe, nº de barras)], timeframe "1D", "5", "15", "60"...

    Devolve {(símbolo, timeframe): [(ts, o, h, l, c, v), ...]} em ordem cronológica. Símbolos que falharem ficam fora.
    `delays`, se passado, recebe {símbolo: atraso em segundos} informado pelo feed (0 = tempo real).
    """
    out: dict[tuple[str, str], list[Bar]] = {}
    if not requests:
        return out
    try:
        ws = WebSocket(WS_URL, origin="https://www.tradingview.com", timeout=timeout)
    except (WsError, OSError) as e:
        raise DataError(f"histórico TradingView indisponível: {e}")
    try:
        ws.send(_frame("set_auth_token", ["unauthorized_user_token"]))
        keys: dict[str, tuple[str, str]] = {}
        for i, (sym, tf, n) in enumerate(requests):
            cs = f"cs_{i}"
            keys[cs] = (sym, tf)
            ws.send(_frame("chart_create_session", [cs, ""]))
            ws.send(_frame("resolve_symbol", [cs, "sym", "=" + json.dumps({"symbol": sym, "adjustment": "splits"})]))
            ws.send(_frame("create_series", [cs, "s", "s1", "sym", tf, int(n), ""]))
        pending = set(keys)
        t0 = time.time()
        while pending and time.time() - t0 < timeout:
            raw = ws.recv()
            for msg in re.split(r"~m~\d+~m~", raw):
                if not msg:
                    continue
                if msg.startswith("~h~"):
                    ws.send(f"~m~{len(msg)}~m~{msg}")
                    continue
                try:
                    d = json.loads(msg)
                except json.JSONDecodeError:
                    continue
                m, p = d.get("m"), d.get("p") or []
                cs = p[0] if p and isinstance(p[0], str) else None
                if m == "timescale_update" and cs in keys:
                    bars = ((p[1].get("s") or {}).get("s")) or []
                    series = out.setdefault(keys[cs], [])
                    for b in bars:
                        v = b.get("v") or []
                        if len(v) >= 5 and None not in v[:5]:
                            series.append((float(v[0]), float(v[1]), float(v[2]), float(v[3]), float(v[4]),
                                           float(v[5]) if len(v) > 5 and v[5] is not None else 0.0))
                elif m == "symbol_resolved" and cs in keys and delays is not None and len(p) > 2 and isinstance(p[2], dict):
                    delays[keys[cs][0]] = int(p[2].get("delay") or 0)
                elif m == "series_completed" and cs in pending:
                    pending.discard(cs)
                elif m in ("symbol_error", "series_error") and cs in pending:
                    pending.discard(cs)
                elif m in ("critical_error", "protocol_error"):
                    raise DataError(f"TradingView recusou a sessão: {str(p)[:160]}")
    except (WsError, OSError) as e:
        if not out:
            raise DataError(f"histórico TradingView falhou: {e}")
    finally:
        ws.close()
    for k in out:
        out[k].sort(key=lambda b: b[0])
    return out


def fetch_history(requests: list[tuple[str, str, int]], timeout: float = 45.0,
                  delays: Optional[dict[str, int]] = None, chunk: int = 12) -> dict[tuple[str, str], list[Bar]]:
    """Histórico de várias séries, em conexões pequenas (até `chunk` séries cada) e com uma nova tentativa por lote.

    O servidor público derruba conexões com séries demais ou pedidos muito frequentes; lotes pequenos e a repetição evitam
    que uma queda derrube a leitura inteira. Levanta DataError só se NENHUM lote vier.
    """
    out: dict[tuple[str, str], list[Bar]] = {}
    last: Optional[Exception] = None
    for i in range(0, len(requests), chunk):
        part = requests[i:i + chunk]
        for attempt in (0, 1):
            try:
                out.update(_fetch_chunk(part, timeout, delays))
                break
            except DataError as e:
                last = e
                time.sleep(1.5 * (attempt + 1))
        if i + chunk < len(requests):
            time.sleep(0.25)
    if not out and requests:
        raise last or DataError("histórico TradingView indisponível")
    return out


def fetch_one(symbol: str, tf: str = "1D", n: int = 300) -> Optional[list[Bar]]:
    return fetch_history([(symbol, tf, n)]).get((symbol, tf))
