"""6.0 — LEADER PROPAGATION ENGINE: detector estatístico, lag map point-in-time, direção aprendida (mesma × oposta), alvo adaptativo,
edge líquido comprovado por sombras antes da entrada, TESTES A→E × INGÊNUA."""
from __future__ import annotations

import json
import os
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from gold_ai.leader_propagation import (STOP_ATR, LeaderPropagationEngine, PropagationConfig, Series, TestResult, propagation_session, quantile,
                                        relation_stats, simulate_propagation_trade, split_time)
from gold_ai.models import Candle
from gold_ai.reaction_hires import PricePath, Quote, save_candles

UTC = timezone.utc
T0 = datetime(2026, 1, 5, 0, 0, tzinfo=UTC)
SPREADS = {"XAUUSD": 0.30, "USDJPY": 0.012, "EURUSD": 0.00008, "US500": 0.4}


def synthetic(days: int = 45, seed: int = 7) -> tuple[dict[str, list[Candle]], list[tuple[int, int]]]:
    """XAUUSD líder com ~3 impulsos/dia (3 min, ±6σ); USDJPY segue na MESMA direção (40 %, 3 min depois); EURUSD segue na direção
    OPOSTA (30 %, 2 min depois); US500 é ruído. Só dias úteis."""
    rng = random.Random(seed)
    n = days * 24 * 60
    weekday = [(T0 + timedelta(minutes=i)).weekday() < 5 for i in range(n)]
    lead = [rng.gauss(0, 1.0) if weekday[i] else 0.0 for i in range(n)]
    imps = []
    for i in range(n):
        if weekday[i] and rng.random() < 3 / (24 * 60):
            sign = rng.choice((1, -1))
            for k in range(3):
                if i + k < n:
                    lead[i + k] += sign * 6.0
            imps.append((i, sign))

    def walk(p0: float, scale: float, rets: list[float]) -> list[Candle]:
        px, out = p0, []
        for i in range(n):
            if not weekday[i]:
                continue
            o = px
            px = px * (1 + rets[i] * scale)
            out.append(Candle(T0 + timedelta(minutes=i), o, max(o, px), min(o, px), px, 100))
        return out

    def follower(lag: int, coef: float) -> list[float]:
        r = [rng.gauss(0, 1.0) if weekday[i] else 0.0 for i in range(n)]
        for i0, sign in imps:
            for k in range(6):
                if i0 + lag + k < n:
                    r[i0 + lag + k] += coef * sign * 18.0 / 6
        return r

    data = {"XAUUSD": walk(2500.0, 0.0002, lead), "USDJPY": walk(147.0, 0.00008, follower(3, +0.4)),
            "EURUSD": walk(1.08, 0.00006, follower(2, -0.3)), "US500": walk(5000.0, 0.0001, [rng.gauss(0, 1.0) if weekday[i] else 0.0 for i in range(n)])}
    return data, imps


def engine(days: int = 45, **cfg) -> LeaderPropagationEngine:
    data, _ = synthetic(days)
    series = {s: Series(s, PricePath.from_candles(cs, SPREADS[s], 1)) for s, cs in data.items()}
    eng = LeaderPropagationEngine(series, PropagationConfig(**cfg), ["XAUUSD"])
    eng.detect()
    eng.run()
    return eng


class HelperTests(unittest.TestCase):
    def test_quantile_and_session(self):
        self.assertAlmostEqual(quantile([1, 2, 3, 4, 5], 0.5), 3.0)
        self.assertAlmostEqual(quantile([1, 2, 3, 4], 0.25), 1.75)
        self.assertEqual(quantile([], 0.5), 0.0)
        self.assertTrue(propagation_session(datetime(2026, 1, 5, 3, tzinfo=UTC)).startswith("Ásia"))
        self.assertTrue(propagation_session(datetime(2026, 1, 5, 14, tzinfo=UTC)).startswith("NY"))
        self.assertTrue(propagation_session(datetime(2026, 1, 5, 22, tzinfo=UTC)).startswith("fecho"))

    def test_series_volatility_only_looks_back_and_atr_uses_previous_hours(self):
        # 10 h planas (amplitude 1.0 por hora) e depois um salto: a σ no instante do salto ainda é a das variações anteriores
        cs = []
        for i in range(600):
            t = T0 + timedelta(minutes=i)
            px = 100.0 + (0.5 if i % 2 else -0.5)
            if i >= 590:
                px = 110.0
            cs.append(Candle(t, px, px, px, px, 0))
        s = Series("X", PricePath.from_candles(cs, 0.0, 1), impulse_min=1, lookback_hours=24, min_samples=30)
        i = s.index_at_or_before(T0 + timedelta(minutes=591))                 # fecho da barra 590 (carimbo +1 min)
        self.assertIsNotNone(s.z_at(i))
        self.assertGreater(abs(s.z_at(i)), 5.0)                                 # salto de ~10 contra σ ≈ 1
        self.assertAlmostEqual(s.atr_at(T0 + timedelta(hours=5, minutes=10)), 1.0, places=6)   # horas cheias anteriores: amplitude 1.0
        self.assertEqual(s.atr_at(T0 + timedelta(minutes=10)), 0.0)                             # sem hora cheia anterior


class SimulateTradeTests(unittest.TestCase):
    def _path(self, mids, spread=0.2, start=T0):
        return PricePath([Quote(start + timedelta(minutes=i), m - spread / 2, m + spread / 2) for i, m in enumerate(mids)], 60.0)

    def test_take_stop_and_timeout(self):
        atr = 10.0
        up = self._path([100, 100.5, 101, 103, 106, 108, 109, 110, 111, 112])
        tr = simulate_propagation_trade(up, atr, T0 + timedelta(seconds=30), +1.0, 0.5, 30.0, 0.0, 0.0, T0 - timedelta(minutes=1))
        self.assertEqual(tr.exit_reason, "take")
        self.assertAlmostEqual(tr.net_atr, 0.5, places=6)                            # entra no ask 100,1; sai exatamente no take (entrada + 0,5 ATR)
        self.assertGreater(tr.follow_atr, tr.net_atr)                                  # sem take teria ido mais longe
        down = self._path([100, 99, 97, 94, 92, 90])
        tr = simulate_propagation_trade(down, atr, T0 + timedelta(seconds=30), +1.0, 0.5, 30.0, 0.0, 0.0, T0 - timedelta(minutes=1))
        self.assertEqual(tr.exit_reason, "stop")
        self.assertAlmostEqual(tr.net_atr, -STOP_ATR, places=6)
        self.assertAlmostEqual(tr.follow_atr, -STOP_ATR, places=6)
        flat = self._path([100, 100.2, 100.1, 100.3, 100.2, 100.4, 100.3])
        tr = simulate_propagation_trade(flat, atr, T0 + timedelta(seconds=30), -1.0, 0.5, 3.0, 0.0, 0.0, T0 - timedelta(minutes=1))
        self.assertEqual(tr.exit_reason, "tempo")
        self.assertAlmostEqual(tr.minutes, 3.5, places=3)               # entrada carimbada na cotação usada (T0), limite 3 min após t_in (T0+30s)
        self.assertAlmostEqual(tr.cost_atr, 0.2 / atr, places=9)             # spread pago; slippage e comissão zero
        self.assertIsNone(simulate_propagation_trade(flat, atr, T0 + timedelta(seconds=30), 1.0, 0.5, 5.0, 0.0, 0.0, T0 + timedelta(minutes=5)))  # cotação velha → sem entrada


class RelationStatsTests(unittest.TestCase):
    def test_direction_is_learned_and_shrunk(self):
        from gold_ai.leader_propagation import PropOutcome
        key = ("XAUUSD", 1.0, "EURUSD")
        outs = [PropOutcome("XAUUSD", 1.0, "EURUSD", T0, T0, 1.0, 0.1, max_up=0.2, max_down=0.6, end_eval=-0.4, first_down_min=3.0) for _ in range(9)]
        outs.append(PropOutcome("XAUUSD", 1.0, "EURUSD", T0, T0, 1.0, 0.1, max_up=0.5, max_down=0.1, end_eval=0.3, first_up_min=2.0))
        st = relation_stats(key, outs, [0.1, -0.05, 0.2], 0.35)
        self.assertEqual(st.sign, -1.0)                                           # 9 em 10 foram contra o líder → direção OPOSTA
        self.assertEqual(st.relation, "oposta")
        self.assertAlmostEqual(st.p_opp, 0.9)
        self.assertLess(st.p, 0.9)                                                # encolhida (n=10 → 0,5 + 0,4 × 10/20 = 0,70)
        self.assertAlmostEqual(st.p, 0.70, places=6)
        self.assertAlmostEqual(st.react_med, 3.0)
        self.assertAlmostEqual(st.continuation, 0.9)
        self.assertAlmostEqual(st.mfe_med, 0.6)
        self.assertEqual(st.shadow_n, 3)
        self.assertAlmostEqual(st.shadow_net, 0.25 / 3)
        self.assertAlmostEqual(st.take_atr(1.0), max(0.15, st.ratio_q))
        self.assertAlmostEqual(st.timeout_min(60), 6.0)                          # 2 × mediana da reação, mínimo 5
        self.assertEqual(relation_stats(key, [], [], 0.35).n, 0)


class EngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = engine()

    def test_detector_finds_planted_impulses_with_statistical_threshold(self):
        imps = self.eng.impulses
        self.assertGreater(len(imps), 60)
        self.assertTrue(all(i.leader == "XAUUSD" and abs(i.z) >= 3.0 and i.move_atr >= 0.25 for i in imps))
        self.assertTrue(all(i.session in ("Ásia 00–07", "Londres 07–13", "NY 13–21", "fecho 21–24") for i in imps))
        times = [i.time for i in imps]
        self.assertTrue(all((b - a) >= timedelta(minutes=60) for a, b in zip(times, times[1:])))     # cooldown
        self.assertEqual(self.eng.leader_board["XAUUSD"]["impulsos"], len(imps))

    def test_lag_map_learns_same_and_opposite_direction_and_rejects_noise(self):
        st = {(s.leader, s.direction, s.target): s for s in self.eng.final_stats()}
        for d in (1.0, -1.0):
            self.assertEqual(st[("XAUUSD", d, "USDJPY")].sign, d)                    # mesma direção
            self.assertEqual(st[("XAUUSD", d, "EURUSD")].sign, -d)                   # oposta
            self.assertGreater(st[("XAUUSD", d, "USDJPY")].p, 0.6)
            self.assertGreater(st[("XAUUSD", d, "EURUSD")].p, 0.6)
            self.assertGreater(st[("XAUUSD", d, "USDJPY")].shadow_net, 0.0)           # edge líquido nas sombras
            self.assertLess(st[("XAUUSD", d, "US500")].p, 0.6)                        # ruído: P encolhida perto de 50 %
            self.assertLessEqual(st[("XAUUSD", d, "US500")].shadow_net, 0.0)          # e custo consome
            self.assertIsNotNone(st[("XAUUSD", d, "USDJPY")].react_med)
            self.assertLessEqual(st[("XAUUSD", d, "USDJPY")].react_med, 12.0)

    def test_outcomes_and_shadows_are_point_in_time(self):
        for key, outs in self.eng.outcomes.items():
            for o in outs:
                self.assertEqual(o.known_at, o.time + timedelta(minutes=self.eng.cfg.horizon_min))
        for key, sh in self.eng.shadows.items():
            self.assertTrue(all(isinstance(v, float) for _, v in sh))
        # a estatística disponível num impulso nunca inclui a medição desse impulso
        imp = self.eng.impulses[len(self.eng.impulses) // 2]
        st_then = self.eng.stats_for(("XAUUSD", imp.direction, "USDJPY"), imp.time)
        st_end = self.eng.stats_for(("XAUUSD", imp.direction, "USDJPY"), imp.time + timedelta(days=365))
        self.assertLess(st_then.n, st_end.n)
        self.assertTrue(all(o.known_at <= imp.time for o in self.eng.outcomes[("XAUUSD", imp.direction, "USDJPY")][:st_then.n]))

    def test_tests_b_to_e_beat_a_and_naive_and_only_enter_proven_targets(self):
        r = self.eng.results
        for name in "BCDE":
            self.assertGreaterEqual(r[name].n, 20, name)
            self.assertGreater(r[name].expectancy, 0.0, name)
            self.assertGreater(r[name].net_r, r["A"].net_r)
            self.assertGreater(r[name].expectancy, self.eng.naive.expectancy)
            self.assertTrue(all(t.target in ("USDJPY", "EURUSD") for t in r[name].trades), name)   # US500 nunca passa no edge
            self.assertTrue(all(t.p >= self.eng.cfg.p_min and t.edge_atr > 0 for t in r[name].trades))
            self.assertGreaterEqual(r[name].peak_concurrent, 1)
        self.assertLessEqual(r["B"].n, r["C"].n)
        self.assertEqual(r["C"].n, r["E"].n)                                     # só há 2 alvos com edge
        # um alvo nunca tem duas operações abertas ao mesmo tempo dentro do mesmo teste
        for name in "BCDE":
            by_t: dict = {}
            for t in sorted(r[name].trades, key=lambda x: x.time):
                last = by_t.get(t.target)
                self.assertTrue(last is None or t.time >= last, name)
                by_t[t.target] = t.exit_time

    def test_metrics_halves_equity_and_json(self):
        e = self.eng.results["E"]
        split = split_time(self.eng.impulses)
        a, b = e.halves(split)
        self.assertEqual(a.n + b.n, e.n)
        self.assertAlmostEqual(a.net_r + b.net_r, e.net_r, places=6)
        self.assertAlmostEqual(e.cost_r, sum(t.cost_r for t in e.trades), places=6)
        self.assertGreater(e.end_equity, e.equity)
        self.assertGreaterEqual(e.max_dd_pct, 0.0)
        self.assertGreater(e.avg_minutes, 0.0)
        self.assertEqual(set(e.by(lambda t: t.target)) <= {"USDJPY", "EURUSD"}, True)
        text = self.eng.render(split)
        for piece in ("LEADER PROPAGATION ENGINE 6.0", "LEADER BOARD", "LAG MAP", "TESTES A → E", "INGÊNUA", "por alvo"):
            self.assertIn(piece, text)
        js = self.eng.to_json(split)
        self.assertEqual(js["version"], "6.0")
        self.assertEqual(len(js["impulses"]), len(self.eng.impulses))
        self.assertEqual(js["tests"]["E"]["n"], e.n)
        self.assertIn("first_half", js["tests"]["E"])
        json.dumps(js, default=str)

    def test_empty_result_rows_do_not_break(self):
        r = TestResult("A", "vazio")
        r.run_equity()
        self.assertEqual(r.n, 0)
        self.assertIn("⚪", r.row())
        self.assertIsNone(r.pf)


class CliTests(unittest.TestCase):
    def test_propagation_command_reads_m1_csv_and_writes_report(self):
        from gold_ai.cli import main
        data, _ = synthetic(30, seed=3)
        with tempfile.TemporaryDirectory() as d:
            for sym, cs in data.items():
                save_candles(cs, os.path.join(d, f"{sym}_m1.csv"))
            out, js = os.path.join(d, "propagacao.txt"), os.path.join(d, "propagacao.json")
            rc = main(["propagation", "--markets", "XAUUSD,USDJPY,EURUSD,US500", "--leaders", "XAUUSD", "--csv-dir", d, "--start", "2026-01-05",
                       "--end", "2026-02-05", "--events", os.path.join(d, "nao_existe.csv"), "--out", out, "--json", js])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(out) and os.path.exists(js))
            with open(js, encoding="utf-8") as f:
                data_js = json.load(f)
            self.assertEqual(data_js["engine"], "LEADER_PROPAGATION_ENGINE")
            self.assertIn("B", data_js["tests"])
            with open(out, encoding="utf-8") as f:
                self.assertIn("TESTES A → E", f.read())

    def test_propagation_needs_two_markets(self):
        from gold_ai.cli import main
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(main(["propagation", "--markets", "XAUUSD,USDJPY", "--csv-dir", d, "--events", ""]), 1)


if __name__ == "__main__":
    unittest.main()
