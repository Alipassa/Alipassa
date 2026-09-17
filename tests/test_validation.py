"""Testes 2.1 — VALIDATION ENGINE."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from gold_ai import EngineConfig, GoldAIEngine, SignalType
from gold_ai.evaluation import Backtester, HistoryFrame, record_from, technical_details, validate, walk_forward
from gold_ai.memory import PredictionMemory
from gold_ai.models import Candle, NewsItem
from gold_ai.report import render_dashboard
from gold_ai.sources.sample import SampleSource, make_candles
from gold_ai.validation import IsotonicCalibrator, calibration_table, factor_scoreboard, lookahead_audit

NOW = datetime(2026, 9, 14, 13, 0, tzinfo=timezone.utc)


class LookaheadTests(unittest.TestCase):
    def test_clean_snapshot_passes(self):
        self.assertEqual(lookahead_audit(SampleSource("premove_alta").snapshot()), [])

    def test_future_data_is_flagged(self):
        s = SampleSource("neutro").snapshot()
        s.candles["H1"].append(Candle(s.time + timedelta(hours=1), 1, 2, 0, 1, 1))
        s.news.append(NewsItem("futuro", time=s.time + timedelta(minutes=5)))
        v = lookahead_audit(s)
        self.assertEqual(len(v), 2)
        self.assertTrue(any("H1" in x for x in v))

    def test_history_frame_snapshots_are_clean(self):
        xau = make_candles("H1", 600, 2500, 0.3, 5.0, NOW, seed=9)
        f = HistoryFrame(xau=xau)
        for i in (250, 400, 599):
            self.assertEqual(lookahead_audit(f.snapshot_at(i)), [])


class CalibrationTests(unittest.TestCase):
    def test_table_and_brier(self):
        pairs = [(0.6, True)] * 6 + [(0.6, False)] * 4 + [(0.9, True)] * 9 + [(0.9, False)] * 1
        rep = calibration_table(pairs, n_bins=5)
        self.assertEqual(rep.n, 20)
        self.assertLess(rep.ece, 0.05)
        self.assertLess(rep.brier, rep.brier_reference)
        self.assertIn("bem calibrado", rep.render())
        self.assertEqual(calibration_table([]).n, 0)

    def test_isotonic_is_monotonic_and_fixes_overconfidence(self):
        pairs = [(0.9, i % 2 == 0) for i in range(20)] + [(0.6, i % 5 == 0) for i in range(20)] + [(0.75, i % 3 == 0) for i in range(21)]
        cal = IsotonicCalibrator().fit(pairs)
        ys = [cal(x / 100) for x in range(50, 100)]
        self.assertTrue(all(a <= b + 1e-9 for a, b in zip(ys, ys[1:])))
        self.assertLess(cal(0.9), 0.7)  # 90% previsto → ~50% observado
        rt = IsotonicCalibrator.from_dict(cal.to_dict())
        self.assertAlmostEqual(rt(0.8), cal(0.8))
        self.assertEqual(IsotonicCalibrator()(0.7), 0.7)  # sem ajuste = identidade

    def test_engine_applies_calibrator(self):
        s = SampleSource("premove_alta").snapshot()
        raw = GoldAIEngine().analyze(s)
        cal = IsotonicCalibrator().fit([(0.9, i % 2 == 0) for i in range(20)] + [(0.6, i % 2 == 0) for i in range(20)])
        calibrated = GoldAIEngine(calibrator=cal).analyze(s)
        self.assertLess(calibrated.prob_up, raw.prob_up)
        self.assertAlmostEqual(calibrated.prob_up + calibrated.prob_down + calibrated.prob_flat, 1.0, places=2)


class ScoreboardTests(unittest.TestCase):
    def test_scoreboard_ranks_factors(self):
        recs = []
        for i in range(40):
            hit = i % 4 != 0  # 75% base
            recs.append({"direction": "ALTA", "hit": hit,
                         "factors": {"dolar": 0.8 if hit else -0.8, "cot": 0.5, "fluxo": 0.05},
                         "technical": {"RSI_H1": 0.6 if hit else 0.6}})
        sb = factor_scoreboard(recs)
        names = {r.name: r for r in sb.rows}
        self.assertEqual(names["dolar"].hit_rate, 1.0)
        self.assertEqual(names["dolar"].lift, 1.0)
        self.assertAlmostEqual(names["cot"].hit_rate, 0.75)
        self.assertNotIn("fluxo", names)  # abaixo do min_ratio
        self.assertIn("tec:RSI_H1", names)
        txt = sb.render()
        self.assertIn("█", txt)
        self.assertIn("dolar", txt)
        self.assertEqual(factor_scoreboard([]).n, 0)


class WalkForwardTests(unittest.TestCase):
    def _frame(self):
        return HistoryFrame(xau=make_candles("H1", 900, 2500, 0.4, 6.0, NOW, seed=3),
                            dxy=make_candles("H1", 900, 104, -0.002, 0.08, NOW, seed=4),
                            us10y=make_candles("H1", 900, 4.2, -0.0005, 0.02, NOW, seed=5))

    def test_rolling_folds_advance_and_never_overlap(self):
        bt = Backtester(self._frame(), warmup=230, step=4, include_watch=True)
        grid = [{"buy": 40, "sell": -40, "min_confirmations": 2}]
        wf = walk_forward(bt, n_folds=3, grid=grid, mode="rolling", train_folds=2)
        self.assertEqual(len(wf.folds), 3)
        self.assertIn("treina → testa → avança", wf.render())
        wf2 = walk_forward(bt, n_folds=3, grid=grid, mode="anchored")
        self.assertEqual(len(wf2.folds), 3)

    def test_record_from_carries_factor_detail(self):
        eng = GoldAIEngine()
        s = SampleSource("venda").snapshot()
        a, sig = eng.run_cycle(s)
        rec = record_from(a, sig, s.atr)
        self.assertIn("dolar", rec.factors)
        self.assertTrue(any(k.startswith("RSI_") for k in rec.technical))
        self.assertTrue(technical_details(a))

    def test_validate_report(self):
        rep = validate(self._frame(), EngineConfig(), n_folds=2, step=6, warmup=230, audit_every=100)
        txt = rep.render()
        for key in ("VALIDATION ENGINE", "AUDITORIA", "0 violação", "BACKTEST", "WALK-FORWARD", "CALIBRAÇÃO", "SCORE POR FATOR", "VEREDITO"):
            self.assertIn(key, txt)
        self.assertEqual(rep.audit_violations, [])


class MemoryV21Tests(unittest.TestCase):
    def test_auto_resolve_and_learning(self):
        eng = GoldAIEngine()
        src = SampleSource("venda")
        a, sig = eng.run_cycle(src.snapshot())
        with tempfile.TemporaryDirectory() as d:
            mem = PredictionMemory(os.path.join(d, "t.db"))
            pid = mem.record(a, sig.type.value, atr=9.0, horizon_min=120)
            # ainda sem candles após a previsão → pendente
            self.assertEqual(mem.auto_resolve([], a.time + timedelta(minutes=5), 9.0), [])
            self.assertEqual(len(mem.pending()), 1)
            # preço cai 10 USD em 30 min → ACERTO com lead
            cs = [Candle(a.time + timedelta(minutes=m), a.price, a.price, a.price - 0.4 * m, a.price - 0.4 * m, 10) for m in range(1, 40)]
            done = mem.auto_resolve(cs, a.time + timedelta(minutes=40), 9.0)
            self.assertEqual(len(done), 1)
            self.assertEqual(done[0][1].result, "ACERTO")
            self.assertLessEqual(done[0][1].time_to_reaction_min, 30)
            self.assertEqual(mem.pending(), [])
            lt = mem.lead_time_stats()
            self.assertEqual(lt["n"], 1)
            self.assertIn(sig.type.value, lt["por_tipo"])
            recs = mem.resolved_records()
            self.assertEqual(len(recs), 1)
            self.assertIn("dolar", recs[0]["factors"])
            self.assertTrue(mem.scoreboard().rows)
            self.assertEqual(mem.calibration().n, 1)
            self.assertTrue(callable(mem.fit_calibrator()))
            # expira sem tocar o limiar → LATERAL
            pid2 = mem.record(a, sig.type.value, atr=50.0, horizon_min=10)
            flat = [Candle(a.time + timedelta(minutes=m), a.price, a.price + 1, a.price - 1, a.price, 10) for m in range(1, 12)]
            done = mem.auto_resolve(flat, a.time + timedelta(minutes=15), 50.0)
            self.assertEqual(done[0][1].result, "LATERAL")
            mem.close()


class DashboardTests(unittest.TestCase):
    def test_dashboard_renders_regime_and_decision(self):
        a = GoldAIEngine().analyze(SampleSource("premove_alta").snapshot())
        txt = render_dashboard(a, 17)
        for key in ("REGIME", "PRE-MOVE", "LEAD TIME", "17 min", "DXY", "REAL YIELD", "STATUS", "DECISÃO", "GOLD PRE-MOVE"):
            self.assertIn(key, txt)
        self.assertIn(a.regime, ("BULLISH", "BEARISH", "RANGE", "VOLATILE", "BULLISH (fraco)", "BEARISH (fraco)", "INDEFINIDO"))
        n = GoldAIEngine().analyze(SampleSource("neutro").snapshot())
        self.assertIn("NÃO SEI", render_dashboard(n))


if __name__ == "__main__":
    unittest.main()
