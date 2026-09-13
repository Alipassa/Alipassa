"""Testes GOLD AI 2.0: Data Engine (parsers + normalização), evidência, vantagem, avaliação e backtest."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from gold_ai import Direction, EngineConfig, EvidenceLevel, GoldAIEngine, MarketSnapshot, SignalType, Stage
from gold_ai.data import DataEngine, DataEngineConfig, DataError
from gold_ai.data.cftc import parse_rows
from gold_ai.data.fred import parse_csv
from gold_ai.data.news import RuleInterpreter, aggregate_sentiment, geopolitical_index, parse_rss
from gold_ai.data.yahoo import YahooCollector, parse_chart, resample
from gold_ai.evaluation import Backtester, HistoryFrame, SignalRecord, detect_moves, evaluate, walk_forward
from gold_ai.evidence import edge_status, event_chain, evidence_level
from gold_ai.models import EconomicEvent, NewsItem
from gold_ai.sources.sample import SampleSource, make_candles

NOW = datetime(2026, 9, 14, 13, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- fixtures de payload real
def yahoo_payload(n: int, start_ts: int, step: int, base: float, drift: float = 0.0) -> dict:
    ts, o, h, l, c, v = [], [], [], [], [], []
    p = base
    for i in range(n):
        ts.append(start_ts + i * step)
        if i == 3:  # barra nula, como o Yahoo devolve às vezes
            o.append(None); h.append(None); l.append(None); c.append(None); v.append(None)
            continue
        o.append(p); c.append(p + drift); h.append(max(p, p + drift) + 1); l.append(min(p, p + drift) - 1); v.append(100 + i)
        p += drift
    return {"chart": {"result": [{"meta": {"symbol": "GC=F", "regularMarketPrice": p},
                                   "timestamp": ts,
                                   "indicators": {"quote": [{"open": o, "high": h, "low": l, "close": c, "volume": v}]}}],
                      "error": None}}


class FakeHttp:
    """HttpClient simulado: devolve payloads por padrão de URL."""

    def __init__(self, routes: dict[str, object]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def _route(self, url: str):
        self.calls.append(url)
        for key, val in self.routes.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                return val
        raise DataError(f"rota não simulada: {url}")

    def get_text(self, url: str, ttl=None, headers=None) -> str:
        v = self._route(url)
        return v if isinstance(v, str) else json.dumps(v)

    def get_json(self, url: str, ttl=None):
        v = self._route(url)
        return json.loads(v) if isinstance(v, str) else v


FRED_CSV = "DATE,DFII10\n2026-09-09,1.85\n2026-09-10,.\n2026-09-11,1.80\n2026-09-12,1.74\n"
COT_ROWS = [
    {"report_date_as_yyyy_mm_dd": f"2026-0{m}-0{d}T00:00:00.000", "m_money_positions_long_all": str(150000 + i * 2000),
     "m_money_positions_short_all": "40000", "prod_merc_positions_long_all": "30000", "prod_merc_positions_short_all": str(120000 + i * 1000),
     "swap_positions_long_all": "10000", "swap_positions_short_all": "50000"}
    for i, (m, d) in enumerate([(7, 1), (7, 8), (8, 5), (8, 12), (9, 2), (9, 9)])
]
RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Feed Teste</title>
<item><title>US CPI rises 2.8% vs 3.0% expected as inflation cools</title><pubDate>Mon, 14 Sep 2026 12:35:00 GMT</pubDate></item>
<item><title>Dollar falls as Treasury yields slide after data</title><pubDate>Mon, 14 Sep 2026 12:50:00 GMT</pubDate></item>
<item><title>Missile strike escalates Middle East conflict</title><pubDate>Mon, 14 Sep 2026 11:00:00 GMT</pubDate></item>
<item><title>Fed decision comes in as expected, no change in guidance</title><pubDate>Sun, 13 Sep 2026 18:00:00 GMT</pubDate></item>
<item><title>Old headline about something</title><pubDate>Fri, 01 Aug 2026 18:00:00 GMT</pubDate></item>
</channel></rss>"""


class ParserTests(unittest.TestCase):
    def test_yahoo_parse_skips_null_bars_and_resamples(self):
        cs = parse_chart(yahoo_payload(12, 1_700_000_000, 3600, 2600, 1.0))
        self.assertEqual(len(cs), 11)
        self.assertEqual(cs[0].time.tzinfo, timezone.utc)
        h4 = resample(cs, 240)
        self.assertTrue(2 <= len(h4) <= 4)
        self.assertEqual(h4[0].open, cs[0].open)
        with self.assertRaises(DataError):
            parse_chart({"chart": {"result": None, "error": {"description": "No data"}}})

    def test_yahoo_change_over_uses_timestamps(self):
        cs = parse_chart(yahoo_payload(30, 1_700_000_000, 900, 100.0, 0.5))
        ch = YahooCollector.change_over(cs, 60)
        self.assertIsNotNone(ch)
        self.assertGreater(ch, 0)
        self.assertAlmostEqual(YahooCollector.change_over(cs, 60, pct=False), 2.0, places=6)

    def test_fred_csv(self):
        data = parse_csv(FRED_CSV)
        self.assertEqual(len(data), 3)  # linha com "." ignorada
        self.assertEqual(data[-1][1], 1.74)

    def test_cot_rows(self):
        r = parse_rows(COT_ROWS)
        self.assertEqual(r.history_weeks, 6)
        self.assertEqual(r.managed_money_net_change, 2000)
        self.assertEqual(r.managed_money_percentile, 100.0)
        self.assertLess(r.commercial_net_change, 0)
        with self.assertRaises(DataError):
            parse_rows(COT_ROWS[:1])

    def test_rss_and_rule_interpreter(self):
        items = parse_rss(RSS, "teste")
        self.assertEqual(len(items), 5)
        interp = RuleInterpreter()
        items = [interp.interpret(i) for i in items]
        cpi = items[0]
        self.assertEqual(cpi.category, "macro")
        self.assertGreater(cpi.gold_impact, 0)  # CPI abaixo do consenso → favorável
        self.assertEqual(len(interp.events), 1)
        self.assertEqual(interp.events[0].kind, "cpi")
        self.assertAlmostEqual(interp.events[0].surprise(), -0.2, places=6)
        self.assertGreater(items[1].gold_impact, 0)
        self.assertEqual(items[2].category, "geopolitical")
        self.assertEqual(items[3].priced_in, 0.8)
        s, ch = aggregate_sentiment(items, NOW)
        self.assertGreater(s, 0)
        g, dg = geopolitical_index(items, NOW)
        self.assertGreater(g, 0)


class DataEngineTests(unittest.TestCase):
    def _routes(self, fail: tuple[str, ...] = ()) -> dict:
        ts = int(NOW.timestamp())
        routes = {
            "GC%3DF?interval=1m": yahoo_payload(300, ts - 300 * 60, 60, 2650, 0.0),
            "GC%3DF?interval=5m": yahoo_payload(300, ts - 300 * 300, 300, 2650, 0.05),
            "GC%3DF?interval=15m": yahoo_payload(300, ts - 300 * 900, 900, 2650, 0.05),
            "GC%3DF?interval=30m": yahoo_payload(300, ts - 300 * 1800, 1800, 2650, 0.05),
            "GC%3DF?interval=1h": yahoo_payload(300, ts - 300 * 3600, 3600, 2600, 0.15),
            "GC%3DF?interval=1d": yahoo_payload(300, ts - 300 * 86400, 86400, 2300, 1.0),
            "GC%3DF?interval=1wk": yahoo_payload(150, ts - 150 * 604800, 604800, 1800, 5.0),
            "DX-Y.NYB": yahoo_payload(40, ts - 40 * 900, 900, 103.5, -0.01),
            "%5ETNX": yahoo_payload(40, ts - 40 * 900, 900, 4.10, -0.005),
            "2YY%3DF": yahoo_payload(40, ts - 40 * 900, 900, 3.60, 0.0),
            "ZQ%3DF": yahoo_payload(40, ts - 40 * 900, 900, 96.00, 0.01),
            "%5EVIX": yahoo_payload(40, ts - 40 * 900, 900, 15.0, 0.02),
            "%5EGSPC": yahoo_payload(40, ts - 40 * 900, 900, 5600, 1.0),
            "SI%3DF": yahoo_payload(40, ts - 40 * 900, 900, 30.0, 0.01),
            "CL%3DF": yahoo_payload(40, ts - 40 * 900, 900, 70.0, 0.05),
            "BTC-USD": yahoo_payload(40, ts - 40 * 900, 900, 60000, 10.0),
            "CNH%3DX": yahoo_payload(40, ts - 40 * 900, 900, 7.10, -0.001),
            "fredgraph.csv?id=DFII10": FRED_CSV,
            "fredgraph.csv?id=T10YIE": "DATE,T10YIE\n2026-09-11,2.30\n2026-09-12,2.32\n",
            "fredgraph.csv?id=BAMLH0A0HYM2": "DATE,BAMLH0A0HYM2\n2026-09-11,3.20\n2026-09-12,3.25\n",
            "publicreporting.cftc.gov": COT_ROWS,
            "fxstreet.com": RSS,
            "kitco.com": RSS,
            "dowjones.io": DataError("feed fora do ar"),
        }
        for f in fail:
            routes[f] = DataError("simulado")
        return routes

    def test_collect_normalizes_all_layers(self):
        http = FakeHttp(self._routes())
        de = DataEngine(DataEngineConfig(cache_dir=None), http=http)
        s = de.collect(NOW)
        self.assertEqual(set(s.candles), {"M1", "M5", "M15", "M30", "H1", "H4", "D1", "W1"})
        self.assertGreater(s.price, 0)
        self.assertGreater(s.atr, 0)
        self.assertIsNotNone(s.dxy_change_pct)
        self.assertLess(s.dxy_change_pct, 0)
        self.assertLess(s.us10y_change_bp, 0)
        self.assertIsNotNone(s.real_yield_change_bp)
        self.assertIsNotNone(s.fed_cut_prob_change_pp)
        self.assertGreater(s.fed_cut_prob_change_pp, 0)  # preço ZQ subindo → taxa implícita caindo
        self.assertEqual(s.credit_spread_bp, 325.0)
        self.assertEqual(s.cot_managed_money_net_change, 2000)
        self.assertTrue(s.news)
        self.assertIsNotNone(s.sentiment)
        self.assertTrue(any(e.kind == "cpi" for e in s.events))
        self.assertIsNotNone(s.order_flow_imbalance)
        self.assertEqual(de.status["xau"], "ok")
        self.assertIn("news", de.status)  # um feed falhou → status registra, sem quebrar
        self.assertIn("erro", de.status["news"])
        # o motor consome o snapshot real sem erro
        a = GoldAIEngine().analyze(s)
        self.assertTrue(-100 <= a.score <= 100)
        self.assertIn("CADEIA DE RACIOCÍNIO", a.chain)
        self.assertIn("CPI", a.chain)

    def test_partial_failure_is_isolated(self):
        http = FakeHttp(self._routes(fail=("DX-Y.NYB", "CNH%3DX", "fredgraph.csv?id=DFII10", "publicreporting.cftc.gov")))
        de = DataEngine(DataEngineConfig(cache_dir=None), http=http)
        s = de.collect(NOW)
        self.assertIsNone(s.dxy_change_pct)
        self.assertIsNone(s.cot_managed_money_net_change)
        self.assertTrue(de.status["dxy"].startswith("erro"))
        self.assertTrue(de.status["cot"].startswith("erro"))
        self.assertEqual(de.status["xau"], "ok")
        self.assertIn("✗ dxy", de.coverage())
        a = GoldAIEngine().analyze(s)
        self.assertFalse(a.factor("dolar").available)

    def test_xau_failure_leaves_empty_candles(self):
        http = FakeHttp(self._routes(fail=("GC%3DF?interval=1m", "GC%3DF?interval=5m", "GC%3DF?interval=15m", "GC%3DF?interval=30m",
                                           "GC%3DF?interval=1h", "GC%3DF?interval=1d", "GC%3DF?interval=1wk")))
        de = DataEngine(DataEngineConfig(cache_dir=None), http=http)
        s = de.collect(NOW)
        self.assertEqual(s.candles, {})
        self.assertTrue(de.status["xau"].startswith("erro"))


class EvidenceTests(unittest.TestCase):
    def test_levels(self):
        eng = GoldAIEngine()
        a = eng.analyze(SampleSource("neutro").snapshot())
        self.assertEqual(a.evidence_level, EvidenceLevel.NONE)
        self.assertFalse(a.has_edge)
        self.assertIn("SEM VANTAGEM", a.edge_status)
        a = eng.analyze(SampleSource("premove_alta").snapshot())
        self.assertGreaterEqual(a.evidence_level, EvidenceLevel.L2_ALERTA)
        self.assertTrue(a.has_edge)
        # nível 4: macro + fluxo + técnico + notícia + divergência
        s = SampleSource("premove_alta").snapshot()
        s.candles = SampleSource("confirmacao_alta").snapshot().candles  # técnico alinhado
        s.price_change_pct = 0.05
        a = eng.analyze(s)
        if a.premove.stage == Stage.PRE_MOVIMENTO and "tecnico" in a.confirmations and "fluxo" in a.confirmations:
            self.assertEqual(a.evidence_level, EvidenceLevel.L4_PREMOVE_FORTE)

    def test_no_edge_blocks_directional_signal(self):
        cfg = EngineConfig()
        cfg.min_edge_confidence = 101  # nunca há vantagem
        eng = GoldAIEngine(cfg)
        a, sig = eng.run_cycle(SampleSource("venda").snapshot())
        self.assertFalse(a.has_edge)
        self.assertIsNone(sig)
        # mas risco sistêmico continua passando
        a, sig = eng.run_cycle(SampleSource("sistemico").snapshot())
        self.assertEqual(sig.type, SignalType.RISK)

    def test_watch_signal(self):
        cfg = EngineConfig()
        cfg.buy, cfg.strong_buy = 90, 95  # BUY inalcançável → só WATCH
        cfg.premove_fundamental_threshold = 200  # sem PRE-MOVE
        eng = GoldAIEngine(cfg)
        a, sig = eng.run_cycle(SampleSource("confirmacao_alta").snapshot())
        self.assertIsNotNone(sig)
        self.assertEqual(sig.type, SignalType.WATCH)
        self.assertIn("GOLD WATCH", sig.text)
        self.assertIn("Aguardando confirmação", sig.text)

    def test_confirmed_signal_text(self):
        eng = GoldAIEngine()
        src = SampleSource("premove_alta")
        _, s1 = eng.run_cycle(src.snapshot())
        self.assertEqual(s1.type, SignalType.PRE_MOVE)
        self.assertIn("CADEIA DE RACIOCÍNIO", s1.text)
        src.scenario, src.now, src.price = "confirmacao_alta", src.now + timedelta(minutes=30), src.price + 6
        _, s2 = eng.run_cycle(src.snapshot())
        self.assertIn("GOLD SIGNAL", s2.text)
        self.assertIn("PRE-MOVE CONFIRMADO", s2.text)

    def test_event_chain_with_release(self):
        s = SampleSource("premove_alta").snapshot()
        s.events = [EconomicEvent("CPI", s.time - timedelta(minutes=20), "MUITO ALTO", consensus=3.0, actual=2.8, kind="cpi", unit="%")]
        a = GoldAIEngine().analyze(s)
        chain = a.chain
        self.assertIn("1. O que aconteceu? CPI = 2.8%", chain)
        self.assertIn("3. Surpresa: -0.20%", chain)
        self.assertIn("ainda não reagiu", chain)
        self.assertIn("PRESSÃO COMPRADORA LATENTE", chain)


class EvaluationTests(unittest.TestCase):
    def _path(self, moves: list[tuple[int, float]], start_price: float = 2650.0):
        """moves: lista de (minutos, delta por minuto)."""
        path, p, t = [], start_price, NOW
        for mins, d in moves:
            for _ in range(mins):
                t += timedelta(minutes=1)
                p += d
                path.append((t, p))
        return path

    def test_detect_moves(self):
        path = self._path([(30, 0.0), (30, 0.5), (60, 0.0), (30, -0.5), (30, 0.0)])
        moves = detect_moves(path, threshold=9.0, horizon_min=120)
        dirs = [m.direction for m in moves]
        self.assertIn("ALTA", dirs)
        self.assertIn("BAIXA", dirs)
        self.assertLessEqual(len(moves), 3)

    def test_metrics_precision_recall_lead(self):
        path = self._path([(30, 0.0), (30, 0.5), (60, 0.0), (30, -0.5), (30, 0.0)])
        sigs = [
            SignalRecord(NOW + timedelta(minutes=10), "ALTA", "GOLD PRE-MOVE", 2650.0, 9.0, 4),          # acerto, lead ~38 min
            SignalRecord(NOW + timedelta(minutes=110), "ALTA", "GOLD BUY", path[109][1], 9.0, 2),        # erro (cai)
            SignalRecord(NOW + timedelta(minutes=115), "BAIXA", "GOLD SELL", path[114][1], 9.0, 3),      # acerto
        ]
        m = evaluate(sigs, path, threshold=9.0, horizon_min=120)
        self.assertEqual(m.n_signals, 3)
        self.assertEqual(m.n_buy, 2)
        self.assertAlmostEqual(m.precision_buy, 0.5)
        self.assertEqual(m.precision_sell, 1.0)
        self.assertIsNotNone(m.recall)
        self.assertGreater(m.recall, 0)
        self.assertEqual(len(m.lead_times), 2)
        self.assertGreater(m.lead_time_avg, 5)
        self.assertGreater(m.gold_lead_score, 0)
        self.assertGreater(m.mfe_avg, 0)
        self.assertIn(4, m.by_level)
        self.assertIn("GOLD LEAD SCORE", m.render())

    def test_empty(self):
        m = evaluate([], [(NOW, 1.0)], 1.0)
        self.assertIsNone(m.precision)
        self.assertIn("n/d", m.render())

    def _frame(self, n: int = 700) -> HistoryFrame:
        xau = make_candles("H1", n, 2500, 0.4, 6.0, NOW, seed=3)
        dxy = make_candles("H1", n, 104, -0.002, 0.08, NOW, seed=4)
        tnx = make_candles("H1", n, 4.2, -0.0005, 0.02, NOW, seed=5)
        return HistoryFrame(xau=xau, dxy=dxy, us10y=tnx)

    def test_snapshot_at_has_no_lookahead(self):
        f = self._frame()
        s = f.snapshot_at(400)
        self.assertEqual(s.time, f.xau[400].time)
        self.assertTrue(all(c.time <= s.time for c in s.candles["H1"]))
        self.assertTrue(all(c.time <= s.time for c in s.candles["D1"]))
        self.assertIsNotNone(s.dxy_change_pct)
        self.assertIsNotNone(s.us10y_change_bp)

    def test_backtest_and_walk_forward_run(self):
        f = self._frame()
        bt = Backtester(f, warmup=230, step=4, include_watch=True)
        r = bt.run()
        self.assertGreater(r.n_steps, 50)
        self.assertIn("BACKTEST", r.render())
        wf = walk_forward(bt, n_folds=2, grid=[{"buy": 40, "sell": -40, "min_confirmations": 2}, {"buy": 60, "sell": -60, "min_confirmations": 3}])
        self.assertEqual(len(wf.folds), 2)
        self.assertIn("WALK-FORWARD", wf.render())


if __name__ == "__main__":
    unittest.main()
