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


# ---------------------------------------------------------------------------------------------------- EXPERIMENTAL
# Reversão à média no OURO, gráfico M2, expiração 10 min, só às 16h e 20h UTC.
# Origem: busca em 60 dias de GC=F (Yahoo, nov/2026): nesses dois horários a regra acertou > 55 % nas DUAS metades dos dados,
# 61,2 % em 209 operações (IC95 54,5–67,6 %). Foi escolhida entre 24 horários — o acerto real pode ser menor. Só vale depois
# de confirmada em DEMO (registro de acerto do painel): precisa ficar acima de 56,2 % (payout 78 %) com centenas de operações.
REVERSION_HOURS = (16, 20)
REVERSION_Z = 1.0


def analyze_reversion(candles: list[dict], hours=REVERSION_HOURS, z_thr: float = REVERSION_Z, sl_atr: float = 3.0,
                      utc_offset: float = 0.0) -> Analysis:
    """Preço esticado > z_thr ATR da média de 20 → aposta na volta. `utc_offset` = fuso do carimbo dos candles (MT5: servidor)."""
    mode = "reversao"
    if len(candles) < 40:
        p = float(candles[-1]["close"]) if candles else 0.0
        return Analysis("NONE", 0.0, p, 0.0, None, None, blocked=["poucos candles (mínimo 40)"], mode=mode, label="Dados insuficientes")
    closes = [float(c["close"]) for c in candles]
    price = closes[-1]
    a = _last(ind.atr(candles, 14)) or 0.0
    m = ind.sma(closes, 20)[-1]
    z = (price - m) / a if a else 0.0
    hour = int(((int(candles[-1]["time"]) - utc_offset * 3600) // 3600) % 24)
    side = -1 if z > 0 else 1
    votes = {"reversao_z": side if abs(z) > z_thr else 0}
    blocked = []
    if hours and hour not in hours:
        blocked.append(f"fora do horário ({hour:02d}h UTC; opera só {', '.join(f'{h:02d}h' for h in hours)} UTC)")
    if abs(z) <= z_thr:
        blocked.append(f"preço só {z:+.2f} ATR da média (precisa > {z_thr})")
    if a <= 0:
        blocked.append("ATR zero")
    signal = "NONE" if blocked else ("BUY" if side > 0 else "SELL")
    sl = (price - sl_atr * a if side > 0 else price + sl_atr * a) if signal != "NONE" else None
    reasons = [f"preço {z:+.2f} ATR {'acima' if z > 0 else 'abaixo'} da média 20 → reversão", f"{hour:02d}h UTC"]
    return Analysis(signal, round(min(abs(z) / 3, 1) * 100, 1), price, a, sl, None, votes, reasons, blocked, mode,
                    f"{'Alta' if side > 0 else 'Baixa'} · Reversão {'armada' if signal != 'NONE' else 'aguardando'}")


def analyze(candles: list[dict], mode: str = "veloz", rr: float = 1.5, sl_atr: float = 1.5, utc_offset: float = 0.0) -> Analysis:
    if mode == "reversao":
        return analyze_reversion(candles, sl_atr=max(sl_atr, 3.0), utc_offset=utc_offset)
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
        tp = (price + rr * dist if side > 0 else price - rr * dist) if rr > 0 else None
    label = ("Sinal forte" if conf >= 0.75 else "Sinal moderado" if conf >= 0.6 else "Sinal fraco")
    return Analysis(signal, round(conf * 100, 1), price, a, sl, tp, votes, reasons, blocked, mode,
                    f"{'Alta' if side > 0 else 'Baixa' if side < 0 else 'Neutro'} · {label}")
