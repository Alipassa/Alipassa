"""CLI: `gold-ai demo`, `gold-ai run`, `gold-ai stats`, `gold-ai event`."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
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

    def attach(frame):
        """BANCO HISTÓRICO point-in-time (--events dados/noticias_historicas.csv): o cérebro só vê o que estava publicado em cada passo."""
        frame.symbol = (getattr(args, "market", None) or getattr(args, "market_symbol", None) or "XAUUSD").upper()
        path = getattr(args, "events", None)
        if path:
            from .history import load_history
            frame.events = load_history(path)
            frame.news_mode = getattr(args, "news_mode", None) or "full"
            print(f"banco histórico: {frame.events.stats()} · modo {frame.news_mode}")
        return frame

    if args.csv:
        return attach(HistoryFrame(xau=read_csv(args.csv), dxy=read_csv(args.dxy_csv) if args.dxy_csv else [],
                                   us10y=read_csv(args.us10y_csv) if args.us10y_csv else []))
    from .data import DataEngineConfig, HttpClient
    from .data.yahoo import YahooCollector
    y = YahooCollector(HttpClient(cache_dir=DataEngineConfig().cache_dir, ttl=900))
    start, end = getattr(args, "start", None), getattr(args, "end", None)
    if start:
        s = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
        e = datetime.fromisoformat(end).replace(tzinfo=timezone.utc) if end else datetime.now(timezone.utc)
        get = lambda sym: y.candles_between(sym, "H1", s, e)  # noqa: E731
    else:
        get = lambda sym: y.candles(sym, "H1")  # noqa: E731
    frame = HistoryFrame(xau=get(args.symbol), dxy=get("DX-Y.NYB"), us10y=get("^TNX"), vix=get("^VIX"), spx=get("^GSPC"))
    try:
        frame.fedfunds = get("ZQ=F")
    except Exception as e:  # noqa: BLE001
        print(f"(ZQ=F indisponível: {e})")
    if not getattr(args, "no_fred", False):
        try:
            from .data.fred import FredCollector
            fred = FredCollector(HttpClient(cache_dir=DataEngineConfig().cache_dir, ttl=6 * 3600))
            frame.real_yield_daily = [(datetime(d.year, d.month, d.day, tzinfo=timezone.utc), v) for d, v in fred.series("DFII10")]
            frame.breakeven_daily = [(datetime(d.year, d.month, d.day, tzinfo=timezone.utc), v) for d, v in fred.series("T10YIE")]
        except Exception as e:  # noqa: BLE001
            print(f"(FRED indisponível: {e})")
    return attach(frame)


def cmd_live_markets(args: argparse.Namespace) -> int:
    """4.0 MARKET AI ENGINE: vários mercados → cérebro único → Asset Selector → melhor oportunidade → risco/exposição → execução → monitor."""
    from .data import DataEngineConfig
    from .data.multi import MultiMarketData
    from .guard import GuardLimits, KillSwitch, TelegramCommands, TradingMode
    from .market_engine import MarketAIEngine
    from .selector import PortfolioLimits
    from .telegram import load_env_file
    from . import __version__

    env = load_env_file()
    limits, plim = GuardLimits.from_env(env), PortfolioLimits.from_env(env)
    mode = TradingMode(args.mode.upper().replace("-", "_"))
    if mode == TradingMode.LIVE and not args.authorize:
        print("modo LIVE exige --authorize explícito; rebaixando para SEMI_LIVE")
        mode = TradingMode.SEMI_LIVE
    symbols = tuple(s.strip().upper() for s in args.markets.split(",") if s.strip())
    ks = KillSwitch.from_env(env, file_path=args.kill_switch_file)
    dcfg = DataEngineConfig(xau_symbol=args.symbol, calendar_path=args.calendar, enable_cot=not args.no_cot, enable_fred=not args.no_fred, enable_news=not args.no_news)
    mt5_client, executors = None, {}
    if args.source == "mt5":
        from .data.mt5 import MT5Client, MT5Config
        from .execution import ExecutionEngine

        mcfg = MT5Config.from_env(env)
        if args.mt5_path:
            mcfg.path = args.mt5_path
        mt5_client = MT5Client(mcfg)
        try:
            mt5_client.connect()
        except Exception as e:  # noqa: BLE001
            print(f"MT5 indisponível: {e}")
            if mode != TradingMode.PAPER:
                return 1
            print("modo PAPER: continuando com dados web (Yahoo) — o MT5 só é obrigatório para executar ordens")
            mt5_client = None
        if mt5_client is not None and mode != TradingMode.PAPER:
            from .markets import get_market
            symbol_map = MultiMarketData.symbol_map_from_env(env)
            for sym in symbols:
                c = MT5Client(MT5Config(path=mcfg.path, symbol=symbol_map.get(sym, get_market(sym).mt5), login=mcfg.login, password=mcfg.password, server=mcfg.server))
                c.mt5, c.connected = mt5_client.mt5, True
                executors[sym] = ExecutionEngine(c, max_slippage=limits.max_slippage)
    elif mode != TradingMode.PAPER:
        print("execução real exige --source mt5; rebaixando para PAPER")
        mode = TradingMode.PAPER
    data = MultiMarketData(symbols, dcfg, mt5_client=mt5_client, mt5_symbol_map=MultiMarketData.symbol_map_from_env(env))
    sender = TelegramSender(dry_run=not args.send)
    commands = TelegramCommands(sender.token, sender.chat_id) if (args.send and not sender.dry_run) else None
    mem = PredictionMemory(args.db)
    engine = MarketAIEngine(mem, limits, symbols, mode, args.equity, plim, executors, sender, ks, commands, args.horizon, print, args.authorize)
    print(f"MARKET AI ENGINE {__version__} · modo {mode.value} · mercados {', '.join(symbols)} · {engine.perf.render()}")
    print(f"portfólio: risco total {plim.max_total_open_risk_pct}% · correlacionado {plim.max_correlated_risk_pct}% · posições {plim.max_positions} · por ativo {plim.max_asset_exposure}")
    try:
        while True:
            snaps = data.collect()
            print(data.coverage())
            pc = engine.run_cycle(snaps)
            print(pc.render())
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        print(engine.status_text())
        mem.close()
        if mt5_client is not None:
            mt5_client.close()
    return 0


def cmd_markets(args: argparse.Namespace) -> int:
    """Ranking de oportunidades AGORA (sem operar) + histórico por mercado no SQLite."""
    from .data import DataEngineConfig
    from .data.multi import MultiMarketData
    from .guard import GuardLimits, KillSwitch, TradingMode
    from .market_engine import MarketAIEngine
    from .selector import PortfolioLimits
    from .telegram import load_env_file

    symbols = tuple(s.strip().upper() for s in args.markets.split(",") if s.strip())
    data = MultiMarketData(symbols, DataEngineConfig(enable_cot=not args.no_cot, enable_fred=not args.no_fred, enable_news=not args.no_news))
    mem = PredictionMemory(args.db)
    engine = MarketAIEngine(mem, GuardLimits.from_env(load_env_file()), symbols, TradingMode.PAPER, 10000.0, PortfolioLimits(),
                            kill_switch=KillSwitch(enabled_env=False), log=print)   # kill switch: só ranqueia, nunca entra
    snaps = data.collect()
    print(data.coverage())
    pc = engine.run_cycle(snaps)
    print(pc.render())
    print(engine.status_text())
    mem.close()
    return 0


def _frames_for_markets(args: argparse.Namespace, markets: str) -> dict:
    from .markets import get_market

    frames = {}
    for sym in (s.strip().upper() for s in markets.split(",") if s.strip()):
        ns = argparse.Namespace(**vars(args))
        ns.symbol = get_market(sym).yahoo
        ns.market_symbol = sym
        csv_dir = getattr(args, "csv_dir", None)
        ns.csv = os.path.join(csv_dir, f"{sym}_h1.csv") if csv_dir else None
        ns.dxy_csv = os.path.join(csv_dir, "DXY_h1.csv") if csv_dir and os.path.exists(os.path.join(csv_dir, "DXY_h1.csv")) else None
        ns.us10y_csv = os.path.join(csv_dir, "US10Y_h1.csv") if csv_dir and os.path.exists(os.path.join(csv_dir, "US10Y_h1.csv")) else None
        frames[sym] = _load_frame(ns)
        print(f"{sym}: {len(frames[sym].xau)} candles H1 carregados")
    return frames


def cmd_estimate(args: argparse.Namespace) -> int:
    """ESTIMATIVA DE LUCRO: histórico <start>→<end> (Yahoo ou CSV) → walk-forward OOS → capital, retorno, drawdown, bootstrap."""
    from datetime import datetime, timezone
    from .estimate import estimate_profit
    from .telegram import load_env_file

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc) if args.end else datetime.now(timezone.utc)
    risk = args.risk if args.risk is not None else float(load_env_file().get("RISK_PER_TRADE", 0.5))
    frames = _frames_for_markets(args, args.markets)
    frames = {k: v for k, v in frames.items() if len(v.xau) > 260}
    if not frames:
        print("sem histórico suficiente (mínimo ~260 candles H1 por mercado). Verifique a rede/Yahoo ou use --csv-dir.")
        return 1
    from .markets import get_market
    factory = lambda sym: _apply_experiment(EngineConfig(factor_signs=dict(get_market(sym).factor_signs), symbol=sym), args)  # noqa: E731
    rep = estimate_profit(frames, start, end, args.equity, risk, n_folds=args.folds, step=args.step, horizon_min=args.horizon, strategy=args.strategy,
                          cfg_factory=factory)
    print(rep.render())
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(rep.render())
        print(f"\nrelatório salvo em {args.out}")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    """BANCO HISTÓRICO DE EVENTOS/NOTÍCIAS (point-in-time): template · fetch-te · fetch-alfred · fetch-gdelt · rules · learn · stats."""
    from datetime import date
    from .history import EventHistory, HistoricalEvent, apply_rule_effects, learn_effects, load_history, merge, render_effect_table, save_history
    from .telegram import load_env_file

    env = load_env_file()
    path = args.file
    exists = os.path.exists(path)
    hist = load_history(path) if exists else EventHistory()
    start = date.fromisoformat(args.start) if args.start else date(2026, 1, 1)
    end = date.fromisoformat(args.end) if args.end else datetime.now(timezone.utc).date()
    if args.action == "template":
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        ex = HistoricalEvent(datetime(2026, 1, 14, 13, 30, tzinfo=timezone.utc), datetime(2026, 1, 14, 13, 30, tzinfo=timezone.utc), "EXEMPLO_CPI_202601",
                             "CPI MoM (exemplo — apague)", "US", "USD", "MUITO ALTO", 0.2, 0.3, 0.4, None, None, "MACRO", "cpi", "manual", "", "")
        n = save_history(merge(hist, EventHistory([ex])) if not exists else hist, path)
        print(f"template salvo em {path} ({n} linhas). Colunas: {', '.join(HistoricalEvent.columns())}")
        print("Preencha com calendário (Investing/TE export) ou use fetch-te / fetch-alfred / fetch-gdelt. Datas em UTC (ISO 8601).")
        return 0
    if args.action == "stats":
        print(hist.stats() if exists else f"{path} não existe — use `history template` ou um fetch-*")
        if exists:
            from .history import EFFECT_MARKETS
            for m in EFFECT_MARKETS:
                vals = [e.effect(m) for e in hist.events if e.effect(m) is not None]
                print(f"  {m}: {len(vals)} eventos com efeito · favoráveis(↑) {sum(1 for v in vals if v > 0)} · contrários(↓) {sum(1 for v in vals if v < 0)}")
        return 0
    if args.action in ("fetch-te", "fetch-alfred", "fetch-gdelt"):
        from .data import DataEngineConfig, HttpClient
        http = HttpClient(cache_dir=DataEngineConfig().cache_dir, ttl=24 * 3600)
        if args.action == "fetch-te":
            from .data.history_sources import TradingEconomicsImporter
            key = args.key or env.get("TE_API_KEY") or os.environ.get("TE_API_KEY")
            if not key:
                print("Trading Economics exige chave: --key ou TE_API_KEY no .env (https://tradingeconomics.com/api)")
                return 1
            new = TradingEconomicsImporter(http, key).fetch(start, end, args.country)
        elif args.action == "fetch-alfred":
            from .data.history_sources import ALFREDImporter
            key = args.key or env.get("FRED_API_KEY") or os.environ.get("FRED_API_KEY")
            if not key:
                print("ALFRED/FRED exige chave gratuita: --key ou FRED_API_KEY no .env (https://fred.stlouisfed.org/docs/api/api_key.html)")
                return 1
            new = ALFREDImporter(http, key).fetch(start, end)
        else:
            from .data.history_sources import GDELTImporter
            topics = [t.strip() for t in args.topics.split(",")] if args.topics else None
            new = GDELTImporter(http).fetch(start, end, topics, chunk_days=args.chunk_days, max_records=args.max_records)
        print(f"{args.action}: {len(new)} registros obtidos ({new.stats()})")
        hist = merge(hist, new)
        apply_rule_effects(hist)
        n = save_history(hist, path)
        print(f"salvo: {path} · {n} linhas · {hist.stats()}")
        return 0
    if not exists:
        print(f"{path} não existe — use `history template` ou um fetch-*")
        return 1
    if args.action == "rules":
        n = apply_rule_effects(hist)
        save_history(hist, path)
        print(f"efeitos por regra macro aplicados a {n} eventos (efeitos empíricos preservados) · salvo em {path}")
        return 0
    if args.action == "learn":
        # efeitos empíricos: o histórico de preço (Yahoo/CSV) decide a direção média 60 min após cada evento
        frames = _frames_for_markets(args, args.markets)
        prices = {sym: [(c.time, c.close) for c in fr.xau] for sym, fr in frames.items()}
        res = learn_effects(hist, prices, horizon_min=args.horizon, min_n=args.min_n)
        print(render_effect_table(res["table"]))
        print(f"\n{res['applied']} eventos passaram a usar efeito empírico (os demais mantêm a regra macro)")
        save_history(hist, path)
        print(f"salvo em {path}")
        return 0
    print(f"ação desconhecida: {args.action}")
    return 1


def cmd_compare_news(args: argparse.Namespace) -> int:
    """TESTE A/B: Preço somente × Preço + Macro (A) × Preço + Macro + News (B) — mesma janela, mesmo piso, walk-forward OOS."""
    from .ablation import compare_information
    from .history import load_history
    from .markets import get_market
    from .telegram import load_env_file

    if not os.path.exists(args.events):
        print(f"banco histórico não encontrado: {args.events} — crie com `history template` / `history fetch-*`")
        return 1
    hist = load_history(args.events)
    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc) if args.end else datetime.now(timezone.utc)
    risk = args.risk if args.risk is not None else float(load_env_file().get("RISK_PER_TRADE", 0.5))
    args.events_path, args.events = args.events, None    # os frames são carregados SEM banco; a ablação liga/desliga por modo
    frames = {k: v for k, v in _frames_for_markets(args, args.markets).items() if len(v.xau) > 260}
    if not frames:
        print("sem histórico suficiente (mínimo ~260 candles H1 por mercado)")
        return 1
    factory = lambda sym: _apply_experiment(EngineConfig(factor_signs=dict(get_market(sym).factor_signs), symbol=sym), args)  # noqa: E731
    modes = tuple(m.strip() for m in args.modes.split(",")) if args.modes else ("none", "macro", "full")
    rep = compare_information(frames, hist, start, end, args.equity, risk, n_folds=args.folds, step=args.step, horizon_min=args.horizon,
                              strategy=args.strategy, cfg_factory=factory, modes=modes, log=(print if args.verbose else None))
    print(rep.render())
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(rep.render())
        print(f"\nrelatório salvo em {args.out}")
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    """SWEEP DE PISO: testa vários |score| mínimos de vantagem no walk-forward, escolhendo o piso NO TREINO de cada fold."""
    from .evaluation import Backtester
    from .sweep import DEFAULT_FLOORS, threshold_sweep
    from .telegram import load_env_file

    cfg = _cfg_for(args)
    frame = _load_frame(args)
    floors = tuple(float(x) for x in args.floors.split(",")) if args.floors else DEFAULT_FLOORS
    risk = args.risk if args.risk is not None else float(load_env_file().get("RISK_PER_TRADE", 0.5))
    bt = Backtester(frame, cfg, step=args.step, horizon_min=args.horizon)
    rep = threshold_sweep(bt, floors, n_folds=args.folds, strategy=args.strategy, equity=args.equity, risk_pct=risk,
                          log=(print if args.verbose else None))
    print(rep.render())
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(rep.render())
    return 0


def cmd_edge(args: argparse.Namespace) -> int:
    """LIVE EDGE: tabela por mercado a partir do que o sistema viveu (fora da amostra por construção) + evolução diária."""
    from .edge_report import edge_trend, live_edge_report

    mem = PredictionMemory(args.db)
    symbols = tuple(s.strip().upper() for s in args.markets.split(",") if s.strip())
    rep = live_edge_report(mem, symbols, equity=mem.last_equity(), min_trades=args.min_trades)
    print(rep.render())
    if args.save:
        mem.save_edge_report(rep)
        print("relatório salvo")
    hist = mem.edge_history()
    if hist:
        print("\nEvolução (expectancy ajustada por relatório diário):")
        for s in symbols:
            print("  " + edge_trend(hist, s))
    mem.close()
    return 0


def cmd_live(args: argparse.Namespace) -> int:
    """3.0 LIVE EXECUTION ENGINE: dados reais → predição → decisão → plano → risco → lote → MT5 → confirmação → monitor → resultado → capital."""
    if args.markets:
        return cmd_live_markets(args)
    from .data import DataEngine, DataEngineConfig
    from .guard import GuardLimits, KillSwitch, TelegramCommands, TradingMode
    from .live_engine import LiveExecutionEngine
    from .telegram import load_env_file
    from .validation import IsotonicCalibrator

    env = load_env_file()
    limits = GuardLimits.from_env(env)
    mode = TradingMode(args.mode.upper().replace("-", "_"))
    if mode == TradingMode.LIVE and not args.authorize:
        print("modo LIVE exige --authorize explícito; rebaixando para SEMI_LIVE")
        mode = TradingMode.SEMI_LIVE
    ks = KillSwitch.from_env(env, file_path=args.kill_switch_file)

    dcfg = DataEngineConfig(xau_symbol=args.symbol, calendar_path=args.calendar, enable_cot=not args.no_cot,
                            enable_fred=not args.no_fred, enable_news=not args.no_news)
    data = DataEngine(dcfg)
    source = data
    executor = None
    if args.source == "mt5":
        from .data.mt5 import MT5Config, MT5Source
        from .execution import ExecutionEngine

        mcfg = MT5Config.from_env(env)
        if args.mt5_path:
            mcfg.path = args.mt5_path
        source = MT5Source(mcfg, data_engine=data)
        if mode != TradingMode.PAPER:
            source.client.connect()
            executor = ExecutionEngine(source.client, max_slippage=limits.max_slippage)
    elif mode != TradingMode.PAPER:
        print("execução real exige --source mt5; rebaixando para PAPER")
        mode = TradingMode.PAPER

    calibrator = None
    if args.calibrator and os.path.exists(args.calibrator):
        with open(args.calibrator, encoding="utf-8") as f:
            calibrator = IsotonicCalibrator.from_dict(json.load(f))
        print(f"calibrador carregado: {args.calibrator}")
    sender = TelegramSender(dry_run=not args.send)
    commands = TelegramCommands(sender.token, sender.chat_id) if (args.send and not sender.dry_run) else None
    mem = PredictionMemory(args.db)
    live = LiveExecutionEngine(mem, limits, mode, args.equity, executor, sender, ks, commands, args.horizon,
                               GoldAIEngine(EngineConfig(), calibrator=calibrator), authorized=args.authorize)
    from . import __version__
    print(f"GOLD AI ENGINE {__version__} · modo {mode.value} · {live.perf.render()}")
    print(f"limites: risco/trade {limits.risk_per_trade_pct}% · perda diária {limits.max_daily_loss_pct}% · drawdown {limits.max_drawdown_pct}% · "
          f"posições {limits.max_positions} · lote máx {limits.max_lot} · spread máx {limits.max_spread} · kill switch: {ks.new_entries_allowed()[1]}")
    if live.managed:
        print(f"{len(live.managed)} operação(ões) aberta(s) retomada(s) pelo GOLD TRADE MONITOR")
    try:
        while True:
            snap = source.collect() if source is data else source.snapshot()
            print(data.coverage())
            if source is not data:
                print(f"MT5: {source.status.get('mt5', 'n/d')}")
            res = live.run_cycle(snap, new_event_key=snap.news[0].headline if snap.news else None)
            for n in res.notes:
                print(n)
            if res.assessment is not None:
                from .report import render_dashboard
                print(render_dashboard(res.assessment, live.engine.expected_lead_min))
                if args.verbose:
                    print(render_report(res.assessment))
            print(f">>> {res.decision}")
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        print(live.status_text())
        mem.close()
        if source is not data:
            source.client.close()
    return 0


def _apply_experiment(cfg: EngineConfig, args: argparse.Namespace) -> EngineConfig:
    """Limiares como parâmetros de EXPERIMENTO (para testar fora da amostra, nunca para 'fazer entrar')."""
    changed = []
    if getattr(args, "edge_score", None) is not None:
        cfg.min_edge_score = args.edge_score; changed.append(f"vantagem |score| ≥ {args.edge_score:g}")
    if getattr(args, "signal_score", None) is not None:
        cfg.buy, cfg.sell = args.signal_score, -args.signal_score; changed.append(f"sinal |score| ≥ {args.signal_score:g}")
    if getattr(args, "min_confirmations", None) is not None:
        cfg.min_confirmations = args.min_confirmations; changed.append(f"confirmações ≥ {args.min_confirmations}")
    if getattr(args, "edge_confidence", None) is not None:
        cfg.min_edge_confidence = args.edge_confidence; changed.append(f"confiança ≥ {args.edge_confidence:g}")
    if changed:
        print("experimento: " + " · ".join(changed) + "  (compare a expectancy OOS com o padrão antes de adotar)")
    return cfg


def _cfg_for(args: argparse.Namespace) -> EngineConfig:
    """EngineConfig com os sinais de fator do mercado (--market); sem --market usa o cérebro do ouro."""
    from .markets import get_market

    market = getattr(args, "market", None)
    if market:
        spec = get_market(market)
        if getattr(args, "symbol", None) in (None, "GC=F") and not getattr(args, "csv", None):
            args.symbol = spec.yahoo
        print(f"cérebro: {spec.symbol} (sinais por fator do mercado) · candles {args.symbol}")
        return _apply_experiment(EngineConfig(factor_signs=dict(spec.factor_signs), symbol=spec.symbol), args)
    print("cérebro: XAUUSD (padrão) — use --market EURUSD|US500|USDJPY|WTI para aplicar os sinais do mercado")
    return _apply_experiment(EngineConfig(), args)


def cmd_backtest(args: argparse.Namespace) -> int:
    from .evaluation import Backtester, walk_forward

    cfg = _cfg_for(args)
    frame = _load_frame(args)
    bt = Backtester(frame, cfg, threshold_atr=args.threshold_atr, horizon_min=args.horizon, include_watch=args.include_watch)
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


def cmd_status(args: argparse.Namespace) -> int:
    mem = PredictionMemory(args.db)
    print(mem.performance_summary())
    curve = mem.equity_curve()
    if curve:
        print("  últimos pontos: " + " → ".join(f"{v:,.0f}" for _, v in curve[-8:]))
    open_ = mem.managed_trades()
    print(f"operações abertas sob monitor: {len(open_)}")
    for tr in open_:
        print(f"  #{tr.trade_id:05d} {tr.thesis.direction.value} entrada {tr.plan.entry:.2f} stop {tr.price_at_r(tr.stop_r):.2f} restante {tr.remaining:.0%}")
    print(mem.r_stats().render())
    print(mem.exit_learning())
    mem.close()
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    """2.1 VALIDATION ENGINE: backtest + walk-forward rolante + calibração + score por fator + auditoria.
    Com --markets: validação multi-mercado (4.0) — qual mercado tem melhor expectativa fora da amostra, ajustada à amostra."""
    from .evaluation import validate

    if args.markets:
        from .evaluation import render_market_validation, validate_markets
        from .markets import get_market

        frames = {}
        for sym in (s.strip().upper() for s in args.markets.split(",") if s.strip()):
            ns = argparse.Namespace(**vars(args))
            ns.symbol = get_market(sym).yahoo
            ns.csv = os.path.join(args.csv_dir, f"{sym}_h1.csv") if args.csv_dir else None
            ns.dxy_csv = os.path.join(args.csv_dir, "DXY_h1.csv") if args.csv_dir and os.path.exists(os.path.join(args.csv_dir, "DXY_h1.csv")) else None
            ns.us10y_csv = os.path.join(args.csv_dir, "US10Y_h1.csv") if args.csv_dir and os.path.exists(os.path.join(args.csv_dir, "US10Y_h1.csv")) else None
            frames[sym] = _load_frame(ns)
        rows = validate_markets(frames, n_folds=args.folds, step=args.step, horizon_min=args.horizon)
        print(render_market_validation(rows))
        if args.verbose_markets:
            for m in rows:
                print(f"\n{'=' * 30} {m.symbol} {'=' * 30}\n" + m.report.render())
        return 0

    cfg = _cfg_for(args)
    frame = _load_frame(args)
    rep = validate(frame, cfg, n_folds=args.folds, step=args.step, threshold_atr=args.threshold_atr,
                   horizon_min=args.horizon, mode=args.mode)
    print(rep.render())
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(rep.render())
        print(f"\nrelatório salvo em {args.out}")
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    """2.2 TRADE SIMULATOR sobre histórico: 1R/2R/3R/4R antes do stop, estratégias de saída, expectancy em R."""
    from .evaluation import Backtester, walk_forward

    cfg = _cfg_for(args)
    frame = _load_frame(args)
    bt = Backtester(frame, cfg, step=args.step, threshold_atr=args.threshold_atr, horizon_min=args.horizon)
    if args.walk_forward:
        wf = walk_forward(bt, n_folds=args.folds)
        print(wf.render())
        rs = wf.oos_trades
    else:
        r = bt.run()
        print(r.render())
        rs = r.trades
    if rs and rs.n:
        print(f"\nRESPOSTA: com {rs.n} operações, 3R é atingido antes do stop em {rs.reach_3r_before_stop:.0%} dos casos; "
              f"melhor estratégia de saída: {rs.best} (E={max(s.expectancy_r for s in rs.strategies):+.2f}R).")
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    """Ajusta o calibrador isotônico com as previsões resolvidas no SQLite e salva em JSON (usado por `live --calibrator`)."""
    mem = PredictionMemory(args.db)
    rep = mem.calibration()
    print(rep.render())
    if rep.n < args.min_n:
        print(f"\nsó {rep.n} previsões resolvidas (mínimo {args.min_n}) — calibrador NÃO salvo")
        mem.close()
        return 1
    cal = mem.fit_calibrator()
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(cal.to_dict(), f)
    print(f"\ncalibrador salvo em {args.out}: " + ", ".join(f"{x:.2f}→{y:.2f}" for x, y in zip(cal.xs, cal.ys)))
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
    lt = mem.lead_time_stats()
    print(f"\n⏱️ LEAD TIME médio dos acertos: {lt['media']:.0f} min (n={lt['n']})" if lt["media"] else "\n⏱️ LEAD TIME: sem acertos resolvidos ainda")
    for k, v in lt["por_tipo"].items():
        print(f"  {k}: {v:.0f} min")
    print()
    print(mem.calibration().render())
    print()
    print(mem.scoreboard().render())
    print()
    rs = mem.r_stats()
    print(rs.render())
    print()
    print(mem.exit_learning())
    print()
    from datetime import datetime, timezone
    mem.resolve_hypotheticals(datetime.now(timezone.utc))
    print(mem.opportunity_report().render())
    print()
    print(mem.funnel().render("FUNIL DE ENTRADA (vivido)"))
    for m in mem.per_market_summary():
        print()
        print(mem.funnel(m["symbol"]).render(f"FUNIL — {m['symbol']}"))
    if rs.n:
        best = next((s for s in rs.strategies if s.name == rs.best), None)
        print(f"\nExpectancy em R ({rs.best}): {best.expectancy_r:+.2f}R por operação · "
              f"1R {rs.reach['1R']:.0%} · 2R {rs.reach['2R']:.0%} · 3R {rs.reach['3R']:.0%} · 3R antes do stop {rs.reach_3r_before_stop:.0%}")
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

    lv = sub.add_parser("live", help="3.0 LIVE EXECUTION ENGINE — dados reais, decisão, execução no MT5 e gestão da posição")
    lv.add_argument("--symbol", default="GC=F", help="GC=F (futuro) ou XAUUSD=X (spot) para o Data Engine web")
    lv.add_argument("--calendar", default=None, help="JSON de eventos econômicos")
    lv.add_argument("--interval", type=int, default=300)
    lv.add_argument("--db", default="gold_ai.db")
    lv.add_argument("--send", action="store_true", help="envia ao Telegram e habilita comandos /STOP /PAUSE /RESUME /STATUS /CLOSE")
    lv.add_argument("--once", action="store_true")
    lv.add_argument("--no-cot", action="store_true")
    lv.add_argument("--no-fred", action="store_true")
    lv.add_argument("--no-news", action="store_true")
    lv.add_argument("--source", choices=["web", "mt5"], default="web", help="mt5 = preço/candles e execução no MetaTrader 5")
    lv.add_argument("--mt5-path", default=None, help="caminho do terminal64.exe (ou MT5_PATH no .env)")
    lv.add_argument("--mode", choices=["paper", "authorize", "semi-live", "live"], default="paper",
                    help="🟢 paper (padrão) · 🟡 authorize · 🟠 semi-live · 🔴 live (exige --authorize)")
    lv.add_argument("--authorize", action="store_true", help="autoriza a próxima entrada (AUTHORIZE) / habilita LIVE")
    lv.add_argument("--equity", type=float, default=10000.0, help="capital inicial (PAPER); em LIVE vem do broker")
    lv.add_argument("--horizon", type=int, default=240, help="minutos para resolver cada previsão/operação")
    lv.add_argument("--calibrator", default="calibrator.json", help="JSON gerado por `calibrate` (ignorado se não existir)")
    lv.add_argument("--kill-switch-file", default="STOP_TRADING", help="se o arquivo existir, nenhuma entrada nova")
    lv.add_argument("-v", "--verbose", action="store_true")
    lv.add_argument("--markets", default=None, help="4.0: lista de mercados, ex.: EURUSD,US500,XAUUSD,USDJPY,WTI (Asset Selector escolhe a melhor)")
    lv.set_defaults(func=cmd_live)

    es = sub.add_parser("estimate", help="estimativa de lucro num período histórico (walk-forward OOS, custo, bootstrap)")
    es.add_argument("--start", default="2026-01-01", help="data inicial (YYYY-MM-DD)")
    es.add_argument("--end", default=None, help="data final (padrão: agora)")
    es.add_argument("--markets", default="XAUUSD", help="ex.: EURUSD,US500,XAUUSD,USDJPY,WTI")
    es.add_argument("--equity", type=float, default=10000.0)
    es.add_argument("--risk", type=float, default=None, help="%% por operação (padrão: RISK_PER_TRADE do .env ou 0.5)")
    es.add_argument("--strategy", default="adaptive", help="adaptive | 3R | 2R+trailing | trailing | 1R | 2R | 4R")
    es.add_argument("--folds", type=int, default=4)
    es.add_argument("--step", type=int, default=1)
    es.add_argument("--horizon", type=int, default=240)
    es.add_argument("--edge-score", type=float, default=None, help="experimento: |score| mínimo de vantagem (padrão 25)")
    es.add_argument("--signal-score", type=float, default=None, help="experimento: |score| mínimo de sinal BUY/SELL (padrão 50)")
    es.add_argument("--min-confirmations", type=int, default=None, help="experimento: famílias independentes (padrão 3)")
    es.add_argument("--edge-confidence", type=float, default=None, help="experimento: confiança mínima de vantagem (padrão 50)")
    es.add_argument("--no-fred", action="store_true", help="não buscar DFII10/T10YIE no FRED para o histórico")
    es.add_argument("--csv-dir", default=None, help="alternativa ao Yahoo: pasta com <SYMBOL>_h1.csv (+ DXY_h1.csv, US10Y_h1.csv)")
    es.add_argument("--out", default=None)
    es.add_argument("--events", default=None, help="banco histórico point-in-time de eventos/notícias (dados/noticias_historicas.csv)")
    es.add_argument("--news-mode", choices=["none", "macro", "full"], default="full", help="o que do banco o cérebro vê: none | macro (A) | full (B)")
    es.set_defaults(func=cmd_estimate)

    sw = sub.add_parser("sweep", help="sweep de piso de vantagem no walk-forward (piso escolhido no treino de cada fold) + sensibilidade OOS")
    sw.add_argument("--csv", default=None)
    sw.add_argument("--dxy-csv", default=None)
    sw.add_argument("--us10y-csv", default=None)
    sw.add_argument("--symbol", default="GC=F")
    sw.add_argument("--market", default=None, help="EURUSD, US500, XAUUSD, USDJPY, WTI")
    sw.add_argument("--start", default=None)
    sw.add_argument("--end", default=None)
    sw.add_argument("--floors", default=None, help="ex.: 10,12,15,17,20,22,25,30,35,40 (padrão)")
    sw.add_argument("--strategy", default="adaptive")
    sw.add_argument("--folds", type=int, default=4)
    sw.add_argument("--step", type=int, default=1)
    sw.add_argument("--horizon", type=int, default=240)
    sw.add_argument("--equity", type=float, default=10000.0)
    sw.add_argument("--risk", type=float, default=None)
    sw.add_argument("--no-fred", action="store_true")
    sw.add_argument("--out", default=None)
    sw.add_argument("-v", "--verbose", action="store_true")
    sw.add_argument("--events", default=None, help="banco histórico point-in-time de eventos/notícias (dados/noticias_historicas.csv)")
    sw.add_argument("--news-mode", choices=["none", "macro", "full"], default="full", help="o que do banco o cérebro vê: none | macro (A) | full (B)")
    sw.set_defaults(func=cmd_sweep)

    hi = sub.add_parser("history", help="BANCO HISTÓRICO de eventos/notícias point-in-time: template | fetch-te | fetch-alfred | fetch-gdelt | rules | learn | stats")
    hi.add_argument("action", choices=["template", "fetch-te", "fetch-alfred", "fetch-gdelt", "rules", "learn", "stats"])
    hi.add_argument("--file", default=os.path.join("dados", "noticias_historicas.csv"))
    hi.add_argument("--start", default="2026-01-01")
    hi.add_argument("--end", default=None)
    hi.add_argument("--key", default=None, help="chave da API (ou TE_API_KEY / FRED_API_KEY no .env)")
    hi.add_argument("--country", default="united states")
    hi.add_argument("--topics", default=None, help="GDELT: geopolitica,petroleo,china,fed,risco (padrão: todos)")
    hi.add_argument("--chunk-days", type=int, default=7)
    hi.add_argument("--max-records", type=int, default=100, help="GDELT: manchetes por tema por janela")
    hi.add_argument("--markets", default="XAUUSD,US500,EURUSD,USDJPY,WTI", help="learn: mercados cujo preço define o efeito empírico")
    hi.add_argument("--horizon", type=int, default=60, help="learn: minutos após o evento para medir a direção")
    hi.add_argument("--min-n", type=int, default=8, help="learn: amostra mínima por tipo/sinal/mercado")
    hi.add_argument("--csv-dir", default=None)
    hi.add_argument("--no-fred", action="store_true")
    hi.set_defaults(func=cmd_history)

    cn = sub.add_parser("compare-news", help="TESTE A/B: Preço somente × Preço + Macro (A) × Preço + Macro + News (B), walk-forward OOS, mesmo piso")
    cn.add_argument("--events", default=os.path.join("dados", "noticias_historicas.csv"))
    cn.add_argument("--start", default="2026-01-01")
    cn.add_argument("--end", default=None)
    cn.add_argument("--markets", default="US500,XAUUSD", help="ex.: EURUSD,US500,XAUUSD,USDJPY,WTI")
    cn.add_argument("--modes", default=None, help="none,macro,full (padrão: os três)")
    cn.add_argument("--equity", type=float, default=10000.0)
    cn.add_argument("--risk", type=float, default=None)
    cn.add_argument("--strategy", default="adaptive")
    cn.add_argument("--folds", type=int, default=4)
    cn.add_argument("--step", type=int, default=1)
    cn.add_argument("--horizon", type=int, default=240)
    cn.add_argument("--edge-score", type=float, default=None, help="experimento: |score| mínimo de vantagem (padrão 25)")
    cn.add_argument("--signal-score", type=float, default=None)
    cn.add_argument("--min-confirmations", type=int, default=None)
    cn.add_argument("--edge-confidence", type=float, default=None)
    cn.add_argument("--no-fred", action="store_true")
    cn.add_argument("--csv-dir", default=None)
    cn.add_argument("--out", default=None)
    cn.add_argument("-v", "--verbose", action="store_true")
    cn.set_defaults(func=cmd_compare_news)

    ed = sub.add_parser("edge", help="4.0: LIVE EDGE — tabela diária por mercado a partir do que foi vivido (o teste definitivo)")
    ed.add_argument("--markets", default="EURUSD,US500,XAUUSD,USDJPY,WTI")
    ed.add_argument("--db", default="gold_ai.db")
    ed.add_argument("--min-trades", type=int, default=30)
    ed.add_argument("--save", action="store_true", help="guarda o relatório de hoje no SQLite")
    ed.set_defaults(func=cmd_edge)

    mk = sub.add_parser("markets", help="4.0: ranking de oportunidades agora (não opera) + histórico por mercado")
    mk.add_argument("--markets", default="EURUSD,US500,XAUUSD,USDJPY,WTI")
    mk.add_argument("--db", default="gold_ai.db")
    mk.add_argument("--no-cot", action="store_true")
    mk.add_argument("--no-fred", action="store_true")
    mk.add_argument("--no-news", action="store_true")
    mk.set_defaults(func=cmd_markets)

    st = sub.add_parser("status", help="capital, performance, operações abertas, aprendizado")
    st.add_argument("--db", default="gold_ai.db")
    st.set_defaults(func=cmd_status)

    bt = sub.add_parser("backtest", help="backtest / walk-forward sobre histórico H1 (CSV ou Yahoo)")
    bt.add_argument("--csv", default=None, help="CSV XAU H1: time,open,high,low,close,volume")
    bt.add_argument("--dxy-csv", default=None)
    bt.add_argument("--us10y-csv", default=None)
    bt.add_argument("--symbol", default="GC=F")
    bt.add_argument("--market", default=None, help="aplica os sinais por fator do mercado (EURUSD, US500, XAUUSD, USDJPY, WTI) e escolhe o símbolo Yahoo")
    bt.add_argument("--start", default=None, help="histórico Yahoo a partir desta data (YYYY-MM-DD)")
    bt.add_argument("--end", default=None)
    bt.add_argument("--threshold-atr", type=float, default=1.0)
    bt.add_argument("--horizon", type=int, default=240, help="minutos")
    bt.add_argument("--edge-score", type=float, default=None, help="experimento: |score| mínimo de vantagem (padrão 25)")
    bt.add_argument("--signal-score", type=float, default=None, help="experimento: |score| mínimo de sinal BUY/SELL (padrão 50)")
    bt.add_argument("--min-confirmations", type=int, default=None, help="experimento: famílias independentes (padrão 3)")
    bt.add_argument("--edge-confidence", type=float, default=None, help="experimento: confiança mínima de vantagem (padrão 50)")
    bt.add_argument("--no-fred", action="store_true", help="não buscar DFII10/T10YIE no FRED para o histórico")
    bt.add_argument("--walk-forward", action="store_true")
    bt.add_argument("--folds", type=int, default=4)
    bt.add_argument("--include-watch", action="store_true")
    bt.add_argument("--events", default=None, help="banco histórico point-in-time de eventos/notícias (dados/noticias_historicas.csv)")
    bt.add_argument("--news-mode", choices=["none", "macro", "full"], default="full", help="o que do banco o cérebro vê: none | macro (A) | full (B)")
    bt.set_defaults(func=cmd_backtest)

    mt = sub.add_parser("metrics", help="precisão/recall/MFE/MAE/lead time das previsões gravadas vs. preço real")
    mt.add_argument("--db", default="gold_ai.db")
    mt.add_argument("--path-csv", required=True, help="CSV time,close com o caminho real do preço")
    mt.add_argument("--threshold", type=float, default=9.0, help="USD (ex.: 1 ATR)")
    mt.add_argument("--horizon", type=int, default=240)
    mt.set_defaults(func=cmd_metrics)

    va = sub.add_parser("validate", help="2.1 VALIDATION ENGINE: auditoria + walk-forward + calibração + score por fator + oportunidades")
    va.add_argument("--csv", default=None, help="CSV XAU H1: time,open,high,low,close,volume")
    va.add_argument("--dxy-csv", default=None)
    va.add_argument("--us10y-csv", default=None)
    va.add_argument("--symbol", default="GC=F")
    va.add_argument("--market", default=None, help="aplica os sinais por fator do mercado (EURUSD, US500, XAUUSD, USDJPY, WTI) e escolhe o símbolo Yahoo")
    va.add_argument("--start", default=None, help="histórico Yahoo a partir desta data (YYYY-MM-DD)")
    va.add_argument("--end", default=None)
    va.add_argument("--folds", type=int, default=4)
    va.add_argument("--step", type=int, default=1)
    va.add_argument("--mode", choices=["rolling", "anchored"], default="rolling")
    va.add_argument("--threshold-atr", type=float, default=1.0)
    va.add_argument("--horizon", type=int, default=240)
    va.add_argument("--edge-score", type=float, default=None, help="experimento: |score| mínimo de vantagem (padrão 25)")
    va.add_argument("--signal-score", type=float, default=None, help="experimento: |score| mínimo de sinal BUY/SELL (padrão 50)")
    va.add_argument("--min-confirmations", type=int, default=None, help="experimento: famílias independentes (padrão 3)")
    va.add_argument("--edge-confidence", type=float, default=None, help="experimento: confiança mínima de vantagem (padrão 50)")
    va.add_argument("--no-fred", action="store_true", help="não buscar DFII10/T10YIE no FRED para o histórico")
    va.add_argument("--out", default=None, help="salva o relatório em arquivo")
    va.add_argument("--markets", default=None, help="4.0: validação multi-mercado, ex.: EURUSD,US500,XAUUSD,USDJPY,WTI")
    va.add_argument("--csv-dir", default=None, help="pasta com <SYMBOL>_h1.csv (+ DXY_h1.csv, US10Y_h1.csv opcionais)")
    va.add_argument("--verbose-markets", action="store_true", help="imprime o relatório completo de cada mercado")
    va.add_argument("--events", default=None, help="banco histórico point-in-time de eventos/notícias (dados/noticias_historicas.csv)")
    va.add_argument("--news-mode", choices=["none", "macro", "full"], default="full", help="o que do banco o cérebro vê: none | macro (A) | full (B)")
    va.set_defaults(func=cmd_validate)

    si = sub.add_parser("simulate", help="2.2 TRADE SIMULATOR: 1R/2R/3R/4R antes do stop, estratégias de saída, expectancy, oportunidades")
    si.add_argument("--csv", default=None, help="CSV XAU H1: time,open,high,low,close,volume")
    si.add_argument("--dxy-csv", default=None)
    si.add_argument("--us10y-csv", default=None)
    si.add_argument("--symbol", default="GC=F")
    si.add_argument("--market", default=None, help="aplica os sinais por fator do mercado (EURUSD, US500, XAUUSD, USDJPY, WTI) e escolhe o símbolo Yahoo")
    si.add_argument("--start", default=None, help="histórico Yahoo a partir desta data (YYYY-MM-DD)")
    si.add_argument("--end", default=None)
    si.add_argument("--step", type=int, default=1)
    si.add_argument("--folds", type=int, default=4)
    si.add_argument("--threshold-atr", type=float, default=1.0)
    si.add_argument("--horizon", type=int, default=240)
    si.add_argument("--edge-score", type=float, default=None, help="experimento: |score| mínimo de vantagem (padrão 25)")
    si.add_argument("--signal-score", type=float, default=None, help="experimento: |score| mínimo de sinal BUY/SELL (padrão 50)")
    si.add_argument("--min-confirmations", type=int, default=None, help="experimento: famílias independentes (padrão 3)")
    si.add_argument("--edge-confidence", type=float, default=None, help="experimento: confiança mínima de vantagem (padrão 50)")
    si.add_argument("--no-fred", action="store_true", help="não buscar DFII10/T10YIE no FRED para o histórico")
    si.add_argument("--walk-forward", action="store_true")
    si.add_argument("--events", default=None, help="banco histórico point-in-time de eventos/notícias (dados/noticias_historicas.csv)")
    si.add_argument("--news-mode", choices=["none", "macro", "full"], default="full", help="o que do banco o cérebro vê: none | macro (A) | full (B)")
    si.set_defaults(func=cmd_simulate)

    ca = sub.add_parser("calibrate", help="ajusta e salva o calibrador de probabilidade a partir do SQLite")
    ca.add_argument("--db", default="gold_ai.db")
    ca.add_argument("--out", default="calibrator.json")
    ca.add_argument("--min-n", type=int, default=30)
    ca.set_defaults(func=cmd_calibrate)

    args = p.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
