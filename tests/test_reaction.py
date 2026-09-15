"""Testes — REACTION ENGINE: cronômetro do evento, estatística point-in-time e relógio de reação."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone

from gold_ai.evaluation import HistoryFrame
from gold_ai.history import EventHistory, HistoricalEvent, apply_rule_effects
from gold_ai.models import MarketSnapshot
from gold_ai.news_engine import IdentifiedEvent
from gold_ai.reaction import FIRST_ATR, ReactionClock, ReactionRecord, ReactionStats, measure_reaction, records_from_history
from gold_ai.sources.sample import make_candles

UTC = timezone.utc
T0 = datetime(2026, 3, 11, 12, 30, tzinfo=UTC)


def path(moves, step_min=5, start=T0, p0=2500.0):
    """série (t, preço): moves em ATR (ATR = 10) por passo, começando 1 passo ANTES do evento."""
    out = [(start - timedelta(minutes=step_min), p0), (start, p0)]
    p = p0
    for k, m in enumerate(moves, 1):
        p = p0 + m * 10.0
        out.append((start + timedelta(minutes=step_min * k), p))
    return out


class MeasureTests(unittest.TestCase):
    def test_times_mfe_mae_and_leads(self):
        # alvo: só reage a partir de 20 min (0,2 ATR), confirma aos 35 (0,5), pico 0,8 aos 45, depois recua
        target = path([0.0, 0.05, 0.1, 0.2, 0.3, 0.3, 0.5, 0.6, 0.8, 0.6, 0.4, -0.1])
        usd = [(T0 - timedelta(minutes=5), 104.0), (T0, 104.0), (T0 + timedelta(minutes=5), 104.02), (T0 + timedelta(minutes=10), 104.12), (T0 + timedelta(minutes=30), 104.2)]
        yld = [(T0, 4.20), (T0 + timedelta(minutes=15), 4.21), (T0 + timedelta(minutes=20), 4.24)]
        rec = measure_reaction("CPI1", "cpi", T0, "USDJPY", +1.0, target, 10.0, {"USD": usd, "YIELD": yld}, {"USD": +1.0, "YIELD": +1.0}, 240, 5)
        self.assertEqual(rec.time_to_first, 20.0)
        self.assertEqual(rec.time_to_confirmation, 35.0)
        self.assertEqual(rec.time_to_full_move, 45.0)
        self.assertAlmostEqual(rec.max_move_atr, 0.8)
        self.assertAlmostEqual(rec.max_adverse_atr, 0.1)
        self.assertTrue(rec.direction_correct)
        self.assertEqual(rec.lead_times["USD"], 10.0)       # +0,115% ≥ 0,08%
        self.assertEqual(rec.lead_times["YIELD"], 20.0)     # +4 bp ≥ 1,5 bp
        self.assertEqual(rec.known_at, T0 + timedelta(minutes=240))

    def test_no_reaction_and_wrong_direction(self):
        rec = measure_reaction("X", "cpi", T0, "XAUUSD", -1.0, path([0.05, 0.1, 0.05, 0.0]), 10.0, horizon_min=60, resolution_min=5)
        self.assertIsNone(rec.time_to_first)
        self.assertFalse(rec.direction_correct)
        self.assertAlmostEqual(rec.max_adverse_atr, 0.1)
        self.assertIsNone(measure_reaction("X", "cpi", T0, "XAUUSD", 0.0, path([0.5]), 10.0))
        self.assertIsNone(measure_reaction("X", "cpi", T0, "XAUUSD", 1.0, path([0.5]), 0.0))


def rec(kind, t, first, conf=None, full=None, correct=True, target="XAUUSD", mfe=0.5):
    return ReactionRecord(f"{kind}{t:%m%d}", kind, t, target, 1.0, first, conf, full, mfe, 0.1, correct, {"USD": 5.0, "YIELD": 10.0}, 240, 5)


class StatsTests(unittest.TestCase):
    def test_point_in_time_only_concluded_events(self):
        recs = [rec("cpi", T0 + timedelta(days=30 * k), 15.0 + 5 * k, 30.0 + 5 * k, 60.0) for k in range(4)]
        st = ReactionStats(recs)
        self.assertIsNone(st.get("cpi", "XAUUSD", T0 + timedelta(minutes=100)))            # o 1º ainda não concluiu
        self.assertEqual(st.get("cpi", "XAUUSD", T0 + timedelta(minutes=240)).n, 1)
        k = st.get("cpi", "XAUUSD", T0 + timedelta(days=61))
        self.assertEqual(k.n, 3)
        self.assertEqual(k.median_first, 20.0)
        self.assertEqual(k.median_lead["USD"], 5.0)
        self.assertEqual(st.at(T0 - timedelta(days=1)), {})
        self.assertIn("cpi", st.render())
        self.assertIn("sem eventos", ReactionStats().render())


def snap(price_change_pct, dxy_change, y_change, price=2500.0, atr=10.0):
    s = MarketSnapshot(time=T0, price=price)
    s.atr, s.price_change_pct, s.dxy_change_pct, s.us10y_change_bp = atr, price_change_pct, dxy_change, y_change
    return s


class ClockTests(unittest.TestCase):
    def _stats(self):
        return ReactionStats([rec("cpi", T0 - timedelta(days=30 * k), 20.0, 35.0, 60.0) for k in range(1, 7)])

    def _event(self, age_min):
        return [IdentifiedEvent("cpi", "CPI MoM", T0 - timedelta(minutes=age_min), 1.0, 0.2, 0.4, 2.0, +1.0)]

    def test_latent_pressure_when_leaders_moved_and_target_did_not(self):
        # CPI acima → USD ↑, yields ↑ esperados; ouro ↓ esperado. USD e yield já reagiram, ouro parado, T+8 vs mediana 20
        clock = ReactionClock(self._stats())
        ra = clock.assess("XAUUSD", snap(0.0, +0.15, +3.0), self._event(8), T0)
        self.assertEqual(ra.status, "PRESSÃO LATENTE")
        self.assertLess(ra.pressure, 0)                      # pressão vendedora no ouro
        self.assertEqual(ra.lead, {"USD": "✓ reagiu", "YIELD": "✓ reagiu"})
        self.assertEqual(ra.expected_min, 20.0)
        self.assertEqual(ra.n_history, 6)
        self.assertGreater(ra.probability, 0.5)
        self.assertIn("assimetria temporal", ra.chain)
        self.assertIn("T+8 min", ra.chain)

    def test_states(self):
        clock = ReactionClock(self._stats())
        self.assertEqual(clock.assess("XAUUSD", snap(0.0, 0.0, 0.0), self._event(5), T0).status, "AGUARDANDO")
        self.assertEqual(clock.assess("XAUUSD", snap(-0.25, +0.15, +3.0), self._event(30), T0).status, "REAGIU")     # −0,25% × 2500 / 10 = −0,62 ATR na direção
        self.assertEqual(clock.assess("XAUUSD", snap(+0.1, +0.15, +3.0), self._event(30), T0).status, "DIVERGÊNCIA")  # ouro sobe contra a notícia
        self.assertEqual(clock.assess("XAUUSD", snap(0.0, +0.15, +3.0), self._event(200), T0).status, "EXPIRADO")     # > 2 × mediana do pleno (120)
        self.assertEqual(clock.assess("XAUUSD", snap(0.0, 0.0, 0.0), [], T0).status, "SEM EVENTO")
        ra = clock.assess("XAUUSD", snap(0.0, -0.15, -3.0), self._event(10), T0)                                       # líderes contra
        self.assertEqual(ra.status, "DIVERGÊNCIA")
        self.assertEqual(ra.pressure, 0.0)

    def test_target_move_measured_since_event_from_past_candles_only(self):
        from gold_ai.models import Candle
        clock = ReactionClock(self._stats())
        ev = self._event(90)
        t_ev = ev[0].time
        # candles H1: fechou 2500 no candle do evento; agora 2495 (−0,5 ATR na direção esperada ↓) embora a última hora tenha sido +0,1%
        cs = [Candle(t_ev - timedelta(hours=2), 2500, 2500, 2500, 2500, 0), Candle(t_ev, 2500, 2500, 2500, 2500, 0), Candle(t_ev + timedelta(hours=1), 2498, 2498, 2490, 2495, 0)]
        s = snap(+0.1, +0.15, +3.0, price=2495.0)
        s.candles = {"H1": cs}
        ra = clock.assess("XAUUSD", s, ev, T0)
        self.assertEqual(ra.status, "REAGIU")                          # movimento desde o evento, não da última hora
        self.assertIn("+0.50 ATR", ra.chain)                            # +0,50 ATR NA DIREÇÃO ESPERADA (queda)
        future = [Candle(t_ev + timedelta(hours=3), 2400, 2400, 2400, 2400, 0)]   # candle futuro nunca é o p0
        s.candles = {"H1": cs + future}
        self.assertEqual(clock.assess("XAUUSD", s, ev, T0).status, "REAGIU")

    def test_frame_stats_cache_resets_when_history_changes(self):
        end = datetime(2026, 9, 14, 13, 0, tzinfo=UTC)
        frame = HistoryFrame(xau=make_candles("H1", 700, 2500, 0.4, 6.0, end, seed=3))
        t = frame.xau[300].time
        h1 = EventHistory([HistoricalEvent(t, t, "A", "CPI MoM", forecast=0.2, actual=0.4, kind="cpi")])
        h2 = EventHistory([HistoricalEvent(t, t, "A", "CPI MoM", forecast=0.2, actual=0.4, kind="cpi"), HistoricalEvent(t, t, "B", "NFP", forecast=100, actual=200, kind="nfp")])
        frame.symbol, frame.events = "XAUUSD", h1
        self.assertEqual(len(frame.reaction_stats().records), 1)
        frame.events = h2
        self.assertEqual(len(frame.reaction_stats().records), 2)
        frame.events = None
        self.assertEqual(len(frame.reaction_stats().records), 0)

    def test_without_history_uses_default_window_and_says_so(self):
        ra = ReactionClock(ReactionStats()).assess("XAUUSD", snap(0.0, +0.15, +3.0), self._event(8), T0)
        self.assertEqual(ra.status, "PRESSÃO LATENTE")
        self.assertIsNone(ra.expected_min)
        self.assertIn("sem histórico suficiente", ra.chain)
        self.assertAlmostEqual(ra.probability, 0.5 + 0.16 + 0.05, places=2)   # sem histórico: base 0,5 + evidência atual


class FrameIntegrationTests(unittest.TestCase):
    def test_backtest_frame_learns_only_from_past_events(self):
        end = datetime(2026, 9, 14, 13, 0, tzinfo=UTC)
        frame = HistoryFrame(xau=make_candles("H1", 900, 2500, 0.4, 6.0, end, seed=3), dxy=make_candles("H1", 900, 104, -0.002, 0.08, end, seed=4),
                             us10y=make_candles("H1", 900, 4.2, -0.0005, 0.02, end, seed=5))
        evs = []
        for k, i in enumerate(range(250, len(frame.xau) - 10, 60)):
            t = frame.xau[i].time
            evs.append(HistoricalEvent(t, t, f"CPI_{k}", "CPI MoM", forecast=0.2, previous=0.3, actual=0.4, kind="cpi", impact="MUITO ALTO"))
        hist = EventHistory(evs)
        apply_rule_effects(hist)
        frame.symbol, frame.events = "XAUUSD", hist
        st = frame.reaction_stats()
        self.assertEqual(len(st.records), len(evs))
        self.assertTrue(all(r.resolution_min == 60 for r in st.records))
        first = evs[0].timestamp
        self.assertEqual(st.at(first + timedelta(minutes=239)), {})                        # nada visível antes de concluir o 1º
        self.assertEqual(st.get("cpi", "XAUUSD", first + timedelta(minutes=240)).n, 1)
        s = frame.snapshot_at(250)                                                          # instante do 1º CPI
        self.assertIn(s.reaction_status, ("AGUARDANDO", "PRESSÃO LATENTE", "DIVERGÊNCIA", "REAGIU"))
        self.assertIn("sem histórico suficiente", s.reaction_chain)                         # ainda não aprendeu nada
        s_late = frame.snapshot_at(250 + 60 * 6)
        self.assertIn("histórico cpi→XAUUSD", s_late.reaction_chain)                         # já aprendeu com os 6 anteriores (mínimo 5)
        self.assertNotIn("sem histórico", s_late.reaction_chain)
        self.assertIn("REACTION CLOCK", s_late.reaction_chain)


if __name__ == "__main__":
    unittest.main()


class LiveIntegrationTests(unittest.TestCase):
    def test_live_engine_clocks_events_and_learns_after_horizon(self):
        import os
        import tempfile
        from gold_ai.guard import GuardLimits, KillSwitch, TradingMode
        from gold_ai.market_engine import MarketAIEngine
        from gold_ai.memory import PredictionMemory
        from gold_ai.selector import PortfolioLimits
        from gold_ai.telegram import TelegramSender
        from tests.test_market40 import build_snapset

        with tempfile.TemporaryDirectory() as d:
            mem = PredictionMemory(os.path.join(d, "m.db"))
            eng = MarketAIEngine(mem, GuardLimits(), ("XAUUSD", "USDJPY"), TradingMode.PAPER, 10000.0, PortfolioLimits(), sender=TelegramSender(dry_run=True, quiet=True),
                                 kill_switch=KillSwitch(enabled_env=False), log=lambda m: None, horizon_min=60)
            t_ev = T0
            ev = IdentifiedEvent("cpi", "CPI MoM", t_ev, 1.0, 0.2, 0.4, 2.0, +1.0)
            # ciclos de 10 em 10 min: USD/yields sobem, ouro parado → PRESSÃO LATENTE; depois o horizonte (60 min) fecha e a reação é medida
            for k in range(8):
                now = t_ev + timedelta(minutes=10 * k)
                ss = build_snapset({"XAUUSD": "neutro", "USDJPY": "neutro"}, now=now)
                ss.identified = [ev]
                ss.base.dxy = 104.0 + 0.03 * k
                ss.base.us10y = 4.20 + 0.01 * k
                for s in ss.by_symbol.values():
                    s.dxy_change_pct, s.us10y_change_bp, s.price_change_pct = 0.03 * k, 1.0 * k, 0.0
                    s.candles = {}                                    # sem candles: o relógio usa a variação da janela (0 = parado)
                eng.run_cycle(ss)
                if k == 2:
                    self.assertEqual(ss.by_symbol["XAUUSD"].reaction_status, "PRESSÃO LATENTE")
                    self.assertLess(ss.by_symbol["XAUUSD"].reaction_pressure, 0)
                    self.assertGreater(ss.by_symbol["USDJPY"].reaction_pressure, 0)
            recs = mem.reaction_records()
            self.assertEqual({r.target for r in recs}, {"XAUUSD", "USDJPY"})
            self.assertEqual(recs[0].kind, "cpi")
            self.assertIsNotNone(recs[0].lead_times["USD"])          # líderes medidos pelas amostras dos ciclos
            self.assertEqual(eng.pending_reactions, {})
            self.assertEqual(len(eng.reaction_stats.records), 2)
            mem.close()
