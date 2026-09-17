"""GOLD AI ENGINE 3.0 — EXECUTION ENGINE + BROKER CONFIRMATION.

REQUEST → MT5 → BROKER → TICKET → POSITION → PREÇO REAL → SL REAL → TP REAL.
Uma ordem só é considerada executada depois de confirmada no broker; qualquer divergência
entre o que foi pedido e o que foi aberto é ⚠️ EXECUTION MISMATCH.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .models import Direction
from .trading import TradePlan

MAGIC = 20260914


@dataclass
class ExecutionReport:
    requested_volume: float
    requested_sl: float
    requested_tp: Optional[float]
    requested_price: float
    retcode: Optional[int] = None
    order: Optional[int] = None
    deal: Optional[int] = None
    ticket: Optional[int] = None          # ticket da posição no broker
    fill_price: Optional[float] = None
    real_volume: Optional[float] = None
    real_sl: Optional[float] = None
    real_tp: Optional[float] = None
    slippage: Optional[float] = None
    confirmed: bool = False
    mismatches: list[str] = field(default_factory=list)
    corrected: bool = False
    error: str = ""
    time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def ok(self) -> bool:
        return self.confirmed and not self.mismatches

    def render(self) -> str:
        if self.error:
            return f"❌ EXECUÇÃO FALHOU: {self.error} (retcode {self.retcode})"
        lines = [f"{'✅ EXECUÇÃO CONFIRMADA' if self.ok else '⚠️ EXECUTION MISMATCH'} — ticket {self.ticket}",
                 f"pedido: {self.requested_volume} @ {self.requested_price:.2f} SL {self.requested_sl:.2f} TP {self.requested_tp}",
                 f"real:   {self.real_volume} @ {self.fill_price} SL {self.real_sl} TP {self.real_tp} · slippage {self.slippage}"]
        lines += [f"  • {m}" for m in self.mismatches]
        if self.corrected:
            lines.append("  • SL/TP corrigidos automaticamente no broker")
        return "\n".join(lines)


@dataclass
class BrokerPosition:
    ticket: int
    symbol: str
    direction: Direction
    volume: float
    price_open: float
    sl: float
    tp: float
    profit: float
    time: datetime


class ExecutionEngine:
    """Executa e confirma ordens no MT5 (o objeto `mt5` é injetável para testes)."""

    def __init__(self, client, price_tol: Optional[float] = None, sl_tol: Optional[float] = None, max_slippage: Optional[float] = None, deviation: int = 20) -> None:
        self.client = client
        self.mt5 = client.mt5
        self._price_tol, self._sl_tol, self._max_slippage, self.deviation = price_tol, sl_tol, max_slippage, deviation

    # ------------------------------------------------------------------ especificação do símbolo (symbol_info): dígitos, tick, lote, stops level, filling
    @property
    def spec(self):
        sp = getattr(self, "_spec", None)
        if sp is None or sp.symbol != self.client.cfg.symbol:
            from .markets import default_symbol_spec
            sp = self.client.symbol_spec() if hasattr(self.client, "symbol_spec") else default_symbol_spec(self.client.cfg.symbol)
            self._spec = sp
        return sp

    def digits(self, symbol: Optional[str] = None) -> int:
        return self.spec.digits

    def point(self, symbol: Optional[str] = None) -> float:
        return self.spec.point

    def rnd(self, x: Optional[float], symbol: Optional[str] = None) -> Optional[float]:
        return None if x is None else self.spec.round_price(x)

    def filling(self):
        mt5 = self.mt5
        name = {"FOK": "ORDER_FILLING_FOK", "RETURN": "ORDER_FILLING_RETURN"}.get(self.spec.filling, "ORDER_FILLING_IOC")
        return getattr(mt5, name, getattr(mt5, "ORDER_FILLING_IOC", 2))

    @property
    def price_tol(self) -> float:      # tolerâncias em unidades de preço do símbolo (padrão: spread máximo do ativo)
        return self._price_tol if self._price_tol is not None else max(self.spec.max_spread, 5 * self.spec.point)

    @property
    def sl_tol(self) -> float:
        return self._sl_tol if self._sl_tol is not None else max(self.spec.max_spread, 5 * self.spec.point)

    @property
    def max_slippage(self) -> float:
        return self._max_slippage if self._max_slippage is not None else max(self.spec.max_slippage, 3 * self.spec.point)

    # ------------------------------------------------------------------ leitura
    def positions(self, symbol: Optional[str] = None) -> list[BrokerPosition]:
        symbol = symbol or self.client.cfg.symbol
        raw = self.mt5.positions_get(symbol=symbol) or []
        out = []
        for p in raw:
            direction = Direction.ALTA if getattr(p, "type", 0) == getattr(self.mt5, "POSITION_TYPE_BUY", 0) else Direction.BAIXA
            out.append(BrokerPosition(int(p.ticket), p.symbol, direction, float(p.volume), float(p.price_open), float(p.sl or 0.0),
                                      float(p.tp or 0.0), float(getattr(p, "profit", 0.0)), datetime.fromtimestamp(int(p.time), tz=timezone.utc)))
        return out

    def position(self, ticket: int) -> Optional[BrokerPosition]:
        return next((p for p in self.positions() if p.ticket == ticket), None)

    def account_equity(self) -> Optional[float]:
        info = self.mt5.account_info()
        return float(info.equity) if info is not None else None

    # ------------------------------------------------------------------ envio + confirmação
    def open(self, plan: TradePlan, comment: str = "GoldAI") -> ExecutionReport:
        mt5 = self.mt5
        bid, ask = self.client.tick()
        buy = plan.direction == Direction.ALTA
        price = ask if buy else bid
        tp = plan.targets.get(plan.recommended) if plan.recommended in plan.targets else plan.targets.get("3R")
        spec = self.spec
        stop = plan.stop
        # distância mínima real: stops level da corretora OU o spread (o broker mede o SL da venda contra o ASK e o da compra contra o BID)
        min_dist = max(spec.min_stop_distance(), abs(ask - bid) + spec.point)
        # lado certo: compra → SL abaixo do bid e TP acima do ask; venda → SL acima do ask e TP abaixo do bid ('Invalid stops' 10016 se não)
        if (buy and stop >= bid) or ((not buy) and stop <= ask):
            rep = ExecutionReport(0.0, self.rnd(stop), self.rnd(tp) if tp else None, price)
            rep.error = (f"SL do lado errado do preço — não enviado: {'compra' if buy else 'venda'} a {price} com SL {stop} "
                         f"(bid {bid} / ask {ask}); plano inconsistente (entrada da zona ≠ preço de mercado?)")
            return rep
        if buy and abs(bid - stop) < min_dist:
            stop = bid - min_dist
        if (not buy) and abs(stop - ask) < min_dist:
            stop = ask + min_dist
        if tp is not None and ((buy and tp <= ask + min_dist) or ((not buy) and tp >= bid - min_dist)):
            tp = None                                            # alvo do lado errado/perto demais: entra sem TP (o gestor de posição cuida)
        vol = spec.normalize_volume(plan.lots or 0.0)
        rep = ExecutionReport(vol, self.rnd(stop), self.rnd(tp) if tp else None, price)
        if not vol:
            rep.error = f"lote {plan.lots} abaixo do mínimo {spec.volume_min} / passo {spec.volume_step}"
            return rep
        if abs(stop - plan.stop) > 1e-12:
            rep.mismatches.append(f"SL ajustado ao stops level/spread ({spec.stops_level_points} pts, spread {ask - bid:.{spec.digits}f}): {plan.stop} → {rep.requested_sl}")
        plan_tp = plan.targets.get(plan.recommended) if plan.recommended in plan.targets else plan.targets.get("3R")
        if plan_tp and tp is None:
            rep.mismatches.append(f"TP {plan_tp} do lado errado/perto demais do preço — enviado sem TP")
        req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": self.client.cfg.symbol, "volume": vol,
               "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL, "price": price, "sl": rep.requested_sl, "tp": rep.requested_tp or 0.0,
               "deviation": self.deviation, "magic": MAGIC, "comment": comment[:31], "type_time": mt5.ORDER_TIME_GTC, "type_filling": self.filling()}
        res = mt5.order_send(req)
        if res is None:
            rep.error = f"order_send devolveu None: {mt5.last_error()}"
            return rep
        rep.retcode, rep.order, rep.deal = getattr(res, "retcode", None), getattr(res, "order", None), getattr(res, "deal", None)
        if rep.retcode != getattr(mt5, "TRADE_RETCODE_DONE", 10009):
            rep.error = (f"broker recusou: {getattr(res, 'comment', '')} (retcode {rep.retcode}) · pedido: {'BUY' if buy else 'SELL'} {vol} @ {price} "
                         f"SL {rep.requested_sl} TP {rep.requested_tp or 0.0} · bid {bid} ask {ask} · stops level {spec.stops_level_points} pts · digits {spec.digits}")
            return rep
        return self.confirm(rep, plan)

    def confirm(self, rep: ExecutionReport, plan: TradePlan) -> ExecutionReport:
        """Confirma a POSIÇÃO no broker (não a requisição) e compara com o pedido."""
        pos = None
        for p in self.positions():
            if (rep.order and p.ticket == rep.order) or abs(p.volume - rep.requested_volume) < 1e-9 and p.direction == plan.direction:
                pos = p
                break
        if pos is None:
            rep.error = "posição não encontrada no broker após o envio"
            return rep
        rep.ticket, rep.fill_price, rep.real_volume, rep.real_sl, rep.real_tp = pos.ticket, pos.price_open, pos.volume, pos.sl, pos.tp
        rep.slippage = round(abs(pos.price_open - rep.requested_price), self.digits())
        rep.confirmed = True
        if abs(pos.volume - rep.requested_volume) > 1e-9:
            rep.mismatches.append(f"volume {pos.volume} ≠ pedido {rep.requested_volume}")
        if rep.slippage > self.max_slippage:
            rep.mismatches.append(f"slippage {rep.slippage} > máximo {self.max_slippage}")
        sl_bad = abs((pos.sl or 0.0) - rep.requested_sl) > self.sl_tol
        tp_bad = rep.requested_tp is not None and abs((pos.tp or 0.0) - rep.requested_tp) > self.sl_tol
        if sl_bad:
            rep.mismatches.append(f"SL real {pos.sl} ≠ pedido {rep.requested_sl}")
        if tp_bad:
            rep.mismatches.append(f"TP real {pos.tp} ≠ pedido {rep.requested_tp}")
        if sl_bad or tp_bad:
            if self.modify(pos.ticket, rep.requested_sl, rep.requested_tp):
                again = self.position(pos.ticket)
                if again and abs((again.sl or 0.0) - rep.requested_sl) <= self.sl_tol and (rep.requested_tp is None or abs((again.tp or 0.0) - rep.requested_tp) <= self.sl_tol):
                    rep.real_sl, rep.real_tp, rep.corrected = again.sl, again.tp, True
                    rep.mismatches = [m for m in rep.mismatches if not m.startswith(("SL real", "TP real"))]
        return rep

    # ------------------------------------------------------------------ gestão no broker
    def modify(self, ticket: int, sl: Optional[float], tp: Optional[float]) -> bool:
        mt5 = self.mt5
        req = {"action": mt5.TRADE_ACTION_SLTP, "position": ticket, "symbol": self.client.cfg.symbol, "sl": self.rnd(sl) if sl else 0.0, "tp": self.rnd(tp) if tp else 0.0}
        res = mt5.order_send(req)
        return res is not None and getattr(res, "retcode", None) == getattr(mt5, "TRADE_RETCODE_DONE", 10009)

    def close(self, ticket: int, volume: Optional[float] = None, comment: str = "GoldAI close") -> tuple[bool, Optional[float]]:
        """Fecha total ou parcialmente. Devolve (ok, preço de fechamento)."""
        mt5 = self.mt5
        pos = self.position(ticket)
        if pos is None:
            return False, None
        bid, ask = self.client.tick()
        buy = pos.direction == Direction.ALTA
        vol = round(min(volume or pos.volume, pos.volume), 2)
        req = {"action": mt5.TRADE_ACTION_DEAL, "position": ticket, "symbol": pos.symbol, "volume": vol,
               "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY, "price": bid if buy else ask, "deviation": self.deviation,
               "magic": MAGIC, "comment": comment[:31], "type_time": mt5.ORDER_TIME_GTC, "type_filling": self.filling()}
        res = mt5.order_send(req)
        ok = res is not None and getattr(res, "retcode", None) == getattr(mt5, "TRADE_RETCODE_DONE", 10009)
        return ok, (float(getattr(res, "price", 0.0)) or (bid if buy else ask)) if ok else None

    def closed_result(self, ticket: int) -> Optional[dict]:
        """Se a posição sumiu do broker (stop/TP), busca o resultado nos deals do histórico."""
        if self.position(ticket) is not None:
            return None
        deals = self.mt5.history_deals_get(position=ticket) or []
        if not deals:
            return None
        profit = sum(float(getattr(d, "profit", 0.0)) for d in deals)
        exits = [d for d in deals if getattr(d, "entry", 1) == getattr(self.mt5, "DEAL_ENTRY_OUT", 1)]
        price = float(exits[-1].price) if exits else None
        t = datetime.fromtimestamp(int(exits[-1].time), tz=timezone.utc) if exits else None
        return {"profit": profit, "price": price, "time": t, "deals": len(deals)}
