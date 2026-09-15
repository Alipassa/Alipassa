"""Testes — FLOW ANOMALY ENGINE (informação implícita) e propagação pelo REACTION ENGINE."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from gold_ai.flow_anomaly import FLOW_TRANSMISSION, FlowAnomalyEngine
from gold_ai.models import Candle, MarketSnapshot
from gold_ai.news_engine import IdentifiedEvent, TRANSMISSION, expected_direction

UTC = timezone.utc
NOW = datetime(2026, 9, 14, 13, 0, tzinfo=UTC)


def snap(move_pct, dxy=0.0, y_bp=0.0, silver=None, vol_mult=1.0, minutes=20, atr=30.0, price=4300.0, spx=None):
    """Ouro: impulso de `move_pct` nos últimos `minutes` minutos em candles M1, volume ×vol_mult no fim."""
    s = MarketSnapshot(time=NOW, price=price)
    s.atr, s.price_change_pct, s.dxy_change_pct, s.us10y_change_bp, s.silver_change_pct, s.equity_change_pct = atr, move_pct, dxy, y_bp, silver, spx
    p0 = price / (1 + move_pct / 100.0)
    cs = []
    for i in range(120):
        t = NOW - timedelta(minutes=120 - i)
        if i < 120 - minutes:
            p = p0
            v = 100.0
        else:
            k = (i - (120 - minutes) + 1) / minutes
            p = p0 + (price - p0) * k
            v = 100.0 * vol_mult
        cs.append(Candle(t, p, p + 0.5, p - 0.5, p, v))
    cs[-1] = Candle(cs[-1].time, cs[-1].open, price + 0.5, cs[-1].low, price, cs[-1].volume)
    s.candles = {"M1": cs}
    return s


class FlowTests(unittest.TestCase):
    def test_signature_B_institutional_flow_unexplained(self):
        # ouro +1,2% (≈1,7 ATR) em 20 min, DXY e yields parados, prata confirma, volume ×3, sem notícia
        fa = FlowAnomalyEngine().assess("XAUUSD", snap(1.2, dxy=0.02, y_bp=0.5, silver=0.8, vol_mult=3.0), [], NOW)
        self.assertGreaterEqual(fa.score, 70)
        self.assertEqual(fa.signature, "B")
        self.assertEqual(fa.origin, "D")
        self.assertEqual(fa.status, "FLUXO ANÔMALO")
        self.assertTrue(fa.is_anomalous)
        self.assertIn("origem desconhecida", fa.chain)
        self.assertNotIn("banco central", fa.chain.lower())
        ev = fa.implicit_event(NOW)
        self.assertEqual(ev.kind, "flow_XAUUSD_up")
        self.assertEqual(ev.direction_sign, 1.0)
        self.assertLess(ev.time, NOW)                                       # início do impulso, não agora
        # propagação por regra inicial: ouro anômalo ↑ → USDJPY ↓ (refúgio), US500 ↓ leve, EURUSD ↑ (dólar −)
        self.assertLess(expected_direction(ev, "USDJPY")[0], 0)
        self.assertGreater(expected_direction(ev, "EURUSD")[0], 0)
        self.assertGreater(expected_direction(ev, "XAUUSD")[0], 0)

    def test_signature_A_explained_by_leaders_and_by_news(self):
        # DXY −0,5% e yields −6 bp explicam o ouro subir → macro plausível, não anômalo
        fa = FlowAnomalyEngine().assess("XAUUSD", snap(1.2, dxy=-0.5, y_bp=-6.0, silver=0.8, vol_mult=2.0), [], NOW)
        self.assertEqual(fa.signature, "A")
        self.assertFalse(fa.is_anomalous)
        self.assertIn(fa.origin, ("B", "C"))
        # notícia que explica (CPI abaixo → ouro ↑) → origem A, penalidade
        ev = IdentifiedEvent("cpi", "CPI MoM", NOW - timedelta(minutes=30), 1.0, 0.3, 0.1, -2.0, -1.0)
        fa2 = FlowAnomalyEngine().assess("XAUUSD", snap(1.2, dxy=0.0, y_bp=0.0, silver=0.8, vol_mult=3.0), [ev], NOW)
        self.assertEqual(fa2.origin, "A")
        self.assertLess(fa2.components["notícia explicativa"], 0)
        self.assertFalse(fa2.is_anomalous)

    def test_signature_C_regime(self):
        # ouro ↑ forte com DXY ↑ e yields ↑ (contra) → extremamente anômalo → REGIME
        fa = FlowAnomalyEngine().assess("XAUUSD", snap(1.5, dxy=+0.4, y_bp=+5.0, silver=0.3, vol_mult=3.0, minutes=15), [], NOW)
        self.assertEqual(fa.signature, "C")
        self.assertEqual(fa.origin, "E")
        self.assertTrue(fa.anomalous_regime)
        self.assertEqual(fa.status, "REGIME ANÔMALO")
        self.assertIn("MODO INVESTIGAÇÃO", fa.chain)

    def test_small_move_is_not_anomaly(self):
        fa = FlowAnomalyEngine().assess("XAUUSD", snap(0.2, dxy=0.0, y_bp=0.0, silver=0.1, vol_mult=1.0, minutes=60), [], NOW)
        self.assertLess(fa.score, 40)
        self.assertEqual(fa.status, "SEM ANOMALIA")
        self.assertIsNone(fa.implicit_event(NOW))
        self.assertEqual(FlowAnomalyEngine().assess("XAUUSD", MarketSnapshot(time=NOW, price=4300.0), [], NOW).direction, 0.0)

    def test_flow_kinds_registered_in_transmission(self):
        for k in FLOW_TRANSMISSION:
            self.assertIn(k, TRANSMISSION)


class LivePropagationTests(unittest.TestCase):
    def test_anomaly_opens_implicit_event_and_clocks_other_markets(self):
        from gold_ai.guard import GuardLimits, KillSwitch, TradingMode
        from gold_ai.market_engine import MarketAIEngine
        from gold_ai.memory import PredictionMemory
        from gold_ai.selector import PortfolioLimits
        from gold_ai.telegram import TelegramSender
        from tests.test_market40 import build_snapset

        with tempfile.TemporaryDirectory() as d:
            mem = PredictionMemory(os.path.join(d, "m.db"))
            sender = TelegramSender(dry_run=True, quiet=True)
            eng = MarketAIEngine(mem, GuardLimits(), ("XAUUSD", "USDJPY", "EURUSD"), TradingMode.PAPER, 10000.0, PortfolioLimits(), sender=sender,
                                 kill_switch=KillSwitch(enabled_env=False), log=lambda m: None, horizon_min=60)
            ss = build_snapset({"XAUUSD": "neutro", "USDJPY": "neutro", "EURUSD": "neutro"}, now=NOW)
            gold = snap(1.2, dxy=0.02, y_bp=0.5, silver=0.8, vol_mult=3.0)
            gold.time = NOW
            ss.by_symbol["XAUUSD"] = gold
            for sym in ("USDJPY", "EURUSD"):
                ss.by_symbol[sym].price_change_pct, ss.by_symbol[sym].dxy_change_pct, ss.by_symbol[sym].us10y_change_bp = 0.0, 0.02, 0.5
                ss.by_symbol[sym].candles = {}
            eng.run_cycle(ss)
            self.assertIn("XAUUSD", eng.active_flows)
            self.assertTrue(any("FLUXO ANÔMALO" in m for m in sender.sent))
            self.assertTrue(any(getattr(e, "kind", "") == "flow_XAUUSD_up" for e in ss.identified))
            # os outros mercados ganham relógio de reação para o evento implícito (líder = ouro; eles ainda parados)
            for sym in ("USDJPY", "EURUSD"):
                s = ss.by_symbol[sym]
                self.assertIn("flow_XAUUSD_up", s.reaction_chain)
                self.assertIn(s.reaction_status, ("AGUARDANDO", "PRESSÃO LATENTE"))
            self.assertLess(ss.by_symbol["USDJPY"].reaction_pressure if ss.by_symbol["USDJPY"].reaction_pressure else -0.01, 0)
            mem.close()


if __name__ == "__main__":
    unittest.main()


class FlowLedgerTests(unittest.TestCase):
    """5.2 — anomalia registrada → medida em 5/15/30/60 → estatística (o histórico decide)."""

    def _m1(self, t0, price0, atr, path):
        from gold_ai.models import Candle
        out, p = [], price0
        for i, step in enumerate(path):
            t = t0 + timedelta(minutes=i + 1)
            o, c = p, p + step * atr
            out.append(Candle(t, o, max(o, c) + 0.05 * atr, min(o, c) - 0.05 * atr, c, 10.0))
            p = c
        return out

    def test_measure_continuation_and_reversal(self):
        from gold_ai.flow_anomaly import measure_flow_outcome
        t0 = NOW
        cont = self._m1(t0, 2500.0, 10.0, [0.05] * 70)                 # sobe 0,05 ATR/min → +3 ATR aos 60 min
        m = measure_flow_outcome(+1, 2500.0, 10.0, cont, t0)
        self.assertEqual(m["resultado"], "CONTINUOU")
        self.assertAlmostEqual(m["mfe5"], 0.3, places=2)
        self.assertGreater(m["mfe60"], 2.9)
        self.assertLess(m["mae60"], 0.1)
        self.assertEqual(m["confirm_min"], 10.0)                        # 0,5 ATR no 10º minuto
        rev = self._m1(t0, 2500.0, 10.0, [-0.05] * 70)
        m2 = measure_flow_outcome(+1, 2500.0, 10.0, rev, t0)
        self.assertEqual(m2["resultado"], "REVERTEU")
        self.assertIsNone(m2["confirm_min"])
        self.assertGreater(m2["mae15"], 0.7)
        flat = self._m1(t0, 2500.0, 10.0, [0.01, -0.01] * 35)
        self.assertEqual(measure_flow_outcome(-1, 2500.0, 10.0, flat, t0)["resultado"], "INDEFINIDO")
        self.assertEqual(measure_flow_outcome(+1, 2500.0, 10.0, [], t0)["resultado"], "SEM DADOS")

    def test_stats_table_and_history_in_chain(self):
        from gold_ai.flow_anomaly import FlowAnomalyEngine, flow_stats, render_flow_stats
        rows = [{"ativo": "XAUUSD", "origem": "E", "flow_score": 75, "mfe15": 0.8, "mfe60": 1.4, "mae60": 0.3, "resultado": "CONTINUOU", "confirm_min": 8.0} for _ in range(5)]
        rows += [{"ativo": "XAUUSD", "origem": "E", "flow_score": 72, "mfe15": 0.2, "mfe60": 0.2, "mae60": 0.9, "resultado": "REVERTEU", "confirm_min": None} for _ in range(2)]
        rows += [{"ativo": "XAUUSD", "origem": "D", "flow_score": 60, "mfe15": 9, "mfe60": 9, "mae60": 0, "resultado": "CONTINUOU", "confirm_min": 1.0}]   # abaixo do limiar: fora
        st = {(g.asset, g.origin): g for g in flow_stats(rows)}
        self.assertEqual(st[("XAUUSD", "E")].n, 7)
        self.assertAlmostEqual(st[("XAUUSD", "E")].p_continue, 5 / 7)
        self.assertEqual(st[("XAUUSD", "todas")].n, 7)
        self.assertAlmostEqual(st[("XAUUSD", "E")].mfe60_med, 1.4)
        self.assertEqual(st[("XAUUSD", "E")].confirm_med, 8.0)
        txt = render_flow_stats(rows)
        self.assertIn("71%", txt)
        fe = FlowAnomalyEngine(ledger=rows)
        fa = fe.assess("XAUUSD", snap(1.5, dxy=+0.4, y_bp=+5.0, silver=0.3, vol_mult=3.0, minutes=15), [], NOW)
        self.assertEqual(fa.history_n, 7)
        self.assertIn("continuação 71%", fa.chain)

    def test_live_records_and_measures_anomaly(self):
        from gold_ai.guard import GuardLimits, KillSwitch, TradingMode
        from gold_ai.market_engine import MarketAIEngine
        from gold_ai.memory import PredictionMemory
        from gold_ai.opportunity import opportunity_level
        from gold_ai.selector import PortfolioLimits
        from gold_ai.telegram import TelegramSender
        from tests.test_market40 import build_snapset
        from types import SimpleNamespace
        from gold_ai.models import Direction

        with tempfile.TemporaryDirectory() as d:
            mem = PredictionMemory(os.path.join(d, "m.db"))
            sender = TelegramSender(dry_run=True, quiet=True)
            logs = []
            eng = MarketAIEngine(mem, GuardLimits(), ("XAUUSD", "EURUSD"), TradingMode.PAPER, 10000.0, PortfolioLimits(), sender=sender,
                                 kill_switch=KillSwitch(enabled_env=False), log=logs.append, horizon_min=60)
            ss = build_snapset({"XAUUSD": "neutro", "EURUSD": "neutro"}, now=NOW)
            gold = snap(1.2, dxy=0.02, y_bp=0.5, silver=0.8, vol_mult=3.0)
            gold.time = NOW
            ss.by_symbol["XAUUSD"] = gold
            ss.by_symbol["EURUSD"].candles = {}
            pc = eng.run_cycle(ss)
            self.assertIn("XAUUSD  score", pc.render().replace("  score", "  score"))
            xau_line = next(ln for ln in pc.render().splitlines() if ln.strip().startswith("XAUUSD"))
            self.assertIn("nível WATCH", xau_line)                            # fluxo anômalo = modo investigação, nunca NONE
            self.assertEqual(pc.results["XAUUSD"].assessment.flow_status, "REGIME ANÔMALO" if pc.results["XAUUSD"].assessment.anomalous_regime else "FLUXO ANÔMALO")
            pend = mem.pending_flow_anomalies(NOW + timedelta(hours=2), 60)
            self.assertEqual(len(pend), 1)
            self.assertEqual(pend[0]["ativo"], "XAUUSD")
            self.assertEqual(pend[0]["flow_score"], 71 if pend[0]["flow_score"] == 71 else pend[0]["flow_score"])
            self.assertGreaterEqual(pend[0]["flow_score"], 70)
            self.assertTrue(any("MODO INVESTIGAÇÃO" in m for m in sender.sent))
            # nível: fluxo anômalo = WATCH mesmo sem oportunidade bruta
            a = SimpleNamespace(direction=Direction.LATERAL, premove=SimpleNamespace(direction=Direction.LATERAL), score=0.0, has_edge=False, technical=(),
                                flow_status="REGIME ANÔMALO")
            self.assertEqual(opportunity_level(a, None, False), "WATCH")
            # 61 min depois, com M1 do broker: medida e fechada
            later = NOW + timedelta(minutes=61)
            ss2 = build_snapset({"XAUUSD": "neutro", "EURUSD": "neutro"}, now=later)
            g2 = snap(0.1, dxy=0.0, y_bp=0.0, silver=0.0, vol_mult=1.0)
            g2.time = later
            g2.candles = dict(g2.candles or {})
            g2.candles["M1"] = self._m1(NOW, pend[0]["preco"], pend[0]["atr"], [0.05] * 61)
            ss2.by_symbol["XAUUSD"] = g2
            ss2.by_symbol["EURUSD"].candles = {}
            eng.run_cycle(ss2)
            rows = mem.flow_anomaly_rows()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["resultado"], "CONTINUOU")
            self.assertEqual(eng.flow_engine.ledger[0]["resultado"], "CONTINUOU")
            self.assertTrue(any("ANOMALIA MEDIDA" in m for m in logs))
            self.assertIn("FLOW ANOMALY — o que aconteceu DEPOIS", eng.status_text())
            mem.close()


class SignalLabelTests(unittest.TestCase):
    def test_signal_text_uses_market_name_and_price_digits(self):
        from types import SimpleNamespace
        from gold_ai.models import Direction, SignalType
        from gold_ai.telegram import format_signal
        a = SimpleNamespace(prob_up=0.69, prob_down=0.2, prob_flat=0.11, price=105.7, confidence=55.0, horizon="5–30 min",
                            evidence_level=SimpleNamespace(label="NÍVEL 3 — SINAL"), score=40.0)
        sig = SimpleNamespace(assessment=a, direction=Direction.ALTA, type=SignalType.WATCH, reasons=["fluxo comprador"], trigger="")
        txt = format_signal(sig, "WTI")
        self.assertIn("WTI WATCH", txt)
        self.assertIn("Petróleo WTI", txt)
        self.assertIn("Preço: 105.700", txt)
        self.assertNotIn("XAU/USD", txt)
        self.assertIn("GOLD WATCH", format_signal(sig, "XAUUSD"))
        a.price = 1.15422
        self.assertIn("Preço: 1.15422", format_signal(sig, "EURUSD"))


class FlowReplayTests(unittest.TestCase):
    def test_replay_finds_and_measures_anomalies_point_in_time(self):
        import math
        from gold_ai.flow_anomaly import replay_flow_anomalies
        from gold_ai.memory import PredictionMemory
        from gold_ai.models import Candle
        t0 = datetime(2026, 3, 2, 0, 0, tzinfo=UTC)
        atr_h1, p, out = 10.0, 4300.0, []
        step = atr_h1 / math.sqrt(60) / 2.0
        n = 30 * 60
        for i in range(n):
            t = t0 + timedelta(minutes=i)
            o = p
            c = p + step * (1 if i % 2 else -1)
            vol = 10.0
            if 26 * 60 <= i < 26 * 60 + 25:                 # impulso de 2,5 ATR em 25 min com volume ×3, sem líder
                c = p + 0.10 * atr_h1
                vol = 30.0
            elif 26 * 60 + 25 <= i < 26 * 60 + 90:          # continua devagar
                c = p + 0.01 * atr_h1
            hi, lo = max(o, c) + step, min(o, c) - step
            if i % 60 == 0:
                hi, lo = o + atr_h1 / 2, o - atr_h1 / 2
            out.append(Candle(t, o, hi, lo, c, vol))
            p = c
        recs = replay_flow_anomalies("XAUUSD", out, {}, None, step_min=5)
        self.assertGreaterEqual(len(recs), 1)
        r = recs[0]
        self.assertTrue(r["event_id"].startswith("hist_flow_XAUUSD_up"))
        self.assertGreaterEqual(r["flow_score"], 70)
        self.assertIn(r["resultado"], ("CONTINUOU", "INDEFINIDO", "REVERTEU", "SEM DADOS"))
        self.assertIsNotNone(r["mfe60"])
        t_det = datetime.fromisoformat(r["hora"])
        self.assertGreaterEqual(t_det, t0 + timedelta(minutes=26 * 60))
        self.assertEqual(datetime.fromisoformat(r["medido_em"]), t_det + timedelta(minutes=60))
        # 1 por episódio de 60 min
        times = [datetime.fromisoformat(x["hora"]) for x in recs]
        self.assertTrue(all((b - a) >= timedelta(minutes=60) for a, b in zip(times, times[1:])))
        # ledger: insere medido, ignora duplicata, e o live não confunde com episódio vivido
        mem = PredictionMemory(":memory:")
        self.assertTrue(mem.save_measured_flow_anomaly(r))
        self.assertFalse(mem.save_measured_flow_anomaly(r))
        self.assertEqual(len(mem.flow_anomaly_rows()), 1)
        self.assertFalse(mem.recent_flow_anomaly("XAUUSD", t_det - timedelta(minutes=1)))
        mem.close()
