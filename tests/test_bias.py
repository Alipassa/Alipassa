"""GOLD BIAS ENGINE (docs/DIRETRIZ_BIAS.md): pesos, classificação, notícias, contradições, confiança, Telegram, memória."""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from gold_ai.bias import (BIAS_WEIGHTS, BiasMemory, BiasNotifier, GoldBiasEngine, bias_classify, bias_score_news, bias_structure,
                          format_bias_closing, format_bias_message)
from gold_ai.models import Candle, EconomicEvent, MarketSnapshot, NewsItem
from gold_ai.sources.sample import SampleSource


def snap(scenario: str) -> MarketSnapshot:
    return SampleSource(scenario=scenario).snapshot()


class BiasTests(unittest.TestCase):
    def test_weights_follow_directive(self):
        self.assertEqual(sum(BIAS_WEIGHTS.values()), 100)
        self.assertEqual(BIAS_WEIGHTS["juros_reais"], 20)
        self.assertEqual(BIAS_WEIGHTS["dolar"], 15)

    def test_classification_thresholds(self):
        self.assertEqual(bias_classify(70)[0], "FORTE ALTA")
        self.assertEqual(bias_classify(40)[0], "ALTA")
        self.assertEqual(bias_classify(39)[0], "NEUTRO")
        self.assertEqual(bias_classify(-39)[0], "NEUTRO")
        self.assertEqual(bias_classify(-40)[0], "BAIXA")
        self.assertEqual(bias_classify(-70)[0], "FORTE BAIXA")

    def test_bullish_and_bearish_scenarios(self):
        eng = GoldBiasEngine()
        up, down = eng.analyze(snap("premove_alta")), eng.analyze(snap("venda"))
        self.assertGreater(up.score, 40)
        self.assertLess(down.score, -40)
        self.assertIn("Minha leitura atual é de ALTA", up.opinion)
        self.assertIn("Minha leitura atual é de BAIXA", down.opinion)
        self.assertEqual(set(up.horizons), {"horas", "1d", "5d"})
        self.assertLessEqual(up.confidence, 90)

    def test_single_indicator_is_not_enough(self):
        s = snap("confirmacao_alta")
        only_tech = MarketSnapshot(time=s.time, price=s.price, candles=s.candles)
        r = GoldBiasEngine().analyze(only_tech)
        self.assertLess(abs(r.score), 40, "só o técnico não pode gerar viés direcional")

    def test_news_scoring_recency_and_credibility(self):
        now = datetime(2026, 9, 14, 13, 0, tzinfo=timezone.utc)
        fresh = bias_score_news(NewsItem("Fed signals rate cut", "Reuters", now, "fed", gold_impact=0.9), now)
        old = bias_score_news(NewsItem("Fed signals rate cut", "Reuters", now - timedelta(hours=9), "fed", gold_impact=0.9), now)
        rumor = bias_score_news(NewsItem("Fed could cut, sources say", "twitter", now, "fed", gold_impact=0.9), now)
        self.assertEqual(fresh.impact, 3)
        self.assertGreater(fresh.weight, old.weight)
        self.assertGreater(fresh.weight, rumor.weight * 4)
        jobs = bias_score_news(NewsItem("Payrolls miss badly", "BLS", now, "macro", gold_impact=0.4), now)
        self.assertEqual(jobs.factor, "emprego")

    def test_contradiction_tech_vs_macro(self):
        s = snap("confirmacao_alta")
        s.dxy_change_pct, s.real_yield_change_bp, s.us10y_change_bp, s.fed_tone, s.fed_cut_prob_change_pp = 0.5, 8.0, 9.0, -0.5, -12.0
        r = GoldBiasEngine().analyze(s)
        self.assertTrue(any("divergência entre técnico e macro" in c for c in r.contradictions))
        clean = GoldBiasEngine().analyze(snap("confirmacao_alta"))
        self.assertLess(r.confidence, clean.confidence)

    def test_event_warning_lowers_confidence(self):
        s = snap("premove_alta")
        base = GoldBiasEngine().analyze(s).confidence
        s.events = [EconomicEvent("CPI EUA", s.time + timedelta(minutes=40), "MUITO ALTO", consensus=0.3, previous=0.2, kind="cpi", unit="%")]
        r = GoldBiasEngine().analyze(s)
        self.assertIsNotNone(r.event)
        self.assertLess(r.confidence, base)
        self.assertIn("CPI EUA em 40 min", format_bias_message(r))

    def test_structure_hh_hl(self):
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        cs = [Candle(t0 + timedelta(hours=i), 0, 100 + i + (5 if i % 8 == 4 else 0), 90 + i - (5 if i % 8 == 0 else 0), 95 + i) for i in range(60)]
        self.assertEqual(bias_structure(cs), "ALTA (HH+HL)")

    def test_message_format(self):
        txt = format_bias_message(GoldBiasEngine().analyze(snap("premove_alta")))
        for part in ("🥇 GOLD MARKET AI", "VIÉS:", "CONFIANÇA:", "RESUMO", "TÉCNICO", "LEITURA DA IA", "CENÁRIO PRINCIPAL"):
            self.assertIn(part, txt)
        self.assertNotIn("certamente", txt)

    def test_notifier_no_repeat_then_reversal(self):
        with tempfile.TemporaryDirectory() as d:
            n = BiasNotifier(os.path.join(d, "s.json"))
            eng = GoldBiasEngine()
            self.assertEqual([k for k, _ in n.decide(eng.analyze(snap("premove_alta")))], ["relatorio"])
            self.assertEqual(n.decide(eng.analyze(snap("premove_alta"))), [])
            n2 = BiasNotifier(os.path.join(d, "s.json"))           # estado sobrevive a reinício
            kinds = [k for k, _ in n2.decide(eng.analyze(snap("venda")))]
            self.assertEqual(kinds, ["reversao"])

    def test_memory_resolves_and_learns(self):
        mem = BiasMemory(":memory:")
        eng = GoldBiasEngine()
        s = snap("premove_alta")
        r = eng.analyze(s)
        mem.record(r)
        later = [Candle(s.time + timedelta(hours=h), 0, 0, 0, s.price * (1 + 0.002 * h)) for h in range(1, 130)]
        n = mem.resolve(later, s.time + timedelta(hours=130))
        self.assertEqual(n, 3)
        st = mem.stats()
        self.assertEqual(st["horizontes"]["1d"], [1, 1])
        self.assertIn("juros_reais", st["fatores"])
        self.assertAlmostEqual(sum(mem.suggest_weights().values()), 100, delta=0.5)
        s.price *= 1.01                                        # fechamento +1 % no mesmo dia → previsão ALTA acertou
        self.assertIn("ACERTO", format_bias_closing(eng.analyze(s), mem))


if __name__ == "__main__":
    unittest.main()


class BiasMT5Tests(unittest.TestCase):
    def test_bias_reads_broker_prices_from_mt5(self):
        """Mesmo caminho do live: MT5Source (preço da corretora) → GOLD BIAS."""
        from gold_ai.data.mt5 import MT5Config, MT5Source
        from tests.test_mt5 import FakeMT5

        src = MT5Source(MT5Config(), mt5=FakeMT5())
        s = src.snapshot()
        self.assertEqual(src.status.get("mt5"), "ok")
        r = GoldBiasEngine().analyze(s)
        self.assertEqual(r.price, s.price)
        self.assertTrue(r.factor("tecnico").available)
        self.assertLess(abs(r.score), 40, "só preço (sem macro) não pode gerar viés direcional")
        self.assertIn("XAU/USD", format_bias_message(r))
