"""Testes — OPPORTUNITY ENGINE: capture rate, entry rate, overfilter, atribuição por filtro, curva de limiar."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from gold_ai import GoldAIEngine
from gold_ai.evaluation import Backtester, HistoryFrame, validate
from gold_ai.guard import GuardLimits, KillSwitch, TradingMode
from gold_ai.live_engine import LiveExecutionEngine
from gold_ai.memory import PredictionMemory
from gold_ai.models import Candle
from gold_ai.opportunity import DecisionRecord, classify_reason, hypothetical_trade, opportunity_report, threshold_curve
from gold_ai.sources.sample import SampleSource, make_candles
from gold_ai.telegram import TelegramSender

NOW = datetime(2026, 9, 14, 13, 0, tzinfo=timezone.utc)


def path(moves):
    out, p, t = [], 2650.0, NOW
    for mins, d in moves:
        for _ in range(mins):
            t += timedelta(minutes=1); p += d
            out.append((t, p))
    return out


class OpportunityTests(unittest.TestCase):
    def test_classify(self):
        self.assertEqual(classify_reason("🟢 PAPER OPEN #00001"), "ENTRADA")
        self.assertEqual(classify_reason("🟡 NÃO OPERAR — sem vantagem estatística"), "SEM_VANTAGEM")
        self.assertEqual(classify_reason("🟡 NÃO OPERAR — confiança 40 < 60"), "CONFIANCA")
        self.assertEqual(classify_reason("BLOQUEADA — TRADING_ENABLED=false"), "KILL_SWITCH")
        self.assertEqual(classify_reason("BLOQUEADA — já existe posição ativa em XAUUSD (MAX_POSITIONS)"), "POSICAO_ABERTA")
        self.assertEqual(classify_reason("SEM SINAL — 🟡 SEM VANTAGEM"), "SEM_SINAL")
        self.assertEqual(classify_reason("🟡 NÃO OPERAR — ⚠️ resistência forte … sem espaço estatístico"), "VIABILIDADE")

    def test_capture_and_entry_rate_and_overfilter(self):
        prices = path([(30, 0.0), (30, 0.5), (60, 0.0), (30, -0.5), (60, 0.0), (30, 0.5), (30, 0.0)])
        decisions = [DecisionRecord(NOW + timedelta(minutes=5 * i), 2650, 30, "ALTA", "SEM_VANTAGEM") for i in range(40)]
        decisions[6] = DecisionRecord(NOW + timedelta(minutes=30), 2650, 60, "ALTA", "ENTRADA")
        rep = opportunity_report(decisions, prices, [(NOW + timedelta(minutes=30), "ALTA")], threshold_usd=9.0, horizon_min=120)
        self.assertGreaterEqual(rep.n_moves, 2)
        self.assertEqual(rep.n_captured, 1)
        self.assertEqual(rep.n_entries, 1)
        self.assertAlmostEqual(rep.entry_rate, 1 / 40)
        self.assertTrue(rep.overfilter)
        txt = rep.render()
        for key in ("OPPORTUNITY CAPTURE RATE", "ENTRY RATE", "OVERFILTER", "SEM_VANTAGEM"):
            self.assertIn(key, txt)

    def test_hypothetical_and_attribution_flags_costly_rule(self):
        cs = [Candle(NOW + timedelta(minutes=5 * i), 2650 + 3 * (i - 1), 2650 + 3 * i + 0.5, 2650 + 3 * (i - 1) - 0.5, 2650 + 3 * i, 10) for i in range(1, 20)]
        rec = DecisionRecord(NOW, 2650, 55, "ALTA", "CONFIANCA", atr=9.0)
        self.assertEqual(hypothetical_trade(rec, cs, 240), 3.0)
        self.assertIsNone(hypothetical_trade(DecisionRecord(NOW, 2650, 55, "LATERAL", "CONFIANCA", atr=9.0), cs, 240))
        decisions = [DecisionRecord(NOW + timedelta(minutes=i), 2650, 55, "ALTA", "CONFIANCA", atr=9.0, hypothetical_r=3.0 if i % 4 else -1.0) for i in range(12)]
        rep = opportunity_report(decisions, [(NOW, 2650.0), (NOW + timedelta(hours=1), 2651.0)], [], 9.0)
        row = rep.attribution[0]
        self.assertEqual(row["rule"], "CONFIANCA")
        self.assertGreater(row["hyp_expectancy"], 0.2)
        self.assertIn("regra cara", rep.render())

    def test_threshold_curve_finds_ideal_region(self):
        rows = [{"score": 45, "r": -0.5}] * 20 + [{"score": 65, "r": 0.4}] * 20 + [{"score": 75, "r": 0.6}] * 12 + [{"score": 85, "r": 0.2}] * 5
        curve = threshold_curve(rows)
        by = {r["threshold"]: r for r in curve}
        self.assertEqual(by[40]["n"], 57)
        self.assertEqual(by[20]["n"], 57)
        self.assertEqual(by[80]["n"], 5)
        self.assertGreater(by[70]["expectancy"], by[60]["expectancy"])
        rep = opportunity_report([], [(NOW, 1.0), (NOW + timedelta(hours=1), 1.0)], [], 1.0, trade_rows=rows)
        self.assertIn("Score ≥ 70", rep.render())
        self.assertIn("região ideal", rep.render())


class OpportunityMemoryTests(unittest.TestCase):
    def test_live_records_decisions_prices_and_report(self):
        with tempfile.TemporaryDirectory() as d:
            mem = PredictionMemory(os.path.join(d, "t.db"))
            eng = LiveExecutionEngine(mem, GuardLimits(), TradingMode.PAPER, 10000, None, TelegramSender(dry_run=True, quiet=True),
                                      KillSwitch(enabled_env=False), log=lambda s: None)
            src = SampleSource("venda")
            for k in range(3):
                src.now = NOW + timedelta(minutes=20 * k)
                eng.run_cycle(src.snapshot())
            decs = mem.decisions()
            self.assertEqual(len(decs), 3)
            self.assertIn("KILL_SWITCH", {x.action for x in decs})
            self.assertGreater(len(mem.prices()), 100)
            src.now = NOW + timedelta(hours=6)
            eng.run_cycle(src.snapshot())          # resolve hipotéticos com preços gravados
            resolved = [x for x in mem.decisions() if x.hypothetical_r is not None]
            self.assertTrue(resolved)
            rep = mem.opportunity_report()
            self.assertEqual(rep.n_entries, 0)
            self.assertIn("KILL_SWITCH", rep.render())
            mem.close()


class OpportunityBacktestTests(unittest.TestCase):
    def test_backtest_and_validate_include_opportunity(self):
        f = HistoryFrame(xau=make_candles("H1", 900, 2500, 0.4, 6.0, NOW, seed=3),
                         dxy=make_candles("H1", 900, 104, -0.002, 0.08, NOW, seed=4),
                         us10y=make_candles("H1", 900, 4.2, -0.0005, 0.02, NOW, seed=5))
        r = Backtester(f, warmup=230, step=3).run()
        self.assertIsNotNone(r.opportunity)
        self.assertGreater(r.opportunity.n_analyzed, 0)
        self.assertIn("OPPORTUNITY ENGINE", r.render())
        self.assertIn("Curva limiar", r.render())
        rep = validate(f, n_folds=2, step=6, warmup=230, audit_every=200)
        self.assertIn("OOS por fold", rep.render())
        from gold_ai.evaluation import walk_forward
        wf = walk_forward(Backtester(f, warmup=230, step=6), n_folds=2, grid=[{"buy": 40, "sell": -40, "min_confirmations": 2}])
        self.assertIsNotNone(wf.oos_opportunity)
        self.assertIn("OPPORTUNITY ENGINE", wf.render())   # agregado fora da amostra aparece no walk-forward


if __name__ == "__main__":
    unittest.main()


class FunnelTests(unittest.TestCase):
    def test_funnel_counts_and_render(self):
        from gold_ai.opportunity import Funnel, FUNNEL_STAGES, funnel_stage
        from gold_ai.config import EngineConfig
        f = Funnel()
        f.add(False, None)                 # análise sem oportunidade bruta
        f.add(True, "SCORE_MIN")
        f.add(True, "CONFIRMACOES")
        f.add(True, "CORRELACAO")
        f.add(True, None)                  # entrada
        self.assertEqual((f.analyses, f.raw, f.entries), (5, 4, 1))
        self.assertEqual(f.qualified, 2)   # entrada + bloqueada só por carteira
        txt = f.render()
        for key in ("ANÁLISES H1:", "OPORTUNIDADES BRUTAS", "Score insuficiente", "Confirmações insuficientes", "Correlação", "OPORTUNIDADES QUALIFICADAS:", "ENTRADAS:"):
            self.assertIn(key, txt)
        g = f.merge(f)
        self.assertEqual(g.analyses, 10)
        self.assertTrue(all(k in dict(FUNNEL_STAGES) for k in g.drops))
        # etapas a partir de uma avaliação real
        cfg = EngineConfig()
        eng = GoldAIEngine(cfg)
        a = eng.analyze(SampleSource("neutro").snapshot())
        self.assertEqual(funnel_stage(a, None, "SEM_VANTAGEM", "", cfg)[0], abs(a.score) >= 15)
        a2, sig = eng.run_cycle(SampleSource("venda").snapshot())
        self.assertEqual(funnel_stage(a2, sig, "", "🟢 PAPER OPEN #1", cfg), (True, None))
        self.assertEqual(funnel_stage(a2, sig, "", "BLOQUEADA — exposição de carteira: risco correlacionado", cfg)[1], "CORRELACAO")
        self.assertEqual(funnel_stage(a2, sig, "", "BLOQUEADA — já existe posição ativa em XAUUSD (MAX_POSITIONS)", cfg)[1], "POSICAO_ABERTA")
        self.assertEqual(funnel_stage(a2, None, "CONFIRMACOES", "", cfg)[1], "CONFIRMACOES")

    def test_funnel_in_backtest_walkforward_and_live(self):
        f = HistoryFrame(xau=make_candles("H1", 900, 2500, 0.4, 6.0, NOW, seed=3),
                         dxy=make_candles("H1", 900, 104, -0.002, 0.08, NOW, seed=4),
                         us10y=make_candles("H1", 900, 4.2, -0.0005, 0.02, NOW, seed=5))
        bt = Backtester(f, warmup=230, step=4)
        r = bt.run()
        self.assertIsNotNone(r.funnel)
        self.assertEqual(r.funnel.analyses, r.n_steps)
        self.assertIn("FUNIL DE ENTRADA", r.render())
        from gold_ai.evaluation import walk_forward
        wf = walk_forward(bt, n_folds=2, grid=[{"buy": 40, "sell": -40, "min_confirmations": 2}])
        self.assertIsNotNone(wf.oos_funnel)
        self.assertIn("fora da amostra, todos os folds", wf.render())
        with tempfile.TemporaryDirectory() as d:
            mem = PredictionMemory(os.path.join(d, "t.db"))
            eng = LiveExecutionEngine(mem, GuardLimits(), TradingMode.PAPER, 10000, None, TelegramSender(dry_run=True, quiet=True), log=lambda s: None)
            src = SampleSource("venda")
            eng.run_cycle(src.snapshot())
            src.now += timedelta(minutes=20)
            eng.run_cycle(src.snapshot())          # bloqueada: posição já aberta
            fun = mem.funnel("XAUUSD")
            self.assertEqual(fun.analyses, 2)
            self.assertEqual(fun.entries, 1)
            self.assertEqual(fun.drops.get("POSICAO_ABERTA"), 1)
            self.assertEqual(fun.qualified, 2)
            mem.close()
