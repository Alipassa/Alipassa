"""Pesquisa de melhorias da Reversão ouro — com trava contra "otimizar o passado".

Divide o histórico em duas metades no tempo:
  A (1ª metade) = onde cada variação é ESCOLHIDA;
  B (2ª metade) = prova: a variação só é aprovada se também ganhar da regra atual em B.
Ranking pelo lucro médio por operação (R), não pelo acerto: acertar mais com alvo menor pode dar MENOS dinheiro.

    python -m turbo_ia.research --source mt5 --symbol XAUUSD --bars 200000
    python -m turbo_ia.research --source yahoo --symbol GC=F

Variações testadas (todas nos horários 16h/20h UTC):
  entrada:  esticada > 1,0 / 1,25 / 1,5 / 2,0 ATR da média 20 · com ou sem vela de confirmação (já virando para a média)
  saída:    alvo 2× / 3× / 4× o stop · alvo = a própria média 20 · 3× com stop no zero a zero após +1R · 3× com saída em 30 velas
  custo:    (MT5) pular quando o spread da vela está > 1,5 × o spread mediano
"""

from __future__ import annotations

import argparse
import itertools
from typing import Optional

from . import indicators as ind
from .analysis import REVERSION_HOURS
from .backtest import SPREAD, load_mt5, load_yahoo

BASE = {"z": 1.0, "confirm": False, "exit": "3R", "spread_cap": False}
EXITS = ("2R", "3R", "4R", "media", "3R+zero", "3R+30velas")


def features(c: list[dict]) -> dict:
    cl = [x["close"] for x in c]
    return {"sma": ind.sma(cl, 20), "atr": ind.atr(c, 14)}


def simulate(c: list[dict], f: dict, v: dict, lo: int, hi: int, spread_def: float, med_spread: float) -> list[float]:
    """Uma operação por vez entre os índices [lo, hi). Devolve o resultado de cada operação em R (custos descontados)."""
    sma, atr = f["sma"], f["atr"]
    out: list[float] = []
    i = max(lo, 30)
    while i < hi - 1:
        a, m = atr[i], sma[i]
        if not a or m is None or (c[i]["time"] // 3600) % 24 not in REVERSION_HOURS:
            i += 1
            continue
        e = c[i]["close"]
        z = (e - m) / a
        if abs(z) <= v["z"]:
            i += 1
            continue
        buy = z < 0
        if v["confirm"] and not ((c[i]["close"] > c[i]["open"]) if buy else (c[i]["close"] < c[i]["open"])):
            i += 1
            continue
        sp = c[i].get("spread") or spread_def
        if v["spread_cap"] and c[i].get("spread") and med_spread and sp > 1.5 * med_spread:
            i += 1
            continue
        dist = a
        sl = e - dist if buy else e + dist
        ex = v["exit"]
        if ex == "media":
            tp = m
            if (buy and tp <= e) or ((not buy) and tp >= e):
                i += 1
                continue
        else:
            rr = float(ex[0])
            tp = e + rr * dist if buy else e - rr * dist
        be_done, res, j = False, None, i + 1
        while j < len(c):
            x = c[j]
            if (x["low"] <= sl) if buy else (x["high"] >= sl):
                res = (sl - e) / dist if buy else (e - sl) / dist
            elif (x["high"] >= tp) if buy else (x["low"] <= tp):
                res = abs(tp - e) / dist
            elif ex == "3R+30velas" and j - i >= 30:
                res = ((x["close"] - e) if buy else (e - x["close"])) / dist
            if res is not None:
                break
            if ex == "3R+zero" and not be_done and (((x["high"] - e) if buy else (e - x["low"])) >= dist):
                sl, be_done = e, True                    # vale a partir da PRÓXIMA vela (sem adivinhar a ordem dentro da vela)
            j += 1
        if res is None:
            break
        out.append(res - sp / dist)
        i = j
    return out


def summary(r: list[float]) -> dict:
    n = len(r)
    if not n:
        return {"n": 0, "exp": 0.0, "win": 0.0, "dd": 0.0}
    eq = pk = dd = 0.0
    for x in r:
        eq += x
        pk = max(pk, eq)
        dd = max(dd, pk - eq)
    return {"n": n, "exp": sum(r) / n, "win": sum(x > 0 for x in r) / n, "dd": dd}


def name(v: dict) -> str:
    return (f"z>{v['z']:g}{' +confirma' if v['confirm'] else ''} · saída {v['exit']}"
            f"{' · filtro spread' if v['spread_cap'] else ''}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="turbo_ia.research")
    ap.add_argument("--source", choices=["mt5", "yahoo"], default="mt5")
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--bars", type=int, default=200_000)
    ap.add_argument("--spread", type=float)
    a = ap.parse_args(argv)
    from .settings import load_env
    load_env()
    c = load_mt5(a.symbol, "M2", a.bars) if a.source == "mt5" else load_yahoo(a.symbol, "M2")
    spread_def = a.spread if a.spread is not None else SPREAD.get(a.symbol, 0.30)
    sps = sorted(x["spread"] for x in c if x.get("spread"))
    med = sps[len(sps) // 2] if sps else 0.0
    f = features(c)
    half = len(c) // 2
    days = (c[-1]["time"] - c[0]["time"]) / 86400
    print(f"\n{a.symbol} M2 · {len(c)} velas ≈ {days:.0f} dias · metade A = escolha, metade B = prova · horários {REVERSION_HOURS} UTC"
          + (f" · spread mediano {med:.2f}" if med else f" · spread fixo {spread_def}"))
    grid = [{"z": z, "confirm": cf, "exit": ex, "spread_cap": sc}
            for z, cf, ex, sc in itertools.product((1.0, 1.25, 1.5, 2.0), (False, True), EXITS, (False, True) if med else (False,))]
    rows = []
    for v in grid:
        ra = summary(simulate(c, f, v, 0, half, spread_def, med))
        rb = summary(simulate(c, f, v, half, len(c), spread_def, med))
        rows.append((v, ra, rb))
    base = next(r for r in rows if r[0] == BASE)
    rows.sort(key=lambda r: -r[1]["exp"] if r[1]["n"] >= 60 else 1e9)
    line = lambda v, ra, rb: (f"  {name(v):42} A: {ra['n']:4} ops {ra['win']:5.1%} {ra['exp']:+.3f}R │ "
                              f"B: {rb['n']:4} ops {rb['win']:5.1%} {rb['exp']:+.3f}R  pior queda {rb['dd']:.0f}R")
    print("\nREGRA ATUAL")
    print(line(*base))
    print("\nTOP 10 escolhidas só pela metade A (mín. 60 operações) → como foram na metade B")
    approved = []
    for v, ra, rb in rows[:10]:
        ok = v != BASE and ra["exp"] > base[1]["exp"] and rb["exp"] > base[2]["exp"] and rb["n"] >= 60
        print(line(v, ra, rb) + ("   ✔ melhor nas duas" if ok else ""))
        if ok:
            approved.append((v, ra, rb))
    print()
    if approved:
        v, ra, rb = approved[0]
        print(f"CANDIDATA: {name(v)} — ganhou da regra atual em A ({ra['exp']:+.3f} × {base[1]['exp']:+.3f}) e em B "
              f"({rb['exp']:+.3f} × {base[2]['exp']:+.3f}). Foram {len(grid)} variações testadas: parte dessa vantagem pode ser sorte.")
    else:
        print("Nenhuma variação escolhida em A ganhou da regra atual também em B: manter a regra atual.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
