"""LEADER PROPAGATION: detector de impulsos, resposta do alvo, aprendizado líder→alvo e teste B…E."""
import random
import unittest
from datetime import datetime, timedelta, timezone

from gold_ai.models import Candle
from gold_ai.propagation import MinuteSeries, detect_impulses, follow_trade, learn_pair, measure_response, run_propagation

T0 = datetime(2026, 1, 5, 0, 0, tzinfo=timezone.utc)


def series(n_min: int, impulses: dict, follow_lag: int = 0, follow_frac: float = 0.0, vol: float = 1.0, seed: int = 1, price: float = 1000.0):
    """Passeio aleatório de 1 min. `impulses`: minuto → salto (em preço) no LÍDER; com follow_lag/frac o mesmo salto aparece
    `follow_lag` minutos depois com fração `follow_frac` (alvo que segue)."""
    rnd = random.Random(seed)
    out, p = [], price
    jumps = {}
    for m, j in impulses.items():
        jumps[m + follow_lag] = jumps.get(m + follow_lag, 0.0) + j * (follow_frac if follow_frac else 1.0)
    for i in range(n_min):
        p += rnd.gauss(0, vol) + jumps.get(i, 0.0)
        out.append(Candle(T0 + timedelta(minutes=i), p, p + 0.3 * vol, p - 0.3 * vol, p, 1.0))
    return out


class DetectorTests(unittest.TestCase):
    def test_detects_only_statistical_impulses(self):
        imps = {2000: +40.0, 5000: -40.0, 8000: +2.0}          # dois impulsos reais (40σ) e um ruído
        s = MinuteSeries("XAUUSD", series(10000, imps))
        found = detect_impulses(s, z_min=4.0)
        mins = [i.minute - s.t[0] for i in found]
        self.assertTrue(any(1995 <= m <= 2006 for m in mins))
        self.assertTrue(any(4995 <= m <= 5006 for m in mins))
        self.assertFalse(any(7995 <= m <= 8006 for m in mins))
        self.assertEqual({i.direction for i in found if 1995 <= i.minute - s.t[0] <= 2006}, {1})
        self.assertTrue(all(i.z >= 4.0 for i in found))

    def test_news_flag_and_cooldown(self):
        imps = {2000: +40.0, 2010: +40.0}
        s = MinuteSeries("XAUUSD", series(4000, imps))
        found = detect_impulses(s, z_min=4.0, cooldown_min=60, news_minutes=[s.t[0] + 1990])
        self.assertEqual(len([i for i in found if 1995 <= i.minute - s.t[0] <= 2020]), 1)   # cooldown: um só
        self.assertTrue(found[0].with_news)


class ResponseAndLearningTests(unittest.TestCase):
    def test_follower_is_learned_and_follow_trade_pays(self):
        imps = {m: (+40.0 if k % 2 == 0 else -40.0) for k, m in enumerate(range(1500, 30000, 300))}
        leader = MinuteSeries("XAUUSD", series(31000, imps, seed=1))
        follower = MinuteSeries("US500", series(31000, imps, follow_lag=3, follow_frac=0.5, seed=2))
        random_tgt = MinuteSeries("WTI", series(31000, {}, seed=3))
        found = detect_impulses(leader, z_min=4.0)
        self.assertGreater(len(found), 50)
        resp_f = [r for r in (measure_response(i, follower) for i in found) if r]
        resp_r = [r for r in (measure_response(i, random_tgt) for i in found) if r]
        st_f = learn_pair(resp_f, "XAUUSD", "US500", "todas")
        st_r = learn_pair(resp_r, "XAUUSD", "WTI", "todas")
        self.assertGreater(st_f.p_same_30, 0.85)
        self.assertEqual(st_f.sign, 1)
        self.assertGreater(st_f.frac30, 0.2)
        self.assertIsNotNone(st_f.ttr_med)
        self.assertLessEqual(st_f.ttr_med, 5)
        self.assertLess(abs(st_r.p_same_30 - 0.5), 0.2)
        trades = [t for t in (follow_trade(i, follower, st_f, spread=0.1) for i in found) if t]
        self.assertGreater(sum(t.r for t in trades) / len(trades), 0.3)

    def test_inverse_relation_is_learned_as_contra(self):
        imps = {m: +40.0 for m in range(1500, 30000, 300)}
        leader = MinuteSeries("USDX", series(31000, imps, seed=4))
        inverse = MinuteSeries("XAUUSD", series(31000, imps, follow_lag=2, follow_frac=-0.6, seed=5))
        found = detect_impulses(leader, z_min=4.0)
        st = learn_pair([r for r in (measure_response(i, inverse) for i in found) if r], "USDX", "XAUUSD", "todas")
        self.assertLess(st.p_same_30, 0.15)
        self.assertEqual(st.sign, -1)
        self.assertGreater(st.frac30, 0.0)


class EndToEndTests(unittest.TestCase):
    def test_run_propagation_reports_edge_only_where_it_exists(self):
        imps = {m: (+40.0 if k % 3 else -40.0) for k, m in enumerate(range(1500, 40000, 250))}
        series_ = {"XAUUSD": MinuteSeries("XAUUSD", series(41000, imps, seed=1)),
                   "US500": MinuteSeries("US500", series(41000, imps, follow_lag=3, follow_frac=0.5, seed=2)),
                   "WTI": MinuteSeries("WTI", series(41000, {}, seed=3))}
        rep = run_propagation(series_, ["XAUUSD"], ["US500", "WTI"], {"US500": 0.1, "WTI": 0.1, "XAUUSD": 0.1}, z_min=4.0)
        txt = rep.render()
        self.assertIn("LAG MAP", txt)
        edge = {(s.leader, s.target) for s in rep.pairs if s.is_edge and s.context == "todas"}
        self.assertIn(("XAUUSD", "US500"), edge)
        self.assertNotIn(("XAUUSD", "WTI"), edge)
        k1 = rep.k_results[0]
        self.assertGreater(k1.n, 20)
        self.assertGreater(k1.e, 0.3)
        self.assertEqual(rep.k_results[1].n, k1.n)                 # só um alvo com edge: k=2 não acrescenta nada
        self.assertIn("US500", json_str(rep))


def json_str(rep) -> str:
    import json
    return json.dumps(rep.to_json())


if __name__ == "__main__":
    unittest.main()
