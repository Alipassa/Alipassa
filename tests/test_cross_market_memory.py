"""5.x: a memória resolve cada previsão/operação SÓ com o preço do seu mercado (antes, EURUSD 1,15 era comparado com ouro 3 600)."""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from gold_ai.memory import PredictionMemory
from gold_ai.models import Candle, Direction
from gold_ai.trading import TradePlan

T0 = datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc)


def _insert_prediction(mem: PredictionMemory, symbol: str, price: float, direction: str, atr: float, t: datetime = T0) -> int:
    cur = mem.conn.execute(
        """INSERT INTO predictions (data, hora, sessao, preco, previsao, probabilidade, confianca, score, horizonte, estagio,
           fundamentos, noticias, atr, horizonte_min, ativo, sinal_tipo) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (t.strftime("%Y-%m-%d"), t.strftime("%H:%M:%S"), "LONDRES", price, direction, 0.72, 70, 55, "4h", "PRÉ-MOVIMENTO",
         "{}", "[]", atr, 240, symbol, "GOLD WATCH"))
    mem.conn.commit()
    return int(cur.lastrowid)


def _flat(price: float, minutes: int, drift: float = 0.0) -> list[Candle]:
    return [Candle(T0 + timedelta(minutes=m), price + drift * m, price + drift * m, price + drift * m, price + drift * m, 1.0) for m in range(1, minutes + 1)]


def _plan(direction: Direction, entry: float, stop: float, atr: float) -> TradePlan:
    p = TradePlan(direction, entry, stop, atr, T0)
    p.lots, p.risk_usd, p.signal_type = 0.1, 100.0, "GOLD SELL"
    return p


class CrossMarketResolutionTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.mem = PredictionMemory(os.path.join(self.d.name, "m.db"))

    def tearDown(self):
        self.mem.close(); self.d.cleanup()

    def test_gold_candles_never_resolve_eurusd_prediction(self):
        pe = _insert_prediction(self.mem, "EURUSD", 1.15, "BAIXA", 0.0006)
        pg = _insert_prediction(self.mem, "XAUUSD", 3600.0, "BAIXA", 9.0)
        gold = _flat(3600.0, 30, drift=-0.5)          # ouro cai 15 USD: ACERTO do ouro
        done = self.mem.auto_resolve(gold, T0 + timedelta(minutes=30), 9.0, symbol="XAUUSD")
        self.assertEqual([pid for pid, _ in done], [pg])
        self.assertEqual(done[0][1].result, "ACERTO")
        self.assertEqual([r["id"] for r in self.mem.pending()], [pe])   # EURUSD continua pendente
        eur = _flat(1.15, 30, drift=+0.00005)          # EURUSD sobe 15 pips: ERRO da venda
        done = self.mem.auto_resolve(eur, T0 + timedelta(minutes=30), 0.0006, symbol="EURUSD")
        self.assertEqual(done[0][1].result, "ERRO")
        self.assertLess(done[0][1].mae, 0.01)          # excursão na escala do EURUSD, não do ouro

    def test_trades_simulated_only_with_own_market(self):
        te = self.mem.open_trade(_plan(Direction.BAIXA, 1.15, 1.1512, 0.0006), "PAPER", symbol="EURUSD")
        tg = self.mem.open_trade(_plan(Direction.BAIXA, 3600.0, 3612.0, 9.0), "PAPER", symbol="XAUUSD")
        gold = _flat(3600.0, 300, drift=+0.1)         # ouro sobe 30 USD: stop do ouro
        done = self.mem.auto_resolve_trades(gold, T0 + timedelta(minutes=300), symbol="XAUUSD")
        self.assertEqual([tid for tid, _ in done], [tg])
        self.assertEqual([r["id"] for r in self.mem.open_trades()], [te])

    def test_prices_kept_per_symbol(self):
        self.mem.store_prices(_flat(3600.0, 3), "XAUUSD")
        self.mem.store_prices(_flat(1.15, 3), "EURUSD")
        self.assertEqual(len(self.mem.prices(symbol="XAUUSD")), 3)
        self.assertEqual(len(self.mem.prices(symbol="EURUSD")), 3)
        self.assertEqual(self.mem.price_symbols(), ["EURUSD", "XAUUSD"])

    def test_old_prices_table_migrates(self):
        path = os.path.join(self.d.name, "old.db")
        conn = sqlite3.connect(path)
        conn.executescript("CREATE TABLE prices (hora TEXT PRIMARY KEY, close REAL NOT NULL); INSERT INTO prices VALUES ('2026-09-16T10:00:00+00:00', 3600.0);")
        conn.commit(); conn.close()
        mem = PredictionMemory(path)
        self.assertEqual(mem.prices(symbol="XAUUSD"), [(datetime(2026, 9, 16, 10, 0, tzinfo=timezone.utc), 3600.0)])
        mem.store_prices(_flat(1.15, 1), "EURUSD")
        self.assertEqual(len(mem.prices()), 2)
        mem.close()


class RepairTests(unittest.TestCase):
    def test_detects_and_repairs_contamination(self):
        with tempfile.TemporaryDirectory() as d:
            mem = PredictionMemory(os.path.join(d, "m.db"))
            pe = _insert_prediction(mem, "EURUSD", 1.15, "BAIXA", 0.0006)
            pu = _insert_prediction(mem, "US500", 6500.0, "BAIXA", 20.0)
            pg = _insert_prediction(mem, "XAUUSD", 3600.0, "BAIXA", 9.0)
            te = mem.open_trade(_plan(Direction.BAIXA, 1.15, 1.1512, 0.0006), "PAPER", symbol="EURUSD")
            # build antiga: tudo resolvido com os candles do ouro
            gold = _flat(3600.0, 30, drift=-0.5)
            mem.auto_resolve(gold, T0 + timedelta(minutes=30), 9.0)         # sem symbol = comportamento antigo
            mem.auto_resolve_trades(gold, T0 + timedelta(minutes=5))   # o live só resolve no ciclo seguinte
            res = {r["id"]: r["resultado"] for r in mem.conn.execute("SELECT id, resultado FROM predictions")}
            self.assertEqual(res[pe], "ERRO")       # 1,15 → 3 600: "subiu" → venda errada (o 0 % do EURUSD)
            self.assertEqual(res[pu], "ACERTO")     # 6 500 → 3 600: "caiu" → venda certa (o 100 % do US500)
            self.assertEqual(res[pg], "ACERTO")
            bad = mem.contaminated_predictions()
            self.assertEqual({r["id"] for r in bad}, {pe, pu})
            self.assertEqual([r["id"] for r in mem.contaminated_trades()], [te])
            # conserto com o M1 certo de cada mercado
            eur = _flat(1.15, 300, drift=-0.00001)   # EURUSD cai 30 pips: venda ACERTO, operação chega a 1R+
            rep = mem.repair_cross_market(T0 + timedelta(minutes=300), {"EURUSD": eur, "XAUUSD": gold})
            self.assertEqual(rep["previsoes"], {"EURUSD": 1, "US500": 1})
            self.assertEqual(rep["operacoes"], {"EURUSD": 1})
            res = {r["id"]: r["resultado"] for r in mem.conn.execute("SELECT id, resultado FROM predictions")}
            self.assertEqual(res[pe], "ACERTO")
            self.assertIsNone(res[pu])              # sem M1 do US500: fica pendente, nunca inventado
            self.assertEqual(res[pg], "ACERTO")
            self.assertEqual(mem.contaminated_predictions(), [])
            self.assertEqual(mem.contaminated_trades(), [])
            tr = mem.conn.execute("SELECT status, estopada, max_r FROM trades WHERE id=?", (te,)).fetchone()
            self.assertEqual(tr["status"], "CLOSED")
            self.assertEqual(tr["estopada"], 0)
            self.assertGreater(tr["max_r"], 1.0)
            self.assertEqual(mem.price_symbols(), ["EURUSD", "XAUUSD"])
            mem.close()

    def test_resimulates_closed_trades_and_keeps_prices_to_lived_period(self):
        with tempfile.TemporaryDirectory() as d:
            mem = PredictionMemory(os.path.join(d, "m.db"))
            _insert_prediction(mem, "EURUSD", 1.15, "BAIXA", 0.0006, T0 + timedelta(hours=30))
            te = mem.open_trade(_plan(Direction.BAIXA, 1.15, 1.1512, 0.0006), "PAPER", symbol="EURUSD")
            mem.conn.execute("UPDATE trades SET aberta_em=? WHERE id=?", ((T0 + timedelta(hours=30)).isoformat(), te))
            # perfil "fechado" com candles errados 20 min depois (fora da janela de detecção automática)
            mem.conn.execute("UPDATE trades SET status='CLOSED', estopada=1, max_r=0.02, mae_r=1.0, fechada_em=? WHERE id=?",
                             ((T0 + timedelta(hours=30, minutes=20)).isoformat(), te))
            mem.conn.commit()
            self.assertEqual(mem.contaminated_trades(), [])
            days = [Candle(T0 + timedelta(minutes=m), 1.15 - 0.00001 * max(0, m - 1800), 1.15, 1.15 - 0.00001 * max(0, m - 1800), 1.15 - 0.00001 * max(0, m - 1800), 1.0)
                    for m in range(1, 60 * 40)]
            rep = mem.repair_cross_market(T0 + timedelta(hours=40), {"EURUSD": days})
            self.assertEqual(rep["re_simuladas"], {"EURUSD": 1})
            tr = mem.conn.execute("SELECT status, estopada, max_r FROM trades WHERE id=?", (te,)).fetchone()
            self.assertEqual(tr["status"], "CLOSED")
            self.assertEqual(tr["estopada"], 0)
            self.assertGreater(tr["max_r"], 1.0)
            # preços guardados só desde 4 h antes do 1º registro vivido, não os 40 h do M1
            ps = mem.prices(symbol="EURUSD")
            self.assertGreaterEqual(ps[0][0], T0 + timedelta(hours=26))
            self.assertLess(len(ps), 15 * 60)
            mem.close()


if __name__ == "__main__":
    unittest.main()


class RestartRequestTests(unittest.TestCase):
    def test_restart_file_and_command_set_flag(self):
        from gold_ai.guard import GuardLimits, KillSwitch, TelegramCommands, TradingMode
        from gold_ai.market_engine import MarketAIEngine
        from gold_ai.selector import PortfolioLimits
        from gold_ai.telegram import TelegramSender

        with tempfile.TemporaryDirectory() as d:
            mem = PredictionMemory(os.path.join(d, "m.db"))
            logs: list[str] = []
            eng = MarketAIEngine(mem, GuardLimits(), ("XAUUSD",), TradingMode.PAPER, 10000.0, PortfolioLimits(),
                                 sender=TelegramSender(dry_run=True, quiet=True), log=logs.append)
            eng.restart_file = os.path.join(d, "REINICIAR")
            self.assertFalse(eng.restart_requested)
            eng.check_restart_file()
            self.assertFalse(eng.restart_requested)
            open(eng.restart_file, "w").close()
            eng.check_restart_file()
            self.assertTrue(eng.restart_requested)
            self.assertFalse(os.path.exists(eng.restart_file))       # lido uma vez só
            self.assertTrue(any("reinício pedido" in l for l in logs))
            self.assertEqual(TelegramCommands(None, None).apply(["/REINICIAR"], KillSwitch()), ["RESTART"])
            mem.close()
