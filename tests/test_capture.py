"""5.2 — níveis WATCH→SETUP→OPPORTUNITY→EXECUTION, funil de captura, Exit Lab e Edge Bank."""
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

UTC = timezone.utc
T0 = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)


def _dec(t, score, direction, action="SEM_VANTAGEM", level="WATCH", stage="SCORE_MIN", **ctx):
    from gold_ai.opportunity import DecisionRecord
    d = DecisionRecord(t, 100.0, score, direction, action, "", 1.0, None, 0, 0.0)
    d.level, d.stage = level, stage
    for k, v in ctx.items():
        setattr(d, k, v)
    return d


class LevelTests(unittest.TestCase):
    def test_levels_map_to_engine_gates(self):
        from gold_ai.models import Direction
        from gold_ai.opportunity import opportunity_level
        a = SimpleNamespace(direction=Direction.ALTA, premove=SimpleNamespace(direction=Direction.ALTA), score=20.0, has_edge=False, technical=())
        self.assertEqual(opportunity_level(a, None, False), "WATCH")
        a.has_edge = True
        self.assertEqual(opportunity_level(a, None, False), "SETUP")
        sig = SimpleNamespace(direction=Direction.ALTA)
        self.assertEqual(opportunity_level(a, sig, False), "OPPORTUNITY")
        self.assertEqual(opportunity_level(a, sig, True), "EXECUTION")
        a.score = 5.0
        self.assertEqual(opportunity_level(a, None, False), "NONE")
        a.direction = a.premove.direction = Direction.LATERAL
        a.score = 40.0
        self.assertEqual(opportunity_level(a, None, False), "NONE")

    def test_vwap_band_and_context_tags(self):
        from gold_ai.opportunity import DecisionRecord, vwap_band
        a = SimpleNamespace(technical=[SimpleNamespace(timeframe="H1", vwap_position=-1.2), SimpleNamespace(timeframe="H4", vwap_position=0.1)])
        self.assertEqual(vwap_band(a), "B3")
        d = DecisionRecord(T0, 1.0, 30.0, "ALTA", "ENTRADA")
        d.regime, d.setup, d.event_kind, d.reaction, d.flow = "RANGE", "B3", "cpi", "PRESSÃO LATENTE", "SEM ANOMALIA"
        self.assertEqual(d.context_tags, ["regime:RANGE", "vwap:RANGE·B3", "evento:cpi", "relogio:PRESSÃO LATENTE"])


class CaptureFunnelTests(unittest.TestCase):
    def _prices(self):
        # três movimentos de alta relevantes (≥ 2.0) em janelas distintas; preço plano entre eles
        pts, p = [], 100.0
        t = T0
        for i in range(120):
            if i in (10, 50, 90):
                p += 3.0
            pts.append((t, p))
            t += timedelta(minutes=60)
        return pts

    def test_best_level_before_evident_and_where_lost(self):
        from gold_ai.opportunity import capture_funnel
        prices = self._prices()
        decs = [
            _dec(T0 + timedelta(hours=9), 30, "ALTA", "ENTRADA", "EXECUTION", None),           # captura o 1º movimento
            _dec(T0 + timedelta(hours=48), 20, "ALTA", level="WATCH", stage="SCORE_MIN"),        # viu o 2º mas parou em SCORE_MIN
            _dec(T0 + timedelta(hours=49), 40, "ALTA", level="SETUP", stage="CONFIRMACOES"),     # …e chegou a SETUP
            _dec(T0 + timedelta(hours=89), 30, "BAIXA", level="OPPORTUNITY", stage="EVIDENCIA"), # 3º movimento: só direção contrária
            _dec(T0 + timedelta(hours=100), 30, "ALTA", "ENTRADA", "EXECUTION", None),         # entrada sem movimento relevante depois
        ]
        cf = capture_funnel(decs, prices, 2.0, 240, {T0 + timedelta(hours=9): 2.5})
        self.assertEqual(cf.n_moves, 3)
        self.assertEqual(cf.reached["WATCH"], 2)
        self.assertEqual(cf.reached["SETUP"], 2)
        self.assertEqual(cf.reached["OPPORTUNITY"], 1)
        self.assertEqual(cf.reached["EXECUTION"], 1)
        self.assertEqual(cf.wrong_direction, 1)
        self.assertEqual(cf.lost_at["SETUP"], {"CONFIRMACOES": 1})
        self.assertEqual(cf.execution_results, [2.5])
        self.assertEqual(cf.entries_total, 2)
        self.assertAlmostEqual(cf.rate("EXECUTION"), 1 / 3)
        txt = cf.render()
        self.assertIn("MOVIMENTOS RELEVANTES:", txt)
        self.assertIn("Confirmações insuficientes", txt)
        m = cf.merge(cf)
        self.assertEqual(m.n_moves, 6)
        self.assertEqual(m.reached["EXECUTION"], 2)
        self.assertEqual(m.lost_at["SETUP"]["CONFIRMACOES"], 2)


class ExitLabTests(unittest.TestCase):
    def _rows(self, n=40):
        rows = []
        for i in range(n):
            mfe = 2.6 if i % 2 == 0 else 0.6          # metade vai longe, metade estopa
            prof = SimpleNamespace(max_r_before_stop=mfe, mae_r=0.4 if mfe > 1 else 1.0, stopped=mfe < 1, horizon_reached=False, bars=10)
            res = {"1R": 1.0 if mfe >= 1 else -1.0, "2R": 2.0 if mfe >= 2 else -1.0, "3R": 3.0 if mfe >= 3 else -1.0, "adaptive": 1.8 if mfe >= 1 else -1.0}
            rows.append({"time": T0 + timedelta(hours=i), "profile": prof, "results": res})
        return rows

    def test_distribution_and_walk_forward_choice(self):
        from gold_ai.exit_lab import exit_lab
        rep = exit_lab("XAUUSD", self._rows())
        self.assertEqual(rep.n, 40)
        self.assertAlmostEqual(rep.mfe_p90, 2.6)
        self.assertAlmostEqual(rep.reach["2R"], 0.5)
        best = rep.strategies[0]
        self.assertEqual(best.name, "2R")                       # 2R: +0.5R/op; 3R: −1R/op (nunca chega); 1R: 0
        self.assertAlmostEqual(best.expectancy, 0.5)
        self.assertIsNotNone(rep.chosen_oos)
        self.assertEqual([c[1] for c in rep.choices][1:], ["2R", "2R", "2R"])   # do 2º bloco em diante o passado escolhe 2R
        self.assertIn("candidata: 2R", rep.recommendation)
        self.assertIn("EXIT LAB", rep.render())

    def test_small_sample_gives_no_recommendation(self):
        from gold_ai.exit_lab import exit_lab
        rep = exit_lab("US500", self._rows(8))
        self.assertIn("sem recomendação", rep.recommendation)


class EdgeBankTests(unittest.TestCase):
    def test_similarity_symmetric_and_bounded(self):
        from gold_ai.edge_bank import similarity
        self.assertEqual(similarity("XAUUSD", "XAUUSD"), 1.0)
        s = similarity("XAUUSD", "EURUSD")
        self.assertGreater(s, 0.5)
        self.assertAlmostEqual(s, similarity("EURUSD", "XAUUSD"))
        self.assertGreaterEqual(similarity("XAUUSD", "USDJPY"), 0.0)
        self.assertLess(similarity("XAUUSD", "USDJPY"), s)

    def test_transfer_keeps_own_cases_separate(self):
        from gold_ai.edge_bank import EdgeBank, similarity
        bank = EdgeBank()
        for _ in range(50):
            bank.add_trade("XAUUSD", ["evento:cpi"], 0.6)
        for _ in range(5):
            bank.add_trade("EURUSD", ["evento:cpi"], -0.2)
        bank.apply_transfer()
        st = bank.get("EURUSD", "evento:cpi")
        self.assertEqual(st.n_own, 5)                                   # o n próprio não muda
        self.assertEqual(st.tier, "sem amostra")
        self.assertAlmostEqual(st.transfer_n, 0.5 * similarity("EURUSD", "XAUUSD") * 50)
        self.assertGreater(st.blended, st.expectancy)                   # a evidência emprestada puxa a média, ponderada
        self.assertLess(st.blended, 0.6)
        xau = bank.get("XAUUSD", "evento:cpi")
        self.assertEqual(xau.tier, "validado")
        self.assertLess(xau.transfer_n, 5)                              # de volta: 5 casos × 0,5 × sim

    def test_negative_contexts_and_live_multiplier_gated_by_own_sample(self):
        from gold_ai.edge_bank import EdgeBank
        bank = EdgeBank()
        for _ in range(25):
            bank.add_trade("US500", ["vwap:RANGE·B1"], -0.3)
        for _ in range(35):
            bank.add_trade("XAUUSD", ["relogio:PRESSÃO LATENTE"], 0.4)
        for _ in range(25):
            bank.add_potential("WTI", ["regime:VOLATILE"], -0.5)
        neg = bank.negative_contexts()
        self.assertEqual({(s.asset, s.tag) for s in neg}, {("US500", "vwap:RANGE·B1"), ("WTI", "regime:VOLATILE")})
        self.assertEqual(bank.multiplier("US500", ["vwap:RANGE·B1"])[0], 1.0)      # 25 < 30: informa, não decide
        self.assertEqual(bank.multiplier("XAUUSD", ["relogio:PRESSÃO LATENTE"])[0], 1.1)
        self.assertEqual(bank.multiplier("XAUUSD", ["regime:RANGE"])[0], 1.0)
        txt = bank.render(["US500", "XAUUSD", "WTI"])
        self.assertIn("QUANDO NÃO OPERAR", txt)
        self.assertIn("operacional", txt)

    def test_add_backtest_and_persistence(self):
        import os
        import tempfile
        from gold_ai.edge_bank import EdgeBank
        decs = [_dec(T0, 30, "ALTA", "ENTRADA", "EXECUTION", None, regime="BULLISH", setup="B1"),
                _dec(T0 + timedelta(hours=1), 30, "ALTA", level="SETUP", stage="CONFIRMACOES", regime="BULLISH", setup="B1"),
                _dec(T0 + timedelta(hours=2), 30, "ALTA", level="WATCH", stage="SCORE_MIN", regime="BULLISH", setup="B1")]
        decs[1].hypothetical_r, decs[2].hypothetical_r = 1.5, 9.0
        rows = [{"time": T0, "results": {"adaptive": 0.8, "3R": 3.0}}]
        bank = EdgeBank()
        self.assertEqual(bank.add_backtest("XAUUSD", decs, rows), 1)
        st = bank.get("XAUUSD", "vwap:BULLISH·B1")
        self.assertEqual(st.own, [0.8])
        self.assertEqual(st.potential, [1.5])                            # WATCH não entra no potencial (só ≥ SETUP)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "edge_bank.json")
            bank.save(path)
            again = EdgeBank.load(path)
            self.assertEqual(again.get("XAUUSD", "regime:BULLISH").own, [0.8])
            self.assertEqual(EdgeBank.load(os.path.join(d, "nada.json")).stats, {})
