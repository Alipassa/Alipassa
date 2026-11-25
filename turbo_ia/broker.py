"""Corretoras: MT5 (terminal MetaTrader 5 real/demo) e PAPER (simulador local para testar sem risco).

Interface comum:
    connect() · info() · symbols() · candles(symbol, tf, n) · tick(symbol) -> (bid, ask)
    open(symbol, side, volume, sl, tp, comment) -> dict · positions(symbol=None) · close(ticket) · update()
"""

from __future__ import annotations

import math
import os
import random
import threading
import time
from typing import Any, Optional

TF_SECONDS = {"M1": 60, "M2": 120, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600}
MAGIC = 260_611


class BrokerError(RuntimeError):
    pass


# ---------------------------------------------------------------------------------------------------- PAPER
class PaperBroker:
    """Simulador: índice sintético (passeio aleatório com volatilidade constante, como um 'Volatility Index')
    e execução local com SL/TP. Serve para ver o painel funcionando em qualquer sistema (Linux/Mac sem MT5)."""

    name = "paper"
    utc_offset = 0.0

    def __init__(self, balance: float = 10_000.0, seed: Optional[int] = None, clock=time.time) -> None:
        self.clock = clock
        self.rng = random.Random(seed)
        self.balance = balance
        self.lock = threading.RLock()
        self.syms = {"Volatility 10 Index": (4_874.0, 0.00022, 3), "Volatility 75 Index": (32_000.0, 0.0012, 2),
                     "XAUUSD": (2_650.0, 0.00012, 2), "EURUSD": (1.0850, 0.00006, 5)}
        self.data: dict[str, list[dict]] = {}
        self.last_t: dict[str, float] = {}
        self._pos: list[dict] = []
        self._ticket = 1000
        self.closed: list[dict] = []

    def connect(self) -> None:
        now = self.clock()
        for s, (p0, vol, _d) in self.syms.items():
            m1: list[dict] = []
            p = p0
            start = int(now // 60) * 60 - 3000 * 60
            for k in range(3000):
                o = p
                hi = lo = o
                for _ in range(12):                     # 12 passos de 5 s por minuto
                    p *= math.exp(self.rng.gauss(0, vol))
                    hi, lo = max(hi, p), min(lo, p)
                m1.append({"time": start + k * 60, "open": o, "high": hi, "low": lo, "close": p})
            self.data[s] = m1
            self.last_t[s] = now

    def _digits(self, s: str) -> int:
        return self.syms[s][2]

    def update(self) -> None:
        """Avança o preço até o relógio atual e verifica SL/TP das posições."""
        with self.lock:
            now = self.clock()
            for s, (_p0, vol, _d) in self.syms.items():
                m1 = self.data[s]
                steps = int((now - self.last_t[s]) // 5)
                for k in range(min(steps, 720)):
                    t = self.last_t[s] + (k + 1) * 5
                    bucket = int(t // 60) * 60
                    last = m1[-1]
                    p = last["close"] * math.exp(self.rng.gauss(0, vol))
                    if bucket > last["time"]:
                        m1.append({"time": bucket, "open": last["close"], "high": max(p, last["close"]), "low": min(p, last["close"]), "close": p})
                        if len(m1) > 6000:
                            del m1[:1000]
                    else:
                        last["close"], last["high"], last["low"] = p, max(last["high"], p), min(last["low"], p)
                    self._check_stops(s)
                if steps:
                    self.last_t[s] += steps * 5

    def _check_stops(self, s: str) -> None:
        bid, ask = self.tick(s)
        for p in list(self._pos):
            if p["symbol"] != s:
                continue
            px = bid if p["side"] == "BUY" else ask
            hit_sl = p["sl"] and ((p["side"] == "BUY" and px <= p["sl"]) or (p["side"] == "SELL" and px >= p["sl"]))
            hit_tp = p["tp"] and ((p["side"] == "BUY" and px >= p["tp"]) or (p["side"] == "SELL" and px <= p["tp"]))
            if hit_sl or hit_tp:
                self._close(p, p["sl"] if hit_sl else p["tp"], "SL" if hit_sl else "TP")

    def info(self) -> dict:
        eq = self.balance + sum(self._profit(p) for p in self._pos)
        return {"broker": "paper", "login": "PAPER", "server": "simulador local", "demo": True, "balance": round(self.balance, 2),
                "equity": round(eq, 2), "currency": "USD"}

    def symbols(self) -> list[str]:
        return list(self.syms)

    def candles(self, symbol: str, tf: str = "M1", n: int = 300) -> list[dict]:
        with self.lock:
            m1 = self.data.get(symbol)
            if m1 is None:
                raise BrokerError(f"símbolo {symbol} inexistente no simulador")
            sec = TF_SECONDS.get(tf, 60)
            d = self._digits(symbol)
            rnd = lambda c: {"time": c["time"], **{k: round(c[k], d) for k in ("open", "high", "low", "close")}}  # noqa: E731
            if sec == 60:
                return [rnd(c) for c in m1[-n:]]
            out: list[dict] = []
            for c in m1:
                b = int(c["time"] // sec) * sec
                if out and out[-1]["time"] == b:
                    o = out[-1]
                    o["high"], o["low"], o["close"] = max(o["high"], c["high"]), min(o["low"], c["low"]), c["close"]
                else:
                    out.append({"time": b, "open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"]})
            return [rnd(c) for c in out[-n:]]

    def tick(self, symbol: str) -> tuple[float, float]:
        p = self.data[symbol][-1]["close"]
        d = self._digits(symbol)
        half = max(p * 0.00001, 10 ** -d)
        return round(p - half, d), round(p + half, d)

    def spec(self, symbol: str) -> dict:
        return {"digits": self._digits(symbol), "volume_min": 0.01, "volume_step": 0.01, "volume_max": 100.0, "contract_size": 1.0}

    def open(self, symbol: str, side: str, volume: float, sl: Optional[float] = None, tp: Optional[float] = None, comment: str = "TurboIA") -> dict:
        with self.lock:
            bid, ask = self.tick(symbol)
            price = ask if side == "BUY" else bid
            self._ticket += 1
            d = self._digits(symbol)
            p = {"ticket": self._ticket, "symbol": symbol, "side": side, "volume": round(volume, 2), "price_open": round(price, d),
                 "sl": round(sl, d) if sl else 0.0, "tp": round(tp, d) if tp else 0.0, "time": self.clock(), "comment": comment}
            self._pos.append(p)
            return {"ok": True, "ticket": p["ticket"], "price": p["price_open"], "volume": p["volume"], "sl": p["sl"], "tp": p["tp"]}

    def _profit(self, p: dict) -> float:
        bid, ask = self.tick(p["symbol"])
        px = bid if p["side"] == "BUY" else ask
        return (px - p["price_open"]) * (1 if p["side"] == "BUY" else -1) * p["volume"]

    def positions(self, symbol: Optional[str] = None) -> list[dict]:
        with self.lock:
            return [{**p, "profit": round(self._profit(p), 2)} for p in self._pos if symbol is None or p["symbol"] == symbol]

    def _close(self, p: dict, price: float, reason: str) -> dict:
        pnl = (price - p["price_open"]) * (1 if p["side"] == "BUY" else -1) * p["volume"]
        self.balance += pnl
        self._pos.remove(p)
        rec = {**p, "price_close": price, "profit": round(pnl, 2), "reason": reason, "time_close": self.clock()}
        self.closed.append(rec)
        return rec

    def close(self, ticket: int, reason: str = "manual") -> dict:
        with self.lock:
            p = next((x for x in self._pos if x["ticket"] == ticket), None)
            if p is None:
                return {"ok": False, "error": "posição não encontrada"}
            bid, ask = self.tick(p["symbol"])
            rec = self._close(p, bid if p["side"] == "BUY" else ask, reason)
            return {"ok": True, **rec}

    def closed_profit(self, ticket: int) -> Optional[float]:
        rec = next((c for c in self.closed if c["ticket"] == ticket), None)
        return rec["profit"] if rec else None


# ---------------------------------------------------------------------------------------------------- MT5
class MT5Broker:
    """MetaTrader 5 via pacote oficial `MetaTrader5` (Windows, terminal aberto e logado).

    Segurança: por padrão SÓ envia ordens em conta DEMO. Para conta real defina TURBO_IA_ALLOW_REAL=1 no ambiente.
    """

    name = "mt5"
    utc_offset = 0.0           # horas que o relógio do servidor está à frente do UTC (detectado no connect)
    TF = {"M1": "TIMEFRAME_M1", "M2": "TIMEFRAME_M2", "M5": "TIMEFRAME_M5", "M15": "TIMEFRAME_M15", "M30": "TIMEFRAME_M30", "H1": "TIMEFRAME_H1"}

    def __init__(self, mt5: Any = None, path: Optional[str] = None, login: Optional[int] = None, password: Optional[str] = None,
                 server: Optional[str] = None, allow_real: Optional[bool] = None, deviation: int = 20) -> None:
        if mt5 is None:
            try:
                import MetaTrader5 as mt5  # type: ignore
            except Exception as e:  # noqa: BLE001
                raise BrokerError("pacote MetaTrader5 não instalado — no Windows: pip install MetaTrader5") from e
        self.mt5 = mt5
        self.path = path or os.environ.get("MT5_PATH")
        lg = login or os.environ.get("MT5_LOGIN")
        self.login = int(lg) if lg else None
        self.password = password or os.environ.get("MT5_PASSWORD")
        self.server = server or os.environ.get("MT5_SERVER")
        self.allow_real = allow_real if allow_real is not None else os.environ.get("TURBO_IA_ALLOW_REAL") == "1"
        self.deviation = deviation
        self.lock = threading.RLock()

    def connect(self) -> None:
        kw: dict[str, Any] = {}
        if self.path:
            kw["path"] = self.path
        if self.login:
            kw.update(login=self.login, password=self.password, server=self.server)
        if not self.mt5.initialize(**kw):
            raise BrokerError(f"MT5 initialize falhou: {self.mt5.last_error()} — abra o terminal, faça login e confira MT5_PATH")
        self.utc_offset = self.detect_offset()

    def detect_offset(self) -> float:
        """Candles do MT5 vêm no horário do SERVIDOR. Compara o tick mais recente com o relógio UTC; TURBO_IA_UTC_OFFSET força."""
        forced = os.environ.get("TURBO_IA_UTC_OFFSET") or os.environ.get("MT5_UTC_OFFSET_HOURS")
        if forced not in (None, ""):
            return float(forced)
        ts = 0.0
        for sym in ("EURUSD", "XAUUSD", "GBPUSD", "USDJPY", "Volatility 10 Index"):
            try:
                t = self.mt5.symbol_info_tick(sym)
                ts = max(ts, float(getattr(t, "time", 0) or 0))
            except Exception:  # noqa: BLE001
                continue
        if not ts:
            return 0.0
        raw = (ts - time.time()) / 3600
        off = round(raw)
        return float(off) if abs(raw - off) < 0.25 and abs(off) <= 14 else 0.0

    def update(self) -> None:
        pass

    def _is_demo(self, acc: Any) -> bool:
        return getattr(acc, "trade_mode", None) == getattr(self.mt5, "ACCOUNT_TRADE_MODE_DEMO", 0)

    def info(self) -> dict:
        acc = self.mt5.account_info()
        if acc is None:
            raise BrokerError(f"sem conta logada: {self.mt5.last_error()}")
        return {"broker": "mt5", "login": acc.login, "server": acc.server, "demo": self._is_demo(acc), "balance": acc.balance,
                "equity": acc.equity, "currency": acc.currency, "allow_real": self.allow_real}

    def symbols(self) -> list[str]:
        syms = self.mt5.symbols_get() or []
        vis = [s.name for s in syms if getattr(s, "visible", True)]
        return vis or [s.name for s in syms][:200]

    def _select(self, symbol: str) -> Any:
        if not self.mt5.symbol_select(symbol, True):
            raise BrokerError(f"símbolo '{symbol}' indisponível no MT5 (confira o nome exato na Observação do Mercado)")
        return self.mt5.symbol_info(symbol)

    def candles(self, symbol: str, tf: str = "M1", n: int = 300) -> list[dict]:
        self._select(symbol)
        rates = self.mt5.copy_rates_from_pos(symbol, getattr(self.mt5, self.TF.get(tf, "TIMEFRAME_M1")), 0, n)
        if rates is None or len(rates) == 0:
            raise BrokerError(f"sem candles de {symbol} {tf}: {self.mt5.last_error()}")
        return [{"time": int(r["time"]), "open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]), "close": float(r["close"])} for r in rates]

    def tick(self, symbol: str) -> tuple[float, float]:
        t = self.mt5.symbol_info_tick(symbol)
        if t is None:
            raise BrokerError(f"sem tick de {symbol}")
        return float(t.bid), float(t.ask)

    def spec(self, symbol: str) -> dict:
        i = self._select(symbol)
        return {"digits": i.digits, "volume_min": i.volume_min, "volume_step": i.volume_step, "volume_max": i.volume_max,
                "contract_size": getattr(i, "trade_contract_size", 1.0), "point": i.point,
                "stops_level": getattr(i, "trade_stops_level", 0), "filling_mode": getattr(i, "filling_mode", 0)}

    def _filling(self, spec: dict) -> int:
        fm = spec.get("filling_mode", 0)
        if fm & 1:
            return self.mt5.ORDER_FILLING_FOK
        if fm & 2:
            return self.mt5.ORDER_FILLING_IOC
        return self.mt5.ORDER_FILLING_RETURN

    @staticmethod
    def normalize_volume(v: float, spec: dict) -> float:
        step = spec["volume_step"] or 0.01
        v = math.floor(v / step + 1e-9) * step
        v = max(spec["volume_min"], min(spec["volume_max"], v))
        dec = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
        return round(v, dec)

    def open(self, symbol: str, side: str, volume: float, sl: Optional[float] = None, tp: Optional[float] = None, comment: str = "TurboIA") -> dict:
        with self.lock:
            acc = self.mt5.account_info()
            if acc is None:
                return {"ok": False, "error": "sem conta logada no MT5"}
            if not self._is_demo(acc) and not self.allow_real:
                return {"ok": False, "error": "conta REAL detectada — ordens bloqueadas. Teste em DEMO ou defina TURBO_IA_ALLOW_REAL=1 conscientemente."}
            spec = self.spec(symbol)
            bid, ask = self.tick(symbol)
            buy = side == "BUY"
            price = ask if buy else bid
            point = spec.get("point") or 10 ** -spec["digits"]
            min_dist = max(spec.get("stops_level", 0) * point, (ask - bid) + point)
            if sl:
                sl = min(sl, bid - min_dist) if buy else max(sl, ask + min_dist)
            if tp:
                tp = max(tp, ask + min_dist) if buy else min(tp, bid - min_dist)
            vol = self.normalize_volume(volume, spec)
            d = spec["digits"]
            req = {"action": self.mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": vol,
                   "type": self.mt5.ORDER_TYPE_BUY if buy else self.mt5.ORDER_TYPE_SELL, "price": price,
                   "sl": round(sl, d) if sl else 0.0, "tp": round(tp, d) if tp else 0.0, "deviation": self.deviation,
                   "magic": MAGIC, "comment": comment[:31], "type_time": self.mt5.ORDER_TIME_GTC, "type_filling": self._filling(spec)}
            res = self.mt5.order_send(req)
            if res is None:
                return {"ok": False, "error": f"order_send None: {self.mt5.last_error()}"}
            if res.retcode != self.mt5.TRADE_RETCODE_DONE:
                return {"ok": False, "error": f"corretora recusou: {res.comment} (retcode {res.retcode})", "request": {k: v for k, v in req.items() if k != 'type_filling'}}
            return {"ok": True, "ticket": int(getattr(res, "order", 0)), "price": float(getattr(res, "price", price)), "volume": vol,
                    "sl": req["sl"], "tp": req["tp"]}

    def positions(self, symbol: Optional[str] = None) -> list[dict]:
        ps = (self.mt5.positions_get(symbol=symbol) if symbol else self.mt5.positions_get()) or []
        out = []
        for p in ps:
            if getattr(p, "magic", MAGIC) != MAGIC:
                continue                          # só mexe no que a IA abriu
            out.append({"ticket": int(p.ticket), "symbol": p.symbol, "side": "BUY" if p.type == self.mt5.POSITION_TYPE_BUY else "SELL",
                        "volume": float(p.volume), "price_open": float(p.price_open), "sl": float(p.sl), "tp": float(p.tp),
                        "profit": float(p.profit), "time": int(p.time)})
        return out

    def close(self, ticket: int, reason: str = "manual") -> dict:
        with self.lock:
            p = next((x for x in self.positions() if x["ticket"] == ticket), None)
            if p is None:
                return {"ok": False, "error": "posição não encontrada"}
            spec = self.spec(p["symbol"])
            bid, ask = self.tick(p["symbol"])
            buy = p["side"] == "BUY"
            req = {"action": self.mt5.TRADE_ACTION_DEAL, "position": ticket, "symbol": p["symbol"], "volume": p["volume"],
                   "type": self.mt5.ORDER_TYPE_SELL if buy else self.mt5.ORDER_TYPE_BUY, "price": bid if buy else ask,
                   "deviation": self.deviation, "magic": MAGIC, "comment": f"TurboIA {reason}"[:31],
                   "type_time": self.mt5.ORDER_TIME_GTC, "type_filling": self._filling(spec)}
            res = self.mt5.order_send(req)
            ok = res is not None and res.retcode == self.mt5.TRADE_RETCODE_DONE
            return {"ok": ok, "ticket": ticket, "profit": p["profit"], "error": None if ok else f"{getattr(res, 'comment', self.mt5.last_error())}"}

    def closed_profit(self, ticket: int) -> Optional[float]:
        deals = self.mt5.history_deals_get(position=ticket) or []
        if not deals:
            return None
        return float(sum(getattr(d, "profit", 0.0) + getattr(d, "commission", 0.0) + getattr(d, "swap", 0.0) for d in deals))


def make_broker(kind: str = "auto") -> Any:
    """'mt5' | 'paper' | 'auto' (tenta MT5, cai no paper)."""
    if kind == "paper":
        b = PaperBroker()
        b.connect()
        return b
    try:
        b = MT5Broker()
        b.connect()
        return b
    except BrokerError:
        if kind == "mt5":
            raise
        b = PaperBroker()
        b.connect()
        return b
