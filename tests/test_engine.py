"""Testes do GOLD AI ENGINE (stdlib unittest; também rodam com pytest)."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from gold_ai import Direction, EngineConfig, GoldAIEngine, MarketSnapshot, SignalType, Stage
from gold_ai.events import analyze_post_event, build_scenario_tree, next_high_impact_event
from gold_ai.factors import score_dolar, score_juros_reais, systemic_risk_index
from gold_ai.memory import PredictionMemory
from gold_ai.models import EconomicEvent
from gold_ai.report import render_report
from gold_ai.signals import classify
from gold_ai.sources.sample import SampleSource, make_candles
from gold_ai.technical import adx, analyze_timeframe, atr, ema, macd, rsi

NOW = datetime(2026, 9, 14, 13, 0, tzinfo=timezone.utc)


class TechnicalTests(unittest.TestCase):
    def test_ema_tracks_trend(self):
        xs = [float(i) for i in range(100)]
        e = ema(xs, 9)
        self.assertEqual(len(e), 100)
        self.assertLess(e[-1], xs[-1])
        self.assertGreater(e[-1], xs[-10])

    def test_rsi_bounds(self):
        up = [100 + i for i in range(40)]
        down = [100 - i for i in range(40)]
        self.assertGreater(rsi(up), 70)
        self.assertLess(rsi(down), 30)
        self.assertIsNone(rsi(up[:10]))

    def test_macd_and_atr_and_adx(self):
        cs = make_candles("H1", 200, 2600, 1.0, 2.0, NOW)
        closes = [c.close for c in cs]
        _, _, hist = macd(closes)
        self.assertTrue(hist)
        self.assertGreater(atr(cs), 0)
        self.assertIsNotNone(adx(cs))

    def test_uptrend_reads_positive(self):
        cs = make_candles("H1", 260, 2600, 1.5, 2.0, NOW)
        r = analyze_timeframe("H1", cs)
        self.assertEqual(r.trend, "ALTA")
        self.assertGreater(r.score, 0.3)

    def test_downtrend_reads_negative(self):
        cs = make_candles("H1", 260, 2700, -1.5, 2.0, NOW)
        r = analyze_timeframe("H1", cs)
        self.assertEqual(r.trend, "BAIXA")

    def test_insufficient_data(self):
        r = analyze_timeframe("M1", make_candles("M1", 10, 2600, 0, 1, NOW))
        self.assertIn("dados insuficientes", r.notes)


class FactorTests(unittest.TestCase):
    def test_weak_dollar_supports_gold(self):
        s = MarketSnapshot(time=NOW, price=2650, dxy_change_pct=-0.5)
        self.assertGreater(score_dolar(s, 15).score, 5)
        s.dxy_change_pct = 0.5
        self.assertLess(score_dolar(s, 15).score, -5)

    def test_missing_factor_is_unavailable(self):
        f = score_juros_reais(MarketSnapshot(time=NOW, price=2650), 18)
        self.assertFalse(f.available)
        self.assertEqual(f.score, 0)

    def test_score_bounded_by_weight(self):
        s = MarketSnapshot(time=NOW, price=2650, real_yield_change_bp=-50, us10y_change_bp=-60)
        f = score_juros_reais(s, 18)
        self.assertLessEqual(abs(f.score), 18)

    def test_systemic_risk_index(self):
        calm = MarketSnapshot(time=NOW, price=2650, vix=13, credit_spread_bp=300)
        stress = MarketSnapshot(time=NOW, price=2650, vix=38, vix_change_pct=50, credit_spread_bp=750, equity_change_pct=-4, bank_stress=80)
        self.assertLess(systemic_risk_index(calm), 15)
        self.assertGreater(systemic_risk_index(stress), 75)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = GoldAIEngine(EngineConfig())

    def test_probabilities_sum_to_one(self):
        a = self.engine.analyze(SampleSource("premove_alta").snapshot())
        self.assertAlmostEqual(a.prob_up + a.prob_down + a.prob_flat, 1.0, places=2)
        self.assertTrue(-100 <= a.score <= 100)
        self.assertTrue(0 <= a.confidence <= 100)

    def test_premove_detected_when_fundamentals_lead_price(self):
        a = self.engine.analyze(SampleSource("premove_alta").snapshot())
        self.assertEqual(a.premove.stage, Stage.PRE_MOVIMENTO)
        self.assertEqual(a.premove.direction, Direction.ALTA)
        self.assertEqual(a.premove.latent_pressure, "PRESSÃO COMPRADORA LATENTE")
        self.assertGreaterEqual(len(a.confirmations), 3)

    def test_premove_signal_emitted_then_confirmation(self):
        src = SampleSource("premove_alta")
        _, sig = self.engine.run_cycle(src.snapshot())
        self.assertIsNotNone(sig)
        self.assertEqual(sig.type, SignalType.PRE_MOVE)
        self.assertIn("GOLD PRE-MOVE", sig.text)
        src.scenario, src.now, src.price = "confirmacao_alta", src.now + timedelta(minutes=30), src.price + 6
        a, sig2 = self.engine.run_cycle(src.snapshot())
        self.assertEqual(a.premove.stage, Stage.CONFIRMACAO)
        self.assertIsNotNone(sig2)
        self.assertIn(sig2.type, (SignalType.BUY, SignalType.STRONG_BUY))
        self.assertEqual(sig2.trigger, "confirmação de movimento")

    def test_sell_scenario(self):
        a, sig = self.engine.run_cycle(SampleSource("venda").snapshot())
        self.assertLess(a.score, -50)
        self.assertEqual(a.direction, Direction.BAIXA)
        self.assertIsNotNone(sig)
        self.assertIn(sig.type, (SignalType.SELL, SignalType.STRONG_SELL))
        self.assertIn("🔴 VENDA", sig.text)

    def test_reversal_alert(self):
        a, sig = self.engine.run_cycle(SampleSource("reversao").snapshot())
        self.assertGreaterEqual(a.reversal.risk, 60)
        self.assertEqual(a.reversal.current_trend, Direction.ALTA)
        self.assertEqual(sig.type, SignalType.REVERSAL)

    def test_systemic_risk_alert_has_priority(self):
        a, sig = self.engine.run_cycle(SampleSource("sistemico").snapshot())
        self.assertGreaterEqual(a.systemic_risk, 75)
        self.assertEqual(sig.type, SignalType.RISK)

    def test_neutral_scenario_no_signal(self):
        a, sig = self.engine.run_cycle(SampleSource("neutro").snapshot())
        self.assertEqual(classify(a.score, self.engine.cfg), SignalType.NEUTRAL)
        self.assertIsNone(sig)

    def test_anti_spam_no_repeat_without_change(self):
        src = SampleSource("venda")
        _, first = self.engine.run_cycle(src.snapshot())
        self.assertIsNotNone(first)
        src.now += timedelta(minutes=2)
        _, second = self.engine.run_cycle(src.snapshot())
        self.assertIsNone(second)

    def test_three_confirmations_required(self):
        cfg = EngineConfig()
        cfg.min_confirmations = 99
        engine = GoldAIEngine(cfg)
        _, sig = engine.run_cycle(SampleSource("venda").snapshot())
        self.assertIsNone(sig)

    def test_stage3_does_not_chase(self):
        s = SampleSource("confirmacao_alta").snapshot()
        s.price_change_pct = 1.5  # ~4.4 ATR
        a, sig = self.engine.run_cycle(s)
        self.assertEqual(a.premove.stage, Stage.MOVIMENTO)
        self.assertIsNone(sig)

    def test_report_renders(self):
        a = self.engine.analyze(SampleSource("premove_alta").snapshot())
        txt = render_report(a)
        for key in ("GOLD AI", "Score:", "Probabilidade de alta", "Pressão dominante", "Pré-movimento", "Conclusão"):
            self.assertIn(key, txt)

    def test_zone_levels_on_correct_side(self):
        a = self.engine.analyze(SampleSource("venda").snapshot())
        z = a.zone
        if z["support"] is not None:
            self.assertLess(z["support"], a.price)
        if z["resistance"] is not None:
            self.assertGreater(z["resistance"], a.price)
        self.assertGreater(z["invalidation"], a.price)

    def test_event_reduces_confidence(self):
        s = SampleSource("premove_alta").snapshot()
        base = self.engine.analyze(s).confidence
        s.events = [EconomicEvent("FOMC", NOW + timedelta(minutes=30), "MUITO ALTO", kind="fomc")]
        with_event = self.engine.analyze(s).confidence
        self.assertLess(with_event, base)


class EventTests(unittest.TestCase):
    def test_next_event_within_window(self):
        evs = [EconomicEvent("CPI", NOW + timedelta(minutes=30), "MUITO ALTO", kind="cpi"),
               EconomicEvent("Housing", NOW + timedelta(minutes=10), "BAIXO", kind="housing")]
        self.assertEqual(next_high_impact_event(evs, NOW, 90).name, "CPI")
        self.assertIsNone(next_high_impact_event(evs, NOW, 10))

    def test_scenario_tree_cpi(self):
        ev = EconomicEvent("CPI", NOW + timedelta(minutes=30), "MUITO ALTO", consensus=0.3, kind="cpi", unit="%")
        tree = build_scenario_tree(ev, MarketSnapshot(time=NOW, price=2650))
        self.assertIn("BAIXA", tree.scenarios["acima do consenso"])
        self.assertIn("ALTA", tree.scenarios["abaixo do consenso"])
        self.assertIn("CPI", tree.render())

    def test_post_event_confirmation_and_reversal(self):
        ev = EconomicEvent("CPI", NOW, "MUITO ALTO", consensus=0.3, actual=0.1, kind="cpi")
        s = MarketSnapshot(time=NOW, price=2650, dxy_change_pct=-0.3, us10y_change_bp=-5, real_yield_change_bp=-4, price_change_pct=0.4, order_flow_imbalance=0.3)
        chain = analyze_post_event(ev, s)
        self.assertEqual(chain.expected_gold_direction, "ALTA")
        self.assertEqual(chain.verdict, "CONFIRMAÇÃO")
        s.price_change_pct = -0.4
        self.assertEqual(analyze_post_event(ev, s).verdict, "REVERSÃO")


class MemoryTests(unittest.TestCase):
    def test_record_resolve_and_accuracy(self):
        engine = GoldAIEngine()
        a = engine.analyze(SampleSource("venda").snapshot())
        with tempfile.TemporaryDirectory() as d:
            mem = PredictionMemory(os.path.join(d, "t.db"))
            pid = mem.record(a, "GOLD SELL")
            self.assertEqual(len(mem.pending()), 1)
            path = [(a.time + timedelta(minutes=m), a.price - 0.6 * m) for m in range(1, 30)]
            out = mem.resolve(pid, path, threshold=9.0)
            self.assertEqual(out.result, "ACERTO")
            self.assertIsNotNone(out.time_to_reaction_min)
            self.assertGreater(out.mfe, 9.0)
            self.assertEqual(mem.pending(), [])
            acc = mem.accuracy("sessao")
            self.assertEqual(acc[0]["n"], 1)
            self.assertEqual(acc[0]["taxa"], 1.0)
            self.assertTrue(mem.factor_power())
            # previsão errada
            pid2 = mem.record(a, "GOLD SELL")
            out2 = mem.resolve(pid2, [(a.time + timedelta(minutes=m), a.price + 0.6 * m) for m in range(1, 30)], threshold=9.0)
            self.assertEqual(out2.result, "ERRO")
            self.assertEqual(mem.accuracy("previsao")[0]["taxa"], 0.5)
            for by in ("hora", "score_bucket", "horizonte", "estagio", "sinal_tipo"):
                self.assertTrue(mem.accuracy(by))
            mem.close()


if __name__ == "__main__":
    unittest.main()


class WatchAntiSpamTests(unittest.TestCase):
    """WATCH repetido a cada poucos minutos (16/09 05:09, 05:12, 05:19, 05:24) — agora respeita o intervalo mínimo por direção."""

    def _a(self, t):
        from types import SimpleNamespace
        from gold_ai.models import Direction, EvidenceLevel, Stage
        return SimpleNamespace(time=t, score=-26.0, direction=Direction.BAIXA, premove=SimpleNamespace(stage=Stage.NEUTRO, direction=Direction.BAIXA),
                               confirmations=["a", "b", "c"], has_edge=True, systemic_risk=0.0, reversal=SimpleNamespace(risk=0.0, current_trend=Direction.ALTA),
                               evidence_level=EvidenceLevel.L2_ALERTA, prob_up=0.31, prob_down=0.69, prob_flat=0.0, factors=[], technical=[], price=1.15,
                               confidence=52.0, horizon="1–5 dias")
    def test_watch_respects_min_interval_per_direction(self):
        from datetime import datetime, timedelta, timezone
        from gold_ai.config import EngineConfig
        from gold_ai.models import Direction, SignalType
        from gold_ai.signals import SignalGate
        gate = SignalGate(EngineConfig())
        t0 = datetime(2026, 9, 16, 5, 9, tzinfo=timezone.utc)
        first = gate.evaluate(self._a(t0))
        self.assertIsNotNone(first)
        self.assertEqual(first.type, SignalType.WATCH)
        for m in (3, 10, 14):                                          # dentro dos 15 min: silêncio
            self.assertIsNone(gate.evaluate(self._a(t0 + timedelta(minutes=m))), f"repetiu aos {m} min")
        again = gate.evaluate(self._a(t0 + timedelta(minutes=16)))
        self.assertIsNotNone(again)
        self.assertEqual(again.type, SignalType.WATCH)
        # direção contrária pode alertar antes do intervalo (após um ciclo sem WATCH, como no desenho original)
        neutral = self._a(t0 + timedelta(minutes=17))
        neutral.has_edge = False
        self.assertIsNone(gate.evaluate(neutral))
        a = self._a(t0 + timedelta(minutes=18))
        a.direction = a.premove.direction = Direction.ALTA
        a.score, a.prob_up, a.prob_down = 26.0, 0.69, 0.31
        flipped = gate.evaluate(a)
        self.assertIsNotNone(flipped)
        self.assertEqual(flipped.type, SignalType.WATCH)
