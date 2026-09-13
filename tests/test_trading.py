"""Testes 2.2 — TRADE SIMULATOR, STOP ENGINE, MAX PROFIT ENGINE, gestão de risco, NÃO OPERAR, modos."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from gold_ai import Direction, EngineConfig, GoldAIEngine, SignalType
from gold_ai.evaluation import Backtester, HistoryFrame, walk_forward
from gold_ai.memory import PredictionMemory
from gold_ai.models import Candle
from gold_ai.sources.sample import SampleSource, make_candles
from gold_ai.trading import (DEFAULT_STRATEGY, STRATEGIES, ExitStrategy, MaxProfitEngine, PositionManager, RiskLimits, RiskManager,
                             StopEngine, TradePlan, TradingMode, excursion_profile, no_trade_check, r_stats, simulate_all, simulate_trade)

NOW = datetime(2026, 9, 14, 13, 0, tzinfo=timezone.utc)


def path_candles(entry: float, deltas: list[float], step_min: int = 5, width: float = 0.5) -> list[Candle]:
    """Candles após a entrada; `deltas` = variação do fechamento por candle."""
    out, p, t = [], entry, NOW
    for d in deltas:
        t += timedelta(minutes=step_min)
        o, c = p, p + d
        out.append(Candle(t, o, max(o, c) + width, min(o, c) - width, c, 100))
        p = c
    return out


def plan_long(entry=2650.0, stop=2640.0) -> TradePlan:
    return TradePlan(Direction.ALTA, entry, stop, 9.0, NOW)


class SimulatorTests(unittest.TestCase):
    def test_reaches_3r_then_targets(self):
        cs = path_candles(2650, [3] * 12)  # sobe 36 → 3.6R
        prof = excursion_profile(plan_long(), cs, 240)
        self.assertTrue(prof.hit(3))
        self.assertFalse(prof.hit(4))
        self.assertFalse(prof.stopped)
        r = {st.name: simulate_trade(plan_long(), cs, st, 240) for st in STRATEGIES}
        self.assertEqual(r["1R"].r_multiple, 1.0)
        self.assertEqual(r["1R"].exit_reason, "TARGET")
        self.assertEqual(r["3R"].r_multiple, 3.0)
        self.assertEqual(r["4R"].exit_reason, "OPEN")
        self.assertGreater(r["trailing"].r_multiple, 1.0)

    def test_stop_first_is_conservative(self):
        # candle que toca stop e alvo ao mesmo tempo → vale o stop
        c = Candle(NOW + timedelta(minutes=5), 2650, 2665, 2639, 2660, 100)
        r = simulate_trade(plan_long(), [c], ExitStrategy("1R", target_r=1.0), 240)
        self.assertEqual(r.exit_reason, "STOP")
        self.assertEqual(r.r_multiple, -1.0)
        prof = excursion_profile(plan_long(), [c], 240)
        self.assertTrue(prof.stopped)
        self.assertEqual(prof.max_r_before_stop, 0.0)

    def test_stop_before_1r(self):
        cs = path_candles(2650, [-2, -3, -3, -3])
        prof = excursion_profile(plan_long(), cs, 240)
        self.assertTrue(prof.stopped)
        self.assertLess(prof.max_r_before_stop, 1.0)
        self.assertEqual(simulate_trade(plan_long(), cs, ExitStrategy("3R", target_r=3.0), 240).r_multiple, -1.0)

    def test_trailing_locks_profit(self):
        cs = path_candles(2650, [5] * 5 + [-4] * 6)  # vai a +2.5R e devolve
        r = simulate_trade(plan_long(), cs, ExitStrategy("trailing", trail_r=1.0, trail_activate_r=1.0), 240)
        self.assertEqual(r.exit_reason, "TRAIL")
        self.assertGreater(r.r_multiple, 0.5)
        fixed3 = simulate_trade(plan_long(), cs, ExitStrategy("3R", target_r=3.0), 240)
        self.assertLess(fixed3.r_multiple, r.r_multiple)  # 3R não foi atingido; trailing salvou lucro

    def test_partial_then_trailing(self):
        cs = path_candles(2650, [5] * 5 + [-6] * 6)
        r = simulate_trade(plan_long(), cs, next(s for s in STRATEGIES if s.name == "2R+trailing"), 240)
        self.assertGreaterEqual(r.r_multiple, 1.0)  # metade em 2R garante ≥ +1R
        self.assertEqual(r.exit_reason, "PARTIAL+TRAIL")

    def test_short_symmetry(self):
        plan = TradePlan(Direction.BAIXA, 2650.0, 2660.0, 9.0, NOW)
        cs = path_candles(2650, [-3] * 12)
        self.assertEqual(simulate_trade(plan, cs, ExitStrategy("3R", target_r=3.0), 240).r_multiple, 3.0)
        self.assertTrue(excursion_profile(plan, cs, 240).hit(3))

    def test_horizon_exit(self):
        cs = path_candles(2650, [0.5] * 5, step_min=60)
        r = simulate_trade(plan_long(), cs, ExitStrategy("4R", target_r=4.0), 180)
        self.assertEqual(r.exit_reason, "HORIZON")
        self.assertTrue(excursion_profile(plan_long(), cs, 180).horizon_reached)

    def test_r_stats_distribution_and_best(self):
        rows = []
        for deltas in ([3] * 12, [3] * 12, [-3] * 4, [2] * 6 + [-5] * 5, [1] * 4 + [-6] * 3, [4] * 12):
            cs = path_candles(2650, deltas)
            sim = simulate_all(plan_long(), cs, 240)
            rows.append({"type": "GOLD PRE-MOVE", "profile": sim["profile"], "results": sim["results"]})
        rs = r_stats(rows)
        self.assertEqual(rs.n, 6)
        self.assertEqual(sum(rs.dist.values()), 6)
        self.assertGreater(rs.reach["1R"], rs.reach["3R"])
        self.assertIsNotNone(rs.best)
        txt = rs.render()
        for key in ("TRADE SIMULATOR", "stop antes de 1R", "3R antes do stop", "Estratégias de saída", "◀ melhor", "GOLD PRE-MOVE"):
            self.assertIn(key, txt)
        self.assertEqual(r_stats([]).n, 0)


class StopAndTargetTests(unittest.TestCase):
    def test_stop_engine_bounds(self):
        a = GoldAIEngine().analyze(SampleSource("premove_alta").snapshot())
        se = StopEngine(RiskLimits(min_stop_atr=0.6, max_stop_atr=2.5))
        stop, why = se.compute(a, Direction.ALTA, 9.0)
        self.assertLess(stop, a.price)
        self.assertTrue(0.6 * 9 - 1e-6 <= a.price - stop <= 2.5 * 9 + 1e-6)
        a.zone["invalidation"] = a.price - 0.5   # invalidação muito próxima → outro candidato estrutural, nunca < 0.6 ATR
        stop, why = se.compute(a, Direction.ALTA, 9.0)
        self.assertGreaterEqual(a.price - stop, 0.6 * 9 - 1e-6)
        self.assertLessEqual(a.price - stop, 2.5 * 9 + 1e-6)
        self.assertNotIn("invalidação estrutural", why)
        a.zone = {"invalidation": a.price + 5, "support": None, "resistance": None}  # nada válido → volatilidade
        a.technical = []
        stop, why = se.compute(a, Direction.ALTA, 9.0)
        self.assertIn("1.2 ATR", why)
        cands = se.candidates(a, Direction.ALTA, 9.0)
        self.assertTrue(cands)

    def test_max_profit_engine_hypothesis_and_history(self):
        s = SampleSource("premove_alta").snapshot()
        a = GoldAIEngine().analyze(s)
        plan = MaxProfitEngine().plan(a, s, Direction.ALTA, "GOLD PRE-MOVE")
        self.assertEqual(plan.recommended, DEFAULT_STRATEGY)
        self.assertEqual(set(plan.targets), {"1R", "2R", "3R", "4R"})
        self.assertAlmostEqual(plan.targets["3R"], plan.entry + 3 * plan.r_value, places=2)
        self.assertIsNone(plan.probabilities["3R"])
        self.assertIn("hipótese", plan.recommended_reason)
        self.assertIn("TP ótimo = 3R", plan.render())
        # com histórico de 20+ operações, recomenda a melhor estratégia e estima probabilidades
        rows = []
        for i in range(24):
            cs = path_candles(2650, [3] * 12 if i % 3 else [-3] * 4)
            sim = simulate_all(plan_long(), cs, 240)
            rows.append({"type": "GOLD PRE-MOVE", "profile": sim["profile"], "results": sim["results"]})
        hist = r_stats(rows)
        plan2 = MaxProfitEngine(history=hist).plan(a, s, Direction.ALTA)
        self.assertEqual(plan2.recommended, hist.best)
        self.assertIsNotNone(plan2.probabilities["1R"])
        self.assertGreaterEqual(plan2.probabilities["1R"], plan2.probabilities["3R"])
        self.assertIn("melhor expectativa histórica", plan2.recommended_reason)


class RiskTests(unittest.TestCase):
    def test_sizing_from_risk_not_confidence(self):
        lim = RiskLimits(risk_per_trade_pct=0.5, max_lot=0.10, contract_size=100)
        rm = RiskManager(lim, equity=10000)
        plan = rm.size(plan_long(2650, 2640))  # 1R = 10 USD → 1000 USD/lote → risco 50 USD → 0.05 lote
        self.assertEqual(plan.lots, 0.05)
        self.assertEqual(plan.risk_usd, 50.0)
        big = rm.size(TradePlan(Direction.ALTA, 2650, 2649, 9.0, NOW))  # stop de 1 USD → 0.5 lote → capado em 0.10
        self.assertEqual(big.lots, 0.10)
        tiny = RiskManager(RiskLimits(risk_per_trade_pct=0.05), equity=1000).size(plan_long())
        self.assertEqual(tiny.lots, 0.0)

    def test_limits_block(self):
        rm = RiskManager(RiskLimits(max_positions=1, max_daily_loss_pct=2.0), equity=10000)
        plan = rm.size(plan_long())
        self.assertEqual(rm.check(plan), [])
        rm.register(plan)
        self.assertTrue(any("posições" in r for r in rm.check(plan)))
        rm.close(plan, -1.0, NOW)
        rm.daily_pnl = -250
        self.assertTrue(any("perda diária" in r for r in rm.check(plan)))
        rm._roll_day(NOW + timedelta(days=1))
        self.assertEqual(rm.daily_pnl, 0.0)

    def test_from_env(self):
        lim = RiskLimits.from_env({"RISK_PER_TRADE": "0.25", "MAX_POSITIONS": "2", "MAX_LOT": "0.5"})
        self.assertEqual((lim.risk_per_trade_pct, lim.max_positions, lim.max_lot), (0.25, 2, 0.5))


class NoTradeAndModesTests(unittest.TestCase):
    def test_no_trade_when_conflicting(self):
        a = GoldAIEngine().analyze(SampleSource("neutro").snapshot())
        reasons = no_trade_check(a, RiskLimits())
        self.assertTrue(reasons)
        self.assertIn("sem vantagem estatística", reasons)
        good = GoldAIEngine().analyze(SampleSource("venda").snapshot())
        self.assertEqual(no_trade_check(good, RiskLimits(), spread=0.3), [])
        self.assertTrue(any("spread" in r for r in no_trade_check(good, RiskLimits(max_spread=0.2), spread=0.3)))

    def test_paper_mode_records_and_authorize_waits(self):
        s = SampleSource("venda").snapshot()
        eng = GoldAIEngine()
        _, sig = eng.run_cycle(s)
        pm = PositionManager(TradingMode.PAPER, RiskLimits(), 10000)
        d = pm.decide(sig, s)
        self.assertEqual(d.action, "PAPER")
        self.assertEqual(d.plan.direction, Direction.BAIXA)
        self.assertGreater(d.plan.lots, 0)
        self.assertIn("PAPER", d.render())
        # segunda operação bloqueada por MAX_POSITIONS=1
        d2 = pm.decide(sig, s)
        self.assertEqual(d2.action, "BLOCKED")
        pm2 = PositionManager(TradingMode.AUTHORIZE, RiskLimits(), 10000)
        d3 = pm2.decide(sig, s)
        self.assertEqual(d3.action, "AWAIT_AUTHORIZATION")
        pm3 = PositionManager(TradingMode.LIVE, RiskLimits(), 10000, executor=None)
        self.assertEqual(pm3.decide(sig, s).action, "BLOCKED")  # sem executor, nunca envia

    def test_live_mode_sends_via_executor(self):
        from tests.test_mt5 import FakeMT5
        from gold_ai.data.mt5 import MT5Client, MT5Config, MT5Executor

        fake = FakeMT5()
        ex = MT5Executor(MT5Client(MT5Config(), mt5=fake), min_confidence=0, min_level=0)
        s = SampleSource("venda").snapshot()
        _, sig = GoldAIEngine().run_cycle(s)
        pm = PositionManager(TradingMode.LIVE, RiskLimits(), 10000, executor=ex)
        d = pm.decide(sig, s)
        self.assertEqual(d.action, "SENT")
        self.assertEqual(len(fake.sent), 1)
        self.assertEqual(fake.sent[0]["volume"], d.plan.lots)
        self.assertAlmostEqual(fake.sent[0]["sl"], round(d.plan.stop, 2))
        self.assertAlmostEqual(fake.sent[0]["tp"], round(d.plan.targets["3R"], 2))

    def test_watch_signal_is_not_operational(self):
        cfg = EngineConfig(); cfg.buy, cfg.strong_buy, cfg.premove_fundamental_threshold = 90, 95, 200
        s = SampleSource("confirmacao_alta").snapshot()
        _, sig = GoldAIEngine(cfg).run_cycle(s)
        self.assertEqual(sig.type, SignalType.WATCH)
        self.assertEqual(PositionManager(TradingMode.PAPER, RiskLimits(), 10000).decide(sig, s).action, "NO_TRADE")


class MemoryTradesTests(unittest.TestCase):
    def test_open_resolve_and_r_stats(self):
        s = SampleSource("venda").snapshot()
        _, sig = GoldAIEngine().run_cycle(s)
        pm = PositionManager(TradingMode.PAPER, RiskLimits(), 10000)
        d = pm.decide(sig, s)
        with tempfile.TemporaryDirectory() as dd:
            mem = PredictionMemory(os.path.join(dd, "t.db"))
            tid = mem.open_trade(d.plan, "PAPER", None, 240)
            self.assertEqual(len(mem.open_trades()), 1)
            self.assertEqual(mem.auto_resolve_trades([], s.time), [])
            R = d.plan.r_value
            cs = [Candle(s.time + timedelta(minutes=5 * i), s.price, s.price + 0.2, s.price - 0.35 * R * i, s.price - 0.35 * R * i, 10) for i in range(1, 12)]
            done = mem.auto_resolve_trades(cs, s.time + timedelta(minutes=300))  # horizonte expirado
            self.assertEqual(len(done), 1)
            self.assertTrue(done[0][1]["profile"].hit(3))
            self.assertEqual(mem.open_trades(), [])
            rs = mem.r_stats()
            self.assertEqual(rs.n, 1)
            self.assertEqual(rs.reach["3R"], 1.0)
            self.assertIn("3R", rs.render())
            mem.close()


class BacktestTradesTests(unittest.TestCase):
    def test_backtest_includes_r_stats(self):
        f = HistoryFrame(xau=make_candles("H1", 900, 2500, 0.4, 6.0, NOW, seed=3),
                         dxy=make_candles("H1", 900, 104, -0.002, 0.08, NOW, seed=4),
                         us10y=make_candles("H1", 900, 4.2, -0.0005, 0.02, NOW, seed=5))
        bt = Backtester(f, warmup=230, step=4)
        r = bt.run()
        self.assertIsNotNone(r.trades)
        self.assertEqual(r.trades.n, len(r.trade_rows))
        wf = walk_forward(bt, n_folds=2, grid=[{"buy": 40, "sell": -40, "min_confirmations": 2}])
        self.assertIn("WALK-FORWARD", wf.render())


if __name__ == "__main__":
    unittest.main()
