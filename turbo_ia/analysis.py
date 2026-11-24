"""Análise do gráfico: cada indicador vota COMPRA (+1), VENDA (−1) ou neutro (0); a confluência vira sinal.

Modos (como no painel):
  VELOZ    — mais negociações, menos precisão: basta a maioria simples e confluência ≥ 55 %
  EQUILIBRADO — confluência ≥ 65 % e SuperTrend a favor
  PRECISO  — confluência ≥ 75 %, SuperTrend + estrutura (CHoCH/LSMA) a favor e RSI fora de exaustão

Saída: direção, força (0..100), votos por indicador, motivos, SL/TP sugeridos (ATR / linha do SuperTrend) e as séries
para desenhar no gráfico. Nada aqui garante lucro: é um filtro técnico de confluência, avalie em conta DEMO.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

from . import indicators as ind

MODES = {
    "veloz": {"min_conf": 0.55, "need_st": False, "need_struct": False, "rsi_guard": False},
    "equilibrado": {"min_conf": 0.65, "need_st": True, "need_struct": False, "rsi_guard": True},
    "preciso": {"min_conf": 0.75, "need_st": True, "need_struct": True, "rsi_guard": True},
}

WEIGHTS = {"supertrend": 2.0, "lsma": 1.0, "keltner": 1.0, "sar": 1.0, "choch": 1.5, "ema_cross": 1.0, "rsi": 0.5, "momentum": 1.0}


@dataclass
class Analysis:
    signal: str                      # "BUY" | "SELL" | "NONE"
    strength: float                  # 0..100 (confluência ponderada a favor do lado escolhido)
    price: float
    atr: float
    sl: Optional[float]
    tp: Optional[float]
    votes: dict = field(default_factory=dict)
    reasons: list = field(default_factory=list)
    blocked: list = field(default_factory=list)
    mode: str = "veloz"
    label: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _last(s):
    for v in reversed(s):
        if v is not None:
            return v
    return None


def series(candles: list[dict]) -> dict:
    """Séries para o gráfico (mesmos parâmetros do painel)."""
    closes = [float(c["close"]) for c in candles]
    st_line, st_dir = ind.supertrend(candles, 10, 3.0)
    up, mid, lo = ind.keltner(candles, 20, 1.0)
    sar, sar_dir = ind.parabolic_sar(candles, 0.02, 0.02, 0.2)
    piv = ind.zigzag_pivots(candles, 5)
    return {
        "lsma": ind.lsma(closes, 25), "supertrend": st_line, "supertrend_dir": st_dir,
        "kc_up": up, "kc_mid": mid, "kc_lo": lo, "sar": sar, "sar_dir": sar_dir,
        "pivots": piv, "choch": ind.choch(candles, piv),
    }


def analyze(candles: list[dict], mode: str = "veloz", rr: float = 1.5, sl_atr: float = 1.5) -> Analysis:
    mode = mode if mode in MODES else "veloz"
    cfg = MODES[mode]
    if len(candles) < 60:
        p = float(candles[-1]["close"]) if candles else 0.0
        return Analysis("NONE", 0.0, p, 0.0, None, None, blocked=["poucos candles (mínimo 60)"], mode=mode, label="Dados insuficientes")
    closes = [float(c["close"]) for c in candles]
    price = closes[-1]
    s = series(candles)
    a = _last(ind.atr(candles, 14)) or 0.0
    votes: dict[str, int] = {}
    why: list[tuple[str, str]] = []          # (indicador, motivo) — no fim ficam só os que apoiam o lado escolhido

    st_dir = s["supertrend_dir"][-1]
    votes["supertrend"] = st_dir
    why.append(("supertrend", f"SuperTrend {'alta' if st_dir > 0 else 'baixa'}"))

    l_now, l_prev = s["lsma"][-1], s["lsma"][-4]
    votes["lsma"] = (1 if price > l_now and l_now > l_prev else -1 if price < l_now and l_now < l_prev else 0) if l_now and l_prev else 0
    if votes["lsma"]:
        why.append(("lsma", f"preço {'acima' if votes['lsma'] > 0 else 'abaixo'} da LSMA 25 inclinada"))

    up, mid, lo = s["kc_up"][-1], s["kc_mid"][-1], s["kc_lo"][-1]
    if mid is not None:
        if price > up:
            votes["keltner"] = 1
            why.append(("keltner", "rompimento da banda superior de Keltner"))
        elif price < lo:
            votes["keltner"] = -1
            why.append(("keltner", "rompimento da banda inferior de Keltner"))
        else:
            votes["keltner"] = 1 if price > mid else -1 if price < mid else 0
    else:
        votes["keltner"] = 0

    votes["sar"] = s["sar_dir"][-1]

    ch = [e for e in s["choch"] if e["i"] >= len(candles) - 30]
    votes["choch"] = ch[-1]["dir"] if ch else 0
    if ch:
        why.append(("choch", f"CHoCH {'↑' if ch[-1]['dir'] > 0 else '↓'} há {len(candles) - 1 - ch[-1]['i']} candles"))

    e9, e21 = ind.ema(closes, 9), ind.ema(closes, 21)
    votes["ema_cross"] = 1 if e9[-1] > e21[-1] else -1 if e9[-1] < e21[-1] else 0

    r = ind.rsi(closes, 14)[-1] or 50.0
    votes["rsi"] = 1 if 52 <= r <= 70 else -1 if 30 <= r <= 48 else 0

    mom = closes[-1] - closes[-6]
    votes["momentum"] = 1 if mom > 0.2 * a else -1 if mom < -0.2 * a else 0

    total = sum(WEIGHTS.values())
    bull = sum(WEIGHTS[k] for k, v in votes.items() if v > 0) / total
    bear = sum(WEIGHTS[k] for k, v in votes.items() if v < 0) / total
    side = 1 if bull > bear else -1 if bear > bull else 0
    conf = max(bull, bear)
    blocked: list[str] = []
    reasons = [txt for k, txt in why if votes.get(k) == side]
    if side == 0:
        blocked.append("indicadores divididos")
    if conf < cfg["min_conf"]:
        blocked.append(f"confluência {conf:.0%} < {cfg['min_conf']:.0%} exigida no modo {mode}")
    if cfg["need_st"] and votes["supertrend"] != side:
        blocked.append("SuperTrend contra o sinal")
    if cfg["need_struct"] and votes["choch"] != side and votes["lsma"] != side:
        blocked.append("sem estrutura a favor (CHoCH/LSMA)")
    if cfg["rsi_guard"] and ((side > 0 and r > 75) or (side < 0 and r < 25)):
        blocked.append(f"RSI {r:.0f} em exaustão")
    if a <= 0:
        blocked.append("ATR zero (mercado parado)")

    signal = "NONE" if blocked else ("BUY" if side > 0 else "SELL")
    sl = tp = None
    if signal != "NONE":
        st_line = s["supertrend"][-1]
        dist = sl_atr * a
        if st_line is not None and 0.5 * a <= abs(price - st_line) <= 3 * a:
            dist = abs(price - st_line) + 0.1 * a          # stop logo além do SuperTrend quando ele está a uma distância sensata
        sl = price - dist if side > 0 else price + dist
        tp = price + rr * dist if side > 0 else price - rr * dist
    label = ("Sinal forte" if conf >= 0.75 else "Sinal moderado" if conf >= 0.6 else "Sinal fraco")
    return Analysis(signal, round(conf * 100, 1), price, a, sl, tp, votes, reasons, blocked, mode,
                    f"{'Alta' if side > 0 else 'Baixa' if side < 0 else 'Neutro'} · {label}")
