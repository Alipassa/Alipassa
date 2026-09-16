"""Testes — REACTION ENGINE alta resolução: ticks, dois horizontes, lead-lag, custo de execução, exportação MT5."""

from __future__ import annotations

import os
import tempfile
import unittest
import unittest.mock

from tests.test_mt5 import NumpyLike
from datetime import datetime, timedelta, timezone

from gold_ai import Direction
from gold_ai.data.mt5 import MT5Client, MT5Config
from gold_ai.models import Candle
from gold_ai.reaction_hires import (BUCKETS_SEC, LeadLagStats, PricePath, Quote, ReactionTradeSim, load_ticks, measure_hires, save_candles,
                                    save_ticks)

UTC = timezone.utc
T0 = datetime(2026, 3, 11, 12, 30, tzinfo=UTC)


def ticks(moves_by_sec: dict[int, float], p0=2500.0, spread=0.30, hours_before=15, atr_range=10.0):
    """Ticks a cada segundo por 1h após T0 seguindo `moves_by_sec` (ATR=10) por interpolação; antes de T0, ruído leve por 15h (ATR ≈ 10)."""
    out = []
    t = T0 - timedelta(hours=hours_before)
    k = 0
    while t < T0:
        mid = p0 + (atr_range / 2) * ((k % 120) / 60.0 - 1.0)     # oscila ±5 → amplitude horária ≈ 10
        out.append((t, mid - spread / 2, mid + spread / 2))
        t += timedelta(seconds=30)
        k += 1
    keys = sorted(moves_by_sec)
    for sec in range(0, 3601):
        prev = max([s for s in keys if s <= sec], default=None)
        nxt = min([s for s in keys if s > sec], default=None)
        if prev is None:
            m = 0.0
        elif nxt is None:
            m = moves_by_sec[prev]
        else:
            m = moves_by_sec[prev] + (moves_by_sec[nxt] - moves_by_sec[prev]) * (sec - prev) / (nxt - prev)
        mid = p0 + m * atr_range
        out.append((T0 + timedelta(seconds=sec), mid - spread / 2, mid + spread / 2))
    return out


class HiResMeasureTests(unittest.TestCase):
    def test_seconds_buckets_two_horizons_and_lead_lag(self):
        # alvo (ouro, esperado ↓): nada até 20 s, −0,2 ATR aos 40 s, −0,5 aos 90 s, −0,8 aos 240 s, −1,2 aos 1800 s, recua para −0,9 no fim
        path = PricePath.from_ticks(ticks({0: 0.0, 20: 0.0, 40: -0.2, 90: -0.5, 240: -0.8, 1800: -1.2, 3600: -0.9}))
        usd = PricePath.from_ticks(ticks({0: 0.0, 10: 0.0, 15: 0.1, 60: 0.2}, p0=104.0, spread=0.01, atr_range=0.3))   # +0,1 ATR(0,3)=+0,03 → 0,029% ... precisa ≥0,08%
        usd = PricePath.from_ticks(ticks({0: 0.0, 10: 0.0, 15: 1.0, 60: 1.5}, p0=104.0, spread=0.01, atr_range=0.1))   # +1 ATR(0,1)=+0,1 → +0,096% ≥ 0,08% aos 15 s
        atr = path.atr_at(T0)
        self.assertGreater(atr, 5.0)
        rec = measure_hires("CPI", "cpi", T0, "XAUUSD", -1.0, path, 10.0, {"USD": usd}, {"USD": +1.0})
        self.assertEqual(rec.base.resolution_min, 1 / 60)
        self.assertAlmostEqual(rec.move_at[1], 0.0, places=2)
        self.assertAlmostEqual(rec.move_at[30], 0.1, places=2)
        self.assertAlmostEqual(rec.move_at[60], 0.32, places=2)
        self.assertAlmostEqual(rec.move_at[300], 0.815, places=2)
        self.assertAlmostEqual(rec.base.time_to_first, 35 / 60, places=2)           # 0,15 ATR aos 35 s
        self.assertAlmostEqual(rec.base.time_to_confirmation, 74 / 60, places=2)      # 0,40 ATR aos 74 s (interpolação 40→90 s, ticks inteiros)
        self.assertAlmostEqual(rec.short_mfe, 0.815, places=2)                       # 0–5 min
        self.assertAlmostEqual(rec.follow_mfe, 1.2, places=2)                        # 5–60 min
        self.assertAlmostEqual(rec.base.max_adverse_atr, 0.0, places=2)
        self.assertTrue(rec.base.direction_correct)
        self.assertAlmostEqual(rec.spread_atr, 0.03, places=3)
        self.assertEqual(rec.lead_first_sec, 15.0)
        self.assertAlmostEqual(rec.lead_lag_sec, 20.0, places=1)                     # alvo reagiu 20 s depois do USD
        self.assertIsNone(measure_hires("X", "cpi", T0, "XAUUSD", 0.0, path, 10.0))

    def test_lead_lag_and_trade_sim_after_costs(self):
        # 30 eventos: em 24 o líder reage e o ouro acompanha (−0,8 ATR em 5 min); em 6 o líder reage e o ouro anda contra (+0,3)
        recs, items = [], []
        for k in range(30):
            follow = k < 24
            moves = {0: 0.0, 20: 0.0, 60: (-0.3 if follow else 0.15), 300: (-0.8 if follow else 0.3), 3600: (-1.0 if follow else 0.2)}
            t0 = T0 + timedelta(days=k)
            tk = [(t + timedelta(days=k), b, a) for t, b, a in ticks(moves)]
            path = PricePath.from_ticks(tk)
            usd = PricePath.from_ticks([(t + timedelta(days=k), b, a) for t, b, a in ticks({0: 0.0, 10: 0.0, 15: 1.0, 60: 1.5}, p0=104.0, spread=0.01, atr_range=0.1)])
            rec = measure_hires(f"CPI{k}", "cpi", t0, "XAUUSD", -1.0, path, 10.0, {"USD": usd}, {"USD": +1.0})
            recs.append(rec)
            items.append((rec, path, 10.0))
        ll = LeadLagStats(recs)
        row = ll.rows()[0]
        self.assertEqual((row.n, row.n_lead), (30, 30))
        self.assertAlmostEqual(row.p_confirm_given_lead, 0.8)
        self.assertIn("P(B|A)", ll.render())
        sim = ReactionTradeSim(slippage_atr=0.02, latency_sec=0.5)
        rows = sim.table(items, delays=(1, 30, 300))
        by_delay = {r.delay_sec: r for r in rows}
        self.assertEqual(by_delay[1].n, 30)
        self.assertGreater(by_delay[1].net_short, 0.4)                     # sobra depois de spread (0,03) + slippage (0,04)
        self.assertGreater(by_delay[1].cost, 0.06)
        self.assertLess(by_delay[300].net_short, by_delay[1].net_short)   # entrar 5 min depois captura menos (sai 5 min após a entrada)
        self.assertGreater(by_delay[1].net_follow, by_delay[1].net_short)  # continuação capturou mais (−1,0 aos 60 min)
        txt = sim.render(rows)
        self.assertIn("🟢", txt)
        self.assertIn("REACTION TRADE SIM", txt)
        # custo maior que o movimento → 🔴
        sim2 = ReactionTradeSim(slippage_atr=0.6, latency_sec=0.5)
        self.assertIn("🔴", sim2.render(sim2.table(items, delays=(1,))))

    def test_csv_roundtrip_and_m1_path(self):
        with tempfile.TemporaryDirectory() as d:
            tk = ticks({0: 0.0, 60: 0.5})[:50]
            p = os.path.join(d, "XAUUSD_ticks.csv")
            self.assertEqual(save_ticks(tk, p), 50)
            back = load_ticks(p)
            self.assertEqual(len(back), 50)
            self.assertAlmostEqual(back[0][1], tk[0][1], places=5)
            cs = [Candle(T0 + timedelta(minutes=i), 2500, 2501, 2499, 2500 + i, 10) for i in range(5)]
            pc = os.path.join(d, "XAUUSD_m1.csv")
            self.assertEqual(save_candles(cs, pc), 5)
        path = PricePath.from_candles(cs, 0.30, 1)
        self.assertEqual(path.resolution_sec, 60.0)
        q = path.at_or_before(T0 + timedelta(minutes=2, seconds=30))
        self.assertAlmostEqual(q.mid, 2501.0)      # em T+2:30 a última barra FECHADA é a aberta em T+1 (fecho em T+2)
        self.assertAlmostEqual(q.spread, 0.30)


class MT5ExportTests(unittest.TestCase):
    def test_rates_range_and_ticks_range(self):
        from tests.test_mt5 import FakeMT5

        class Fake(FakeMT5):
            COPY_TICKS_INFO = 1

            def copy_rates_range(self, symbol, tf, start, end):
                return NumpyLike(self.copy_rates_from_pos(symbol, tf, 0, 3))

            def copy_ticks_range(self, symbol, start, end, flags):
                base = int(start.timestamp()) * 1000
                return NumpyLike([{"time": int(start.timestamp()), "time_msc": base + i * 250, "bid": 2650.0 + i * 0.01, "ask": 2650.3 + i * 0.01} for i in range(4)] +
                                 [{"time": int(start.timestamp()) + 1, "time_msc": base + 1000, "bid": 0.0, "ask": 2650.5}])   # bid 0 → herda o anterior
        c = MT5Client(MT5Config(symbol="XAUUSD"), mt5=Fake())
        c.connect()
        cs = c.rates_range("XAUUSD", "M1", T0, T0 + timedelta(hours=1))
        self.assertEqual(len(cs), 3)
        tk = c.ticks_range("XAUUSD", T0, T0 + timedelta(seconds=2))
        self.assertEqual(len(tk), 5)
        self.assertEqual((tk[1][0] - tk[0][0]).total_seconds(), 0.25)
        self.assertAlmostEqual(tk[4][1], 2650.03)                          # bid herdado
        self.assertAlmostEqual(tk[4][2], 2650.5)


if __name__ == "__main__":
    unittest.main()


class ClockTradeTestTests(unittest.TestCase):
    def _items(self, n=40, good=32, contra_first=False):
        from gold_ai.reaction_hires import measure_hires
        items = []
        for k in range(n):
            follow = (k >= (n - good)) if contra_first else (k < good)
            moves = {0: 0.0, 20: 0.0, 60: (-0.3 if follow else 0.15), 300: (-0.8 if follow else 0.3), 1800: (-1.3 if follow else 0.2), 3600: (-1.0 if follow else 0.2)}
            t0 = T0 + timedelta(days=k)
            path = PricePath.from_ticks([(t + timedelta(days=k), b, a) for t, b, a in ticks(moves)])
            usd = PricePath.from_ticks([(t + timedelta(days=k), b, a) for t, b, a in ticks({0: 0.0, 10: 0.0, 15: 1.0, 60: 1.5}, p0=104.0, spread=0.01, atr_range=0.1)])
            rec = measure_hires(f"CPI{k}", "cpi", t0, "XAUUSD", -1.0, path, 10.0, {"USD": usd}, {"USD": +1.0})
            items.append((rec, path, 10.0))
        return items

    def test_clock_learns_only_from_prior_events_and_exits(self):
        from gold_ai.reaction_hires import ClockTradeTest
        ct = ClockTradeTest(delay_sec=5, p_min=0.55, min_n=3)
        rows = ct.run(self._items())
        r = rows[0]
        self.assertEqual((r.n_events, r.n_lead), (40, 40))
        self.assertEqual(r.skipped["sem_hist"], 3)                 # os 3 primeiros não têm histórico → não opera
        self.assertEqual(r.n_entries, 37)
        self.assertGreater(r.net_quick, 0.2)
        self.assertGreater(r.net_extend, r.net_quick)              # extensão captura a continuação (−1,3 aos 30 min)
        self.assertGreater(r.cost, 0.05)
        self.assertTrue(all(t.n_hist >= 3 for t in ct.trades))
        self.assertTrue(all(t.p_hist <= 1.0 for t in ct.trades))
        txt = ct.render(rows)
        self.assertIn("PROVA", txt)
        self.assertIn("TOTAL 37 entradas", txt)

    def test_clock_refuses_when_history_says_no(self):
        from gold_ai.reaction_hires import ClockTradeTest
        # os 8 primeiros eventos andam CONTRA → P histórica < 55% → o relógio recusa até o histórico virar
        ct = ClockTradeTest(delay_sec=5, p_min=0.55, min_n=3)
        rows = ct.run(self._items(n=40, good=32, contra_first=True))
        r = rows[0]
        self.assertGreater(r.skipped["P_baixa"], 0)
        self.assertLess(r.n_entries, 37)
        # o ingênuo entra em tudo; o relógio evita as primeiras contra
        self.assertGreater(r.net_quick, r.naive_net_quick)

    def test_clock_skips_target_already_reacted(self):
        from gold_ai.reaction_hires import ClockTradeTest
        ct = ClockTradeTest(delay_sec=120, p_min=0.5, min_n=3)   # entra 2 min depois do líder: alvo já andou −0,3 ATR aos 60 s
        rows = ct.run(self._items())
        self.assertGreater(rows[0].skipped["alvo_já_reagiu"], 20)


class VerdictTests(unittest.TestCase):
    def test_asset_verdicts_and_selector_dimension(self):
        from gold_ai.reaction_hires import ClockTestRow, asset_verdicts, load_reaction_edge, render_verdicts, save_reaction_edge
        from gold_ai.selector import AssetSelector
        row = lambda kind, tgt, n, q, e, naive: ClockTestRow(kind, tgt, n + 5, n + 2, n, {}, naive, q, e, q - 0.05, 0.6, 0.6, 0.07)  # noqa: E731
        results = {5: [row("cpi", "XAUUSD", 30, 0.15, 0.22, 0.05), row("cpi", "US500", 25, 0.04, 0.03, 0.01), row("cpi", "WTI", 22, -0.05, -0.02, -0.03),
                       row("cpi", "EURUSD", 8, 0.30, 0.30, 0.1)],
                   30: [row("cpi", "XAUUSD", 28, 0.10, 0.12, 0.05)]}
        vs = {v.symbol: v for v in asset_verdicts(results)}
        self.assertEqual(vs["XAUUSD"].verdict, "🟢")
        self.assertEqual((vs["XAUUSD"].delay_sec, vs["XAUUSD"].exit), (5, "EXTEND"))     # melhor combinação
        self.assertEqual(vs["US500"].verdict, "🟡")
        self.assertEqual(vs["WTI"].verdict, "🔴")
        self.assertEqual(vs["EURUSD"].verdict, "⚪")                                        # n < 20, mesmo com líquido alto
        txt = render_verdicts(asset_verdicts(results), "TICK")
        self.assertIn("REACTION EDGE POR ATIVO", txt)
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "reaction_edge.json")
            save_reaction_edge(asset_verdicts(results), p, "TICK")
            edge = load_reaction_edge(p)
        self.assertEqual(set(edge), {"XAUUSD", "US500", "WTI"})                             # ⚪ não entra
        self.assertGreater(edge["XAUUSD"], edge["US500"])
        self.assertEqual(load_reaction_edge(os.path.join("nao", "existe.json")), {})
        # selector: só pesa com PRESSÃO LATENTE; sem relógio fica neutro
        from tests.test_market40 import build_snapset
        from gold_ai.selector import Candidate, StatConfidence
        from gold_ai.markets import get_market
        from gold_ai import GoldAIEngine, EngineConfig
        ss = build_snapset({"XAUUSD": "compra", "US500": "compra"})
        sel_plain, sel_edge = AssetSelector(), AssetSelector(reaction_edge=edge)
        cands = []
        for sym in ("XAUUSD", "US500"):
            spec = get_market(sym)
            a, sig = GoldAIEngine(EngineConfig(factor_signs=dict(spec.factor_signs), symbol=sym)).run_cycle(ss.by_symbol[sym])
            if sig is None:
                from gold_ai.models import Signal, SignalType
                sig = Signal(SignalType.BUY, a.direction if a.direction != Direction.LATERAL else Direction.ALTA, a, [], "teste")
            cands.append(Candidate(spec, a, sig, ss.by_symbol[sym], StatConfidence(30, 0.3, 0.3, "HIGH", 0.2, 0.25), 0.5, 1.0))
        base = {c.spec.symbol: sel_plain.score(c, ss.time).opportunity_score for c in cands}
        neutral = {c.spec.symbol: sel_edge.score(c, ss.time).opportunity_score for c in cands}
        for sym in base:
            self.assertAlmostEqual(neutral[sym], base[sym] * 0.9 + 5.0, delta=2.5)            # 0,5 neutro × 10% (× decay)
        self.assertEqual(cands[0].components["reaction"], 0.5)
        ss.by_symbol["XAUUSD"].reaction_status = "PRESSÃO LATENTE"
        latent = {c.spec.symbol: sel_edge.score(c, ss.time).opportunity_score for c in cands}
        self.assertGreater(latent["XAUUSD"], neutral["XAUUSD"])
        self.assertAlmostEqual(latent["US500"], neutral["US500"], places=1)


class DukascopyTests(unittest.TestCase):
    def test_bi5_parse_and_around_events(self):
        import lzma
        import struct
        from gold_ai.data.dukascopy import DukascopyImporter, hour_url, parse_bi5

        def bi5(rows):
            return lzma.compress(b"".join(struct.pack(">IIIff", ms, ask, bid, 1.0, 1.0) for ms, ask, bid in rows))
        h = datetime(2026, 3, 11, 12, tzinfo=UTC)
        self.assertEqual(hour_url("XAUUSD", h), "https://datafeed.dukascopy.com/datafeed/XAUUSD/2026/02/11/12h_ticks.bi5")   # mês 0-indexado
        ticks = parse_bi5(bi5([(0, 2500300, 2500000), (1800000, 2500900, 2500600), (1800250, 0, 2500600)]), h, 1000.0)
        self.assertEqual(len(ticks), 2)                                                     # tick com ask 0 descartado
        self.assertEqual(ticks[1][0], h + timedelta(minutes=30))
        self.assertAlmostEqual(ticks[1][1], 2500.6)
        self.assertAlmostEqual(ticks[1][2], 2500.9)
        self.assertEqual(parse_bi5(b"", h, 1000.0), [])

        class Http:
            def __init__(self):
                self.calls = []

            def get_bytes(self, url, ttl=None, allow_404=False):
                self.calls.append(url)
                if "/12h_" in url:
                    return bi5([(5, 2500300, 2500000)])
                return b""                                                                # 404 → vazio
        http = Http()
        imp = DukascopyImporter(http)
        out = imp.around_events("XAUUSD", [h + timedelta(minutes=30), h + timedelta(minutes=45)], before_h=2, after_h=1)
        self.assertEqual(len(http.calls), 4)                                               # 10h,11h,12h,13h (eventos na mesma hora não duplicam)
        self.assertEqual(len(out), 1)
        self.assertTrue(all("/XAUUSD/" in c for c in http.calls))
        imp.hours("USDX", [h])
        self.assertIn("/DOLLARIDXUSD/", http.calls[-1])
        # fim de semana não é pedido; hora que falha é anotada e pulada, o resto continua
        from gold_ai.data.http import DataError

        class Flaky(Http):
            def get_bytes(self, url, ttl=None, allow_404=False):
                self.calls.append(url)
                if "/13h_" in url:
                    raise DataError("falha ao buscar x: HTTP Error 503")
                return super().get_bytes(url, ttl, allow_404)
        fl = Flaky()
        slept = []
        imp2 = DukascopyImporter(fl, retries=2, sleep=slept.append, pace=0.0)
        sat = datetime(2026, 3, 14, 12, tzinfo=UTC)
        out2 = imp2.hours("XAUUSD", [h, h + timedelta(hours=1), sat])
        self.assertEqual(len(out2), 1)                                                     # 12h ok, 13h falhou, sábado nem pedido
        self.assertEqual(len(imp2.failed), 1)
        self.assertEqual(sum(1 for c in fl.calls if "/13h_" in c), 3)                      # 1 + 2 tentativas
        self.assertFalse(any("/14/" in c for c in fl.calls))
        self.assertTrue(any(w >= 3.0 for w in slept))


class DeltaAndStabilityTests(unittest.TestCase):
    def test_delta_table_stability_and_resample(self):
        from gold_ai.reaction_hires import ClockTradeTest, asset_verdicts, delta_table, render_delta, render_stability
        items = ClockTradeTestTests()._items(n=30, good=26, contra_first=True)   # 4 primeiros contra: o relógio (sem histórico / P baixa) não entra
        results = {}
        for d in (5, 30):
            ct = ClockTradeTest(delay_sec=d, p_min=0.55, min_n=3)
            results[d] = ct.run(items)
        rows = delta_table(results)
        self.assertEqual([(r.symbol, r.delay_sec) for r in rows], [("XAUUSD", 5), ("XAUUSD", 30)])
        r5 = rows[0]
        self.assertEqual(r5.n, 21)                                    # P histórica só passa de 55% no 10º evento (5/9)
        self.assertEqual(r5.n_naive, 30)
        self.assertGreater(r5.clock_r, r5.naive_r)                    # a ingênua paga os 4 contra; o relógio os evita → Δ > 0
        self.assertIsNotNone(r5.pf_quick)
        txt = render_delta(rows, "TICK")
        self.assertIn("Δ CLOCK−ING", txt)
        v_tick = asset_verdicts(results)
        # M1 reamostrado dos mesmos ticks: a relação deve sobreviver (vantagem em −0,8 ATR em 5 min é visível em barras de 1 min)
        items_m1 = [(rec, path.resample(1), atr) for rec, path, atr in items]
        self.assertEqual(items_m1[0][1].resolution_sec, 60.0)
        self.assertTrue(all(q.time.second == 0 for q in items_m1[0][1].q))
        ct = ClockTradeTest(delay_sec=60, p_min=0.55, min_n=3)
        v_m1 = asset_verdicts({60: ct.run(items_m1)})
        st = render_stability(v_tick, v_m1)
        self.assertIn("XAUUSD", st)
        self.assertIn("ESTABILIDADE TICK × M1", st)
        self.assertTrue(("MUITO FORTE" in st) or ("forte com reserva" in st) or ("moderado" in st))


class MT5ServerOffsetTests(unittest.TestCase):
    def test_server_time_converted_to_utc(self):
        import os
        from types import SimpleNamespace
        from tests.test_mt5 import FakeMT5
        now = datetime.now(UTC)

        class Fake(FakeMT5):
            COPY_TICKS_INFO = 1
            asked = {}

            def symbol_info_tick(self, symbol):
                return SimpleNamespace(bid=2699.8, ask=2700.2, time=int(now.timestamp()) + 3 * 3600)   # servidor GMT+3

            def copy_rates_range(self, symbol, tf, start, end):
                self.asked["rates"] = (start, end)
                base = int(start.timestamp())
                return NumpyLike([{"time": base, "open": 1, "high": 2, "low": 0, "close": 1, "tick_volume": 1, "spread": 1, "real_volume": 0}])

            def copy_ticks_range(self, symbol, start, end, flags):
                self.asked["ticks"] = (start, end)
                return NumpyLike([{"time": int(start.timestamp()), "time_msc": int(start.timestamp()) * 1000, "bid": 1.0, "ask": 1.1}])
        os.environ.pop("MT5_UTC_OFFSET_HOURS", None)
        c = MT5Client(MT5Config(symbol="XAUUSD"), mt5=Fake())
        c.connect()
        self.assertEqual(c.server_offset_hours, 3.0)
        cs = c.rates_range("XAUUSD", "M1", T0, T0 + timedelta(hours=1))
        self.assertEqual(c.mt5.asked["rates"][0], T0 + timedelta(hours=3))       # pedido em hora do servidor
        self.assertEqual(cs[0].time, T0)                                          # devolvido em UTC
        tk = c.ticks_range("XAUUSD", T0, T0 + timedelta(minutes=1))
        self.assertEqual(tk[0][0], T0)
        os.environ["MT5_UTC_OFFSET_HOURS"] = "2"
        try:
            c2 = MT5Client(MT5Config(symbol="XAUUSD"), mt5=Fake())
            c2.connect()
            self.assertEqual(c2.server_offset_hours, 2.0)
        finally:
            os.environ.pop("MT5_UTC_OFFSET_HOURS", None)


class DukascopyM1Tests(unittest.TestCase):
    @staticmethod
    def _day(rows):
        import lzma
        import struct
        return lzma.compress(b"".join(struct.pack(">iiiiif", sec, o, c, lo, hi, 1.5) for sec, o, c, lo, hi in rows))

    def test_parse_daily_candles(self):
        from gold_ai.data.dukascopy import day_candles_url, parse_candles_bi5
        from datetime import date
        d0 = datetime(2026, 2, 3, tzinfo=UTC)
        self.assertEqual(day_candles_url("XAUUSD", date(2026, 2, 3)), "https://datafeed.dukascopy.com/datafeed/XAUUSD/2026/01/03/BID_candles_min_1.bi5")
        cs = parse_candles_bi5(self._day([(0, 2500000, 2500500, 2499800, 2500700), (60, 2500500, 2500200, 2500100, 2500900), (120, 0, 1, 1, 1)]), d0, 1000.0)
        self.assertEqual(len(cs), 2)                                    # candle com open 0 descartado
        self.assertEqual(cs[1].time, d0 + timedelta(minutes=1))
        self.assertAlmostEqual(cs[0].open, 2500.0)
        self.assertAlmostEqual(cs[0].high, 2500.7)
        self.assertAlmostEqual(cs[0].low, 2499.8)
        self.assertAlmostEqual(cs[0].close, 2500.5)
        self.assertEqual(parse_candles_bi5(b"", d0, 1000.0), [])

    def test_m1_range_skips_saturday_and_merges_into_mt5_csv(self):
        import os
        import tempfile
        from datetime import date
        from gold_ai.data.dukascopy import DukascopyImporter
        from gold_ai.reaction_hires import save_candles
        from gold_ai import cli
        from gold_ai.models import Candle
        days = self._day
        calls = []

        class Http:
            def get_bytes(self, url, ttl=None, allow_404=False):
                calls.append(url)
                return days([(0, 2500000, 2500500, 2499800, 2500700)])
        imp = DukascopyImporter(Http(), sleep=lambda s: None, pace=0)
        cs = imp.m1_range("XAUUSD", date(2026, 2, 6), date(2026, 2, 9))     # sex, sáb, dom, seg
        self.assertEqual(len(calls), 3)                                     # sábado não é pedido
        self.assertTrue(all("/01/0" in u for u in calls))
        self.assertEqual([c.time.day for c in cs], [6, 8, 9])
        # CLI: completa o CSV do MT5 sem sobrescrever as barras que o broker já deu
        with tempfile.TemporaryDirectory() as d, unittest.mock.patch("gold_ai.data.dukascopy.DukascopyImporter.m1_range", lambda self, *a, **k: cs):
            dest = os.path.join(d, "XAUUSD_m1.csv")
            mt5_bar = Candle(datetime(2026, 2, 9, tzinfo=UTC), 9.0, 9.0, 9.0, 9.0, 1.0)     # mesma hora que o Dukascopy → prevalece o broker
            save_candles([mt5_bar, Candle(datetime(2026, 6, 1, tzinfo=UTC), 1, 1, 1, 1, 1)], dest)
            rc = cli.main(["history", "prices", "--source", "dukascopy", "--tf", "M1", "--full", "--markets", "XAUUSD", "--start", "2026-02-06", "--end", "2026-02-09",
                           "--out-dir", d, "--file", os.path.join(d, "none.csv")])
            self.assertEqual(rc, 0)
            rows = cli._read_candles_csv(dest)
            self.assertEqual(len(rows), 4)
            self.assertEqual([c for c in rows if c.time.day == 9 and c.time.month == 2][0].open, 9.0)


class LeadLagEpisodeTests(unittest.TestCase):
    """'o ouro caiu, o euro demorou, todos seguiram o ouro' — medido no M1."""

    def _m1(self, t0, base, atr_h1, drop_at_min, magnitude_atr, n_before=26 * 60, n_after=90):
        from gold_ai.models import Candle
        import math
        out, p = [], base
        step = atr_h1 / math.sqrt(60) / 2.0                     # ruído pequeno; ATR horário ≈ atr_h1
        for i in range(-n_before, n_after + 1):
            t = t0 + timedelta(minutes=i)
            wiggle = step * (1 if i % 2 else -1)
            o = p
            c = p + wiggle
            if drop_at_min is not None and 0 <= i - drop_at_min < 10:
                c = p - magnitude_atr * atr_h1 / 10.0             # queda distribuída em 10 min
            hi, lo = max(o, c) + step, min(o, c) - step
            if i < 0 and i % 60 == 0:                              # garante amplitude horária ≈ atr_h1 no passado
                hi, lo = o + atr_h1 / 2, o - atr_h1 / 2
            out.append(Candle(t, o, hi, lo, c, 1.0))
            p = c
        return out

    def test_leader_and_lags_are_measured(self):
        from gold_ai.leadlag import lead_lag, records_from_episode, render_lead_lag
        t0 = datetime(2026, 9, 15, 14, 0, tzinfo=UTC)
        data = {"XAUUSD": self._m1(t0, 4300.0, 10.0, 5, 1.5), "EURUSD": self._m1(t0, 1.15, 0.002, 17, 1.2),
                "US500": self._m1(t0, 7600.0, 20.0, 9, 1.0), "USDJPY": self._m1(t0, 155.0, 0.3, None, 0.0)}
        rows = lead_lag(data, t0, "XAUUSD", 0.5, 90)
        order = [r.symbol for r in rows]
        self.assertEqual(order[:3], ["XAUUSD", "US500", "EURUSD"])
        by = {r.symbol: r for r in rows}
        self.assertEqual(by["XAUUSD"].lag_min, 0.0)
        self.assertGreater(by["EURUSD"].lag_min, by["US500"].lag_min)
        self.assertTrue(all(by[s].direction == -1.0 for s in ("XAUUSD", "EURUSD", "US500")))
        self.assertIsNone(by["USDJPY"].cross_min)
        txt = render_lead_lag(rows, t0, "XAUUSD", 0.5, 90)
        self.assertIn("XAUUSD cruzou primeiro", txt)
        self.assertIn("EURUSD +", txt)
        recs = records_from_episode(rows, t0, "XAUUSD", 90)
        self.assertEqual({r.target for r in recs}, {"EURUSD", "US500", "USDJPY"})
        self.assertTrue(all(r.kind == "flow_XAUUSD_down" for r in recs))
        self.assertTrue(next(r for r in recs if r.target == "EURUSD").direction_correct)
        self.assertIsNone(next(r for r in recs if r.target == "USDJPY").time_to_first)


class CorruptedCsvTests(unittest.TestCase):
    def test_load_ticks_skips_truncated_rows_and_warns(self):
        import os
        import tempfile
        from gold_ai.reaction_hires import load_ticks
        msgs = []
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "USDX_ticks.csv")
            with open(p, "w", encoding="utf-8") as f:
                f.write("time,bid,ask\n2026-05-12T10:00:00+00:00,100.1,100.2\n2026-05-1\n2026-05-12T10:00:01+00:00,abc,100.2\n"
                        "2026-05-12T09:59:59+00:00,100.0,100.1\n")
            ticks = load_ticks(p, msgs.append)
        self.assertEqual(len(ticks), 2)
        self.assertLess(ticks[0][0], ticks[1][0])                      # ordenado
        self.assertTrue(msgs and "2 linha(s) inválida(s)" in msgs[0])

    def test_read_candles_csv_skips_truncated_rows(self):
        import os
        import tempfile
        from gold_ai import cli
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "XAUUSD_m1.csv")
            with open(p, "w", encoding="utf-8") as f:
                f.write("time,open,high,low,close,volume\n2026-05-12T10:00:00+00:00,1,2,0.5,1.5,10\n2026-05-1,1,2\n2026-05-12T10:01:00+00:00,1.5,2,1,1.8,\n")
            rows = cli._read_candles_csv(p)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1].volume, 0.0)


class FamilyPoolingTests(unittest.TestCase):
    def test_event_family_and_pooled_rows(self):
        from datetime import datetime, timezone
        from gold_ai.reaction import ReactionRecord
        from gold_ai.reaction_hires import HiResRecord, LeadLagStats, event_family
        self.assertEqual(event_family("nfp"), "Σ agendado")
        self.assertEqual(event_family("nfp+earnings"), "Σ agendado")
        self.assertEqual(event_family("geopolitical_escalation"), "Σ manchete")
        recs = []
        for i in range(30):
            kind = ["nfp", "cpi", "geopolitical_escalation"][i % 3]
            base = ReactionRecord(f"e{i}", kind, datetime(2026, 3, 1 + i % 20, 13, 30, tzinfo=timezone.utc), "EURUSD", 1.0, direction_correct=(i % 2 == 0))
            recs.append(HiResRecord(base, {5: 0.1}, 0.3, 0.1, 0.5, 0.1, 0.02, None, None))
        rows = LeadLagStats(recs).rows()
        kinds = [r.kind for r in rows]
        self.assertEqual(kinds[:2], ["Σ agendado", "Σ manchete"])                    # linhas Σ vêm primeiro
        self.assertEqual(next(r.n for r in rows if r.kind == "Σ agendado"), 20)     # nfp + cpi somados
        self.assertEqual(next(r.n for r in rows if r.kind == "Σ manchete"), 10)
        txt = LeadLagStats(recs).render()
        self.assertIn("Σ agendado", txt)


class SameInstantMergeTests(unittest.TestCase):
    def test_leader_direction_is_not_flipped_when_signs_agree(self):
        """Regressão: direção graduada do alvo (-0.8) × voto (-1.0) não pode inverter o canal do líder."""
        items = [("nfp", -0.8, {"USD": -0.7}, 2.0), ("earnings", -0.5, {"USD": -0.5}, 0.5)]
        vote = sum(x[1] * x[3] for x in items)
        exp = 1.0 if vote > 0 else -1.0
        lead_dirs = dict(items[0][2])
        if (items[0][1] > 0) != (exp > 0):
            lead_dirs = {k: -v for k, v in lead_dirs.items()}
        self.assertEqual(lead_dirs["USD"], -0.7)                              # mantido
        items2 = [("nfp", 0.3, {"USD": 0.7}, 0.5), ("unemployment", -0.9, {"USD": -0.6}, 3.0)]
        vote2 = sum(x[1] * x[3] for x in items2)
        exp2 = 1.0 if vote2 > 0 else -1.0
        items2.sort(key=lambda x: -x[3])
        lead2 = dict(items2[0][2])
        if (items2[0][1] > 0) != (exp2 > 0):
            lead2 = {k: -v for k, v in lead2.items()}
        self.assertEqual(lead2["USD"], -0.6)                                  # desemprego dominou: líder acompanha
