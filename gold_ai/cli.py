"""CLI: `gold-ai demo`, `gold-ai run`, `gold-ai stats`, `gold-ai event`."""

from __future__ import annotations

import argparse
import sys
import time
from datetime import timedelta

from .config import EngineConfig
from .engine import GoldAIEngine
from .events import analyze_post_event, build_scenario_tree, upcoming_events
from .memory import PredictionMemory
from .report import render_report
from .sources.sample import SampleSource
from .telegram import TelegramSender


def cmd_demo(args: argparse.Namespace) -> int:
    engine = GoldAIEngine(EngineConfig())
    sender = TelegramSender(dry_run=not args.send)
    mem = PredictionMemory(args.db) if args.db else None
    scenarios = args.scenarios or ["neutro", "premove_alta", "confirmacao_alta", "reversao", "venda", "sistemico", "pre_evento"]
    src = SampleSource()
    for i, sc in enumerate(scenarios):
        src.scenario = sc
        src.now = src.now + timedelta(minutes=20)
        src.price += {"confirmacao_alta": 6, "venda": -8, "reversao": 4, "sistemico": -18}.get(sc, 0)
        snap = src.snapshot()
        assessment, signal = engine.run_cycle(snap, new_event_key=snap.news[0].headline if snap.news else None)
        print(f"\n{'=' * 78}\nCENÁRIO {i + 1}: {sc}\n{'=' * 78}")
        print(render_report(assessment))
        for ev in upcoming_events(snap.events, snap.time, hours=6):
            print("\n" + build_scenario_tree(ev, snap).render())
        if signal:
            print(f"\n>>> SINAL: {signal.type.value} ({signal.trigger})")
            sender.send(signal.text)
            if mem:
                pid = mem.record(assessment, signal.type.value)
                print(f"[memória] previsão #{pid} registrada")
        else:
            print("\n>>> sem sinal (anti-spam / critérios não atingidos)")
    if mem:
        mem.close()
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    """Loop contínuo. Substitua SampleSource por uma fonte real (gold_ai.sources.base.DataSource)."""
    engine = GoldAIEngine(EngineConfig())
    sender = TelegramSender(dry_run=not args.send)
    mem = PredictionMemory(args.db)
    src = SampleSource(scenario=args.scenario)
    try:
        while True:
            snap = src.snapshot()
            assessment, signal = engine.run_cycle(snap)
            if args.verbose:
                print(render_report(assessment))
            if signal:
                sender.send(signal.text)
                mem.record(assessment, signal.type.value)
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        mem.close()
    return 0


def _load_frame(args: argparse.Namespace):
    """HistoryFrame de CSV (time,open,high,low,close,volume) ou do Yahoo (H1, até ~3 meses)."""
    from .evaluation import HistoryFrame
    from .models import Candle
    from datetime import datetime, timezone
    import csv

    def read_csv(path: str) -> list[Candle]:
        out = []
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                t = datetime.fromisoformat(r["time"].replace("Z", "+00:00"))
                out.append(Candle(t if t.tzinfo else t.replace(tzinfo=timezone.utc), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r.get("volume") or 0)))
        return sorted(out, key=lambda c: c.time)

    if args.csv:
        return HistoryFrame(xau=read_csv(args.csv), dxy=read_csv(args.dxy_csv) if args.dxy_csv else [],
                            us10y=read_csv(args.us10y_csv) if args.us10y_csv else [])
    from .data import DataEngineConfig, HttpClient
    from .data.yahoo import YahooCollector
    y = YahooCollector(HttpClient(cache_dir=DataEngineConfig().cache_dir, ttl=900))
    return HistoryFrame(xau=y.candles(args.symbol, "H1"), dxy=y.candles("DX-Y.NYB", "H1"), us10y=y.candles("^TNX", "H1"),
                        vix=y.candles("^VIX", "H1"), spx=y.candles("^GSPC", "H1"))


def cmd_live(args: argparse.Namespace) -> int:
    """Ciclo com DADOS REAIS (Yahoo/FRED/CFTC/RSS) → MarketSnapshot → GOLD AI → Telegram/SQLite."""
    from .data import DataEngine, DataEngineConfig

    dcfg = DataEngineConfig(xau_symbol=args.symbol, calendar_path=args.calendar, enable_cot=not args.no_cot,
                            enable_fred=not args.no_fred, enable_news=not args.no_news)
    data = DataEngine(dcfg)
    source = data
    executor = None
    if args.source == "mt5":
        from .data.mt5 import MT5Config, MT5Executor, MT5Source
        from .telegram import load_env_file

        mcfg = MT5Config.from_env(load_env_file())
        if args.mt5_path:
            mcfg.path = args.mt5_path
        source = MT5Source(mcfg, data_engine=data)
        if args.execute:
            executor = MT5Executor(source.client, volume=args.volume)
    engine = GoldAIEngine(EngineConfig())
    sender = TelegramSender(dry_run=not args.send)
    mem = PredictionMemory(args.db)
    try:
        while True:
            snap = source.collect() if source is data else source.snapshot()
            print(data.coverage())
            if source is not data:
                print(f"MT5: {source.status.get('mt5', 'n/d')}")
            if not snap.candles:
                print("sem candles XAU — ciclo abortado")
            else:
                assessment, signal = engine.run_cycle(snap, new_event_key=snap.news[0].headline if snap.news else None)
                print(render_report(assessment))
                if signal:
                    sender.send(signal.text)
                    mem.record(assessment, signal.type.value)
                    if executor is not None:
                        plan = executor.plan(signal)
                        if plan is not None:
                            plan = executor.execute(plan, authorize=args.authorize)
                            print(plan.render())
                            if args.send:
                                sender.send("🧾 " + plan.render())
                else:
                    print(">>> sem sinal — " + assessment.edge_status)
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        mem.close()
        if source is not data:
            source.client.close()
    return 0


def cmd_backtest(args: argparse.Namespace) -> int:
    from .evaluation import Backtester, walk_forward

    frame = _load_frame(args)
    bt = Backtester(frame, EngineConfig(), threshold_atr=args.threshold_atr, horizon_min=args.horizon, include_watch=args.include_watch)
    if args.walk_forward:
        print(walk_forward(bt, n_folds=args.folds).render())
    else:
        print(bt.run().render())
    return 0


def cmd_metrics(args: argparse.Namespace) -> int:
    """Confronta as previsões gravadas no SQLite com o caminho real do preço (CSV time,close)."""
    import csv
    from datetime import datetime, timezone

    path = []
    with open(args.path_csv, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            t = datetime.fromisoformat(r["time"].replace("Z", "+00:00"))
            path.append((t if t.tzinfo else t.replace(tzinfo=timezone.utc), float(r["close"])))
    mem = PredictionMemory(args.db)
    print(mem.metrics(path, args.threshold, args.horizon).render())
    mem.close()
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    mem = PredictionMemory(args.db)
    pending = mem.pending()
    print(f"Previsões pendentes (sem resultado): {len(pending)} — resolva com PredictionMemory.resolve(id, caminho_de_preço, limiar)")
    for by in args.by:
        print(f"\nTAXA DE ACERTO por {by}:")
        rows = mem.accuracy(by)
        if not rows:
            print("  (nenhuma previsão resolvida)")
        for row in rows:
            print(f"  {row['chave']!s:>12}  n={row['n']:<4} acertos={row['acertos']:<4} taxa={row['taxa']:.1%}")
    print("\nPODER PREDITIVO DOS FATORES (média alinhada nos acertos − nos erros):")
    for row in mem.factor_power():
        print(f"  {row['fator']:<12} poder={row['poder']:+.2f} (n={row['n']})")
    mem.close()
    return 0


def cmd_event(args: argparse.Namespace) -> int:
    src = SampleSource(scenario="pre_evento")
    snap = src.snapshot()
    ev = snap.events[0]
    print(build_scenario_tree(ev, snap).render())
    if args.actual is not None:
        ev.actual = args.actual
        snap.dxy_change_pct, snap.us10y_change_bp, snap.real_yield_change_bp = args.dxy, args.us10y, args.real
        snap.price_change_pct, snap.order_flow_imbalance = args.gold, args.flow
        print("\n" + analyze_post_event(ev, snap).render())
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gold-ai", description="GOLD AI ENGINE — inteligência preditiva do ouro (XAU/USD)")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("demo", help="roda cenários sintéticos e mostra relatórios/sinais")
    d.add_argument("--scenarios", nargs="*")
    d.add_argument("--send", action="store_true", help="envia de fato ao Telegram (usa TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID)")
    d.add_argument("--db", default=None, help="caminho do SQLite para registrar previsões")
    d.set_defaults(func=cmd_demo)

    r = sub.add_parser("run", help="loop contínuo de análise")
    r.add_argument("--interval", type=int, default=60)
    r.add_argument("--scenario", default="neutro")
    r.add_argument("--db", default="gold_ai.db")
    r.add_argument("--send", action="store_true")
    r.add_argument("--once", action="store_true")
    r.add_argument("-v", "--verbose", action="store_true")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("stats", help="taxa de acerto e poder preditivo dos fatores")
    s.add_argument("--db", default="gold_ai.db")
    s.add_argument("--by", nargs="*", default=["sessao", "previsao", "score_bucket", "estagio", "nivel_evidencia"])
    s.set_defaults(func=cmd_stats)

    e = sub.add_parser("event", help="árvore de reação pré-evento e cadeia pós-evento (exemplo CPI)")
    e.add_argument("--actual", type=float, default=None)
    e.add_argument("--dxy", type=float, default=0.0)
    e.add_argument("--us10y", type=float, default=0.0)
    e.add_argument("--real", type=float, default=0.0)
    e.add_argument("--gold", type=float, default=0.0)
    e.add_argument("--flow", type=float, default=0.0)
    e.set_defaults(func=cmd_event)

    lv = sub.add_parser("live", help="ciclo com dados reais (Yahoo/FRED/CFTC/RSS)")
    lv.add_argument("--symbol", default="GC=F", help="GC=F (futuro) ou XAUUSD=X (spot)")
    lv.add_argument("--calendar", default=None, help="JSON de eventos econômicos")
    lv.add_argument("--interval", type=int, default=300)
    lv.add_argument("--db", default="gold_ai.db")
    lv.add_argument("--send", action="store_true")
    lv.add_argument("--once", action="store_true")
    lv.add_argument("--no-cot", action="store_true")
    lv.add_argument("--no-fred", action="store_true")
    lv.add_argument("--no-news", action="store_true")
    lv.add_argument("--source", choices=["web", "mt5"], default="web", help="mt5 = candles/preço do terminal MetaTrader 5")
    lv.add_argument("--mt5-path", default=None, help="caminho do terminal64.exe (ou MT5_PATH no .env)")
    lv.add_argument("--execute", action="store_true", help="gera plano de ordem no MT5 (simulado, salvo com --authorize)")
    lv.add_argument("--authorize", action="store_true", help="AUTORIZA envio real de ordens ao broker")
    lv.add_argument("--volume", type=float, default=0.01)
    lv.set_defaults(func=cmd_live)

    bt = sub.add_parser("backtest", help="backtest / walk-forward sobre histórico H1 (CSV ou Yahoo)")
    bt.add_argument("--csv", default=None, help="CSV XAU H1: time,open,high,low,close,volume")
    bt.add_argument("--dxy-csv", default=None)
    bt.add_argument("--us10y-csv", default=None)
    bt.add_argument("--symbol", default="GC=F")
    bt.add_argument("--threshold-atr", type=float, default=1.0)
    bt.add_argument("--horizon", type=int, default=240, help="minutos")
    bt.add_argument("--walk-forward", action="store_true")
    bt.add_argument("--folds", type=int, default=4)
    bt.add_argument("--include-watch", action="store_true")
    bt.set_defaults(func=cmd_backtest)

    mt = sub.add_parser("metrics", help="precisão/recall/MFE/MAE/lead time das previsões gravadas vs. preço real")
    mt.add_argument("--db", default="gold_ai.db")
    mt.add_argument("--path-csv", required=True, help="CSV time,close com o caminho real do preço")
    mt.add_argument("--threshold", type=float, default=9.0, help="USD (ex.: 1 ATR)")
    mt.add_argument("--horizon", type=int, default=240)
    mt.set_defaults(func=cmd_metrics)

    args = p.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
