"""Teste de acerto da IA por tempo gráfico, no estilo Rise/Fall com expiração fixa.

Para cada candle FECHADO o painel faz a mesma análise do botão "Iniciar IA" (últimos 300 candles). Se houver sinal,
abre uma "operação" no fechamento do candle e confere o preço `--expiry` minutos depois:
    COMPRA acerta se o preço subiu; VENDA acerta se caiu (empate conta como erro, como na Deriv).
Uma operação por vez: a próxima só pode sair depois que a anterior expirou ("de 10 em 10 minutos").

    python -m turbo_ia.backtest --source mt5 --symbols "Volatility 10 Index" --tfs M2,M5 --expiry 10
    python -m turbo_ia.backtest --source deriv --symbols R_10 --tfs M2,M5 --days 30        # pip install websocket-client
    python -m turbo_ia.backtest --source yahoo --symbols EURUSD=X,GC=F --tfs M2,M5          # forex/ouro, 60 dias
    python -m turbo_ia.backtest --source csv --csv dados/v10_m2.csv --tfs M2                  # time,open,high,low,close

Com payout p (ex.: 0,78 = 78 %), o acerto mínimo para empatar é 1 / (1 + p) — 56,2 % para 78 %.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import time
from typing import Optional

from .analysis import analyze

TF_SEC = {"M1": 60, "M2": 120, "M3": 180, "M5": 300, "M10": 600, "M15": 900, "M30": 1800, "H1": 3600}
WINDOW = 300


# ---------------------------------------------------------------------------------------------------- dados
def load_yahoo(symbol: str, tf: str) -> list[dict]:
    import requests
    iv = {"M1": "1m", "M2": "2m", "M5": "5m", "M15": "15m", "M30": "30m", "H1": "60m"}[tf]
    rng = "7d" if tf == "M1" else "60d"
    r = requests.get(f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}", params={"interval": iv, "range": rng},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    out = []
    for i, t in enumerate(res["timestamp"]):
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        out.append({"time": int(t), "open": o, "high": h, "low": l, "close": c})
    return out


def load_mt5(symbol: str, tf: str, count: int = 50_000) -> list[dict]:
    import MetaTrader5 as mt5  # type: ignore
    from .settings import mt5_path
    path = mt5_path()
    if not (mt5.initialize(path=path) if path else mt5.initialize()):
        raise RuntimeError(f"MT5 initialize falhou: {mt5.last_error()}")
    mt5.symbol_select(symbol, True)
    rates = mt5.copy_rates_from_pos(symbol, getattr(mt5, f"TIMEFRAME_{tf}"), 1, count)
    if rates is None or len(rates) == 0:
        raise RuntimeError(f"sem candles de {symbol} {tf}: {mt5.last_error()}")
    from .broker import detect_offset, server_to_utc
    off = detect_offset(mt5)
    print(f"  {symbol} {tf}: {len(rates)} velas do MT5 · servidor UTC{off:+g} agora → horários convertidos para UTC vela a vela", flush=True)
    return [{"time": server_to_utc(int(r["time"]), off), "open": float(r["open"]), "high": float(r["high"]), "low": float(r["low"]), "close": float(r["close"])} for r in rates]


def load_deriv(symbol: str, tf: str, days: int = 30) -> list[dict]:
    """Índices sintéticos da Deriv (R_10 = Volatility 10 Index, R_75, 1HZ10V…) pela API pública."""
    import websocket  # pip install websocket-client
    gran = TF_SEC[tf]
    need = int(days * 86400 / gran)
    ws = websocket.create_connection("wss://ws.derivws.com/websockets/v3?app_id=1089", timeout=30)
    out: dict[int, dict] = {}
    end: object = "latest"
    try:
        while len(out) < need:
            ws.send(json.dumps({"ticks_history": symbol, "style": "candles", "granularity": gran, "count": min(5000, need - len(out)), "end": end}))
            msg = json.loads(ws.recv())
            if msg.get("error"):
                raise RuntimeError(msg["error"].get("message"))
            cs = msg.get("candles") or []
            if not cs:
                break
            for c in cs:
                out[int(c["epoch"])] = {"time": int(c["epoch"]), "open": float(c["open"]), "high": float(c["high"]),
                                        "low": float(c["low"]), "close": float(c["close"])}
            end = int(cs[0]["epoch"]) - 1
            time.sleep(0.3)
    finally:
        ws.close()
    return [out[k] for k in sorted(out)]


def load_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return [{"time": int(float(r["time"])), **{k: float(r[k]) for k in ("open", "high", "low", "close")}} for r in csv.DictReader(f)]


# ---------------------------------------------------------------------------------------------------- teste
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return c - h, c + h


def run(candles: list[dict], tf: str, mode: str, expiry_min: int = 10, payout: float = 0.78, stake: float = 1.0) -> dict:
    sec = TF_SEC[tf]
    if (expiry_min * 60) % sec:
        raise ValueError(f"expiração de {expiry_min} min não é múltiplo do tempo gráfico {tf}")
    steps = expiry_min * 60 // sec
    trades: list[dict] = []
    i = WINDOW - 1
    n = len(candles)
    while i + steps < n:
        j = i + steps
        if candles[j]["time"] - candles[i]["time"] != expiry_min * 60:      # buraco no histórico (fim de semana, pausa): pula
            i += 1
            continue
        a = analyze(candles[i - WINDOW + 1:i + 1], mode)
        if a.signal == "NONE":
            i += 1
            continue
        entry, exit_ = candles[i]["close"], candles[j]["close"]
        win = exit_ > entry if a.signal == "BUY" else exit_ < entry
        trades.append({"time": candles[i]["time"] + sec, "side": a.signal, "entry": entry, "exit": exit_, "win": win, "strength": a.strength})
        i = j                                   # próxima só depois da expiração
    k = sum(t["win"] for t in trades)
    nt = len(trades)
    ties = sum(t["exit"] == t["entry"] for t in trades)
    lo, hi = wilson(k, nt)
    pnl = sum(stake * payout if t["win"] else -stake for t in trades)
    days = (candles[-1]["time"] - candles[WINDOW - 1]["time"]) / 86400 if n > WINDOW else 0
    return {"tf": tf, "mode": mode, "trades": nt, "wins": k, "rate": k / nt if nt else 0.0, "ci": (lo, hi),
            "ties": ties, "rate_ex_ties": k / (nt - ties) if nt > ties else 0.0, "pnl": pnl, "per_day": nt / days if days else 0.0, "days": days, "breakeven": 1 / (1 + payout),
            "buy": sum(t["side"] == "BUY" for t in trades), "list": trades}


# ---------------------------------------------------------------------------------------------------- alvo × stop
SPREAD = {"GC=F": 0.30, "XAUUSD": 0.30, "EURUSD=X": 0.00012, "EURUSD": 0.00012}


def signals(candles: list[dict], mode: str, utc_offset: float = 0.0, all_hours: bool = False) -> list[tuple[int, str, float]]:
    """(índice, lado, ATR) de todo candle fechado com sinal — calculado uma vez e reaproveitado em vários alvos/stops.
    `all_hours`: na reversão, ignora o filtro de horário (para medir hora a hora)."""
    from .analysis import analyze_reversion
    out = []
    for i in range(WINDOW - 1, len(candles) - 1):
        w = candles[i - WINDOW + 1:i + 1]
        a = analyze_reversion(w, hours=None, utc_offset=utc_offset) if (all_hours and mode == "reversao") else analyze(w, mode, utc_offset=utc_offset)
        if a.signal != "NONE" and a.atr > 0:
            out.append((i, a.signal, a.atr))
    return out


def simulate_rr(candles: list[dict], sigs: list[tuple[int, str, float]], rr: float, sl_atr: float, spread: float = 0.0,
                max_bars: int = 0) -> dict:
    """Entra no fechamento do candle do sinal com stop = sl_atr × ATR e alvo = rr × stop; anda candle a candle.
    Se stop e alvo cabem no mesmo candle, conta o STOP (pior caso). Spread descontado em R. Uma operação por vez."""
    by_i = {i: (side, a) for i, side, a in sigs}
    res: list[dict] = []
    i, n = WINDOW - 1, len(candles)
    while i < n - 1:
        if i not in by_i:
            i += 1
            continue
        side, a = by_i[i]
        e = candles[i]["close"]
        dist = sl_atr * a
        buy = side == "BUY"
        sl, tp = (e - dist, e + rr * dist) if buy else (e + dist, e - rr * dist)
        out, j = None, i + 1
        while j < n:
            c = candles[j]
            hit_sl = c["low"] <= sl if buy else c["high"] >= sl
            hit_tp = c["high"] >= tp if buy else c["low"] <= tp
            if hit_sl:
                out = -1.0
            elif hit_tp:
                out = rr
            elif max_bars and j - i >= max_bars:
                out = ((c["close"] - e) if buy else (e - c["close"])) / dist
            if out is not None:
                break
            j += 1
        if out is None:
            break                                   # histórico acabou com a operação aberta
        res.append({"time": candles[i]["time"], "side": side, "r": out - spread / dist, "win": out > 0, "bars": j - i})
        i = j
    nt = len(res)
    wins = sum(t["win"] for t in res)
    tot = sum(t["r"] for t in res)
    gp = sum(t["r"] for t in res if t["r"] > 0)
    gl = -sum(t["r"] for t in res if t["r"] < 0)
    eq = peak = dd = 0.0
    streak = worst = 0
    for t in res:
        eq += t["r"]
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
        streak = streak + 1 if not t["win"] else 0
        worst = max(worst, streak)
    half = nt // 2
    return {"rr": rr, "sl_atr": sl_atr, "trades": nt, "wins": wins, "rate": wins / nt if nt else 0.0, "ci": wilson(wins, nt),
            "breakeven": 1 / (1 + rr), "exp_r": tot / nt if nt else 0.0, "total_r": tot, "pf": gp / gl if gl else float("inf"),
            "max_dd_r": dd, "worst_streak": worst, "avg_bars": sum(t["bars"] for t in res) / nt if nt else 0.0,
            "exp_r_h1": sum(t["r"] for t in res[:half]) / half if half else 0.0,
            "exp_r_h2": sum(t["r"] for t in res[half:]) / (nt - half) if nt - half else 0.0, "list": res}


def baseline(candles: list[dict], tf: str, expiry_min: int = 10) -> float:
    """Taxa de 'subiu em 10 min' em todo o histórico: um palpite fixo de COMPRA acerta isso."""
    steps = expiry_min * 60 // TF_SEC[tf]
    ups = tot = 0
    for i in range(WINDOW - 1, len(candles) - steps):
        if candles[i + steps]["time"] - candles[i]["time"] == expiry_min * 60:
            tot += 1
            ups += candles[i + steps]["close"] > candles[i]["close"]
    return ups / tot if tot else 0.0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="turbo_ia.backtest", description="Acerto da IA por tempo gráfico (expiração fixa)")
    ap.add_argument("--source", choices=["yahoo", "mt5", "deriv", "csv"], default="mt5")
    ap.add_argument("--symbols", default="Volatility 10 Index")
    ap.add_argument("--csv")
    ap.add_argument("--tfs", default="M2,M5")
    ap.add_argument("--modes", default="veloz,equilibrado,preciso")
    ap.add_argument("--expiry", type=int, default=10, help="minutos até conferir o resultado")
    ap.add_argument("--payout", type=float, default=0.78)
    ap.add_argument("--days", type=int, default=30, help="só para --source deriv")
    ap.add_argument("--save", help="grava as operações em JSON")
    ap.add_argument("--rr", help="testa alvo × stop em vez de expiração, ex.: 2,3 (alvo 2× e 3× o stop)")
    ap.add_argument("--sl-atr", default="1.5", help="stop em ATRs, um ou vários: 1,1.5,2")
    ap.add_argument("--spread", type=float, help="spread em preço (padrão: típico do ativo)")
    ap.add_argument("--by-hour", action="store_true", help="reversão: mede cada hora UTC separadamente (com --rr)")
    a = ap.parse_args(argv)
    from .settings import load_env
    load_env()
    if a.rr:
        return main_rr(a)
    rows, dump = [], {}
    for sym in (a.symbols.split(",") if a.source != "csv" else [a.csv]):
        for tf in a.tfs.split(","):
            c = {"yahoo": lambda: load_yahoo(sym, tf), "mt5": lambda: load_mt5(sym, tf), "deriv": lambda: load_deriv(sym, tf, a.days),
                 "csv": lambda: load_csv(sym)}[a.source]()
            base = baseline(c, tf, a.expiry)
            for mode in a.modes.split(","):
                r = run(c, tf, mode, a.expiry, a.payout)
                r["symbol"], r["base"], r["candles"] = sym, base, len(c)
                rows.append(r)
                dump[f"{sym}|{tf}|{mode}"] = r.pop("list")
                print(f"  {sym:22} {tf}  {mode:11}  {r['trades']:5} ops  acerto {r['rate']:6.1%}  (IC95 {r['ci'][0]:.1%}–{r['ci'][1]:.1%})"
                      f"  sem empates {r['rate_ex_ties']:6.1%} ({r['ties']} empates)  resultado {r['pnl']:+8.2f}/1  · {r['per_day']:.0f} ops/dia", flush=True)
    be = 1 / (1 + a.payout)
    print(f"\nexpiração {a.expiry} min · payout {a.payout:.0%} → empate em {be:.1%} de acerto")
    for sym in dict.fromkeys(r["symbol"] for r in rows):
        rs = [r for r in rows if r["symbol"] == sym and r["trades"] >= 30]
        if rs:
            best = max(rs, key=lambda r: r["rate"])
            print(f"  {sym}: melhor = {best['tf']} {best['mode']} com {best['rate']:.1%} em {best['trades']} ops"
                  f" — {'ACIMA' if best['ci'][0] > be else 'NÃO comprovadamente acima'} do empate (IC95 mínimo {best['ci'][0]:.1%})")
    if a.save:
        with open(a.save, "w", encoding="utf-8") as f:
            json.dump({"rows": rows, "trades": dump}, f, default=str)
    return 0


def by_hour(c: list[dict], sym: str, tf: str, rrs: list[float], sl: float, spread: float) -> None:
    """Reversão em todas as horas; cada hora UTC simulada só com os seus sinais. Metades = 1ª e 2ª metade do período."""
    sigs = signals(c, "reversao", all_hours=True)
    print(f"\n  {sym} {tf} reversão hora a hora (UTC; Brasília = UTC−3) · stop {sl:g} ATR")
    print("   hora  " + "  ".join(f"{'%g:1' % rr:>34}" for rr in rrs))
    mid = c[len(c) // 2]["time"]
    for h in range(24):
        hs = [x for x in sigs if (c[x[0]]["time"] // 3600) % 24 == h]
        cells = []
        for rr in rrs:
            r = simulate_rr(c, hs, rr, sl, spread)
            lst = r.pop("list")
            e1 = [t["r"] for t in lst if t["time"] < mid]
            e2 = [t["r"] for t in lst if t["time"] >= mid]
            m1 = sum(e1) / len(e1) if e1 else 0.0
            m2 = sum(e2) / len(e2) if e2 else 0.0
            flag = "✔" if e1 and e2 and m1 > 0 and m2 > 0 and r["trades"] >= 40 else " "
            cells.append(f"{r['trades']:4} ops {r['rate']:5.1%} {r['exp_r']:+.2f}R ({m1:+.2f}/{m2:+.2f}){flag}")
        print(f"   {h:02d}h  " + "  ".join(f"{x:>34}" for x in cells), flush=True)
    print("   ✔ = positivo nas duas metades do período com ≥ 40 operações. Com 24 horas testadas, 1–2 ✔ podem ser sorte.")


def main_rr(a) -> int:
    rrs = [float(x) for x in a.rr.split(",")]
    sls = [float(x) for x in a.sl_atr.split(",")]
    print(f"alvo × stop · {', '.join(f'{r:g}:1' for r in rrs)} · stop {', '.join(f'{x:g}' for x in sls)} ATR · "
          f"stop antes do alvo quando os dois cabem no mesmo candle · spread descontado")
    for sym in (a.symbols.split(",") if a.source != "csv" else [a.csv]):
        spread = a.spread if a.spread is not None else SPREAD.get(sym, 0.0)
        for tf in a.tfs.split(","):
            c = {"yahoo": lambda: load_yahoo(sym, tf), "mt5": lambda: load_mt5(sym, tf), "deriv": lambda: load_deriv(sym, tf, a.days),
                 "csv": lambda: load_csv(sym)}[a.source]()
            if a.by_hour:
                by_hour(c, sym, tf, rrs, sls[0], spread)
                continue
            for mode in a.modes.split(","):
                sigs = signals(c, mode)
                for sl in sls:
                    for rr in rrs:
                        r = simulate_rr(c, sigs, rr, sl, spread)
                        r.pop("list")
                        print(f"  {sym:9} {tf} {mode:11} {rr:g}:1 stop {sl:g}ATR  {r['trades']:5} ops  acerto {r['rate']:5.1%} (precisa {r['breakeven']:5.1%})"
                              f"  média {r['exp_r']:+.3f}R  (1ª metade {r['exp_r_h1']:+.3f} · 2ª {r['exp_r_h2']:+.3f})  total {r['total_r']:+7.1f}R"
                              f"  PF {r['pf']:.2f}  pior queda {r['max_dd_r']:.0f}R  {r['worst_streak']} stops seguidos  ~{r['avg_bars']:.0f} velas", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
