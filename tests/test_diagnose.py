"""DIRECTION DIAGNOSTIC: classificação das perdas, veredito da matriz fator × mercado e reality check."""
import unittest
from datetime import datetime, timedelta, timezone

from gold_ai.diagnose import RealityRow, VariantResult, classify_trade, direction_report, factor_matrix, verdict
from gold_ai.trading import ExcursionProfile

NOW = datetime(2026, 9, 14, 13, 0, tzinfo=timezone.utc)


def row(r, max_r, stopped, direction="ALTA", factors=None):
    return {"results": {"adaptive": r}, "profile": ExcursionProfile(max_r, 1.0 if stopped else 0.4, stopped, not stopped, 10),
            "direction": direction, "factors": factors or {}}


class ClassifyTests(unittest.TestCase):
    def test_categories(self):
        self.assertEqual(classify_trade(row(+1.2, 1.5, False)), "ACERTO")
        self.assertEqual(classify_trade(row(-0.2, 1.4, False)), "SAÍDA RUIM")       # chegou a 1,4R e devolveu
        self.assertEqual(classify_trade(row(-1.0, 0.6, True)), "ENTRADA RUIM")      # andou a favor e estopou
        self.assertEqual(classify_trade(row(-1.0, 0.1, True)), "INVERTIDO")         # direto contra
        self.assertEqual(classify_trade(row(-0.3, 0.5, False)), "EXPIROU")
        self.assertEqual(classify_trade({"results": {}, "profile": None}), "SEM RESULTADO")

    def test_direction_report_factor_power(self):
        # alinhamento = sinal da direção × score do fator. Acertos (compra): dolar +3 → +3. Invertidos (venda): dolar −5 → alinhamento +5:
        # o fator apontava MAIS forte na direção tomada justamente nas perdas diretas → poder = 3 − 5 = −2 (⚠️ fator do lado errado)
        rows = [row(+1.0, 1.5, False, "ALTA", {"dolar": 3.0, "fluxo": 1.0}) for _ in range(6)]
        rows += [row(-1.0, 0.1, True, "BAIXA", {"dolar": -5.0, "fluxo": 2.0}) for _ in range(6)]
        st = direction_report(rows, "XAUUSD")
        self.assertEqual(st.n, 12)
        self.assertEqual(st.counts["ACERTO"], 6)
        self.assertEqual(st.counts["INVERTIDO"], 6)
        power = dict((n, p) for n, p, _, _ in st.factor_power)
        self.assertAlmostEqual(power["dolar"], -2.0)
        self.assertAlmostEqual(power["fluxo"], 1.0 - (-2.0))   # fluxo apontava contra a venda nas perdas: fator certo, sinal errado
        txt = st.render()
        self.assertIn("INVERTIDO", txt)
        self.assertIn("poder OOS por fator", txt)


class VerdictTests(unittest.TestCase):
    def v(self, rs_blocks):
        rs = [r for b in rs_blocks for r in b]
        return VariantResult("", [sum(b) / len(b) if b else None for b in rs_blocks], [len(b) for b in rs_blocks], rs)

    def test_inverted_only_when_it_wins_almost_every_block(self):
        base = self.v([[-1, -1, 1, -1, -1, -1]] * 4)                         # E ≈ −0,67R, n=24
        inv = self.v([[1, 1, -1, 1, 1, 1]] * 4)                              # E ≈ +0,67R em todos os blocos
        rem = self.v([[-1, 1, -1, 1, -1, 1]] * 4)
        self.assertTrue(verdict(base, inv, rem).startswith("INVERTIDO? (4/4"))
        inv2 = self.v([[1, 1, 1, 1, 1, 1], [-2, -2, -2, -2, -2, -2], [1, 1, 1, 1, 1, 1], [-2, -2, -2, -2, -2, -2]])   # ganha em 2/4: não
        self.assertNotIn("INVERTIDO", verdict(base, inv2, rem))
        self.assertEqual(verdict(base, inv, self.v([[0.5] * 6] * 4)), "INVERTIDO? (4/4 blocos)")
        self.assertEqual(verdict(self.v([[0.2] * 6] * 4), self.v([[0.2] * 6] * 4), self.v([[0.5] * 6] * 4)), "ATRAPALHA")
        self.assertEqual(verdict(self.v([[0.5] * 6] * 4), self.v([[0.2] * 6] * 4), self.v([[0.1] * 6] * 4)), "AJUDA")
        self.assertEqual(verdict(self.v([[0.5] * 2] * 4), inv, rem), "amostra < 20")

    def test_reality_verdicts(self):
        b = VariantResult("", [0.3] * 4, [6] * 4, [0.3] * 24)
        y = VariantResult("", [-0.3] * 4, [6] * 4, [-0.3] * 24)
        self.assertIn("NÃO CONFIRMADO", RealityRow("XAUUSD", b, y).verdict)
        self.assertIn("CONFIRMADO nas duas", RealityRow("XAUUSD", b, b).verdict)
        self.assertIn("inconclusivo", RealityRow("XAUUSD", b, VariantResult("", [0.3], [3], [0.3] * 3)).verdict)
        self.assertIn("sem uma das fontes", RealityRow("XAUUSD", b, None).verdict)


class FactorMatrixSmokeTests(unittest.TestCase):
    def test_runs_on_synthetic_frame(self):
        from gold_ai.config import EngineConfig
        from gold_ai.evaluation import HistoryFrame
        from gold_ai.markets import get_market
        from gold_ai.sources.sample import make_candles

        f = HistoryFrame(xau=make_candles("H1", 700, 2500, 0.4, 6.0, NOW, seed=3), dxy=make_candles("H1", 700, 104, -0.002, 0.08, NOW, seed=4),
                         us10y=make_candles("H1", 700, 4.2, -0.0005, 0.02, NOW, seed=5))
        cfg = EngineConfig(factor_signs=dict(get_market("XAUUSD").factor_signs), symbol="XAUUSD")
        logs = []
        fm = factor_matrix(f, "XAUUSD", cfg, n_blocks=2, step=6, log=logs.append, factors=["dolar"])
        self.assertEqual(len(fm.rows), 1)
        name, sign, inv, rem, vd = fm.rows[0]
        self.assertEqual(name, "dolar")
        self.assertEqual(len(inv.e_blocks), 2)
        self.assertIn("XAUUSD", fm.render())
        self.assertTrue(any("dolar" in l for l in logs))


if __name__ == "__main__":
    unittest.main()


class TradeModeTests(unittest.TestCase):
    def test_inverter_flips_every_trade_and_fade_only_late_stages(self):
        from gold_ai.config import EngineConfig
        from gold_ai.evaluation import Backtester, HistoryFrame
        from gold_ai.markets import get_market
        from gold_ai.sources.sample import make_candles

        f = HistoryFrame(xau=make_candles("H1", 900, 2500, 0.4, 6.0, NOW, seed=3), dxy=make_candles("H1", 900, 104, -0.002, 0.08, NOW, seed=4),
                         us10y=make_candles("H1", 900, 4.2, -0.0005, 0.02, NOW, seed=5))
        signs = dict(get_market("XAUUSD").factor_signs)
        base = Backtester(f, EngineConfig(factor_signs=signs, symbol="XAUUSD"), step=4).run()
        inv = Backtester(f, EngineConfig(factor_signs=signs, symbol="XAUUSD", trade_mode="inverter"), step=4).run()
        fade = Backtester(f, EngineConfig(factor_signs=signs, symbol="XAUUSD", trade_mode="fade_confirmacao"), step=4).run()
        by_time = {r["time"]: r for r in base.trade_rows}
        self.assertTrue(base.trade_rows, "o sintético precisa gerar operações")
        for r in inv.trade_rows:
            b = by_time[r["time"]]
            self.assertEqual(r["signal_direction"], b["signal_direction"])
            self.assertNotEqual(r["direction"], b["direction"])          # contra todo sinal
        for r in fade.trade_rows:
            b = by_time[r["time"]]
            late = r["stage"] in ("CONFIRMAÇÃO", "MOVIMENTO")
            self.assertEqual(r["direction"] != b["direction"], late)      # contra só nos estágios tardios
