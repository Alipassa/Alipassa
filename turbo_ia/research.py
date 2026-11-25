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
    return {"sma": ind.sma(cl, 20), "atr": ind.atr(c, 14), "ema200": ind.ema(cl, 200), "close": cl}


def trend_ok(f: dict, i: int, buy: bool, kind: Optional[str]) -> bool:
    """Filtros de tendência (só olham o passado). True = pode entrar.
    "favor":  só a favor da EMA 200 (inclinação nas últimas 30 velas): compra só com ela subindo, venda só caindo.
    "lateral": só quando a EMA 200 está quase plana (|inclinação em 30 velas| < 1,5 ATR).
    "impulso": não entra contra um impulso de mais de 4 ATR nas últimas 30 velas."""
    if not kind:
        return True
    e, a, cl = f["ema200"], f["atr"][i], f["close"]
    if i < 230 or e[i] is None or e[i - 30] is None or not a:
        return False
    slope = (e[i] - e[i - 30]) / a
    if kind == "favor":
        return slope > 0 if buy else slope < 0
    if kind == "lateral":
        return abs(slope) < 1.5
    if kind == "impulso":
        run = (cl[i] - cl[i - 30]) / a
        return not ((buy and run < -4) or ((not buy) and run > 4))
    return True


def simulate(c: list[dict], f: dict, v: dict, lo: int, hi: int, spread_def: float, med_spread: float) -> list[float]:
    """Uma operação por vez entre os índices [lo, hi). Devolve o resultado de cada operação em R (custos descontados)."""
    sma, atr = f["sma"], f["atr"]
    out: list[float] = []
    i = max(lo, 30)
    while i < hi - 1:
        a, m = atr[i], sma[i]
        if not a or m is None or (c[i]["time"] // 3600) % 24 not in v.get("hours", REVERSION_HOURS):
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
            elif ex == "3R+30velas" and j - i >= v.get("max_bars", 30):
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


# ---------------------------------------------------------------------------------------------------- varredura
TF_MIN = {"M1": 1, "M2": 2, "M3": 3, "M5": 5, "M10": 10, "M15": 15, "M30": 30}


def candidates(c: list[dict], f: dict, z_thr: float) -> list[tuple[int, bool, int]]:
    """(índice, compra?, hora UTC) de toda vela fechada esticada além de z_thr ATR."""
    out = []
    for i in range(30, len(c) - 1):
        a, m = f["atr"][i], f["sma"][i]
        if not a or m is None:
            continue
        z = (c[i]["close"] - m) / a
        if abs(z) > z_thr:
            out.append((i, z < 0, (c[i]["time"] // 3600) % 24))
    return out


def run_cands(c: list[dict], f: dict, cands, hours, ex: str, lo: int, hi: int, spread_def: float, max_bars: int,
              block: Optional[str] = None, trend: Optional[str] = None) -> list[tuple[int, float]]:
    """Simula só nas velas candidatas (rápido). Uma operação por vez. Devolve [(índice da entrada, R)].
    block: None · "dir" (depois de um stop, não repete o MESMO lado naquela janela de 1 h)
           · "window" (depois de um stop, encerra aquela janela de 1 h) · "2stops" (encerra a janela após 2 stops)."""
    out: list[tuple[int, float]] = []
    free = lo
    stops: dict = {}
    for i, buy, h in cands:
        if i < free or i < lo or h not in hours:
            continue
        win = c[i]["time"] // 3600
        if block == "dir" and stops.get((win, buy)):
            continue
        if block == "window" and stops.get(win):
            continue
        if block == "2stops" and stops.get(win, 0) >= 2:
            continue
        if not trend_ok(f, i, buy, trend):
            continue
        if i >= hi - 1:
            break
        a, m, e = f["atr"][i], f["sma"][i], c[i]["close"]
        dist = a
        sl = e - dist if buy else e + dist
        if ex == "media":
            tp = m
        else:
            rr = float(ex[0])
            tp = e + rr * dist if buy else e - rr * dist
        res, j = None, i + 1
        while j < len(c):
            x = c[j]
            if (x["low"] <= sl) if buy else (x["high"] >= sl):
                res = -1.0
            elif (x["high"] >= tp) if buy else (x["low"] <= tp):
                res = abs(tp - e) / dist
            elif ex.endswith("+1h") and j - i >= max_bars:
                res = ((x["close"] - e) if buy else (e - x["close"])) / dist
            if res is not None:
                break
            j += 1
        if res is None:
            break
        out.append((i, res - (c[i].get("spread") or spread_def) / dist))
        if res < 0:
            stops[(win, buy)] = stops.get((win, buy), 0) + 1
            stops[win] = stops.get(win, 0) + 1
        free = j
    return out


def trend_test(load, tfs: list[str], spread_def: float) -> None:
    """Regra atual × filtros de tendência (regras fixas, sem escolha) nas duas metades do histórico."""
    rules = ((None, None, "regra atual (sem filtro)"), ("favor", None, "só a favor da tendência (EMA 200)"),
             ("lateral", None, "só com tendência fraca (EMA 200 plana)"), ("impulso", None, "não entra contra impulso > 4 ATR"),
             ("favor", "2stops", "a favor da tendência + 2 stops encerra"))
    print("\nFILTRO DE TENDÊNCIA · 16h/20h UTC · esticada 2 · alvo 3× · spread real · metades A e B")
    for tf in tfs:
        c = load(tf)
        f = features(c)
        cz = candidates(c, f, 2.0)
        half = len(c) // 2
        dA = (c[half]["time"] - c[0]["time"]) / 86400 or 1
        dB = (c[-1]["time"] - c[half]["time"]) / 86400 or 1
        print(f"  {tf} ({(dA + dB):.0f} dias)")
        for trend, blk, label in rules:
            rA = [x for _, x in run_cands(c, f, cz, set(REVERSION_HOURS), "3R", 0, half, spread_def, 0, blk, trend)]
            rB = [x for _, x in run_cands(c, f, cz, set(REVERSION_HOURS), "3R", half, len(c), spread_def, 0, blk, trend)]
            sA, sB = summary(rA), summary(rB)
            streak = worst = 0
            for x in rA + rB:
                streak = streak + 1 if x < 0 else 0
                worst = max(worst, streak)
            print(f"    {label:40} A: {sA['n']:4} ops {sA['exp']:+.3f} R/op {sum(rA) / dA * 30:+6.1f} R/mês │ "
                  f"B: {sB['n']:4} ops {sB['exp']:+.3f} R/op {sum(rB) / dB * 30:+6.1f} R/mês │ pior queda {max(sA['dd'], sB['dd']):3.0f} R · "
                  f"{worst} stops seguidos", flush=True)
    print("Adotar um filtro só se ele tiver lucro por mês igual ou maior nas DUAS metades, ou quase igual com queda bem menor.")


def cooldown_test(load, tfs: list[str], spread_def: float) -> None:
    """Regra atual × travas depois de stop, nas duas metades do histórico (não há escolha: as 4 regras são fixas)."""
    rules = ((None, "regra atual (sem trava)"), ("dir", "após stop, não repete o lado na janela"),
             ("2stops", "após 2 stops, encerra a janela"), ("window", "após 1 stop, encerra a janela"))
    print("\nTRAVA DEPOIS DE STOP · 16h/20h UTC · esticada 2 · alvo 3× · spread real · metades A e B")
    for tf in tfs:
        c = load(tf)
        f = features(c)
        cz = candidates(c, f, 2.0)
        half = len(c) // 2
        dA = (c[half]["time"] - c[0]["time"]) / 86400 or 1
        dB = (c[-1]["time"] - c[half]["time"]) / 86400 or 1
        print(f"  {tf} ({(dA + dB):.0f} dias)")
        for blk, label in rules:
            rA = [x for _, x in run_cands(c, f, cz, set(REVERSION_HOURS), "3R", 0, half, spread_def, 0, blk)]
            rB = [x for _, x in run_cands(c, f, cz, set(REVERSION_HOURS), "3R", half, len(c), spread_def, 0, blk)]
            sA, sB = summary(rA), summary(rB)
            allr = rA + rB
            streak = worst = 0
            for x in allr:
                streak = streak + 1 if x < 0 else 0
                worst = max(worst, streak)
            print(f"    {label:40} A: {sA['n']:4} ops {sA['exp']:+.3f} R/op {sum(rA) / dA * 30:+6.1f} R/mês │ "
                  f"B: {sB['n']:4} ops {sB['exp']:+.3f} R/op {sum(rB) / dB * 30:+6.1f} R/mês │ pior queda {max(sA['dd'], sB['dd']):3.0f} R · "
                  f"{worst} stops seguidos", flush=True)
    print("Adotar uma trava só se ela tiver lucro por mês igual ou maior nas DUAS metades, ou quase igual com queda bem menor.")


def sweep(load, tfs: list[str], spread_def: float) -> None:
    """Para cada tempo gráfico: escolhe NA METADE A as horas, a esticada e a saída; mede o pacote inteiro NA METADE B.
    O que conta é a coluna B: é o resultado de um procedimento de escolha que nunca viu aqueles dados."""
    zs = (1.5, 2.0, 2.5)
    exits = ("3R", "media", "3R+1h")
    print("\nVARREDURA · tempo gráfico × hora × esticada × saída · escolha na metade A, prova na metade B · spread real quando houver")
    print(f"  {'gráfico':7} {'dias':>4} │ {'escolhido na metade A':48} │ {'A: ops   R/op  R/mês':22} │ {'B (PROVA): ops  acerto   R/op  R/mês  queda':44}")
    summary_rows = []
    for tf in tfs:
        try:
            c = load(tf)
        except Exception as e:  # noqa: BLE001
            print(f"  {tf:7} sem dados: {e}")
            continue
        if len(c) < 2000:
            print(f"  {tf:7} poucas velas ({len(c)})")
            continue
        f = features(c)
        half = len(c) // 2
        dA = (c[half]["time"] - c[0]["time"]) / 86400 or 1
        dB = (c[-1]["time"] - c[half]["time"]) / 86400 or 1
        mb = max(1, 60 // TF_MIN.get(tf, 1))
        best = None
        for z in zs:
            cz = candidates(c, f, z)
            for ex in exits:
                good = []
                for h in range(24):
                    r = [x for _, x in run_cands(c, f, cz, {h}, ex, 0, half, spread_def, mb)]
                    if len(r) >= 25 and sum(r) / len(r) > 0.10:
                        good.append(h)
                if not good:
                    continue
                rA = [x for _, x in run_cands(c, f, cz, set(good), ex, 0, half, spread_def, mb)]
                totA = sum(rA)
                if best is None or totA > best[0]:
                    best = (totA, z, ex, good, rA, cz)
        base_cz = candidates(c, f, 2.0)
        base = [x for _, x in run_cands(c, f, base_cz, set(REVERSION_HOURS), "3R", half, len(c), spread_def, mb)]
        sb = summary(base)
        base_line = (f"  {'':7} {'':4} │ {'regra atual (16h/20h · z>2 · 3R) na metade B':48} │ {'':22} │"
                     f" {sb['n']:4}  {sb['win']:5.1%}  {sb['exp']:+.3f} {sum(base) / dB * 30:+6.1f}  {sb['dd']:4.0f}R")
        if best is None:
            print(f"  {tf:7} {dB + dA:4.0f} │ nenhuma hora com +0,10 R na metade A")
            print(base_line)
            summary_rows.append((tf, sum(base) / dB * 30, sb, "regra atual (16h/20h · z>2 · 3R)"))
            continue
        totA, z, ex, good, rA, cz = best
        rB = [x for _, x in run_cands(c, f, cz, set(good), ex, half, len(c), spread_def, mb)]
        sB = summary(rB)
        desc = f"z>{z:g} · {ex:6} · {len(good):2}h: {','.join(str(h) for h in good)}"
        print(f"  {tf:7} {dA + dB:4.0f} │ {desc[:48]:48} │ {len(rA):4} {sum(rA) / len(rA):+.3f} {totA / dA * 30:+6.1f} │"
              f" {sB['n']:4}  {sB['win']:5.1%}  {sB['exp']:+.3f} {sum(rB) / dB * 30:+6.1f}  {sB['dd']:4.0f}R", flush=True)
        print(base_line, flush=True)
        summary_rows.append((tf, sum(rB) / dB * 30, sB, desc))
        summary_rows.append((tf, sum(base) / dB * 30, sb, "regra atual (16h/20h · z>2 · 3R)"))
    if summary_rows:
        cur = next((r for r in summary_rows if r[0] == "M2" and r[3].startswith("regra atual")), None)
        print("\nRANKING NA PROVA (metade B) — lucro por mês em R; só conta o que tem ≥ 30 operações:")
        ok = [r for r in summary_rows if r[2]["n"] >= 30]
        for tf, rm, sB, desc in sorted(ok, key=lambda r: -r[1])[:8]:
            print(f"  {tf:4} {rm:+6.1f} R/mês · {sB['exp']:+.3f} R/op · {sB['n']:4} ops · queda {sB['dd']:3.0f} R · {desc}")
        if ok and cur:
            tf, rm, sB, desc = max(ok, key=lambda r: r[1])
            if (tf, desc) == (cur[0], cur[3]) or rm <= cur[1] * 1.2 or sB["exp"] <= 0.10:
                print(f"\nVEREDITO: nada supera com folga a regra atual no M2 ({cur[1]:+.1f} R/mês na prova). Manter.")
            else:
                print(f"\nVEREDITO: candidata {tf} — {desc}: {rm:+.1f} R/mês na prova contra {cur[1]:+.1f} da regra atual. "
                      "Muitas combinações testadas: rode em DEMO lado a lado antes de trocar.")
        print("R/mês × risco = % do saldo por mês (ex.: +10 R/mês a 2 % ≈ +20 %/mês, antes dos juros compostos).")


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
    ap.add_argument("--sweep", action="store_true", help="varre tempos gráficos × horas × esticada × saída")
    ap.add_argument("--tfs", default="M1,M2,M3,M5,M10,M15")
    ap.add_argument("--cooldown", action="store_true", help="compara travas depois de stop com a regra atual")
    ap.add_argument("--trend", action="store_true", help="compara filtros de tendência com a regra atual")
    a = ap.parse_args(argv)
    from .settings import load_env
    load_env()
    if a.trend:
        spread_def = a.spread if a.spread is not None else SPREAD.get(a.symbol, 0.30)
        load = (lambda tf: load_mt5(a.symbol, tf, a.bars)) if a.source == "mt5" else (lambda tf: load_yahoo(a.symbol, tf))
        trend_test(load, a.tfs.split(","), spread_def)
        return 0
    if a.cooldown:
        spread_def = a.spread if a.spread is not None else SPREAD.get(a.symbol, 0.30)
        load = (lambda tf: load_mt5(a.symbol, tf, a.bars)) if a.source == "mt5" else (lambda tf: load_yahoo(a.symbol, tf))
        cooldown_test(load, a.tfs.split(","), spread_def)
        return 0
    if a.sweep:
        spread_def = a.spread if a.spread is not None else SPREAD.get(a.symbol, 0.30)
        load = (lambda tf: load_mt5(a.symbol, tf, a.bars)) if a.source == "mt5" else (lambda tf: load_yahoo(a.symbol, tf))
        sweep(load, a.tfs.split(","), spread_def)
        return 0
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
