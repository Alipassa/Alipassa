"""Testes 4.0 — mercados, sinais por fator, selector, decay, confiança estatística, exposição, dados multi-mercado, orquestrador."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from gold_ai import Direction, EngineConfig, GoldAIEngine, SignalType
from gold_ai.data.multi import MarketSnapshotSet, data_quality, derive_market_snapshot
from gold_ai.evaluation import HistoryFrame, render_market_validation, validate_markets
from gold_ai.guard import GuardLimits, KillSwitch, TradingMode
from gold_ai.market_engine import MarketAIEngine
from gold_ai.markets import MARKETS, PHASE1, correlation, get_market
from gold_ai.memory import PredictionMemory
from gold_ai.selector import (AssetSelector, Candidate, OpenExposure, PortfolioExposureEngine, PortfolioLimits, opportunity_decay, render_rank,
                              statistical_confidence)
from gold_ai.sources.sample import SampleSource, make_candles
from gold_ai.telegram import TelegramSender

NOW = datetime(2026, 9, 14, 13, 0, tzinfo=timezone.utc)


class MarketRegistryTests(unittest.TestCase):
    def test_registry_and_correlation(self):
        self.assertEqual(set(PHASE1), {"EURUSD", "US500", "XAUUSD", "USDJPY", "WTI"})
        self.assertEqual(get_market("usdjpy").factor_signs["dolar"], -1)
        self.assertEqual(get_market("EURUSD").factor_signs["geopolitica"], -1)
        self.assertEqual(get_market("US500").factor_signs["dolar"], 0)
        self.assertAlmostEqual(correlation("EURUSD", "GBPUSD"), 0.75)
        self.assertAlmostEqual(correlation("GBPUSD", "EURUSD"), 0.75)
        self.assertEqual(correlation("XAUUSD", "XAUUSD"), 1.0)
        with self.assertRaises(KeyError):
            get_market("DOGE")
        self.assertTrue(all(m.phase >= 1 for m in MARKETS.values()))

    def test_single_brain_applies_factor_signs(self):
        s = SampleSource("premove_alta").snapshot()   # dólar ↓, juros ↓ → favorável ao ouro
        gold = GoldAIEngine().analyze(s)
        jpy = GoldAIEngine(EngineConfig(factor_signs=dict(get_market("USDJPY").factor_signs), symbol="USDJPY")).analyze(s)
        self.assertGreater(gold.factor("dolar").score, 0)
        self.assertLess(jpy.factor("dolar").score, 0)             # sinal invertido
        self.assertFalse(jpy.factor("opcoes").available)           # sem relação conhecida → indisponível
        self.assertIn("USDJPY", jpy.factor("dolar").rationale)
        us500 = GoldAIEngine(EngineConfig(factor_signs=dict(get_market("US500").factor_signs), symbol="US500")).analyze(s)
        self.assertFalse(us500.factor("dolar").available)
        self.assertLess(us500.factor("geopolitica").score, 0)


class SelectorTests(unittest.TestCase):
    def test_statistical_confidence_prefers_sample_size(self):
        big = statistical_confidence([0.42 + (0.9 if i % 3 == 0 else -0.3) for i in range(487)])
        small = statistical_confidence([0.9 if i % 2 else 0.3 for i in range(31)])
        self.assertEqual(big.level, "HIGH")
        self.assertIn(small.level, ("MEDIUM", "LOW"))
        self.assertLess(small.shrunk, small.expectancy)
        self.assertEqual(statistical_confidence([]).level, "NONE")

    def test_decay(self):
        a = GoldAIEngine().analyze(SampleSource("premove_alta").snapshot())
        fresh, _ = opportunity_decay(a, NOW, NOW)
        old, note = opportunity_decay(a, NOW - timedelta(minutes=90), NOW)
        self.assertGreater(fresh, old)
        self.assertIn("idade 90 min", note)
        moved = GoldAIEngine().analyze(SampleSource("confirmacao_alta").snapshot())
        moved.premove.move_in_atr = 1.8
        far, _ = opportunity_decay(moved, NOW, NOW)
        self.assertLess(far, fresh)

    def _cand(self, sym, scenario, hist, now=NOW, cap=None):
        spec = get_market(sym)
        s = SampleSource(scenario, now=now).snapshot()
        eng = GoldAIEngine(EngineConfig(factor_signs=dict(spec.factor_signs), symbol=sym))
        a, sig = eng.run_cycle(s)
        return Candidate(spec, a, sig, s, statistical_confidence(hist), cap, 0.9) if sig else None

    def test_rank_separates_history_from_now(self):
        strong_hist = [0.42 + (0.8 if i % 3 == 0 else -0.25) for i in range(300)]
        weak_hist = [0.05 + (0.5 if i % 2 else -0.4) for i in range(300)]
        c_gold = self._cand("XAUUSD", "premove_alta", weak_hist)
        c_wti = self._cand("WTI", "premove_alta", strong_hist)
        self.assertIsNotNone(c_gold)
        self.assertIsNotNone(c_wti)
        ranked = AssetSelector().rank([c_gold, c_wti], NOW)
        self.assertEqual(len(ranked), 2)
        self.assertTrue(all(0 <= c.opportunity_score <= 100 for c in ranked))
        self.assertTrue(all(set(c.components) == {"expectancy", "probability", "score", "premove", "regime", "capture", "execution", "data"} for c in ranked))
        txt = render_rank(ranked, {"EURUSD": statistical_confidence(strong_hist)})
        self.assertIn("MARKET OPPORTUNITY RANK", txt)
        self.assertIn("MELHOR OPORTUNIDADE", txt)
        self.assertIn("EURUSD", txt)   # excelente historicamente, sem oportunidade agora
        self.assertIn("sem oportunidade agora", txt)

    def test_selector_never_creates_entries(self):
        # cenário neutro → sem sinal → nenhum candidato possível; o selector só recebe o que o Prediction Engine produziu
        self.assertIsNone(self._cand("EURUSD", "neutro", []))


class ExposureTests(unittest.TestCase):
    def test_same_bet_three_times_is_blocked(self):
        pe = PortfolioExposureEngine(PortfolioLimits(max_total_open_risk_pct=1.5, max_correlated_risk_pct=1.0, max_positions=3))
        equity = 10000.0
        open_ = [OpenExposure("EURUSD", Direction.ALTA, 50.0), OpenExposure("XAUUSD", Direction.ALTA, 50.0)]
        # GBPUSD BUY correlaciona 0.75 com EURUSD BUY e 0.30 com XAUUSD BUY → risco correlacionado > 1 %
        reasons = pe.check("GBPUSD", Direction.ALTA, 50.0, open_, equity)
        self.assertTrue(any("correlacionado" in r for r in reasons))
        self.assertTrue(any("EURUSD" in r for r in reasons))
        # USDJPY BUY = aposta oposta (USD forte): correlação assinada negativa → não soma
        self.assertEqual(pe.correlated_risk("USDJPY", Direction.ALTA, 50.0, open_), 50.0)
        self.assertEqual(pe.check("USDJPY", Direction.ALTA, 50.0, open_, equity), [])
        # limites absolutos
        self.assertTrue(any("MAX_ASSET_EXPOSURE" in r for r in pe.check("EURUSD", Direction.BAIXA, 50.0, open_, equity)))
        self.assertTrue(any("MAX_PORTFOLIO_POSITIONS" in r for r in pe.check("WTI", Direction.ALTA, 10.0, open_ + [OpenExposure("US500", Direction.ALTA, 10.0)], equity)))
        self.assertTrue(any("MAX_TOTAL_OPEN_RISK" in r for r in pe.check("WTI", Direction.ALTA, 80.0, open_, equity)))
        self.assertIn("EXPOSIÇÃO", pe.render(open_, equity))
        lim = PortfolioLimits.from_env({"MAX_TOTAL_OPEN_RISK": "2", "MAX_PORTFOLIO_POSITIONS": "2"})
        self.assertEqual((lim.max_total_open_risk_pct, lim.max_positions), (2.0, 2))


class MultiDataTests(unittest.TestCase):
    def test_derive_snapshot_shares_macro_and_own_candles(self):
        base = SampleSource("premove_alta").snapshot()
        cs = {"H1": make_candles("H1", 260, 1.10, 0.0002, 0.001, base.time, seed=2), "M5": make_candles("M5", 260, 1.10, 0.00002, 0.0003, base.time, seed=3)}
        s = derive_market_snapshot(base, get_market("EURUSD"), cs, base.time)
        self.assertEqual(s.dxy_change_pct, base.dxy_change_pct)
        self.assertEqual(s.real_yield_change_bp, base.real_yield_change_bp)
        self.assertNotEqual(s.price, base.price)
        self.assertGreater(s.atr, 0)
        self.assertIsNone(s.cot_managed_money_net_change)   # COT do ouro não vaza para EURUSD
        self.assertIsNone(s.etf_flow_musd)
        q = data_quality(s, get_market("EURUSD"))
        self.assertTrue(0 < q <= 1)
        gold = derive_market_snapshot(base, get_market("XAUUSD"), base.candles, base.time)
        self.assertEqual(gold.cot_managed_money_net_change, base.cot_managed_money_net_change)


SCALE = {"EURUSD": 1 / 2400.0, "GBPUSD": 1 / 2000.0, "USDJPY": 1 / 18.0, "US500": 2.1, "WTI": 1 / 38.0}


def scale_snapshot(s, k: float):
    """Reescala preço/ATR/candles do cenário sintético (em escala de ouro) para a escala de outro mercado."""
    s.price, s.atr = s.price * k, s.atr * k
    for cs in s.candles.values():
        for c in cs:
            c.open, c.high, c.low, c.close = c.open * k, c.high * k, c.low * k, c.close * k
    return s


def build_snapset(scenarios: dict[str, str], now=NOW, price_map=None) -> MarketSnapshotSet:
    base = SampleSource(scenarios.get("XAUUSD", "neutro"), now=now).snapshot()
    ss = MarketSnapshotSet(now, base)
    for sym, sc in scenarios.items():
        s = SampleSource(sc, now=now, price=(price_map or {}).get(sym, 2650.0)).snapshot()
        s = scale_snapshot(s, SCALE.get(sym, 1.0))
        ss.by_symbol[sym] = s
        ss.data_quality[sym] = 0.9
        ss.status[sym] = "ok"
    return ss


class MarketEngineTests(unittest.TestCase):
    def _engine(self, db, symbols=("EURUSD", "XAUUSD", "USDJPY"), **kw):
        mem = PredictionMemory(db)
        return MarketAIEngine(mem, GuardLimits(), symbols, TradingMode.PAPER, 10000.0, PortfolioLimits(max_positions=2, max_correlated_risk_pct=0.5),
                              sender=TelegramSender(dry_run=True, quiet=True), log=lambda s: None, **kw), mem

    def test_one_cycle_one_entry_best_opportunity(self):
        with tempfile.TemporaryDirectory() as d:
            eng, mem = self._engine(os.path.join(d, "t.db"))
            # XAUUSD e EURUSD com o mesmo cenário forte de venda (dólar ↑): duas oportunidades; USDJPY sem sinal
            pc = eng.run_cycle(build_snapset({"EURUSD": "venda", "XAUUSD": "venda", "USDJPY": "neutro"}))
            self.assertGreaterEqual(len(pc.ranked), 1)
            self.assertIsNotNone(pc.chosen)
            self.assertIn(pc.chosen, ("EURUSD", "XAUUSD"))
            entered = [s for s, r in pc.results.items() if r.decision.startswith("🟢")]
            self.assertEqual(entered, [pc.chosen])                         # um ciclo, uma entrada
            others = [r.decision for s, r in pc.results.items() if s != pc.chosen and r.signal is not None]
            self.assertTrue(any("PRIORIDADE" in x for x in others))
            self.assertEqual(len(eng.open_exposures()), 1)
            self.assertIn("MARKET AI STATUS", eng.status_text())
            # decisões de todos os mercados registradas para o Opportunity Engine
            self.assertEqual(len(mem.decisions()), 3)
            self.assertEqual({d.action for d in mem.decisions() if d.action == "ENTRADA"}, {"ENTRADA"})
            # capital único compartilhado
            self.assertIs(eng.engines["EURUSD"].perf, eng.engines["XAUUSD"].perf)
            mem.close()

    def test_correlated_second_entry_blocked_by_exposure(self):
        with tempfile.TemporaryDirectory() as d:
            eng, mem = self._engine(os.path.join(d, "t.db"))
            eng.run_cycle(build_snapset({"EURUSD": "venda", "XAUUSD": "neutro", "USDJPY": "neutro"}))
            self.assertEqual(eng.open_exposures()[0].symbol, "EURUSD")
            # próximo ciclo: XAUUSD SELL = mesma aposta (USD forte) que EURUSD SELL → exposição correlacionada bloqueia
            pc = eng.run_cycle(build_snapset({"EURUSD": "venda", "XAUUSD": "venda", "USDJPY": "neutro"}, now=NOW + timedelta(minutes=30)))
            self.assertIsNone(pc.chosen)
            self.assertIn("exposição de carteira", pc.results["XAUUSD"].decision)
            self.assertEqual(len(eng.open_exposures()), 1)
            mem.close()

    def test_kill_switch_blocks_all_markets(self):
        with tempfile.TemporaryDirectory() as d:
            eng, mem = self._engine(os.path.join(d, "t.db"), kill_switch=KillSwitch(enabled_env=False))
            pc = eng.run_cycle(build_snapset({"EURUSD": "venda", "XAUUSD": "venda", "USDJPY": "venda"}))
            self.assertIsNone(pc.chosen)
            self.assertTrue(all("TRADING_ENABLED" in r.decision for r in pc.results.values() if r.signal))
            mem.close()


class MultiValidationTests(unittest.TestCase):
    def test_validate_markets_ranks_by_adjusted_expectancy(self):
        def frame(seed, drift):
            return HistoryFrame(xau=make_candles("H1", 700, 2500, drift, 6.0, NOW, seed=seed),
                                dxy=make_candles("H1", 700, 104, -0.002, 0.08, NOW, seed=seed + 1),
                                us10y=make_candles("H1", 700, 4.2, -0.0005, 0.02, NOW, seed=seed + 2))
        rows = validate_markets({"XAUUSD": frame(3, 0.4), "EURUSD": frame(7, 0.3)}, n_folds=2, step=6, warmup=230)
        self.assertEqual({r.symbol for r in rows}, {"XAUUSD", "EURUSD"})
        self.assertTrue(all(r.status in ("🟢", "🟡", "🔴") for r in rows))
        self.assertEqual(rows, sorted(rows, key=lambda m: -m.confidence.shrunk))
        txt = render_market_validation(rows)
        self.assertIn("VALIDAÇÃO MULTI-MERCADO", txt)
        self.assertIn("Conf.", txt)


if __name__ == "__main__":
    unittest.main()
