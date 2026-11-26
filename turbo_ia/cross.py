"""Teste da estratégia do usuário: cruzamento de médias 9 × 20 com filtro de ATR contra mercado lateral.

    python -m turbo_ia.cross --source mt5 --symbol XAUUSD --tfs M5,M15,H1

Regra (fixada antes do teste):
  • média 9 cruza a média 20 para CIMA → compra; para BAIXO → venda (no fechamento da vela do cruzamento)
  • sai no cruzamento contrário (e vira a mão se o filtro deixar)
  • filtro de lateralidade: só entra se ATR(14) > média de 50 velas do próprio ATR (mercado com movimento)
Variações: médias exponenciais (EMA) ou simples (SMA) · sem stop ou stop de 2 ATR (stop antes de tudo na vela).
Resultado em R (1 R = 1 ATR na entrada) e em pontos de preço, com spread descontado. Metades A/B e, no MT5, o COFRE
(antes de 20/05/2026).
"""

from __future__ import annotations

import argparse
import calendar
from typing import Optional

from . import indicators as ind
from .backtest import SPREAD, load_mt5, load_yahoo
from .research import summary

CUTOFF = calendar.timegm((2026, 5, 20, 0, 0, 0))


REGIME = {
    "ADX>20": ("adx", 20), "ADX>25": ("adx", 25), "CHOP<61.8": ("chop", 61.8), "CHOP<50": ("chop", 50),
    "ER>0.3": ("er", 0.3), "lado da SMA200": ("sma200", None), "SMA50 inclinada": ("slope50", 1.0),
}


def regime_series(c: list[dict]) -> dict:
    cl = [x["close"] for x in c]
    s50 = ind.sma(cl, 50)
    return {"adx": ind.adx(c, 14), "chop": ind.choppiness(c, 14), "er": ind.efficiency_ratio(cl, 20),
            "sma200": ind.sma(cl, 200), "sma50": s50}


def regime_ok(rs: dict, c: list[dict], atr: list, i: int, side: int, rule: Optional[str]) -> bool:
    """True = pode entrar. Só olha o passado (valores até a vela i)."""
    if not rule:
        return True
    kind, thr = REGIME[rule]
    if kind == "adx":
        return rs["adx"][i] is not None and rs["adx"][i] > thr
    if kind == "chop":
        return rs["chop"][i] is not None and rs["chop"][i] < thr
    if kind == "er":
        return rs["er"][i] is not None and rs["er"][i] > thr
    if kind == "sma200":
        m = rs["sma200"][i]
        return m is not None and ((c[i]["close"] > m) if side > 0 else (c[i]["close"] < m))
    if kind == "slope50":
        a, b_ = rs["sma50"][i], rs["sma50"][i - 10] if i >= 10 else None
        return a is not None and b_ is not None and bool(atr[i]) and ((a - b_) * side > thr * atr[i])
    return True


def simulate(c: list[dict], kind: str = "ema", stop_atr: Optional[float] = None, atr_filter: bool = True,
             spread_def: float = 0.3, lo: int = 0, hi: Optional[int] = None, regime: Optional[str] = None,
             rs: Optional[dict] = None) -> list[tuple[int, float, float]]:
    """Devolve [(tempo da entrada, R, pontos)] — uma posição por vez. `regime`: filtro de lateralidade na ENTRADA
    (a saída no cruzamento contrário acontece sempre; se o filtro barra a nova entrada, fica de fora até o próximo cruzamento)."""
    hi = hi or len(c)
    if regime and rs is None:
        rs = regime_series(c)
    cl = [x["close"] for x in c]
    f9 = ind.ema(cl, 9) if kind == "ema" else ind.sma(cl, 9)
    f20 = ind.ema(cl, 20) if kind == "ema" else ind.sma(cl, 20)
    atr = ind.atr(c, 14)
    atr_avg = ind.sma([a or 0.0 for a in atr], 50)
    out: list[tuple[int, float, float]] = []
    pos = None                                        # (lado, entrada, índice, atr, stop)
    for i in range(max(lo, 70), hi):
        if f9[i] is None or f20[i] is None or f9[i - 1] is None or f20[i - 1] is None or not atr[i]:
            continue
        x = c[i]
        if pos and stop_atr:                          # stop dentro da vela (antes do cruzamento do fechamento)
            side, e, k, a, sl = pos
            if (x["low"] <= sl) if side > 0 else (x["high"] >= sl):
                pts = (sl - e) * side
                sp = c[k].get("spread") or spread_def
                out.append((c[k]["time"], (pts - sp) / a, pts - sp))
                pos = None
        up = f9[i - 1] <= f20[i - 1] and f9[i] > f20[i]
        dn = f9[i - 1] >= f20[i - 1] and f9[i] < f20[i]
        if not (up or dn):
            continue
        if pos:                                       # cruzamento contrário: sai no fechamento
            side, e, k, a, _ = pos
            if (side > 0 and dn) or (side < 0 and up):
                pts = (x["close"] - e) * side
                sp = c[k].get("spread") or spread_def
                out.append((c[k]["time"], (pts - sp) / a, pts - sp))
                pos = None
        if pos is None:
            if atr_filter and (atr_avg[i] is None or atr[i] <= atr_avg[i]):
                continue                              # mercado lateral (ATR abaixo da média): não entra
            side = 1 if up else -1
            if not regime_ok(rs, c, atr, i, side, regime):
                continue
            a = atr[i]
            sl = (x["close"] - side * stop_atr * a) if stop_atr else None
            pos = (side, x["close"], i, a, sl)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="turbo_ia.cross")
    ap.add_argument("--source", choices=["mt5", "yahoo"], default="mt5")
    ap.add_argument("--symbol", default="XAUUSD", help="um ou vários, separados por vírgula")
    ap.add_argument("--tfs", default="M5,M15,H1")
    ap.add_argument("--bars", type=int, default=2_000_000)
    ap.add_argument("--years", action="store_true", help="mostra o resultado ano a ano")
    ap.add_argument("--regime", action="store_true", help="testa filtros de mercado lateral no SMA 9×20 stop 2 ATR")
    a = ap.parse_args(argv)
    from .settings import load_env
    load_env()
    if a.regime:
        return regime_main(a)
    print("CRUZAMENTO 9 × 20 + filtro ATR · saída no cruzamento contrário · spread descontado · 1 R = 1 ATR na entrada")
    summary_rows = []
    for sym, tf in [(s_, t_) for s_ in a.symbol.split(",") for t_ in a.tfs.split(",")]:
        spread_def = SPREAD.get(sym, 0.0)
        try:
            c = load_mt5(sym, tf, a.bars) if a.source == "mt5" else load_yahoo(sym, tf)
        except Exception as e:  # noqa: BLE001
            print(f"  {sym} {tf}: sem dados ({str(e)[:80]})")
            continue
        if len(c) < 600:
            print(f"  {sym} {tf}: poucas velas ({len(c)})")
            continue
        n = len(c)
        cut = next((i for i, x in enumerate(c) if x["time"] >= CUTOFF), 0)
        active = sorted({x["time"] // 86400 for x in c})                 # dias com velas (histórico antigo tem buracos)
        mid = cut + (n - cut) // 2
        def days(lo: int, hi: int) -> float:
            """Dias corridos EQUIVALENTES aos dias com dados (5 dias de mercado = 7 corridos) — não infla o período com buracos."""
            d0, d1 = c[lo]["time"] // 86400, c[hi - 1]["time"] // 86400
            k = sum(1 for d in active if d0 <= d <= d1)
            return max(k * 7 / 5, 1)
        print(f"\n  {sym} {tf} · spread {spread_def:g} · {n} velas ≈ {days(0, n):.0f} dias" + (f" · cofre {days(0, cut):.0f} dias" if cut > 500 else ""))
        print(f"    {'variação':30} {'ops':>5} {'acerto':>7} {'R/op':>7} {'pts/op':>7} {'R/mês':>7} {'PF':>5} {'queda':>6} │ A R/mês │ B R/mês" + (" │ COFRE R/mês" if cut > 500 else ""))
        for kind in ("ema", "sma"):
            for stop in (None, 2.0):
                for filt in (True, False):
                    tag = f"{kind.upper()} 9×20 {'+ATR' if filt else 'sem filtro'}{' stop 2ATR' if stop else ''}"
                    r = simulate(c, kind, stop, filt, spread_def, cut, n)
                    rs = [x[1] for x in r]
                    s = summary(rs)
                    gp, gl = sum(x for x in rs if x > 0), -sum(x for x in rs if x < 0)
                    pts = sum(x[2] for x in r) / len(r) if r else 0.0
                    ra = [x[1] for x in r if x[0] < c[mid]["time"]]
                    rb = [x[1] for x in r if x[0] >= c[mid]["time"]]
                    line = (f"    {tag:30} {s['n']:5} {s['win']:7.1%} {s['exp']:+7.3f} {pts:+7.2f} {sum(rs) / days(cut, n) * 30:+7.1f} "
                            f"{(gp / gl if gl else 0):5.2f} {s['dd']:5.0f}R │ {sum(ra) / days(cut, mid) * 30:+7.1f} │ {sum(rb) / days(mid, n) * 30:+7.1f}")
                    rc_m = None
                    if cut > 500:
                        rc = [x[1] for x in simulate(c, kind, stop, filt, spread_def, 0, cut)]
                        rc_m = sum(rc) / days(0, cut) * 30
                        line += f" │ {rc_m:+7.1f} ({summary(rc)['exp']:+.3f}/op)"
                    print(line, flush=True)
                    ok = (ra and rb and sum(ra) > 0 and sum(rb) > 0 and (rc_m is None or rc_m > 0) and s["n"] >= 30)
                    summary_rows.append((sym, tf, tag, ok, s["exp"], rc_m))
        if a.years:
            print(f"    ano a ano ({sym} {tf}) — R por operação · total R · operações:")
            import time as _t
            yrs = sorted({_t.gmtime(x["time"]).tm_year for x in c})
            print("      " + "".join(f"{y:>16}" for y in yrs))
            for kind, stop, filt in (("sma", None, False), ("sma", 2.0, False), ("sma", None, True), ("ema", None, False)):
                tag = f"{kind.upper()} 9×20 {'+ATR' if filt else 'sem filtro'}{' stop 2ATR' if stop else ''}"
                r = simulate(c, kind, stop, filt, spread_def, 0, n)
                cells = []
                pos_years = 0
                for y in yrs:
                    ry = [x[1] for x in r if _t.gmtime(x[0]).tm_year == y]
                    if ry:
                        pos_years += sum(ry) > 0
                        cells.append(f"{sum(ry) / len(ry):+.2f} {sum(ry):+5.0f} {len(ry):3}")
                    else:
                        cells.append("—")
                print(f"      {tag:28}" + "".join(f"{x:>16}" for x in cells) + f"   → {pos_years}/{len([1 for x in cells if x != '—'])} anos positivos")
    good = [r for r in summary_rows if r[3]]
    print("\nPOSITIVO EM TODOS OS PERÍODOS (A, B" + (" e cofre" if any(r[5] is not None for r in summary_rows) else "") + "):")
    for sym, tf, tag, _, e, rc in good:
        print(f"  ✔ {sym:10} {tf:4} {tag:30} {e:+.3f} R/op" + (f" · cofre {rc:+.1f} R/mês" if rc is not None else ""))
    if not good:
        print("  nenhum")
    tested = len(summary_rows)
    print(f"  ({tested} combinações testadas: espere ~{max(1, tested // 8)} 'aprovação' só por acaso — desconfie de casos isolados)")
    print("\nLeitura: R/op > 0 nas metades A e B (e no cofre) = vantagem que se repete. Spread pesa mais em gráficos curtos.")
    return 0


def regime_main(a) -> int:
    """Filtros de lateralidade sobre o modo do painel (SMA 9×20, sem filtro de ATR, stop 2 ATR), ano a ano.
    Adoção (fixada antes): mais R total E pelo menos tantos anos positivos quanto sem filtro E pior ano não pior,
    olhando 2016+ (histórico denso) — e melhor também no cofre (antes de 20/05/2026) e no desenvolvimento."""
    import time as _t
    for sym in a.symbol.split(","):
        for tf in a.tfs.split(","):
            spread_def = SPREAD.get(sym, 0.0)
            c = load_mt5(sym, tf, a.bars) if a.source == "mt5" else load_yahoo(sym, tf)
            rs = regime_series(c)
            yrs = [y for y in sorted({_t.gmtime(x["time"]).tm_year for x in c})
                   if len([1 for x in c if _t.gmtime(x["time"]).tm_year == y]) > 1000]          # anos com histórico denso
            print(f"\nFILTROS DE LATERALIDADE · {sym} {tf} · SMA 9×20 stop 2 ATR · spread real · anos com dados densos: {yrs[0]}–{yrs[-1]}")
            print(f"  {'filtro':18} " + "".join(f"{y:>6}" for y in yrs) + f" │ {'total':>6} {'anos+':>5} {'pior':>5} {'R/op':>6} {'ops':>5} │ cofre  desenv │ veredito")
            base = None
            for name in [None] + list(REGIME):
                r = simulate(c, "sma", 2.0, False, spread_def, 0, len(c), regime=name, rs=rs)
                by = {y: sum(x[1] for x in r if _t.gmtime(x[0]).tm_year == y) for y in yrs}
                rr = [x for x in r if _t.gmtime(x[0]).tm_year >= yrs[0]]
                tot, pos, worst = sum(by.values()), sum(v > 0 for v in by.values()), min(by.values())
                cof = sum(x[1] for x in rr if x[0] < CUTOFF)
                dev = sum(x[1] for x in rr if x[0] >= CUTOFF)
                row = {"tot": tot, "pos": pos, "worst": worst, "cof": cof, "dev": dev}
                if name is None:
                    base, ver = row, "referência"
                else:
                    ok = (tot > base["tot"] and pos >= base["pos"] and worst >= base["worst"]
                          and cof > base["cof"] and dev > base["dev"])
                    ver = "✔ ADOTAR" if ok else "✗"
                print(f"  {(name or 'sem filtro'):18} " + "".join(f"{by[y]:+6.0f}" for y in yrs) +
                      f" │ {tot:+6.0f} {pos:3}/{len(yrs):<2} {worst:+5.0f} {tot / max(len(rr), 1):+6.3f} {len(rr):5} │ {cof:+5.0f} {dev:+6.0f} │ {ver}",
                      flush=True)
    print("\n7 filtros testados: uma vitória pequena pode ser sorte. Só adoto o que vencer em TUDO (total, anos, pior ano, cofre e desenvolvimento).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
