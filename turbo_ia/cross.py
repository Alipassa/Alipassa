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


def simulate(c: list[dict], kind: str = "ema", stop_atr: Optional[float] = None, atr_filter: bool = True,
             spread_def: float = 0.3, lo: int = 0, hi: Optional[int] = None) -> list[tuple[int, float, float]]:
    """Devolve [(tempo da entrada, R, pontos)] — uma posição por vez."""
    hi = hi or len(c)
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
    a = ap.parse_args(argv)
    from .settings import load_env
    load_env()
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
        mid = cut + (n - cut) // 2
        days = lambda lo, hi: max((c[hi - 1]["time"] - c[lo]["time"]) / 86400, 1)  # noqa: E731
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


if __name__ == "__main__":
    raise SystemExit(main())
