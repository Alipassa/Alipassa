"""MetaTrader 5 — fonte de candles reais do broker e executor com autorização explícita.

Requer o pacote `MetaTrader5` (Windows) e o terminal instalado:
    pip install MetaTrader5

`MT5Source` lê XAU/USD (M1…W1, tick volume, bid/ask) direto do terminal e completa o
resto do MarketSnapshot com o DataEngine (DXY, juros, FRED, COT, notícias).
`MT5Executor` NUNCA envia ordem sem `authorize=True` — por padrão apenas simula.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..models import Candle, Direction, MarketSnapshot, Signal, SignalType
from ..technical import atr as _atr

try:  # pragma: no cover - só existe no Windows com o terminal instalado
    import MetaTrader5 as _mt5  # type: ignore
except Exception:  # noqa: BLE001
    _mt5 = None

TF_TO_MT5 = {"M1": "TIMEFRAME_M1", "M5": "TIMEFRAME_M5", "M15": "TIMEFRAME_M15", "M30": "TIMEFRAME_M30",
             "H1": "TIMEFRAME_H1", "H4": "TIMEFRAME_H4", "D1": "TIMEFRAME_D1", "W1": "TIMEFRAME_W1"}
BARS = {"M1": 300, "M5": 300, "M15": 300, "M30": 300, "H1": 300, "H4": 300, "D1": 300, "W1": 160}


@dataclass
class MT5Config:
    path: Optional[str] = None            # ex.: r"C:\Program Files\MetaTrader 5\terminal64.exe"
    symbol: str = "XAUUSD"
    login: Optional[int] = None
    password: Optional[str] = None
    server: Optional[str] = None
    window_minutes: int = 60

    @classmethod
    def from_env(cls, env: Optional[dict[str, str]] = None) -> "MT5Config":
        e = {**(env or {}), **os.environ}
        login = e.get("MT5_LOGIN")
        return cls(path=e.get("MT5_PATH") or e.get("CAMINHO_MT5"), symbol=e.get("MT5_SYMBOL", "XAUUSD"),
                   login=int(login) if login else None, password=e.get("MT5_PASSWORD"), server=e.get("MT5_SERVER"))


class MT5Error(RuntimeError):
    pass


def rates_to_candles(rates: Any, server_offset_hours: float = 0.0) -> list[Candle]:
    """Converte o array de `copy_rates_from_pos` (time, open, high, low, close, tick_volume, spread, real_volume).
    O MT5 carimba no horário do SERVIDOR da corretora (Pepperstone: GMT+2/+3): `server_offset_hours` converte para UTC."""
    out: list[Candle] = []
    for r in rates or []:
        t = datetime.fromtimestamp(int(r["time"]) - int(server_offset_hours * 3600), tz=timezone.utc)
        vol = float(r["real_volume"]) if _has(r, "real_volume") else 0.0
        if vol <= 0:
            vol = float(r["tick_volume"]) if _has(r, "tick_volume") else 0.0
        out.append(Candle(t, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), vol))
    return out


def _has(row: Any, key: str) -> bool:
    try:
        names = row.dtype.names
        return key in names
    except AttributeError:
        try:
            return key in row
        except TypeError:
            return False


class MT5Client:
    """Wrapper fino sobre o módulo MetaTrader5 (injetável para testes)."""

    def __init__(self, cfg: MT5Config, mt5: Any = None) -> None:
        self.cfg = cfg
        self.mt5 = mt5 or _mt5
        if self.mt5 is None:
            raise MT5Error("pacote MetaTrader5 não disponível (instale no Windows: pip install MetaTrader5)")
        self.connected = False

    def connect(self) -> None:
        kwargs: dict[str, Any] = {}
        if self.cfg.path:
            kwargs["path"] = self.cfg.path
        if self.cfg.login:
            kwargs.update(login=self.cfg.login, password=self.cfg.password, server=self.cfg.server)
        if not self.mt5.initialize(**kwargs):
            err = self.mt5.last_error()
            code = err[0] if isinstance(err, (tuple, list)) and err else None
            hints = {
                -6: "Authorization failed: o terminal abriu mas não há conta autorizada. Abra o terminal da corretora, faça login na conta (demo ou real) e "
                    "deixe-o aberto; ou defina MT5_LOGIN, MT5_PASSWORD e MT5_SERVER no .env (ex.: MT5_SERVER=Pepperstone-Demo).",
                -10003: "IPC initialize failed: caminho do terminal64.exe incorreto em MT5_PATH ou terminal de outro usuário do Windows.",
                -10004: "IPC timeout: o terminal demorou a responder; abra-o manualmente e tente de novo.",
                -2: "Invalid params: confira MT5_PATH (use barras invertidas) e MT5_LOGIN numérico.",
            }
            raise MT5Error(f"initialize falhou: {err}. {hints.get(code, 'Confira MT5_PATH, se o terminal está aberto e logado, e se o pacote MetaTrader5 é da mesma arquitetura (64 bits) do Python.')}")
        if not self.mt5.symbol_select(self.cfg.symbol, True):
            raise MT5Error(f"símbolo {self.cfg.symbol} indisponível: {self.mt5.last_error()}")
        self.connected = True
        self.server_offset_hours = self._detect_server_offset()

    OFFSET_CACHE = os.environ.get("GOLD_AI_OFFSET_CACHE") or os.path.join(os.path.expanduser("~"), ".gold_ai_mt5_offset.json")

    def _detect_server_offset(self) -> float:
        """Fuso do servidor da corretora: compara o carimbo do último tick com o relógio UTC local. SÓ confia quando o tick é
        FRESCO (≤ 2 h) e o resíduo é pequeno — em fim de semana/feriado o último tick é velho e o cálculo sairia errado.
        Nesses casos usa o último valor detectado (cache) ou 0 com aviso. MT5_UTC_OFFSET_HOURS no .env força um valor."""
        forced = os.environ.get("MT5_UTC_OFFSET_HOURS") or getattr(self.cfg, "utc_offset_hours", None)
        self.offset_note = ""
        if forced not in (None, ""):
            self.offset_note = "forçado por MT5_UTC_OFFSET_HOURS"
            return float(forced)
        try:
            t = self.mt5.symbol_info_tick(self.cfg.symbol)
            ts = float(getattr(t, "time", 0) or 0)
        except Exception:  # noqa: BLE001
            ts = 0.0
        now = datetime.now(timezone.utc).timestamp()
        if not ts:                      # terminal sem tick nenhum (ou simulado): nada a inferir
            self.offset_note = "sem tick para inferir o fuso — 0 (defina MT5_UTC_OFFSET_HOURS se necessário)"
            return 0.0
        if ts:
            raw = (ts - now) / 3600.0
            off = float(round(raw))
            fresh = abs(raw - off) <= 0.25 and abs(off) <= 14 and abs(ts - now - off * 3600) <= 2 * 3600
            if fresh:
                try:
                    with open(self.OFFSET_CACHE, "w", encoding="utf-8") as f:
                        json.dump({"offset": off, "at": datetime.now(timezone.utc).isoformat()}, f)
                except OSError:
                    pass
                self.offset_note = "detectado pelo último tick"
                return off
        try:
            with open(self.OFFSET_CACHE, encoding="utf-8") as f:
                cached = json.load(f)
            self.offset_note = f"último tick antigo (mercado fechado?) — usando o fuso detectado em {cached.get('at', '')[:16]}"
            return float(cached["offset"])
        except (OSError, ValueError, KeyError):
            self.offset_note = "AVISO: sem tick recente e sem cache — fuso 0; defina MT5_UTC_OFFSET_HOURS no .env (Pepperstone: 2 no inverno, 3 no verão)"
            return 0.0

    def symbol_spec(self, symbol: Optional[str] = None):
        """Especificação de execução real do símbolo (symbol_info): dígitos, tick, valor do tick, passo de lote, stops level, filling."""
        from ..markets import SymbolSpec, default_symbol_spec
        sym = symbol or self.cfg.symbol
        base = default_symbol_spec(sym)
        try:
            info = self.mt5.symbol_info(sym)
        except Exception:  # noqa: BLE001
            info = None
        if info is None:
            return base
        g = lambda k, d: (getattr(info, k, None) if getattr(info, k, None) not in (None, 0, 0.0) else d)  # noqa: E731
        fm = int(getattr(info, "filling_mode", 0) or 0)
        filling = "FOK" if fm & 1 and not fm & 2 else "IOC" if fm & 2 else base.filling
        spread_pts = float(getattr(info, "spread", 0) or 0)
        point = float(g("point", base.point))
        spec = SymbolSpec(sym, int(g("digits", base.digits)), float(g("trade_tick_size", base.tick_size)), float(g("trade_tick_value", base.tick_value_usd)),
                          float(g("volume_min", base.volume_min)), float(g("volume_max", base.volume_max)), float(g("volume_step", base.volume_step)),
                          int(getattr(info, "trade_stops_level", 0) or 0), int(getattr(info, "trade_freeze_level", 0) or 0),
                          base.max_spread or max(2 * spread_pts * point, 2 * point), base.max_slippage or max(spread_pts * point, point), filling, "mt5")
        return spec

    def close(self) -> None:
        if self.connected:
            self.mt5.shutdown()
            self.connected = False

    def candles(self, tf: str, n: Optional[int] = None) -> list[Candle]:
        rates = self.mt5.copy_rates_from_pos(self.cfg.symbol, getattr(self.mt5, TF_TO_MT5[tf]), 0, n or BARS[tf])
        if rates is None:
            raise MT5Error(f"copy_rates_from_pos({tf}) falhou: {self.mt5.last_error()}")
        return rates_to_candles(rates, getattr(self, "server_offset_hours", 0.0))

    def rates_range(self, symbol: str, tf: str, start: datetime, end: datetime) -> list[Candle]:
        """Histórico por intervalo (copy_rates_range) — M1 costuma existir por anos na corretora."""
        if not self.mt5.symbol_select(symbol, True):
            raise MT5Error(f"símbolo {symbol} indisponível: {self.mt5.last_error()}")
        off = getattr(self, "server_offset_hours", 0.0)
        shift = timedelta(hours=off)
        rates = self.mt5.copy_rates_range(symbol, getattr(self.mt5, TF_TO_MT5[tf]), start + shift, end + shift)   # pedido em hora do servidor
        if rates is None:
            raise MT5Error(f"copy_rates_range({symbol},{tf}) falhou: {self.mt5.last_error()}")
        return rates_to_candles(rates, off)

    def ticks_range(self, symbol: str, start: datetime, end: datetime) -> list[tuple[datetime, float, float]]:
        """Ticks (time_msc, bid, ask) por intervalo (copy_ticks_range, COPY_TICKS_INFO) — a corretora guarda semanas/meses."""
        if not self.mt5.symbol_select(symbol, True):
            raise MT5Error(f"símbolo {symbol} indisponível: {self.mt5.last_error()}")
        flags = getattr(self.mt5, "COPY_TICKS_INFO", 1)
        off = getattr(self, "server_offset_hours", 0.0)
        shift = timedelta(hours=off)
        ticks = self.mt5.copy_ticks_range(symbol, start + shift, end + shift, flags)
        if ticks is None:
            raise MT5Error(f"copy_ticks_range({symbol}) falhou: {self.mt5.last_error()}")
        out = []
        last_bid = last_ask = None
        for r in ticks:
            ms = (int(r["time_msc"]) if _has(r, "time_msc") else int(r["time"]) * 1000) - int(off * 3600 * 1000)
            bid = float(r["bid"]) if _has(r, "bid") and r["bid"] else last_bid
            ask = float(r["ask"]) if _has(r, "ask") and r["ask"] else last_ask
            if bid is None or ask is None:
                continue
            last_bid, last_ask = bid, ask
            out.append((datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc), bid, ask))
        return out

    def tick(self) -> tuple[float, float]:
        t = self.mt5.symbol_info_tick(self.cfg.symbol)
        if t is None:
            raise MT5Error(f"tick indisponível: {self.mt5.last_error()}")
        return float(t.bid), float(t.ask)


class MT5Source:
    """DataSource: XAU do MT5 + demais camadas do DataEngine (opcional)."""

    def __init__(self, cfg: Optional[MT5Config] = None, data_engine: Any = None, mt5: Any = None) -> None:
        self.cfg = cfg or MT5Config.from_env()
        self.client = MT5Client(self.cfg, mt5)
        self.data_engine = data_engine
        self.status: dict[str, str] = {}

    def snapshot(self) -> MarketSnapshot:
        now = datetime.now(timezone.utc)
        s = self.data_engine.collect(now) if self.data_engine is not None else MarketSnapshot(time=now)
        if self.data_engine is not None:
            self.status.update(self.data_engine.status)
        try:
            if not self.client.connected:
                self.client.connect()
            candles = {tf: self.client.candles(tf) for tf in TF_TO_MT5}
            candles = {k: v for k, v in candles.items() if v}
            if not candles.get("H1"):
                raise MT5Error("sem candles H1")
            s.candles = candles  # o broker é a fonte primária do preço
            bid, ask = self.client.tick()
            s.price = (bid + ask) / 2
            s.atr = _atr(candles["H1"]) or s.atr
            m5 = candles.get("M5") or candles["H1"]
            w = self.cfg.window_minutes
            ref = next((c for c in reversed(m5[:-1]) if (m5[-1].time - c.time).total_seconds() >= w * 60), m5[0])
            s.price_change_pct = (m5[-1].close / ref.close - 1) * 100 if ref.close else 0.0
            recent = [c for c in m5 if (now - c.time).total_seconds() <= w * 60]
            vol = sum(c.volume for c in recent)
            if vol > 0:
                s.order_flow_imbalance = round((sum(c.volume for c in recent if c.close > c.open) - sum(c.volume for c in recent if c.close < c.open)) / vol, 3)
            self.status["mt5"] = "ok"
        except MT5Error as e:
            self.status["mt5"] = f"erro: {e}"
        return s


@dataclass
class OrderPlan:
    direction: Direction
    volume: float
    entry: float
    stop_loss: Optional[float]
    take_profit: Optional[float]
    comment: str
    authorized: bool = False
    result: Optional[dict] = None
    notes: list[str] = field(default_factory=list)

    def render(self) -> str:
        side = "COMPRA" if self.direction == Direction.ALTA else "VENDA"
        st = "ENVIADA" if self.result else ("AUTORIZADA — não enviada" if self.authorized else "SIMULADA (sem autorização)")
        return (f"ORDEM {side} {self.volume} lote(s) @ {self.entry:.2f} | SL {self.stop_loss} | TP {self.take_profit} | {st}"
                + (f" | {self.result}" if self.result else "") + ("".join(f"\n  • {n}" for n in self.notes)))


class MT5Executor:
    """Converte um Signal em plano de ordem. Só envia ao broker com `authorize=True`
    (por padrão apenas simula) e nunca para sinais WATCH/PRE-MOVE/REVERSAL/RISK."""

    EXECUTABLE = {SignalType.BUY, SignalType.STRONG_BUY, SignalType.SELL, SignalType.STRONG_SELL}

    def __init__(self, client: MT5Client, volume: float = 0.01, rr: float = 2.0, min_confidence: float = 70.0, min_level: int = 3) -> None:
        self.client = client
        self.volume, self.rr = volume, rr
        self.min_confidence, self.min_level = min_confidence, min_level

    def plan(self, sig: Signal) -> Optional[OrderPlan]:
        if sig.type not in self.EXECUTABLE or sig.direction == Direction.LATERAL:
            return None
        a = sig.assessment
        notes: list[str] = []
        if a.confidence < self.min_confidence:
            notes.append(f"confiança {a.confidence:.0f} < {self.min_confidence:.0f}")
        if int(a.evidence_level) < self.min_level:
            notes.append(f"evidência nível {int(a.evidence_level)} < {self.min_level}")
        if not a.has_edge:
            notes.append("sem vantagem estatística")
        inval = a.zone.get("invalidation")
        entry = a.price
        sl = inval
        tp = None
        if sl is not None:
            risk = abs(entry - sl)
            tp = entry + risk * self.rr if sig.direction == Direction.ALTA else entry - risk * self.rr
        plan = OrderPlan(sig.direction, self.volume, entry, sl, round(tp, 2) if tp else None, f"GoldAI {sig.type.value}", notes=notes)
        return plan

    def send_plan(self, plan, authorize: bool = False) -> OrderPlan:
        """Executa um trading.TradePlan (2.2): stop do Stop Engine, TP da estratégia recomendada, lote do gestor de risco."""
        from ..trading import STRATEGIES

        st = next((s for s in STRATEGIES if s.name == plan.recommended), None)
        tp = plan.price_at_r(st.target_r) if st and st.target_r else (plan.price_at_r(st.partial_r) if st and st.partial_r else None)
        order = OrderPlan(plan.direction, plan.lots or self.volume, plan.entry, round(plan.stop, 2), round(tp, 2) if tp else None,
                          f"GoldAI {plan.signal_type} {plan.recommended}", notes=[])
        if not plan.lots:
            order.notes.append("lote zero — gestor de risco")
        if plan.confidence < self.min_confidence:
            order.notes.append(f"confiança {plan.confidence:.0f} < {self.min_confidence:.0f}")
        if plan.evidence_level < self.min_level:
            order.notes.append(f"evidência nível {plan.evidence_level} < {self.min_level}")
        return self.execute(order, authorize=authorize)

    def execute(self, plan: OrderPlan, authorize: bool = False) -> OrderPlan:
        plan.authorized = authorize
        if not authorize or plan.notes:
            return plan  # simulação: nada é enviado
        mt5 = self.client.mt5
        bid, ask = self.client.tick()
        buy = plan.direction == Direction.ALTA
        req = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": self.client.cfg.symbol, "volume": plan.volume,
            "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL, "price": ask if buy else bid,
            "sl": plan.stop_loss or 0.0, "tp": plan.take_profit or 0.0, "deviation": 20, "magic": 20260914,
            "comment": plan.comment, "type_time": mt5.ORDER_TIME_GTC, "type_filling": mt5.ORDER_FILLING_IOC,
        }
        res = mt5.order_send(req)
        plan.result = {"retcode": getattr(res, "retcode", None), "order": getattr(res, "order", None), "comment": getattr(res, "comment", "")}
        return plan
