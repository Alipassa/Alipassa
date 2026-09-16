"""CLI: `gold-ai demo`, `gold-ai run`, `gold-ai stats`, `gold-ai event`."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from datetime import timedelta
from typing import Optional

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

    def stage(msg: str) -> None:
        print(f"[{datetime.now(timezone.utc):%H:%M:%S} UTC] {msg}", flush=True)

    stage(f"MARKET AI ENGINE {__version__} iniciando · modo {mode.value} · mercados {', '.join(symbols)}")
    if args.source == "mt5":
        from .data.mt5 import MT5Client, MT5Config
        from .execution import ExecutionEngine

        mcfg = MT5Config.from_env(env)
        if args.mt5_path:
            mcfg.path = args.mt5_path
        try:
            stage("conectando ao MetaTrader 5…")
            mt5_client = MT5Client(mcfg)
            mt5_client.connect()
            stage(f"MT5 conectado · fuso do servidor {getattr(mt5_client, 'server_offset_hours', 0):+.1f}h ({getattr(mt5_client, 'offset_note', '')})")
            if mode != TradingMode.PAPER:
                # TRAVA DE CONTA: com --demo-only (padrão) o modo real só roda em conta DEMO da corretora
                info = mt5_client.mt5.account_info()
                trade_mode = int(getattr(info, "trade_mode", -1)) if info is not None else -1     # 0 = demo · 1 = contest · 2 = real
                acct = f"conta {getattr(info, 'login', '?')} · {getattr(info, 'server', '?')} · saldo {float(getattr(info, 'balance', 0.0)):,.2f} {getattr(info, 'currency', '')}"
                kind = {0: "DEMO", 1: "CONTEST", 2: "REAL"}.get(trade_mode, "DESCONHECIDA")
                print(f"MT5: {acct} · tipo {kind}")
                if info is not None and float(getattr(info, "equity", 0.0)) > 0:
                    args.equity = float(info.equity)      # capital REAL da conta: 3% de risco sobre o saldo do broker, não sobre o --equity padrão
                if getattr(args, "demo_only", True) and trade_mode != 0:
                    print("🛑 TRAVA: modo real pedido mas a conta NÃO é demo (ou não foi possível confirmar). Use --no-demo-only apenas quando decidir operar dinheiro real.")
                    return 1
        except Exception as e:  # noqa: BLE001
            print(f"MT5 indisponível: {e}")
            if mode != TradingMode.PAPER:
                return 1
            print("modo PAPER: continuando com dados web (Yahoo) — o MT5 só é obrigatório para executar ordens")
            mt5_client = None
        if mt5_client is not None and mode != TradingMode.PAPER:
            from .markets import get_market
            symbol_map = MultiMarketData.symbol_map_from_env({**env, **os.environ})
            for sym in symbols:
                c = MT5Client(MT5Config(path=mcfg.path, symbol=symbol_map.get(sym, get_market(sym).mt5), login=mcfg.login, password=mcfg.password, server=mcfg.server))
                c.mt5, c.connected = mt5_client.mt5, True
                executors[sym] = ExecutionEngine(c)      # tolerâncias por símbolo (symbol_info), não o MAX_SLIPPAGE global do ouro
    elif mode != TradingMode.PAPER:
        print("execução real exige --source mt5; rebaixando para PAPER")
        mode = TradingMode.PAPER
    data = MultiMarketData(symbols, dcfg, mt5_client=mt5_client, mt5_symbol_map=MultiMarketData.symbol_map_from_env({**env, **os.environ}))
    sender = TelegramSender(dry_run=not args.send)
    commands = TelegramCommands(sender.token, sender.chat_id, offset_path=os.path.join("dados", "telegram_offset.txt")) if (args.send and not sender.dry_run) else None
    mem = PredictionMemory(args.db)
    from .reaction_hires import load_reaction_edge
    from .selector import AssetSelector
    edge = load_reaction_edge(args.reaction_edge) if getattr(args, "reaction_edge", None) else {}
    if edge:
        print("REACTION EDGE carregado (Asset Selector): " + ", ".join(f"{k} {v:.2f}" for k, v in edge.items()))
    from .edge_bank import EdgeBank
    bank = EdgeBank.load(args.edge_bank) if getattr(args, "edge_bank", None) else None
    if bank is not None and bank.stats:
        print(f"EDGE BANK carregado: {len(bank.stats)} contextos ({args.edge_bank}) — só contextos com n próprio ≥ 30 ajustam a prioridade")
    from .autotune import load_params
    learned = load_params(getattr(args, "params", None) or "")
    engine = MarketAIEngine(mem, limits, symbols, mode, args.equity, plim, executors, sender, ks, commands, args.horizon, print, args.authorize,
                            selector=AssetSelector(reaction_edge=edge), edge_bank=(bank if bank is not None and bank.stats else None), params=learned)
    # REACTION ENGINE live: T0 real dos líderes via M1 do Yahoo (DXY, US10Y) — cache curto, falha silenciosa
    from .data import HttpClient as _Http
    from .data.yahoo import YahooCollector as _Yahoo
    _y = _Yahoo(_Http(cache_dir=dcfg.cache_dir, ttl=60))

    def lead_history(name, t_from, t_to):
        sym = {"USD": "DX-Y.NYB", "YIELD": "^TNX"}[name]
        return [(c.time + timedelta(minutes=1), c.close) for c in _y.candles(sym, "M1") if t_from <= c.time + timedelta(minutes=1) <= t_to]
    engine.lead_history = lead_history
    print(f"MARKET AI ENGINE {__version__} · modo {mode.value} · mercados {', '.join(symbols)} · {engine.perf.render()}")
    print(f"portfólio: risco total {plim.max_total_open_risk_pct}% · correlacionado {plim.max_correlated_risk_pct}% · posições {plim.max_positions} · por ativo {plim.max_asset_exposure}")
    stage("coletando dados (Yahoo/MT5/FRED/CFTC/RSS) — o PRIMEIRO ciclo pode levar alguns minutos; depois cada ciclo leva segundos")
    cycle = 0
    try:
        while True:
            cycle += 1
            t0 = time.monotonic()
            snaps = data.collect()
            t1 = time.monotonic()
            print(data.coverage())
            pc = engine.run_cycle(snaps)
            print(pc.render())
            stage(f"ciclo {cycle} · coleta {t1 - t0:.0f}s · análise {time.monotonic() - t1:.0f}s · próximo em {args.interval}s · "
                  f"entradas são raras por desenho (o robô só entra em oportunidade estatisticamente válida)")
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
    from .reaction_hires import load_reaction_edge
    from .selector import AssetSelector
    edge = load_reaction_edge(args.reaction_edge) if args.reaction_edge else {}
    engine = MarketAIEngine(mem, GuardLimits.from_env(load_env_file()), symbols, TradingMode.PAPER, 10000.0, PortfolioLimits(),
                            kill_switch=KillSwitch(enabled_env=False), log=print, selector=AssetSelector(reaction_edge=edge))   # kill switch: só ranqueia, nunca entra
    if edge:
        print("REACTION EDGE carregado: " + ", ".join(f"{k} {v:.2f}" for k, v in edge.items()))
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


def _read_candles_csv(path: str) -> list:
    import csv as _csv
    from .models import Candle
    from .reaction_hires import parse_time
    out, bad = [], 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for r in _csv.DictReader(f):
            t = parse_time(r.get("time"))
            try:
                vals = [float(r[k]) for k in ("open", "high", "low", "close")]
            except (KeyError, ValueError, TypeError):
                t = None
            if t is None:
                bad += 1
                continue
            out.append(Candle(t, *vals, float(r.get("volume") or 0)))
    if bad:
        print(f"[aviso] {os.path.basename(path)}: {bad} linha(s) inválida(s) ignorada(s) (arquivo com trecho truncado); {len(out)} candles válidos")
    return sorted(out, key=lambda c: c.time)


def cmd_history(args: argparse.Namespace) -> int:
    """BANCO HISTÓRICO DE EVENTOS/NOTÍCIAS (point-in-time): template · fetch-te · fetch-alfred · fetch-gdelt · rules · learn · stats."""
    from datetime import date
    from .history import (EventHistory, FetchProgress, HistoricalEvent, apply_rule_effects, coverage, learn_effects, load_history, merge,
                          render_effect_table, save_history)
    from .telegram import load_env_file

    env = load_env_file()
    path = args.file
    exists = os.path.exists(path)
    hist = load_history(path) if exists else EventHistory()
    start = date.fromisoformat(args.start) if args.start else date(2026, 1, 1)
    end = date.fromisoformat(args.end) if args.end else datetime.now(timezone.utc).date()
    if args.action == "prices" and args.source == "dukascopy":
        # ticks bid/ask gratuitos (sem chave, sem MT5): por padrão só as horas ao redor dos eventos do banco
        from .data import DataEngineConfig, HttpClient
        from .data.dukascopy import DUKA_INSTRUMENTS, DukascopyImporter
        from .reaction_hires import save_candles, save_ticks
        http = HttpClient(cache_dir=DataEngineConfig().cache_dir, ttl=365 * 24 * 3600, timeout=90, retries=2)   # arquivos de hora podem ter MBs
        imp = DukascopyImporter(http, log=print)
        out_dir = args.out_dir or "dados"
        os.makedirs(out_dir, exist_ok=True)
        symbols = [x.strip().upper() for x in (args.markets + ("," + args.extra if args.extra else "")).split(",") if x.strip()]
        if args.tf.upper() == "M1":
            # candles M1 diários: completa o {sym}_m1.csv do MT5 (que só guarda as últimas ~100 000 barras) sem sobrescrever o que o broker deu
            hard_failed = False
            for sym in symbols:
                inst, sc = DUKA_INSTRUMENTS.get(sym, (sym, 1000.0))
                dest = os.path.join(out_dir, f"{sym}_m1.csv")
                existing = _read_candles_csv(dest) if os.path.exists(dest) else []
                have = {c.time for c in existing}

                def merged(new, existing=existing, have=have):
                    return sorted(existing + [c for c in new if c.time not in have], key=lambda c: c.time)
                try:
                    cs = imp.m1_range(sym, start, end, args.scale, checkpoint=lambda c, d=dest, m=merged: save_candles(m(c), d))
                except Exception as e:  # noqa: BLE001
                    print(f"{sym} ({inst}): FALHOU — {e}")
                    hard_failed = True
                    continue
                allc = merged(cs)
                n = save_candles(allc, dest)
                added = len(allc) - len(existing)
                span = f" · de {allc[0].time:%d/%m/%Y} a {allc[-1].time:%d/%m/%Y}" if allc else ""
                print(f"{sym} ({inst}, escala {args.scale or sc:g}): {len(cs)} candles M1 do Dukascopy · {added} novos · {n} no arquivo{span} → {dest}")
            if imp.failed or hard_failed:
                print(f"\n{len(imp.failed)} dia(s) falharam. Repita o mesmo comando: os já baixados estão em cache.")
                return 2
            return 0
        ev_times = []
        hard_failed = False
        if not args.full:
            if not exists:
                print(f"{path} não existe — sem eventos para delimitar as horas; use --full para baixar o período inteiro")
                return 1
            ev_times = [e.published_at for e in hist.events if e.revised is None and e.category in ("MACRO", "CENTRAL_BANK") and start <= e.published_at.date() <= end]
            print(f"{len(ev_times)} eventos macro com hora exata entre {start} e {end}")
        for sym in symbols:
            inst, sc = DUKA_INSTRUMENTS.get(sym, (sym, 1000.0))
            dest = os.path.join(out_dir, f"{sym}_ticks.csv")
            try:
                ticks = imp.range(sym, start, end, args.scale, checkpoint=lambda t, d=dest: save_ticks(t, d)) if args.full else \
                    imp.around_events(sym, ev_times, args.before, args.after, args.scale, checkpoint=lambda t, d=dest: save_ticks(t, d))
            except Exception as e:  # noqa: BLE001
                print(f"{sym} ({inst}): FALHOU — {e}")
                hard_failed = True
                continue
            n = save_ticks(ticks, dest)
            first = f" · 1º tick {ticks[0][0]:%Y-%m-%d %H:%M} bid {ticks[0][1]:g} ask {ticks[0][2]:g} (confira a escala!)" if ticks else " · nenhum tick (instrumento/escala/período?)"
            print(f"{sym} ({inst}, escala {args.scale or sc:g}): {n} ticks → {dest}{first}")
        if imp.failed or hard_failed:
            print(f"\n{len(imp.failed)} hora(s) falharam" + (" e houve símbolo com falha total" if hard_failed else "") +
                  ". Repita o mesmo comando: as horas já baixadas estão em cache e só as que faltam são pedidas.")
            return 2      # código 2 = incompleto (o .bat repete até 0)
        return 0
    if args.action == "prices":
        # exportação de M1 / ticks do MT5 para CSV (a corretora guarda M1 por anos e ticks por semanas/meses)
        from .data.mt5 import MT5Client, MT5Config, MT5Error
        from .data.multi import MultiMarketData
        from .markets import MARKETS, get_market
        from .reaction_hires import save_candles, save_ticks
        cfg = MT5Config.from_env(env)
        symbol_map = MultiMarketData.symbol_map_from_env({**env, **os.environ})
        out_dir = args.out_dir or "dados"
        os.makedirs(out_dir, exist_ok=True)
        t0 = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        t1 = datetime(end.year, end.month, end.day, 23, 59, tzinfo=timezone.utc)
        try:
            client = MT5Client(cfg)
            client.connect()
        except MT5Error as e:
            print(f"MT5 indisponível: {e}")
            return 1
        print(f"fuso do servidor da corretora: UTC{client.server_offset_hours:+.0f}h (carimbos convertidos para UTC; force com MT5_UTC_OFFSET_HOURS)")
        symbols = [x.strip().upper() for x in (args.markets + ("," + args.extra if args.extra else "")).split(",") if x.strip()]
        for sym in symbols:
            broker = symbol_map.get(sym) or (get_market(sym).mt5 if sym in MARKETS else sym)
            try:
                if args.tf.upper() == "TICK":
                    n = 0
                    rows = []
                    t = t0
                    while t < t1:                      # ticks em blocos de 1 dia (volume grande)
                        tt = min(t + timedelta(days=1), t1)
                        rows += client.ticks_range(broker, t, tt)
                        t = tt
                    n = save_ticks(rows, os.path.join(out_dir, f"{sym}_ticks.csv"))
                    print(f"{sym} ({broker}): {n} ticks → {out_dir}/{sym}_ticks.csv")
                else:
                    cs, t = [], t0
                    while t < t1:                      # candles em blocos de 14 dias: pedido único de meses de M1 excede o limite do terminal ("Invalid params")
                        tt = min(t + timedelta(days=14), t1)
                        cs += client.rates_range(broker, args.tf.upper(), t, tt)
                        t = tt
                    seen, uniq = set(), []
                    for c in cs:                       # bordas dos blocos podem repetir a barra do limite
                        if c.time not in seen:
                            seen.add(c.time)
                            uniq.append(c)
                    n = save_candles(uniq, os.path.join(out_dir, f"{sym}_{args.tf.lower()}.csv"))
                    first = min((c.time for c in uniq), default=None)
                    span = f" · de {first:%d/%m/%Y}" if first else ""
                    print(f"{sym} ({broker}): {n} candles {args.tf.upper()}{span} → {out_dir}/{sym}_{args.tf.lower()}.csv")
                    if first is not None and first > t0 + timedelta(days=7):
                        print(f"  ⚠️ histórico começa em {first:%d/%m/%Y}, não em {t0:%d/%m/%Y}: o terminal limita as barras que guarda. No MT5: "
                              "Ferramentas → Opções → Gráficos → 'Máximo de barras no gráfico' = Unlimited (ou 1 000 000), reinicie o terminal, "
                              "abra um gráfico M1 do símbolo e repita este comando.")
            except MT5Error as e:
                print(f"{sym} ({broker}): FALHOU — {e}")
        client.close()
        return 0
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
            print(coverage(hist, start, end).render())
            for m in EFFECT_MARKETS:
                vals = [e.effect(m) for e in hist.events if e.effect(m) is not None]
                print(f"  {m}: {len(vals)} eventos com efeito · favoráveis(↑) {sum(1 for v in vals if v > 0)} · contrários(↓) {sum(1 for v in vals if v < 0)}")
        return 0
    if args.action in ("fetch-te", "fetch-alfred", "fetch-gdelt"):
        from .data import DataEngineConfig, HttpClient
        from .data.http import DataError
        http = HttpClient(cache_dir=DataEngineConfig().cache_dir, ttl=24 * 3600)
        progress = FetchProgress(path)
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
                from .telegram import env_file_candidates, find_env_file
                found = find_env_file()
                print("ALFRED/FRED exige chave gratuita: --key ou FRED_API_KEY no .env (https://fred.stlouisfed.org/docs/api/api_key.html)")
                print(f"  .env lido: {found}" if found else "  nenhum .env encontrado; procurei em: " + ", ".join(env_file_candidates()))
                return 1
            imp = ALFREDImporter(http, key, log=print)
            try:
                new = imp.fetch(start, end, progress=progress, checkpoint=lambda partial: save_history(merge(hist, partial), path))
            except DataError as e:
                print(str(e))
                return 1
            if imp.failed:
                print(f"séries com falha ({len(imp.failed)}): " + "; ".join(f"{sid}: {r}" for sid, r in imp.failed))
        else:
            from .data.history_sources import GDELTImporter
            if args.skip_if_covered and exists:
                cov_now = coverage(hist, start, end)
                if cov_now.news_pct >= args.skip_if_covered:
                    print(f"GDELT: manchetes já cobrem {cov_now.news_pct:.0%} dos dias (≥ {args.skip_if_covered:.0%}) — nada a buscar; use --skip-if-covered 0 para forçar")
                    return 0
            topics = [t.strip() for t in args.topics.split(",")] if args.topics else None

            def checkpoint(partial):   # salva o parcial a cada janela: um 429 ou queda de rede não perde o que já veio
                save_history(merge(hist, partial), path)
            imp = GDELTImporter(http, min_interval=args.pace, log=print, budget_sec=(args.max_minutes * 60 if args.max_minutes else None))
            new = imp.fetch(start, end, topics, chunk_days=args.chunk_days, max_records=args.max_records, checkpoint=checkpoint, enrich=args.enrich,
                            progress=progress, mode=args.gdelt_mode)
            if imp.failed:
                print(f"janelas pendentes ({len(imp.failed)}) — rode o mesmo comando de novo para completá-las: " +
                      "; ".join(f"{t} {w}" for t, w, _ in imp.failed[:12]) + (" …" if len(imp.failed) > 12 else ""))
                gdelt_incomplete = True
        print(f"{args.action}: {len(new)} registros novos ({new.stats()})")
        if args.action in ("fetch-te", "fetch-alfred") and len(new):
            src = "tradingeconomics" if args.action == "fetch-te" else "alfred"
            old = [e for e in hist.events if e.source == src]
            if old:
                print(f"substituindo {len(old)} registros anteriores da fonte {src} (reimportação é a versão definitiva)")
            hist = EventHistory([e for e in hist.events if e.source != src])
        hist = merge(hist, new)
        apply_rule_effects(hist)
        n = save_history(hist, path)
        print(f"salvo: {path} · {n} linhas\n" + coverage(hist, start, end).render())
        return 2 if locals().get("gdelt_incomplete") else 0
    if not exists:
        print(f"{path} não existe — use `history template` ou um fetch-*")
        return 1
    if args.action == "list":
        rows = [e for e in hist.events if (not args.kind or e.kind == args.kind) and (not args.category or e.category == args.category)]
        rows = [e for e in rows if start <= e.timestamp.date() <= end]
        print(f"{'evento (UTC)':<17}{'publicado':<17}{'tipo':<14}{'evento':<34}{'anterior':>9}{'consenso':>9}{'real':>8}{'surpresa':>9}  efeito XAU/US500/USDJPY")
        for e in rows[-args.limit:]:
            fmt = lambda v: "" if v is None else f"{v:g}"  # noqa: E731
            eff = "/".join("" if e.effect(m) is None else f"{e.effect(m):+.2f}" for m in ("XAUUSD", "US500", "USDJPY"))
            print(f"{e.timestamp:%Y-%m-%d %H:%M}  {e.published_at:%Y-%m-%d %H:%M}  {e.kind:<14}{(e.headline or e.event)[:33]:<34}{fmt(e.previous):>9}{fmt(e.forecast):>9}"
                  f"{fmt(e.actual):>8}{fmt(e.surprise):>9}  {eff}")
        print(f"{len(rows)} registros" + (f" (últimos {args.limit})" if len(rows) > args.limit else ""))
        return 0
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


def _reaction_learn_hires(args: argparse.Namespace, tf: Optional[str] = None, edge_out: Optional[str] = None):
    """Alta resolução: ticks/M1 (`history prices`) × banco de eventos → segundos, dois horizontes, lead-lag, custo, prova do relógio,
    tabela CLOCK − INGÊNUA e veredito por ativo. Devolve a lista de vereditos (ou 1 em erro)."""
    from .history import EXTRA_TRANSMISSION, TYPICAL, load_history, rule_direction
    from .markets import get_market
    from .news_engine import TRANSMISSION
    from .reaction_hires import ClockTradeTest, LeadLagStats, PricePath, ReactionTradeSim, load_ticks, measure_hires

    if not os.path.exists(args.events):
        print(f"banco histórico não encontrado: {args.events}")
        return 1
    hist = load_history(args.events)
    csv_dir = args.csv_dir or "dados"
    tf = (tf or args.tf).upper()
    edge_out = args.edge_out if edge_out is None else edge_out

    def read_candles(path):
        from .models import Candle
        import csv as _csv
        out = []
        with open(path, encoding="utf-8") as f:
            for r in _csv.DictReader(f):
                t = datetime.fromisoformat(r["time"].replace("Z", "+00:00"))
                out.append(Candle(t if t.tzinfo else t.replace(tzinfo=timezone.utc), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r.get("volume") or 0)))
        return out

    def load_path(sym: str, spread: float):
        p_t = os.path.join(csv_dir, f"{sym}_ticks.csv")
        p_c = os.path.join(csv_dir, f"{sym}_{tf.lower()}.csv")
        if tf == "TICK" and os.path.exists(p_t):
            return PricePath.from_ticks(load_ticks(p_t, print)), "ticks"
        if os.path.exists(p_c):
            return PricePath.from_candles(read_candles(p_c), spread, 1 if tf == "M1" else 5), tf
        if os.path.exists(p_t):
            if tf in ("M1", "M5"):
                return PricePath.from_ticks(load_ticks(p_t, print)).resample(1 if tf == "M1" else 5), f"{tf} (reamostrado dos ticks)"
            return PricePath.from_ticks(load_ticks(p_t, print)), "ticks"
        return None, ""

    leads = {}
    # sinal do líder USD: USDX/DXY/USDJPY sobem com o dólar; EURUSD/GBPUSD/AUDUSD/XAUUSD caem (dólar é a moeda cotada) → inverte
    USD_LEADER_SIGN = {"USDX": 1.0, "DXY": 1.0, "USDJPY": 1.0, "USDCHF": 1.0, "USDCAD": 1.0, "EURUSD": -1.0, "GBPUSD": -1.0, "AUDUSD": -1.0,
                       "NZDUSD": -1.0, "XAUUSD": -1.0}
    usd_sign = 1.0
    if args.lead_usd:
        lp, kind = load_path(args.lead_usd.upper(), 0.0)
        if lp:
            leads["USD"] = lp
            usd_sign = USD_LEADER_SIGN.get(args.lead_usd.upper(), 1.0)
            print(f"líder USD: {args.lead_usd} ({kind})" + (" · sinal invertido: este par cai quando o dólar sobe" if usd_sign < 0 else ""))
        else:
            print(f"líder USD {args.lead_usd}: arquivo não encontrado em {csv_dir} (sem líder USD → lead-lag e trade sim ficam vazios)")
    if args.lead_yield:
        lp, kind = load_path(args.lead_yield.upper(), 0.0)
        if lp:
            leads["YIELD"] = lp
            print(f"líder YIELD: {args.lead_yield} ({kind})")
    all_recs, sim_items = [], []
    for sym in (x.strip().upper() for x in args.markets.split(",") if x.strip()):
        spec = get_market(sym)
        path, kind = load_path(sym, spec.typical_spread)
        if path is None:
            print(f"{sym}: sem {sym}_{tf.lower()}.csv / {sym}_ticks.csv em {csv_dir} — exporte com `history prices --tf {tf} --markets {sym}`")
            continue
        n = 0
        # UMA PUBLICAÇÃO = UM CASO: releases no mesmo instante (NFP + salário médio, CPI + núcleo) viram um único registro com rótulo
        # composto ('nfp+earnings'); se as regras discordarem da direção, o instante é ambíguo e não é medido (contado abaixo)
        per_instant: dict[datetime, list] = {}
        for e in hist.events:
            if e.revised is not None:
                continue
            sign, sigma = 0.0, None
            if e.surprise is not None:
                sign = 1.0 if e.surprise > 0 else -1.0 if e.surprise < 0 else 0.0
                typ = TYPICAL.get(e.kind, max(abs(e.forecast or e.previous or 1.0) * 0.1, 0.1))
                sigma = e.surprise / typ if typ else None
            elif e.kind in TRANSMISSION and e.actual is None:
                sign = 1.0
            if sign == 0.0:
                continue
            exp = rule_direction(e.kind, sign, sigma, sym)
            if exp == 0.0:
                continue
            chans = TRANSMISSION.get(e.kind) or EXTRA_TRANSMISSION.get(e.kind) or {}
            lead_dirs = {"USD": usd_sign * sign * chans.get("dollar", 0.0), "YIELD": sign * chans.get("yields", 0.0)}
            per_instant.setdefault(e.published_at.replace(second=0, microsecond=0), []).append((e, exp, lead_dirs, abs(sigma) if sigma is not None else 1.0))
        ambiguous, merged = 0, 0
        for t0, items in sorted(per_instant.items()):
            # a SURPRESA MAIOR decide (NFP forte + desemprego pior no mesmo segundo → vence quem surpreendeu mais, em desvios típicos);
            # ambíguo só quando as surpresas se anulam
            vote = sum(x[1] * x[3] for x in items)
            if abs(vote) < 1e-9:
                ambiguous += 1
                continue
            items.sort(key=lambda x: -x[3])
            e, _, lead_dirs, _ = items[0]
            exp = 1.0 if vote > 0 else -1.0
            if (items[0][1] > 0) != (exp > 0):                                # canal do líder segue a direção vencedora (comparar SINAIS:
                lead_dirs = {k: -v for k, v in lead_dirs.items()}             # a direção do alvo é graduada, ex.: -0.8, e o voto é ±1)
            label = "+".join(sorted({x[0].kind for x in items}))
            merged += len(items) - 1
            atr = path.atr_at(e.published_at)
            if not atr:
                continue
            rec = measure_hires(e.event_id, label, e.published_at, sym, exp, path, atr, leads, lead_dirs)
            if rec:
                all_recs.append(rec)
                sim_items.append((rec, path, atr))
                n += 1
        extra = (f" · {merged} rótulo(s) fundido(s) no mesmo instante" if merged else "") + (f" · {ambiguous} instante(s) ambíguo(s) ignorado(s)" if ambiguous else "")
        print(f"{sym}: {n} publicações medidas em {kind} (resolução {path.resolution_sec:.0f} s){extra}")
    ll = LeadLagStats(all_recs)
    sim = ReactionTradeSim(slippage_atr=args.slippage, latency_sec=args.latency)
    from .reaction_hires import asset_verdicts, render_verdicts, save_reaction_edge
    delays_default = "1,5,10,30,60" if tf == "TICK" else "60,120,300"
    delays = [int(x) for x in (args.delays or delays_default).split(",")]
    sim_rows = sim.table(sim_items, delays=delays)
    txt = ll.render() + "\n\n" + sim.render(sim_rows)
    results, tests = {}, {}
    for d in delays:
        ct = ClockTradeTest(delay_sec=d, p_min=args.p_min, slippage_atr=args.slippage, latency_sec=args.latency)
        results[d] = ct.run(sim_items)
        tests[d] = ct
        txt += "\n\n" + ct.render(results[d])
    from .reaction_hires import walk_forward_choice
    txt += "\n\n" + walk_forward_choice(tests)
    from .reaction_hires import delta_table, render_delta
    txt += "\n\n" + render_delta(delta_table(results), tf)
    verdicts = asset_verdicts(results)
    txt += "\n\n" + render_verdicts(verdicts, tf)
    if edge_out:
        save_reaction_edge(verdicts, edge_out, tf)
        txt += f"\n   veredito salvo em {edge_out} (o `live --markets` e o `markets` leem este arquivo)"
    txt += "\n\nLIMITES: manchetes GDELT (volinfo) têm published_at no fim do dia — só releases (ALFRED/TE) têm hora exata para segundos; "
    txt += "custo = ask/bid reais dos ticks (ou spread típico no M1) + slippage + latência; liquidez fora do horário e gaps não modelados."
    print(txt)
    if args.out:
        with open(args.out, "a" if tf == "M1" and args.tf.upper() == "BOTH" else "w", encoding="utf-8") as f:
            f.write(("\n\n" if tf == "M1" and args.tf.upper() == "BOTH" else "") + txt)
    return verdicts


def cmd_flow(args: argparse.Namespace) -> int:
    """FLOW ANOMALY ENGINE agora: FLOW SCORE por mercado, assinatura, origem e mapa de propagação (líder → atrasados)."""
    from .data import DataEngineConfig
    from .data.multi import MultiMarketData
    from .flow_anomaly import FlowAnomalyEngine, render_propagation
    from .reaction import ReactionClock, ReactionStats

    symbols = tuple(s.strip().upper() for s in args.markets.split(",") if s.strip())
    mem = PredictionMemory(args.db)
    if args.learn:
        # REPLAY: o M1 do passado (dados/<SYM>_m1.csv) percorrido como se fosse ao vivo → ledger com meses de anomalias medidas
        from .flow_anomaly import render_flow_stats, replay_flow_anomalies
        csv_dir = args.csv_dir or "dados"
        history = None
        if args.events and os.path.exists(args.events):
            from .history import load_history
            history = load_history(args.events)
            print(f"banco histórico: {history.stats()}")
        leaders = {}
        lead_path = os.path.join(csv_dir, f"{args.lead_usd}_m1.csv") if args.lead_usd else ""
        if lead_path and os.path.exists(lead_path):
            leaders["USD"] = _read_candles_csv(lead_path)
            print(f"líder USD: {args.lead_usd} ({len(leaders['USD'])} candles M1)")
        total_new = 0
        for sym in symbols:
            path = os.path.join(csv_dir, f"{sym}_m1.csv")
            if not os.path.exists(path):
                print(f"{sym}: {path} não existe — rode `history prices --tf M1` (mt5 e dukascopy)")
                continue
            m1 = _read_candles_csv(path)
            recs = replay_flow_anomalies(sym, m1, leaders, history, step_min=args.step, log=print)
            new = sum(1 for r in recs if mem.save_measured_flow_anomaly(r))
            total_new += new
            cont = sum(1 for r in recs if r.get("resultado") == "CONTINUOU")
            print(f"{sym}: {len(m1)} candles M1 · {len(recs)} anomalias (FLOW ≥ 70) · {new} novas no ledger · continuação {cont}/{len(recs) or 1}")
        print()
        rows = mem.flow_anomaly_rows()
        print(render_flow_stats(rows))
        hist_n = sum(1 for r in rows if str(r.get("event_id", "")).startswith("hist_"))
        print(f"ledger: {len(rows)} medidas ({hist_n} do replay histórico, {len(rows) - hist_n} vividas) · {total_new} novas nesta execução")
        mem.close()
        return 0
    if args.stats:
        from .flow_anomaly import render_flow_stats
        rows = mem.flow_anomaly_rows(measured_only=False)
        print(render_flow_stats(rows))
        pend = [r for r in rows if not r.get("resultado")]
        hist_n = sum(1 for r in rows if str(r.get("event_id", "")).startswith("hist_"))
        print(f"registradas {len(rows)} · medidas {len(rows) - len(pend)} · aguardando medição {len(pend)} · do replay histórico {hist_n}")
        mem.close()
        return 0
    data = MultiMarketData(symbols, DataEngineConfig(enable_cot=False, enable_fred=not args.no_fred, enable_news=not args.no_news))
    snaps = data.collect()
    print(data.coverage())
    fe = FlowAnomalyEngine(ledger=mem.flow_anomaly_rows())
    fas = {}
    for sym, snap in snaps.by_symbol.items():
        fas[sym] = fe.assess(sym, snap, snaps.identified, snaps.time, snaps.by_symbol)
        print(fas[sym].chain)
        ev = fas[sym].implicit_event(snaps.time)
        if ev is not None:
            snaps.identified.append(ev)
    clock = ReactionClock(ReactionStats(mem.reaction_records()))
    clocks = {sym: clock.assess(sym, snap, snaps.identified, snaps.time).chain for sym, snap in snaps.by_symbol.items()}
    print()
    print(render_propagation(fas, clocks))
    mem.close()
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """DOCTOR: tudo está funcionando? qual a eficiência? — painel por camada com ação, e leitura do que está provado."""
    from .doctor import run_doctor
    from .telegram import TelegramSender, load_env_file

    env = load_env_file()
    mt5_probe = None
    if args.mt5:
        def mt5_probe():
            try:
                from .data.mt5 import MT5Client, MT5Config
                c = MT5Client(MT5Config.from_env(env))
                c.connect()
                bid, ask = c.tick()
                off = c.server_offset_hours
                note = getattr(c, "offset_note", "")
                # TESTE DE CARIMBO: o último candle M1 (convertido para UTC) tem de ter aberto há menos de 3 min (mercado aberto)
                cs = c.candles("M1", 3)
                c.close()
                age = (datetime.now(timezone.utc) - cs[-1].time).total_seconds() / 60 if cs else None
                ok_ts = age is not None and -1 <= age <= 3
                ts_txt = f"último M1 aberto há {age:.1f} min ({'✅ carimbos alinhados' if ok_ts else '⚠️ desalinhado: mercado fechado ou fuso errado → MT5_UTC_OFFSET_HOURS'})" if age is not None else "sem candles M1"
                return True, f"conectado · {c.cfg.symbol} bid {bid:g} ask {ask:g} · fuso UTC{off:+.0f}h ({note}) · {ts_txt}"
            except Exception as e:  # noqa: BLE001
                return False, str(e)[:160]
    tg_probe = None
    if args.telegram:
        def tg_probe():
            try:
                snd = TelegramSender(env_file=".env", quiet=True)
                if snd.dry_run:
                    return False, "sem TOKEN_TELEGRAM/CHAT_ID"
                ok = snd.send("🩺 MARKET AI DOCTOR: teste de envio OK")
                return ok, "mensagem de teste enviada" if ok else "API respondeu erro"
            except Exception as e:  # noqa: BLE001
                return False, str(e)[:160]
    start = datetime.fromisoformat(args.start).date() if args.start else None
    end = datetime.fromisoformat(args.end).date() if args.end else None
    rep = run_doctor(env, args.db, args.events, tuple(s.strip().upper() for s in args.markets.split(",") if s.strip()), start, end, mt5_probe, tg_probe)
    print(rep.render())
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(rep.render())
    ok, warn, bad = rep.score
    return 1 if bad else 0


def _reaction_leadlag(args: argparse.Namespace) -> int:
    """quem se moveu primeiro? M1 do broker (ou dados/<SYM>_m1.csv) em torno de --at; --record grava o episódio como histórico do relógio."""
    from .leadlag import lead_lag, records_from_episode, render_lead_lag
    from .markets import MARKETS, get_market
    from .telegram import load_env_file

    if not args.at:
        print("informe --at 'AAAA-MM-DD HH:MM' (hora UTC; use --tz server para hora do terminal, ou --tz -3 para hora local)")
        return 1
    t_raw = datetime.fromisoformat(args.at.replace(" ", "T"))
    symbols = [x.strip().upper() for x in args.markets.split(",") if x.strip()]
    if args.leader.upper() not in symbols:
        symbols.insert(0, args.leader.upper())
    env = load_env_file()
    candles: dict = {}
    if args.source == "mt5":
        from .data.mt5 import MT5Client, MT5Config, MT5Error
        from .data.multi import MultiMarketData
        try:
            client = MT5Client(MT5Config.from_env(env))
            client.connect()
        except MT5Error as e:
            print(f"MT5 indisponível: {e} — use --source csv com dados/<SYM>_m1.csv")
            return 1
        off = float(client.server_offset_hours)
        tz = args.tz.lower()
        shift = off if tz == "server" else (0.0 if tz == "utc" else float(tz))
        t0 = (t_raw - timedelta(hours=shift)).replace(tzinfo=timezone.utc)
        symbol_map = MultiMarketData.symbol_map_from_env({**env, **os.environ})
        for sym in symbols:
            broker = symbol_map.get(sym) or (get_market(sym).mt5 if sym in MARKETS else sym)
            try:
                candles[sym] = client.rates_range(broker, "M1", t0 - timedelta(hours=25), t0 + timedelta(minutes=args.window + 5))
            except MT5Error as e:
                print(f"{sym} ({broker}): {e}")
        client.close()
        print(f"fuso do servidor UTC{off:+.0f}h · t0 = {t0:%Y-%m-%d %H:%M} UTC")
    else:
        tz = args.tz.lower()
        shift = 0.0 if tz in ("utc", "server") else float(tz)
        t0 = (t_raw - timedelta(hours=shift)).replace(tzinfo=timezone.utc)
        for sym in symbols:
            path = os.path.join(args.csv_dir or "dados", f"{sym}_m1.csv")
            if os.path.exists(path):
                candles[sym] = [c for c in _read_candles_csv(path) if t0 - timedelta(hours=25) <= c.time <= t0 + timedelta(minutes=args.window + 5)]
            else:
                print(f"{sym}: {path} não existe")
    rows = lead_lag(candles, t0, args.leader.upper(), args.threshold, args.window)
    if not rows:
        print("sem M1 suficiente em torno do horário (precisa de 24 h antes e o horizonte depois)")
        return 1
    print(render_lead_lag(rows, t0, args.leader.upper(), args.threshold, args.window))
    if args.record:
        mem = PredictionMemory(args.db)
        recs = records_from_episode(rows, t0, args.leader.upper(), args.window)
        for rec in recs:
            mem.save_reaction(rec)
        mem.close()
        print(f"{len(recs)} registro(s) gravados como {recs[0].kind if recs else '—'} (histórico do relógio; ≥ 5 casos para virar estatística)")
    return 0


def cmd_reaction(args: argparse.Namespace) -> int:
    """REACTION ENGINE: learn (banco histórico × preço → tempo de reação por evento e ativo) · stats (o que o live viveu) · clock (agora)."""
    from .reaction import ReactionStats

    if args.action == "leadlag":
        return _reaction_leadlag(args)
    if args.action == "learn" and args.tf.upper() == "BOTH":
        from .reaction_hires import render_stability
        outs = {}
        for tf in ("TICK", "M1"):
            print(f"\n{'=' * 100}\n{tf}\n{'=' * 100}")
            outs[tf] = _reaction_learn_hires(args, tf=tf, edge_out=(args.edge_out if tf == "TICK" else ""))
        if isinstance(outs["TICK"], list) and isinstance(outs["M1"], list):
            txt = render_stability(outs["TICK"], outs["M1"])
            print("\n" + txt)
            if args.out:
                with open(args.out, "a", encoding="utf-8") as f:
                    f.write("\n\n" + txt)
        return 0 if (isinstance(outs["TICK"], list) or isinstance(outs["M1"], list)) else 1
    if args.action == "learn" and args.tf.upper() in ("M1", "M5", "TICK"):
        return 0 if isinstance(_reaction_learn_hires(args), list) else 1
    if args.action == "learn":
        from .history import load_history
        if not os.path.exists(args.events):
            print(f"banco histórico não encontrado: {args.events}")
            return 1
        hist = load_history(args.events)
        frames = _frames_for_markets(args, args.markets)
        all_recs = []
        for sym, frame in frames.items():
            frame.symbol, frame.events = sym, hist
            st = frame.reaction_stats()
            all_recs += st.records
            print(f"{sym}: {len(st.records)} eventos medidos (H1)")
        st = ReactionStats(all_recs)
        txt = st.render()
        print(txt)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(txt)
        return 0
    if args.action == "stats":
        mem = PredictionMemory(args.db)
        st = ReactionStats(mem.reaction_records())
        print(st.render(title="⏱️ REACTION ENGINE — o que o live viveu (resolução por ciclo/M5)"))
        mem.close()
        return 0
    if args.action == "clock":
        from .data import DataEngineConfig
        from .data.multi import MultiMarketData
        from .reaction import ReactionClock
        symbols = tuple(s.strip().upper() for s in args.markets.split(",") if s.strip())
        mem = PredictionMemory(args.db)
        data = MultiMarketData(symbols, DataEngineConfig(enable_cot=False, enable_fred=not args.no_fred, enable_news=True))
        snaps = data.collect()
        clock = ReactionClock(ReactionStats(mem.reaction_records()))
        print(data.coverage())
        for sym, snap in snaps.by_symbol.items():
            print(clock.assess(sym, snap, snaps.identified, snaps.time).chain)
        mem.close()
        return 0
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
    from .history import coverage
    hist = load_history(args.events)
    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc) if args.end else datetime.now(timezone.utc)
    risk = args.risk if args.risk is not None else float(load_env_file().get("RISK_PER_TRADE", 0.5))
    modes = tuple(m.strip() for m in args.modes.split(",")) if args.modes else ("none", "macro", "full")
    if args.ladder:
        from .ablation import LADDER
        modes = tuple(m for m, _ in LADDER)
    cov = coverage(hist, start.date(), end.date())
    print(cov.render() + "\n")
    gaps = []
    if "macro" in modes or "full" in modes:
        if cov.macro_pct < args.min_coverage:
            gaps.append(f"MACRO cobre {cov.macro_pct:.0%} das semanas (mínimo {args.min_coverage:.0%}) — rode `history fetch-alfred` / `fetch-te`")
    if any(m.startswith("full") for m in modes) and cov.news_pct < args.min_coverage:
        gaps.append(f"NEWS cobre {cov.news_pct:.0%} dos dias (mínimo {args.min_coverage:.0%}) — rode `history fetch-gdelt` até completar")
    if gaps:
        for g in gaps:
            print("⚠️ " + g)
        if not args.allow_partial:
            print("Banco incompleto para o período: um TESTE A/B sobre fração do histórico não vale como resultado. Complete o banco ou use --allow-partial para um ensaio.")
            return 1
        print("(--allow-partial: resultado é ENSAIO, não conclusão)\n")
    args.events_path, args.events = args.events, None    # os frames são carregados SEM banco; a ablação liga/desliga por modo
    frames = {k: v for k, v in _frames_for_markets(args, args.markets).items() if len(v.xau) > 260}
    if not frames:
        print("sem histórico suficiente (mínimo ~260 candles H1 por mercado)")
        return 1
    factory = lambda sym: _apply_experiment(EngineConfig(factor_signs=dict(get_market(sym).factor_signs), symbol=sym), args)  # noqa: E731
    rep = compare_information(frames, hist, start, end, args.equity, risk, n_folds=args.folds, step=args.step, horizon_min=args.horizon,
                              strategy=args.strategy, cfg_factory=factory, modes=modes, log=(print if args.verbose else None), ladder=args.ladder)
    print(rep.render())
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(rep.render())
        print(f"\nrelatório salvo em {args.out}")
    return 0


def _oos_results_for_markets(args: argparse.Namespace) -> dict:
    """Walk-forward OOS por mercado (mesmo motor do compare-news) → {símbolo: [BacktestResult por fold]}."""
    from .evaluation import Backtester, walk_forward
    from .markets import get_market
    from .config import EngineConfig

    if getattr(args, "events", None) and not os.path.exists(args.events):
        print(f"(banco histórico {args.events} não encontrado — contexto sem evento/relógio/fluxo)")
        args.events = None
    frames = {k: v for k, v in _frames_for_markets(args, args.markets).items() if len(v.xau) > 260}
    out = {}
    for sym, frame in frames.items():
        cfg = _apply_experiment(EngineConfig(factor_signs=dict(get_market(sym).factor_signs), symbol=sym), args)
        bt = Backtester(frame, cfg, step=args.step, horizon_min=args.horizon)
        wf = walk_forward(bt, n_folds=args.folds)
        out[sym] = [res for _, res in wf.folds]
    return out


def cmd_exit_lab(args: argparse.Namespace) -> int:
    """EXIT LAB (5.2): MFE/MAE das operações OOS → expectancy por saída (1R/2R/3R/trailing…) e política walk-forward; recomenda com n ≥ 20."""
    from .exit_lab import exit_lab

    results = _oos_results_for_markets(args)
    if not results:
        print("sem histórico suficiente (mínimo ~260 candles H1 por mercado)")
        return 1
    reports = []
    for sym, folds in results.items():
        rows = [r for res in folds for r in res.trade_rows]
        reports.append(exit_lab(sym, rows, n_blocks=args.blocks))
    txt = "\n\n".join(r.render() for r in reports)
    txt += "\n\nREGRA: a saída ao vivo não muda sozinha. Adote a candidata só quando a política walk-forward também superar a padrão com n ≥ 20 (ciclo de vida)."
    print(txt)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(txt)
        print(f"\nrelatório salvo em {args.out}")
    return 0


def cmd_edge_bank(args: argparse.Namespace) -> int:
    """EDGE BANK (5.2): contextos (regime, evento, banda VWAP, relógio, fluxo) × ativo → R por operação OOS, potencial, tier e transferência ponderada."""
    from .edge_bank import EdgeBank

    results = _oos_results_for_markets(args)
    if not results:
        print("sem histórico suficiente (mínimo ~260 candles H1 por mercado)")
        return 1
    bank = EdgeBank()
    n = 0
    for sym, folds in results.items():
        for res in folds:
            n += bank.add_backtest(sym, res.decisions, res.trade_rows, args.strategy)
    bank.apply_transfer()
    bank.note = (f"fonte: walk-forward OOS {args.start} → {args.end or 'hoje'} · {n} operações · saída '{args.strategy}' · "
                 "transferência = 0,5 × similaridade (fatores) × n do outro ativo; o n próprio define o tier")
    print(bank.render(list(results)))
    if args.out:
        bank.save(args.out)
        print(f"\nbanco salvo em {args.out} (o live carrega com --edge-bank; só n próprio ≥ 30 ajusta prioridade)")
    return 0


def cmd_portfolio_sim(args: argparse.Namespace) -> int:
    """PORTFOLIO SIM (5.2): as operações OOS de cada mercado em carteira com 1, 2, 3 e 4 posições simultâneas — retorno líquido, DD, recusas por correlação."""
    from .portfolio_sim import render_portfolio_sim, simulate_portfolio, trades_from_rows
    from .selector import PortfolioLimits
    from .telegram import load_env_file

    env = load_env_file()
    plim = PortfolioLimits.from_env(env)
    risk = args.risk if args.risk is not None else float(env.get("RISK_PER_TRADE", 0.5))
    results = _oos_results_for_markets(args)
    if not results:
        print("sem histórico suficiente (mínimo ~260 candles H1 por mercado)")
        return 1
    rows_by = {sym: [r for res in folds for r in res.trade_rows] for sym, folds in results.items()}
    trades = trades_from_rows(rows_by, args.strategy, cost_r=args.cost)
    if not trades:
        print("nenhuma operação OOS no período")
        return 1
    days = (trades[-1].time - trades[0].time).total_seconds() / 86400 if len(trades) > 1 else 0.0
    sims = [simulate_portfolio(trades, n, plim, args.equity, risk) for n in (1, 2, 3, 4)]
    txt = render_portfolio_sim(sims, args.equity, risk, plim, len(trades), days)
    print(txt)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(txt)
        print(f"\nrelatório salvo em {args.out}")
    return 0


def cmd_matrix(args: argparse.Namespace) -> int:
    """MATRIZ 5.2: confirmações 1→5 × posições simultâneas 1→4 — o teste escolhe na 1ª metade, a 2ª metade confere (nada é adotado por este quadro)."""
    from .config import EngineConfig
    from .evaluation import Backtester
    from .markets import get_market
    from .matrix import build_matrix
    from .selector import PortfolioLimits
    from .telegram import load_env_file

    if getattr(args, "events", None) and not os.path.exists(args.events):
        print(f"(banco histórico {args.events} não encontrado — funil sem evento/relógio/fluxo)")
        args.events = None
    frames = {k: v for k, v in _frames_for_markets(args, args.markets).items() if len(v.xau) > 260}
    if not frames:
        print("sem histórico suficiente (mínimo ~260 candles H1 por mercado)")
        return 1
    env = load_env_file()
    plim = PortfolioLimits.from_env(env)
    risk = args.risk if args.risk is not None else float(env.get("RISK_PER_TRADE", 0.5))
    confs = tuple(int(x) for x in args.confirmations.split(",")) if args.confirmations else (1, 2, 3, 4, 5)
    poss = tuple(int(x) for x in args.positions.split(",")) if args.positions else (1, 2, 3, 4)
    bts = {}
    for sym, frame in frames.items():
        cfg = _apply_experiment(EngineConfig(factor_signs=dict(get_market(sym).factor_signs), symbol=sym), args)
        bts[sym] = Backtester(frame, cfg, step=args.step, horizon_min=args.horizon)
    t0 = time.time()
    rep = build_matrix(bts, plim, args.equity, risk, args.cost, confs, poss, args.strategy, args.start,
                       args.end or datetime.now(timezone.utc).strftime("%Y-%m-%d"), log=lambda m: print(f"  [{time.time() - t0:6.0f}s] {m}", flush=True))
    txt = rep.render()
    print("\n" + txt)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(txt)
        rep.save(os.path.splitext(args.out)[0] + ".json" if not args.out.endswith(".json") else args.out)
        print(f"\nmatriz salva em {args.out} (+ .json)")
    return 0


def cmd_autotune(args: argparse.Namespace) -> int:
    """AUTOTUNE (5.2): a IA procura piso/confirmações/limiar no passado (walk-forward) e grava dados/parametros.json; o live adota só com n OOS ≥ 20."""
    from .autotune import DEFAULT_GRID, TuneReport, autotune_market
    from .config import EngineConfig
    from .evaluation import Backtester
    from .markets import get_market
    from .telegram import load_env_file

    if getattr(args, "events", None) and not os.path.exists(args.events):
        print(f"(banco histórico {args.events} não encontrado — funil sem evento/relógio/fluxo)")
        args.events = None
    frames = {k: v for k, v in _frames_for_markets(args, args.markets).items() if len(v.xau) > 260}
    if not frames:
        print("sem histórico suficiente (mínimo ~260 candles H1 por mercado)")
        return 1
    grid = dict(DEFAULT_GRID)
    if args.floors:
        grid["min_edge_score"] = tuple(float(x) for x in args.floors.split(","))
    if args.confirmations:
        grid["min_confirmations"] = tuple(int(x) for x in args.confirmations.split(","))
    if args.signals:
        grid["signal_score"] = tuple(int(x) for x in args.signals.split(","))
    risk = args.risk if args.risk is not None else float(load_env_file().get("RISK_PER_TRADE", 0.5))
    rep = TuneReport(args.start, args.end or datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    for sym, frame in frames.items():
        cfg = EngineConfig(factor_signs=dict(get_market(sym).factor_signs), symbol=sym)
        bt = Backtester(frame, cfg, step=args.step, horizon_min=args.horizon)
        rep.markets.append(autotune_market(sym, bt, grid, n_folds=args.folds, strategy=args.strategy, equity=args.equity, risk_pct=risk,
                                           log=(print if args.verbose else None)))
        print(rep.markets[-1].render() + "\n")
    print(rep.render())
    if args.out:
        rep.save(args.out)
        print(f"\nparâmetros salvos em {args.out} — o live lê com --params e adota só o que está marcado 'apply'")
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
        try:
            source = MT5Source(mcfg, data_engine=data)
            if mode != TradingMode.PAPER:
                source.client.connect()
                executor = ExecutionEngine(source.client)
        except Exception as e:  # noqa: BLE001
            if mode != TradingMode.PAPER:
                print(f"MT5 indisponível: {e}")
                return 1
            print(f"MT5 indisponível ({e}); modo PAPER continua com dados web")
            source, executor = data, None
    elif mode != TradingMode.PAPER:
        print("execução real exige --source mt5; rebaixando para PAPER")
        mode = TradingMode.PAPER

    calibrator = None
    if args.calibrator and os.path.exists(args.calibrator):
        with open(args.calibrator, encoding="utf-8") as f:
            calibrator = IsotonicCalibrator.from_dict(json.load(f))
        print(f"calibrador carregado: {args.calibrator}")
    sender = TelegramSender(dry_run=not args.send)
    commands = TelegramCommands(sender.token, sender.chat_id, offset_path=os.path.join("dados", "telegram_offset.txt")) if (args.send and not sender.dry_run) else None
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


def _utf8_console() -> None:
    """Windows: com a saída redirecionada para arquivo o Python usa cp1252 e '→'/emoji derrubam o programa. Força UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream and hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


class _Tee:
    """Escreve na tela E no arquivo de log (para os .bat mostrarem progresso sem perder o registro)."""

    def __init__(self, stream, path: str) -> None:
        self.stream, self.path = stream, path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.f = open(path, "a", encoding="utf-8", errors="replace")

    def write(self, data: str) -> int:
        self.stream.write(data)
        self.stream.flush()
        self.f.write(data)
        self.f.flush()
        return len(data)

    def flush(self) -> None:
        self.stream.flush()
        self.f.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    if argv is None:
        argv = sys.argv[1:]
    if "--log-file" in argv:
        i = argv.index("--log-file")
        if i + 1 < len(argv):
            log_path, argv = argv[i + 1], argv[:i] + argv[i + 2:]
            old_out, old_err = sys.stdout, sys.stderr
            sys.stdout, sys.stderr = _Tee(old_out, log_path), _Tee(old_err, log_path)
            try:
                return _main(argv)
            finally:
                for t in (sys.stdout, sys.stderr):
                    try:
                        t.f.close()
                    except Exception:  # noqa: BLE001
                        pass
                sys.stdout, sys.stderr = old_out, old_err
    return _main(argv)


def _main(argv: list[str]) -> int:
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
    lv.add_argument("--no-demo-only", dest="demo_only", action="store_false", default=True,
                    help="permite modo real em conta REAL (padrão: só em conta DEMO da corretora — trava de segurança)")
    lv.add_argument("--equity", type=float, default=10000.0, help="capital inicial (PAPER); em LIVE vem do broker")
    lv.add_argument("--horizon", type=int, default=240, help="minutos para resolver cada previsão/operação")
    lv.add_argument("--calibrator", default="calibrator.json", help="JSON gerado por `calibrate` (ignorado se não existir)")
    lv.add_argument("--kill-switch-file", default="STOP_TRADING", help="se o arquivo existir, nenhuma entrada nova")
    lv.add_argument("-v", "--verbose", action="store_true")
    lv.add_argument("--markets", default=None, help="4.0: lista de mercados, ex.: EURUSD,US500,XAUUSD,USDJPY,WTI (Asset Selector escolhe a melhor)")
    lv.add_argument("--reaction-edge", default=os.path.join("dados", "reaction_edge.json"), help="veredito do REACTION EDGE por ativo ('' = ignorar)")
    lv.add_argument("--edge-bank", default=os.path.join("dados", "edge_bank.json"), help="EDGE BANK (5.2) gerado por `edge-bank` ('' = ignorar)")
    lv.add_argument("--params", default=os.path.join("dados", "parametros.json"), help="parâmetros aprendidos por `autotune` ('' = ignorar); só 'apply' é adotado")
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

    for name, fn, hlp in (("exit-lab", cmd_exit_lab, "EXIT LAB 5.2: saída com maior expectancy OOS (MFE/MAE, 1R…4R, trailing, política walk-forward)"),
                          ("edge-bank", cmd_edge_bank, "EDGE BANK 5.2: o que funciona, onde funciona, quanto se transfere entre ativos (salva dados/edge_bank.json)"),
                          ("portfolio-sim", cmd_portfolio_sim, "PORTFOLIO SIM 5.2: 1 × 2 × 3 × 4 posições simultâneas com as operações OOS, líquido de custo e correlação")):
        xp = sub.add_parser(name, help=hlp)
        xp.add_argument("--events", default=os.path.join("dados", "noticias_historicas.csv"), help="banco histórico point-in-time (contexto: evento, relógio, fluxo)")
        xp.add_argument("--start", default="2026-01-01")
        xp.add_argument("--end", default=None)
        xp.add_argument("--markets", default="XAUUSD,US500,EURUSD,USDJPY,WTI")
        xp.add_argument("--csv-dir", default=None, help="pasta com <SYM>_h1.csv (senão Yahoo)")
        xp.add_argument("--strategy", default="adaptive")
        xp.add_argument("--folds", type=int, default=4)
        xp.add_argument("--step", type=int, default=1)
        xp.add_argument("--horizon", type=int, default=240)
        xp.add_argument("--blocks", type=int, default=4, help="exit-lab: blocos cronológicos da política walk-forward")
        xp.add_argument("--edge-score", type=float, default=None)
        xp.add_argument("--signal-score", type=float, default=None)
        xp.add_argument("--min-confirmations", type=int, default=None)
        xp.add_argument("--out", default=(os.path.join("dados", "edge_bank.json") if name == "edge-bank" else None))
        xp.add_argument("--equity", type=float, default=10000.0)
        xp.add_argument("--risk", type=float, default=None, help="portfolio-sim: risco %% por operação (padrão RISK_PER_TRADE do .env)")
        xp.add_argument("--cost", type=float, default=0.05, help="portfolio-sim: custo por operação em R (spread+slippage), descontado do resultado")
        xp.set_defaults(func=fn)

    at = sub.add_parser("autotune", help="AUTOTUNE 5.2: piso × confirmações × limiar de sinal escolhidos no passado (walk-forward) → dados/parametros.json")
    at.add_argument("--events", default=os.path.join("dados", "noticias_historicas.csv"))
    at.add_argument("--start", default="2026-01-01")
    at.add_argument("--end", default=None)
    at.add_argument("--markets", default="XAUUSD,US500,EURUSD,USDJPY,WTI")
    at.add_argument("--csv-dir", default=None)
    at.add_argument("--floors", default=None, help="pisos de vantagem, ex.: 15,25,35 (padrão)")
    at.add_argument("--confirmations", default=None, help="confirmações mínimas, ex.: 2,3 (padrão)")
    at.add_argument("--signals", default=None, help="limiar de sinal, ex.: 40,50 (padrão)")
    at.add_argument("--strategy", default="adaptive")
    at.add_argument("--folds", type=int, default=4)
    at.add_argument("--step", type=int, default=1)
    at.add_argument("--horizon", type=int, default=240)
    at.add_argument("--equity", type=float, default=10000.0)
    at.add_argument("--risk", type=float, default=None)
    at.add_argument("-v", "--verbose", action="store_true")
    at.add_argument("--out", default=os.path.join("dados", "parametros.json"))
    at.set_defaults(func=cmd_autotune)

    mx = sub.add_parser("matrix", help="MATRIZ 5.2: confirmações 1→5 × posições simultâneas 1→4 (n, acerto, R, expectancy, lucro, custos, MFE, MAE, DD, sequência, duração, por ativo/evento, 1ª × 2ª metade)")
    mx.add_argument("--events", default=os.path.join("dados", "noticias_historicas.csv"))
    mx.add_argument("--start", default="2026-01-01")
    mx.add_argument("--end", default=None)
    mx.add_argument("--markets", default="XAUUSD,US500,EURUSD,USDJPY,WTI")
    mx.add_argument("--csv-dir", default=None)
    mx.add_argument("--confirmations", default=None, help="níveis testados, ex.: 1,2,3,4,5 (padrão)")
    mx.add_argument("--positions", default=None, help="posições simultâneas testadas, ex.: 1,2,3,4 (padrão)")
    mx.add_argument("--strategy", default="adaptive")
    mx.add_argument("--step", type=int, default=1)
    mx.add_argument("--horizon", type=int, default=240)
    mx.add_argument("--edge-score", type=float, default=None)
    mx.add_argument("--signal-score", type=float, default=None)
    mx.add_argument("--equity", type=float, default=10000.0)
    mx.add_argument("--risk", type=float, default=None, help="risco %% por operação (padrão RISK_PER_TRADE do .env)")
    mx.add_argument("--cost", type=float, default=0.05, help="custo por operação em R (spread+slippage)")
    mx.add_argument("--out", default=None, help="ex.: matriz.txt (gera também matriz.json)")
    mx.set_defaults(func=cmd_matrix)

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

    hi = sub.add_parser("history", help="BANCO HISTÓRICO point-in-time: template | fetch-te | fetch-alfred | fetch-gdelt | rules | learn | stats | list | prices (M1/ticks do MT5)")
    hi.add_argument("action", choices=["template", "fetch-te", "fetch-alfred", "fetch-gdelt", "rules", "learn", "stats", "list", "prices"])
    hi.add_argument("--tf", default="TICK", help="prices: TICK (ticks bid/ask) | M1 (mt5: blocos de 14 dias; dukascopy: candles diários, completa o CSV do MT5) | M5")
    hi.add_argument("--source", choices=["mt5", "dukascopy"], default="dukascopy", help="prices: dukascopy (ticks gratuitos, sem chave) | mt5 (terminal logado)")
    hi.add_argument("--full", action="store_true", help="prices dukascopy: período inteiro (padrão: só horas ao redor dos eventos macro)")
    hi.add_argument("--before", type=int, default=4, help="prices dukascopy: horas antes de cada evento")
    hi.add_argument("--after", type=int, default=1, help="prices dukascopy: horas depois de cada evento")
    hi.add_argument("--scale", type=float, default=None, help="prices dukascopy: escala de preço do instrumento (padrão por mercado)")
    hi.add_argument("--extra", default=None, help="prices: símbolos extras da corretora para líderes, ex.: USDX,USTNOTE")
    hi.add_argument("--out-dir", default="dados")
    hi.add_argument("--kind", default=None, help="list: cpi, core_cpi, nfp, unemployment, jobless_claims, gdp, ppi, retail_sales, earnings, core_pce…")
    hi.add_argument("--category", default=None, help="list: MACRO, CENTRAL_BANK, GEOPOLITICAL, ENERGY, CHINA, NEWS")
    hi.add_argument("--limit", type=int, default=40)
    hi.add_argument("--file", default=os.path.join("dados", "noticias_historicas.csv"))
    hi.add_argument("--start", default="2026-01-01")
    hi.add_argument("--end", default=None)
    hi.add_argument("--key", default=None, help="chave da API (ou TE_API_KEY / FRED_API_KEY no .env)")
    hi.add_argument("--country", default="united states")
    hi.add_argument("--topics", default=None, help="GDELT: geopolitica,petroleo,china,fed,risco (padrão: todos)")
    hi.add_argument("--chunk-days", type=int, default=30, help="GDELT: dias por janela (menos janelas = menos chamadas; o GDELT limita a 1 a cada ~5 s)")
    hi.add_argument("--max-records", type=int, default=250, help="GDELT: manchetes por tema por janela (máx. 250)")
    hi.add_argument("--enrich", action="store_true", help="GDELT: além das manchetes/volume, baixar o tom (2× chamadas)")
    hi.add_argument("--gdelt-mode", choices=["volinfo", "artlist"], default="volinfo",
                    help="volinfo (padrão): manchetes mais relevantes de CADA DIA + volume, 1 chamada/janela; artlist: as mais recentes com hora exata")
    hi.add_argument("--pace", type=float, default=8.0, help="GDELT: segundos entre chamadas (aumente se receber 429 repetidos)")
    hi.add_argument("--max-minutes", type=float, default=None, help="GDELT: orçamento de tempo; ao esgotar, salva o que veio e devolve código 2 (incompleto)")
    hi.add_argument("--skip-if-covered", type=float, default=0.0, help="GDELT: se as manchetes já cobrirem esta fração dos dias do período, não busca (ex.: 0.8)")
    hi.add_argument("--markets", default="XAUUSD,US500,EURUSD,USDJPY,WTI", help="learn: mercados cujo preço define o efeito empírico")
    hi.add_argument("--horizon", type=int, default=60, help="learn: minutos após o evento para medir a direção")
    hi.add_argument("--min-n", type=int, default=8, help="learn: amostra mínima por tipo/sinal/mercado")
    hi.add_argument("--csv-dir", default=None)
    hi.add_argument("--no-fred", action="store_true")
    hi.set_defaults(func=cmd_history)

    fl = sub.add_parser("flow", help="5.0 FLOW ANOMALY ENGINE agora: FLOW SCORE, assinatura, origem (nunca 'banco central') e propagação líder → atrasados")
    fl.add_argument("--markets", default="XAUUSD,US500,EURUSD,USDJPY,WTI")
    fl.add_argument("--db", default="gold_ai.db")
    fl.add_argument("--no-fred", action="store_true")
    fl.add_argument("--no-news", action="store_true")
    fl.add_argument("--stats", action="store_true", help="5.2: o que aconteceu DEPOIS de cada anomalia registrada pelo live (continuação, MFE/MAE 5/15/30/60, confirmação)")
    fl.add_argument("--learn", action="store_true", help="5.2: REPLAY do M1 histórico (dados/<SYM>_m1.csv) como se fosse ao vivo → ledger com meses de anomalias medidas")
    fl.add_argument("--csv-dir", default="dados")
    fl.add_argument("--events", default=os.path.join("dados", "noticias_historicas.csv"))
    fl.add_argument("--lead-usd", default="USDX", help="learn: símbolo do índice do dólar em <csv-dir>/<SYM>_m1.csv (líder)")
    fl.add_argument("--step", type=int, default=5, help="learn: passo do replay em minutos")
    fl.set_defaults(func=cmd_flow)

    dc = sub.add_parser("doctor", help="tudo está funcionando? qual a eficiência? — painel por camada (✅ ⚠️ ❌) com ação e leitura do que está provado")
    dc.add_argument("--mt5", action="store_true", help="testa a conexão com o MetaTrader 5 (terminal aberto)")
    dc.add_argument("--telegram", action="store_true", help="envia uma mensagem de teste ao Telegram")
    dc.add_argument("--db", default="gold_ai.db")
    dc.add_argument("--events", default=os.path.join("dados", "noticias_historicas.csv"))
    dc.add_argument("--markets", default="XAUUSD,US500,EURUSD,USDJPY,WTI")
    dc.add_argument("--start", default="2026-01-01")
    dc.add_argument("--end", default=None)
    dc.add_argument("--out", default=None)
    dc.set_defaults(func=cmd_doctor)

    rc = sub.add_parser("reaction", help="REACTION ENGINE: learn (tempo de reação por evento/ativo no histórico) | stats (vivido) | clock (agora)")
    rc.add_argument("action", choices=["learn", "stats", "clock", "leadlag"])
    rc.add_argument("--at", default=None, help="leadlag: horário do episódio, ex.: '2026-09-15 17:05'")
    rc.add_argument("--tz", default="utc", help="leadlag: fuso do --at: utc | server (hora do terminal MT5) | -3 (hora local)")
    rc.add_argument("--leader", default="XAUUSD", help="leadlag: líder declarado (o M1 diz quem cruzou primeiro de fato)")
    rc.add_argument("--window", type=int, default=90, help="leadlag: horizonte em minutos após t0")
    rc.add_argument("--threshold", type=float, default=0.5, help="leadlag: cruzamento = |Δ| ≥ limiar × ATR horário")
    rc.add_argument("--source", choices=["mt5", "csv"], default="mt5", help="leadlag: M1 do terminal ou dados/<SYM>_m1.csv")
    rc.add_argument("--record", action="store_true", help="leadlag: grava o episódio como flow_<líder>_<up|down> na memória do relógio")
    rc.add_argument("--events", default=os.path.join("dados", "noticias_historicas.csv"))
    rc.add_argument("--markets", default="XAUUSD,US500,EURUSD,USDJPY,WTI")
    rc.add_argument("--start", default="2026-01-01")
    rc.add_argument("--end", default=None)
    rc.add_argument("--db", default="gold_ai.db")
    rc.add_argument("--csv-dir", default=None)
    rc.add_argument("--no-fred", action="store_true")
    rc.add_argument("--tf", default="H1", help="learn: H1 (Yahoo/CSV) | M1 | M5 | TICK | BOTH (TICK + M1 reamostrado + tabela de estabilidade)")
    rc.add_argument("--lead-usd", default="USDX", help="learn M1/TICK: símbolo exportado do líder USD (ex.: USDX); vazio = sem líder")
    rc.add_argument("--lead-yield", default=None, help="learn M1/TICK: símbolo exportado do líder de juros (ex.: USTNOTE)")
    rc.add_argument("--slippage", type=float, default=0.02, help="trade sim: slippage em ATR por perna")
    rc.add_argument("--latency", type=float, default=0.5, help="trade sim: latência de execução em segundos")
    rc.add_argument("--delays", default=None, help="prova do relógio: atrasos (s) após a reação do líder (padrão TICK 1,5,10,30,60; M1 60,120,300)")
    rc.add_argument("--edge-out", default=os.path.join("dados", "reaction_edge.json"), help="veredito por ativo para o Asset Selector ('' = não salvar)")
    rc.add_argument("--p-min", type=float, default=0.55, help="prova do relógio: P(alvo confirma | líder) mínima no histórico anterior")
    rc.add_argument("--out", default=None)
    rc.set_defaults(func=cmd_reaction)

    cn = sub.add_parser("compare-news", help="TESTE A/B: Preço somente × Preço + Macro (A) × Preço + Macro + News (B), walk-forward OOS, mesmo piso")
    cn.add_argument("--events", default=os.path.join("dados", "noticias_historicas.csv"))
    cn.add_argument("--start", default="2026-01-01")
    cn.add_argument("--end", default=None)
    cn.add_argument("--markets", default="US500,XAUUSD", help="ex.: EURUSD,US500,XAUUSD,USDJPY,WTI")
    cn.add_argument("--modes", default=None, help="none,macro,full,full_sem_relogio,full_sem_flow (padrão: none,macro,full; os dois últimos isolam o relógio e o fluxo)")
    cn.add_argument("--ladder", action="store_true", help="ESCADA 5.2: A preço · B +macro · C +news · D +flow · E +reaction clock, com funil de captura por camada")
    cn.add_argument("--min-coverage", type=float, default=0.8, help="cobertura mínima do banco no período (macro por semana, news por dia)")
    cn.add_argument("--allow-partial", action="store_true", help="roda mesmo com banco incompleto (resultado é ensaio, não conclusão)")
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
    mk.add_argument("--reaction-edge", default=os.path.join("dados", "reaction_edge.json"), help="veredito do REACTION EDGE por ativo ('' = ignorar)")
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
