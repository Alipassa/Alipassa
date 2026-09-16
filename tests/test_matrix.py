"""5.2 — MATRIZ DE DECISÃO: confirmações 1→5 × posições 1→4, escolhida na 1ª metade e conferida na 2ª."""
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

UTC = timezone.utc
T0 = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)


class _Prof:
    def __init__(self, mfe, mae):
        self.max_r_before_stop, self.mae_r = mfe, mae


class _FakeBT:
    """Backtester falso: quanto menos confirmações, mais operações (e um pouco piores); expõe cfg/frame/warmup como o real."""

    def __init__(self, sym, n_bars=400, conf=3):
        from gold_ai.config import EngineConfig
        self.cfg = EngineConfig(symbol=sym, min_confirmations=conf)
        self.warmup = 20
        self.frame = SimpleNamespace(xau=[SimpleNamespace(time=T0 + timedelta(hours=i)) for i in range(n_bars)], symbol=sym)
        self.sym = sym
        self.runs = []

    def run(self, start=None, end=None, cfg=None):
        c = cfg.min_confirmations
        self.runs.append(c)
        every = 6 + 4 * c
        rows = []
        for i in range(self.warmup, len(self.frame.xau), every):
            t = self.frame.xau[i].time
            r = 0.9 if (i // every) % (2 + c) else -1.0
            rows.append({"time": t, "results": {"adaptive": r}, "exits": {"adaptive": t + timedelta(hours=3)}, "direction": "ALTA",
                         "symbol": self.sym, "profile": _Prof(1.4, 0.5), "event_kind": "NFP" if (i // every) % 2 else ""})
        return SimpleNamespace(trade_rows=rows)


class MatrixTests(unittest.TestCase):
    def test_build_matrix_covers_grid_and_splits_halves(self):
        from gold_ai.matrix import build_matrix
        from gold_ai.selector import PortfolioLimits
        bts = {"XAUUSD": _FakeBT("XAUUSD"), "WTI": _FakeBT("WTI")}
        lim = PortfolioLimits(max_total_open_risk_pct=12.0, max_correlated_risk_pct=6.0, max_positions=4)
        rep = build_matrix(bts, lim, 10000.0, 3.0, 0.05, (1, 2, 3), (1, 2), "adaptive", "2026-03-02", "2026-03-18")
        self.assertEqual(len(rep.cells), 6)
        self.assertEqual(bts["XAUUSD"].runs, [1, 2, 3])                       # só o parâmetro confirmações muda, uma corrida por nível
        c1, c3 = rep.cell(1, 2), rep.cell(3, 2)
        self.assertGreater(c1.full.admitted, c3.full.admitted)                 # menos confirmações → mais operações
        for x in rep.cells:
            self.assertEqual(x.first.admitted + x.second.admitted + x.first.refused + x.second.refused, x.n_candidates)
            self.assertGreater(x.full.avg_hours, 0)
            self.assertAlmostEqual(x.full.mfe_avg, 1.4, places=6)
            self.assertIn("NFP", x.full.by_kind)
            self.assertIn("sem evento", x.full.by_kind)
            self.assertTrue(set(x.full.by_symbol) <= {"XAUUSD", "WTI"})
            self.assertAlmostEqual(x.full.gross_r - x.full.net_r, 0.05 * x.full.admitted, places=6)   # custo descontado
        self.assertEqual(set(rep.cell(1, 2).full.by_symbol), {"XAUUSD", "WTI"})   # com 2 vagas, o segundo ativo entra
        self.assertEqual(set(rep.cell(1, 1).full.by_symbol), {"XAUUSD"})          # com 1 vaga, o simultâneo é recusado
        self.assertGreaterEqual(rep.cell(1, 2).full.admitted, rep.cell(1, 1).full.admitted)
        txt = rep.render()
        for key in ("MATRIZ DE DECISÃO", "QUADRO 1", "QUADRO 2", "QUADRO 3", "QUADRO 4", "POSIÇÕES SIMULTÂNEAS", "VEREDITO", "1ª metade", "2ª metade"):
            self.assertIn(key, txt)
        d = rep.to_dict()
        self.assertEqual(len(d["cells"]), 6)
        self.assertIn("winner", d)

    def test_winner_chosen_on_first_half_only_and_small_sample_is_inconclusive(self):
        from gold_ai.matrix import MIN_N, build_matrix
        from gold_ai.selector import PortfolioLimits
        lim = PortfolioLimits(max_total_open_risk_pct=12.0, max_correlated_risk_pct=6.0, max_positions=4)
        big = build_matrix({"XAUUSD": _FakeBT("XAUUSD", n_bars=1200)}, lim, 10000.0, 3.0, 0.05, (1, 2), (1,), "adaptive")
        w = big.winner()
        self.assertIsNotNone(w)
        self.assertGreaterEqual(w.first.admitted, MIN_N)
        best_first = max((x.score(10000.0, "first") for x in big.cells if x.score(10000.0, "first") is not None))
        self.assertEqual(w.score(10000.0, "first"), best_first)                # escolhido pela 1ª metade, não pelo total
        small = build_matrix({"XAUUSD": _FakeBT("XAUUSD", n_bars=120)}, lim, 10000.0, 3.0, 0.05, (3,), (1,), "adaptive")
        self.assertIsNone(small.winner())
        self.assertIn("nenhuma célula chegou", small.render())

    def test_portfolio_result_streak_and_costs(self):
        from gold_ai.models import Direction
        from gold_ai.portfolio_sim import SimTrade, simulate_portfolio
        from gold_ai.selector import PortfolioLimits
        rs = [1.0, -1.0, -1.0, -1.0, 2.0, -1.0]
        trades = [SimTrade(T0 + timedelta(hours=6 * i), "XAUUSD", Direction.ALTA, r - 0.05, T0 + timedelta(hours=6 * i + 2), 0.05, 1.2, 0.4, "CPI" if i % 2 else "")
                  for i, r in enumerate(rs)]
        res = simulate_portfolio(trades, 1, PortfolioLimits(max_total_open_risk_pct=12.0, max_correlated_risk_pct=6.0, max_positions=4), 10000.0, 3.0)
        self.assertEqual(res.admitted, 6)
        self.assertEqual(res.max_losing_streak, 3)
        self.assertAlmostEqual(res.avg_hours, 2.0)
        self.assertGreater(res.costs_usd, 0)
        self.assertEqual(res.by_kind["CPI"][0], 3)
        self.assertEqual(res.by_kind["sem evento"][0], 3)
        self.assertAlmostEqual(res.gross_r - res.net_r, 0.30, places=6)
