"""Testes 3.0 — Execution Engine, confirmação no broker, Risk Guard, Performance, Kill Switch, ciclo completo."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from gold_ai import Direction, GoldAIEngine, SignalType
from gold_ai.data.mt5 import MT5Client, MT5Config
from gold_ai.execution import ExecutionEngine
from gold_ai.guard import GuardLimits, KillSwitch, PerformanceEngine, TelegramCommands, TradingMode, size_lots
from gold_ai.live_engine import LiveExecutionEngine
from gold_ai.memory import PredictionMemory
from gold_ai.models import Candle
from gold_ai.monitor import adaptive_trail_r
from gold_ai.sources.sample import SampleSource
from gold_ai.trading import TradePlan
from tests.test_mt5 import FakeMT5

NOW = datetime(2026, 9, 14, 13, 0, tzinfo=timezone.utc)


class BrokerSim(FakeMT5):
    """MT5 simulado com posições reais: abre, modifica SL/TP, fecha (parcial), histórico de deals."""

    TRADE_ACTION_SLTP, TRADE_RETCODE_DONE, POSITION_TYPE_BUY, DEAL_ENTRY_OUT = 6, 10009, 0, 1

    def __init__(self, bid=2649.8, ask=2650.2, sl_offset=0.0, equity=10000.0, reject=False):
        super().__init__()
        self.bid, self.ask = bid, ask
        self.sl_offset = sl_offset       # simula broker abrindo com SL diferente do pedido
        self.equity = equity
        self.reject = reject
        self.positions: dict[int, SimpleNamespace] = {}
        self.deals: dict[int, list] = {}
        self.next_ticket = 500

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(bid=self.bid, ask=self.ask)

    def account_info(self):
        return SimpleNamespace(equity=self.equity)

    def positions_get(self, symbol=None, ticket=None):
        return [p for p in self.positions.values() if (ticket is None or p.ticket == ticket)]

    def history_deals_get(self, position=None):
        return self.deals.get(position, [])

    def order_send(self, req):
        self.sent.append(req)
        if self.reject:
            return SimpleNamespace(retcode=10013, comment="rejected", order=None, deal=None)
        if req["action"] == self.TRADE_ACTION_SLTP:
            p = self.positions.get(req["position"])
            if p is None:
                return SimpleNamespace(retcode=10013, comment="no position")
            p.sl, p.tp = req["sl"], req["tp"]
            return SimpleNamespace(retcode=10009, comment="done")
        if req.get("position"):  # fechamento (parcial ou total)
            p = self.positions[req["position"]]
            vol = req["volume"]
            price = self.bid if p.type == self.POSITION_TYPE_BUY else self.ask
            pnl = (price - p.price_open) * vol * 100 * (1 if p.type == self.POSITION_TYPE_BUY else -1)
            self.deals.setdefault(p.ticket, []).append(SimpleNamespace(entry=1, price=price, profit=pnl, time=int(NOW.timestamp()) + 600))
            p.volume = round(p.volume - vol, 2)
            if p.volume <= 1e-9:
                del self.positions[p.ticket]
            self.equity += pnl
            return SimpleNamespace(retcode=10009, comment="done", price=price, order=p.ticket, deal=1)
        ticket = self.next_ticket
        self.next_ticket += 1
        buy = req["type"] == self.ORDER_TYPE_BUY
        self.positions[ticket] = SimpleNamespace(ticket=ticket, symbol=req["symbol"], type=0 if buy else 1, volume=req["volume"],
                                                 price_open=self.ask if buy else self.bid, sl=req["sl"] + self.sl_offset, tp=req["tp"],
                                                 profit=0.0, time=int(NOW.timestamp()))
        return SimpleNamespace(retcode=10009, comment="done", order=ticket, deal=ticket)

    def hit_stop(self, ticket):
        """Simula o broker fechando a posição no stop."""
        p = self.positions.pop(ticket)
        pnl = (p.sl - p.price_open) * p.volume * 100 * (1 if p.type == 0 else -1)
        self.deals.setdefault(ticket, []).append(SimpleNamespace(entry=1, price=p.sl, profit=pnl, time=int(NOW.timestamp()) + 1800))
        self.equity += pnl


def make_engine(mode, broker=None, equity=10000.0, db=None, limits=None, **kw):
    mem = PredictionMemory(db or ":memory:")
    executor = ExecutionEngine(MT5Client(MT5Config(), mt5=broker)) if broker is not None else None
    from gold_ai.telegram import TelegramSender
    return LiveExecutionEngine(mem, limits or GuardLimits(), mode, equity, executor, TelegramSender(dry_run=True, quiet=True), log=lambda s: None, **kw), mem


def plan_long(entry=2650.2, stop=2641.8, lots=0.05):
    p = TradePlan(Direction.ALTA, entry, stop, 9.0, NOW, targets={"1R": 2658.6, "2R": 2667.0, "3R": 2675.4, "4R": 2683.8}, recommended="3R", lots=lots, risk_usd=42.0)
    return p


class ExecutionTests(unittest.TestCase):
    def test_open_and_confirm(self):
        b = BrokerSim()
        ex = ExecutionEngine(MT5Client(MT5Config(), mt5=b))
        rep = ex.open(plan_long())
        self.assertTrue(rep.confirmed)
        self.assertTrue(rep.ok)
        self.assertEqual(rep.real_volume, 0.05)
        self.assertEqual(rep.fill_price, 2650.2)
        self.assertEqual(rep.real_sl, 2641.8)
        self.assertEqual(rep.real_tp, 2675.4)
        self.assertIn("EXECUÇÃO CONFIRMADA", rep.render())
        self.assertEqual(len(ex.positions()), 1)

    def test_mismatch_detected_and_corrected(self):
        b = BrokerSim(sl_offset=-5.0)   # broker abre com SL 5 USD abaixo do pedido
        ex = ExecutionEngine(MT5Client(MT5Config(), mt5=b))
        rep = ex.open(plan_long())
        self.assertTrue(rep.confirmed)
        self.assertTrue(rep.corrected)      # SL corrigido via TRADE_ACTION_SLTP
        self.assertEqual(rep.real_sl, 2641.8)
        self.assertTrue(rep.ok)

    def test_rejection_and_slippage(self):
        ex = ExecutionEngine(MT5Client(MT5Config(), mt5=BrokerSim(reject=True)))
        rep = ex.open(plan_long())
        self.assertFalse(rep.confirmed)
        self.assertIn("recusou", rep.error)
        b = BrokerSim(ask=2650.2)
        ex2 = ExecutionEngine(MT5Client(MT5Config(), mt5=b), max_slippage=0.1)
        b.ask = 2650.9  # preço muda entre o tick e o fill
        orig_tick = b.symbol_info_tick
        b.symbol_info_tick = lambda s: SimpleNamespace(bid=2649.8, ask=2650.2)
        rep2 = ex2.open(plan_long())
        self.assertTrue(rep2.confirmed)
        self.assertTrue(any("slippage" in m for m in rep2.mismatches))

    def test_close_partial_and_history(self):
        b = BrokerSim()
        ex = ExecutionEngine(MT5Client(MT5Config(), mt5=b))
        rep = ex.open(plan_long(lots=0.10))
        ok, price = ex.close(rep.ticket, 0.05)
        self.assertTrue(ok)
        self.assertEqual(ex.position(rep.ticket).volume, 0.05)
        self.assertIsNone(ex.closed_result(rep.ticket))
        b.hit_stop(rep.ticket)
        res = ex.closed_result(rep.ticket)
        self.assertIsNotNone(res)
        self.assertLess(res["profit"], 0)
        self.assertEqual(res["price"], 2641.8)


class GuardTests(unittest.TestCase):
    def test_sizing_grows_with_capital_not_with_confidence(self):
        lim = GuardLimits(risk_per_trade_pct=0.5, max_lot=1.0)
        for equity, expected in ((1000, 0.0), (2000, 0.01), (5000, 0.02), (10000, 0.05)):
            perf = PerformanceEngine(lim, equity)
            lots, risk = size_lots(lim, perf.risk_usd, 10.0)
            self.assertEqual(lots, expected, equity)
        self.assertEqual(size_lots(lim, 50.0, 0.0), (0.0, 0.0))

    def test_daily_loss_trading_stop_and_drawdown(self):
        lim = GuardLimits(risk_per_trade_pct=0.5, max_daily_loss_pct=2.0, max_drawdown_pct=10.0)
        perf = PerformanceEngine(lim, 10000)
        for _ in range(4):
            perf.record_result(-40, NOW)
        self.assertEqual(perf.blocks(NOW), [])
        perf.record_result(-40, NOW)   # -200 → 2 %
        self.assertTrue(perf.trading_stop)
        self.assertTrue(any("TRADING STOP" in b for b in perf.blocks(NOW)))
        self.assertEqual(perf.risk_usd, round(perf.equity * 0.005, 2))  # risco continua 0,5 % do capital — nunca sobe para recuperar
        perf.roll_day(NOW + timedelta(days=1))
        self.assertFalse(perf.trading_stop)
        perf.record_result(-900, NOW + timedelta(days=1))
        self.assertTrue(any("drawdown" in b for b in perf.blocks(NOW + timedelta(days=1))))

    def test_kill_switch_and_commands(self):
        ks = KillSwitch.from_env({"TRADING_ENABLED": "false"})
        self.assertFalse(ks.new_entries_allowed()[0])
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "STOP")
            ks = KillSwitch.from_env({}, file_path=path)
            self.assertTrue(ks.new_entries_allowed()[0])
            open(path, "w").close()
            self.assertIn("kill switch", ks.new_entries_allowed()[1])
        ks = KillSwitch()
        cmds = TelegramCommands(None, None)
        self.assertEqual(cmds.apply(["/STOP"], ks), ["STOP"])
        self.assertFalse(ks.new_entries_allowed()[0])
        self.assertEqual(cmds.apply(["/RESUME", "/STATUS", "/FLOW"], ks), ["RESUME", "STATUS", "FLOW"])
        self.assertTrue(ks.new_entries_allowed()[0])
        self.assertEqual(cmds.apply(["/CLOSE"], ks), ["CLOSE_REQUESTED"])
        self.assertEqual(cmds.apply(["/CLOSE CONFIRM"], ks), ["CLOSE_CONFIRMED"])
        self.assertEqual(cmds.poll(), [])  # sem token → inativo

    def test_adaptive_trailing(self):
        self.assertEqual(adaptive_trail_r(90, 95), 1.5)
        self.assertEqual(adaptive_trail_r(50, 70), 1.0)
        self.assertEqual(adaptive_trail_r(10, 30), 0.75)
        self.assertEqual(adaptive_trail_r(0, 20), 0.5)


class LiveCycleTests(unittest.TestCase):
    def test_paper_cycle_opens_monitors_and_closes(self):
        eng, mem = make_engine(TradingMode.PAPER)
        src = SampleSource("venda")
        res = eng.run_cycle(src.snapshot())
        self.assertIsNotNone(res.signal)
        self.assertTrue(res.decision.startswith("🟢 PAPER OPEN"))
        self.assertEqual(len(eng.managed), 1)
        self.assertTrue(any("MARKET AI" in m and "SELL XAUUSD" in m for m in res.messages))
        tr = eng.managed[0]
        self.assertGreater(tr.plan.lots, 0)
        # próximo ciclo: cenário virou → tese invalidada → encerra, capital atualizado
        src.scenario, src.now, src.price = "premove_alta", src.now + timedelta(minutes=30), src.price - 6
        res2 = eng.run_cycle(src.snapshot())
        self.assertEqual(tr.status, "CLOSED")
        self.assertNotIn(tr, eng.managed)
        self.assertTrue(any("TRADE ENCERRADO" in m for m in res2.messages))
        self.assertTrue(any("CENÁRIO ALTERADO" in m for m in res2.messages))
        self.assertNotEqual(eng.perf.equity, 10000.0)
        self.assertGreaterEqual(len(mem.equity_curve()), 2)
        self.assertIn("PERFORMANCE", mem.performance_summary())

    def test_one_position_per_symbol_and_kill_switch(self):
        eng, mem = make_engine(TradingMode.PAPER)
        src = SampleSource("venda")
        eng.run_cycle(src.snapshot())
        src.now += timedelta(minutes=20)
        res = eng.run_cycle(src.snapshot())
        self.assertEqual(len(eng.managed), 1)
        eng2, _ = make_engine(TradingMode.PAPER, kill_switch=KillSwitch(enabled_env=False))
        r = eng2.run_cycle(SampleSource("venda").snapshot())
        self.assertIn("TRADING_ENABLED=false", r.decision)
        self.assertEqual(eng2.managed, [])

    def test_authorize_waits_then_executes(self):
        b = BrokerSim()
        eng, mem = make_engine(TradingMode.AUTHORIZE, broker=b)
        r = eng.run_cycle(SampleSource("venda").snapshot())
        self.assertEqual(r.decision, "AGUARDANDO AUTORIZAÇÃO")
        self.assertEqual(b.positions, {})
        eng.authorized = True
        eng.engine.gate.last_score = None  # força novo sinal
        src = SampleSource("venda", now=NOW + timedelta(minutes=30))
        r2 = eng.run_cycle(src.snapshot())
        self.assertTrue(r2.decision.startswith("🟢 POSITION OPEN"), r2.decision)
        self.assertEqual(len(b.positions), 1)
        self.assertFalse(eng.authorized)  # autorização vale para uma entrada

    def test_live_full_cycle_with_broker(self):
        b = BrokerSim(equity=10000.0)
        eng, mem = make_engine(TradingMode.LIVE, broker=b, authorized=True)
        src = SampleSource("venda")
        r = eng.run_cycle(src.snapshot())
        self.assertTrue(r.decision.startswith("🟢 POSITION OPEN"), r.decision)
        ticket = list(b.positions)[0]
        tr = eng.managed[0]
        row = mem.conn.execute("SELECT ticket, preco_execucao, sl_real, capital, risco_pct FROM trades WHERE id=?", (tr.trade_id,)).fetchone()
        self.assertEqual(row["ticket"], ticket)
        self.assertEqual(row["capital"], 10000.0)
        self.assertEqual(row["risco_pct"], 0.5)
        self.assertTrue(any("EXECUÇÃO CONFIRMADA" in m for m in r.messages))
        # broker fecha no stop → monitor detecta, resultado financeiro real, capital sincronizado
        b.hit_stop(ticket)
        src.now += timedelta(minutes=30)
        r2 = eng.run_cycle(src.snapshot())
        self.assertEqual(eng.managed, [])
        self.assertEqual(tr.close_reason, "BROKER")
        fin = mem.conn.execute("SELECT resultado_financeiro FROM trades WHERE id=?", (tr.trade_id,)).fetchone()["resultado_financeiro"]
        self.assertLess(fin, 0)
        self.assertTrue(any("TRADE ENCERRADO" in m for m in r2.messages))

    def test_live_monitor_actions_reach_broker(self):
        b = BrokerSim()
        eng, mem = make_engine(TradingMode.LIVE, broker=b, authorized=True)
        src = SampleSource("premove_alta")
        r = eng.run_cycle(src.snapshot())
        self.assertTrue(r.decision.startswith("🟢 POSITION OPEN"), r.decision)
        ticket = list(b.positions)[0]
        tr = eng.managed[0]
        # preço vai a +2.2R com cenário forte → PROTEGER: parcial no broker + SL no zero a zero
        R = tr.plan.r_value
        up = tr.plan.entry + 2.2 * R
        b.bid, b.ask = up - 0.2, up + 0.2
        src.scenario, src.now, src.price = "confirmacao_alta", src.now + timedelta(minutes=30), up
        snap = src.snapshot()
        snap.candles["M1"] = [Candle(snap.time - timedelta(minutes=2), up - 1, up, up - 1.5, up, 10)]
        r2 = eng.run_cycle(snap)
        acts = [rd.action for _, rd in r2.readings]
        self.assertIn("PROTEGER", acts)
        pos = b.positions[ticket]
        self.assertLess(pos.volume, tr.plan.lots)                 # parcial executada
        self.assertGreaterEqual(pos.sl, tr.plan.entry - 0.01)    # pelo menos zero a zero no broker (trailing pode estar acima)
        self.assertTrue(any("🛡️" in m for m in r2.messages))
        # cenário vira → ENCERRAR no broker
        src.scenario, src.now = "venda", src.now + timedelta(minutes=30)
        snap = src.snapshot()
        r3 = eng.run_cycle(snap)
        self.assertNotIn(ticket, b.positions)
        self.assertNotIn(tr, eng.managed)

    def test_semi_live_asks_confirmation_for_profitable_close(self):
        b = BrokerSim()
        eng, mem = make_engine(TradingMode.SEMI_LIVE, broker=b)
        src = SampleSource("premove_alta")
        eng.run_cycle(src.snapshot())
        self.assertEqual(len(b.positions), 1)
        tr = eng.managed[0]
        up = tr.plan.entry + 1.2 * tr.plan.r_value
        b.bid, b.ask = up - 0.2, up + 0.2
        src.scenario, src.now, src.price = "venda", src.now + timedelta(minutes=30), up
        r = eng.run_cycle(src.snapshot())
        self.assertEqual(len(b.positions), 1)                     # não fechou sozinho
        self.assertTrue(any("/CLOSE CONFIRM" in m for m in r.messages))
        self.assertIn(tr.trade_id, eng.pending_close_confirm)
        self.assertEqual(tr.status, "OPEN")
        self.assertEqual(tr.remaining, 1.0)      # estado preservado enquanto aguarda confirmação
        self.assertIsNone(tr.result_r)
        self.assertAlmostEqual(list(b.positions.values())[0].sl, tr.plan.entry, places=2)  # protegido no zero a zero

    def test_status_and_resume_from_db(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "t.db")
            eng, mem = make_engine(TradingMode.PAPER, db=db)
            eng.run_cycle(SampleSource("venda").snapshot())
            self.assertIn("Posições sob monitor: 1", eng.status_text())
            mem.close()
            eng2, mem2 = make_engine(TradingMode.PAPER, db=db)
            self.assertEqual(len(eng2.managed), 1)               # retomada
            self.assertEqual(eng2.perf.equity, 10000.0)


if __name__ == "__main__":
    unittest.main()


class DailyTargetTests(unittest.TestCase):
    def test_target_is_a_lock_not_an_obligation(self):
        from datetime import datetime, timezone
        from gold_ai.guard import GuardLimits, PerformanceEngine
        lim = GuardLimits.from_env({"RISK_PER_TRADE": "3", "MAX_DAILY_LOSS": "6", "DAILY_TARGET": "10", "MAX_LOT": "1.0"})
        self.assertEqual((lim.risk_per_trade_pct, lim.daily_target_pct, lim.max_daily_loss_pct), (3.0, 10.0, 6.0))
        p = PerformanceEngine(lim, 10000.0)
        t = datetime(2026, 9, 14, 13, 0, tzinfo=timezone.utc)
        p.roll_day(t)
        self.assertEqual(p.risk_usd, 300.0)                      # 3% de 10 000
        self.assertEqual(p.daily_target_usd, 1000.0)
        self.assertAlmostEqual(p.r_to_target(), 3.33, places=2)  # +3,33R líquidos para +10%
        self.assertEqual(p.blocks(t), [])                        # meta longe: nada bloqueia, nada obriga
        p.record_result(600.0, t, "t1")                          # +2R → risco passa a 3% de 10 600 (compounding, nunca por perda)
        self.assertEqual(p.risk_usd, 318.0)
        self.assertFalse(p.target_reached)
        p.record_result(-318.0, t, "t2")                         # −1R: risco cai junto com o capital; nunca sobe para recuperar
        self.assertEqual(p.risk_usd, round(10282.0 * 0.03, 2))
        p.record_result(750.0, t, "t3")                          # dia +1 032 ≥ 1 000 → META
        self.assertTrue(p.target_reached)
        self.assertTrue(any("META DIÁRIA ATINGIDA" in b for b in p.blocks(t)))
        self.assertFalse(p.trading_stop)                         # meta ≠ stop por perda
        self.assertIn("META ATINGIDA", p.render())
        p.roll_day(datetime(2026, 9, 15, 0, 5, tzinfo=timezone.utc))
        self.assertFalse(p.target_reached)                       # dia novo, trava liberada
        self.assertEqual(p.blocks(datetime(2026, 9, 15, 0, 5, tzinfo=timezone.utc)), [])
        # perda diária 6% = duas perdas cheias
        q = PerformanceEngine(lim, 10000.0)
        q.record_result(-300.0, t); q.record_result(-300.0, t)
        self.assertTrue(q.trading_stop)


class BrokerSyncBaselineTests(unittest.TestCase):
    def test_first_sync_is_baseline_not_daily_result(self):
        from datetime import datetime, timezone
        from gold_ai.guard import GuardLimits, PerformanceEngine
        perf = PerformanceEngine(GuardLimits(risk_per_trade_pct=3.0, daily_target_pct=10.0), 10000.0)
        now = datetime(2026, 9, 15, 10, 0, tzinfo=timezone.utc)
        perf.sync_equity(50000.0, now)
        self.assertEqual(perf.equity, 50000.0)
        self.assertEqual(perf.daily_pnl, 0.0)
        self.assertFalse(perf.target_reached)
        self.assertEqual(perf.blocks(now), [])
        self.assertAlmostEqual(perf.risk_usd, 1500.0)
        perf.sync_equity(50250.0, now)          # a partir da segunda leitura, a variação é resultado do dia
        self.assertEqual(perf.daily_pnl, 250.0)


class RestoreBaselineTests(unittest.TestCase):
    def test_legacy_baseline_sync_does_not_lock_target(self):
        from datetime import datetime, timezone
        from gold_ai.guard import GuardLimits, PerformanceEngine
        from gold_ai.memory import PredictionMemory
        mem = PredictionMemory(":memory:")
        now = datetime(2026, 9, 15, 10, 45, tzinfo=timezone.utc)
        mem.record_equity(now, 50000.0, 40000.0, "sync broker")          # versão antiga: diferença 10 000 → 50 000 gravada como lucro
        mem.record_equity(now, 50120.0, 120.0, "sync broker")            # resultado real de operação
        self.assertEqual(mem.neutralize_baseline_syncs(), 1)
        perf = PerformanceEngine(GuardLimits(daily_target_pct=10.0), 50000.0)
        perf.restore(mem.account_rows(), now)
        self.assertEqual(perf.daily_pnl, 120.0)
        self.assertFalse(perf.target_reached)
        mem.close()
