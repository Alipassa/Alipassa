"""GOLD MARKET INTELLIGENCE — PAINEL (cockpit do ouro). Segunda camada do GOLD BIAS ENGINE.

A IA analisa por trás (MT5 + macro + notícias + técnico → GOLD BIAS) e o painel mostra os dados que sustentam o sinal,
para VOCÊ decidir se entra ou não. Apoio à decisão: o painel NUNCA envia ordens.

    python market_ai_engine_v6.py painel --source mt5 --send     # abre http://127.0.0.1:8765

Arquitetura:  MT5 / DataEngine → snapshot → GoldBiasEngine → DashState (este módulo) → HTTP local (JSON + página)
                                                           └→ BiasNotifier/Telegram · BiasMemory (previsão × resultado)

Blocos: preço · viés/confiança/score · macro (DXY, Treasury 2Y/10Y/30Y, juros reais, FED, petróleo, VIX) · notícias ·
geopolítica · China · fluxo · técnico · calendário · PAINEL DE CONFLUÊNCIA · SEMÁFORO · CÉREBRO DA IA · POSSO ENTRAR? ·
gráfico (candles, EMA 9/21/50/200, VWAP + bandas, suportes/resistências, Fibonacci, sinais da IA, entrada hipotética,
zona de risco, eventos) em M1…D1 · alertas · histórico "o que a IA disse × o que o ouro fez".
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional, Sequence
from urllib.parse import parse_qs, urlparse

from .bias import BIAS_FACTOR_LABELS, BiasMemory, BiasNotifier, BiasReading, GoldBiasEngine, bias_apply_manual, bias_structure
from .models import Candle, MarketSnapshot
from .technical import adx, atr, ema, macd, rsi, swing_levels

DASH_TFS: tuple[str, ...] = ("M1", "M5", "M15", "M30", "H1", "H4", "D1")
DASH_BARS = 160                      # candles visíveis no gráfico
DASH_CONTRACT_OZ = 100.0             # XAUUSD: 1 lote = 100 onças (ajuste com --contract)
DASH_GROUP_WEIGHTS: dict[str, float] = {"macro": 0.40, "fluxo": 0.15, "tecnico": 0.20, "noticias": 0.10, "estrutura": 0.15}
DASH_GROUP_LABELS: dict[str, str] = {"macro": "Macro", "fluxo": "Fluxo", "tecnico": "Técnico", "noticias": "Notícias", "estrutura": "Estrutura"}


def dash_t(x: float) -> float:
    return math.tanh(x)


def dash_state_of(v: Optional[float], thr: float = 20.0) -> int:
    """+1 favorável ao ouro · −1 desfavorável · 0 neutro/sem dado."""
    if v is None:
        return 0
    return 1 if v >= thr else -1 if v <= -thr else 0


def dash_icon(v: Optional[float], thr: float = 20.0) -> str:
    if v is None:
        return "⚪"
    return {1: "🟢", -1: "🔴", 0: "🟡"}[dash_state_of(v, thr)]


def dash_item(name: str, value: Optional[float], detail: str = "", directional: bool = True) -> dict:
    v = None if value is None else round(max(-100.0, min(100.0, value)), 0)
    return {"name": name, "value": v, "detail": detail, "icon": dash_icon(v) if directional else ("🟢" if (v or 0) >= 25 else "🟡"),
            "state": dash_state_of(v) if directional else 0, "directional": directional}


def dash_mean(items: Sequence[dict]) -> Optional[float]:
    vals = [i["value"] for i in items if i["value"] is not None and i.get("directional", True)]
    return round(sum(vals) / len(vals), 0) if vals else None


# --------------------------------------------------------------------------- técnico por timeframe
def dash_tf_indicators(candles: Sequence[Candle]) -> dict:
    """RSI, MACD, ADX, EMAs, VWAP da janela, ATR — valores brutos + leitura −100..+100 (sentido do ouro)."""
    closes = [c.close for c in candles]
    if len(closes) < 30:
        return {}
    e9, e21, e50 = ema(closes, 9)[-1], ema(closes, 21)[-1], ema(closes, 50)[-1]
    e200 = ema(closes, 200)[-1] if len(closes) >= 200 else None
    r = rsi(closes)
    _, _, hist = macd(closes)
    a = adx(candles)
    at = atr(candles) or 0.0
    vw = dash_vwap_series(candles, "H1")[-1]
    close = closes[-1]
    out = {"close": close, "ema9": e9, "ema21": e21, "ema50": e50, "ema200": e200, "rsi": r, "macd_hist": hist[-1] if hist else None,
           "adx": a, "atr": at, "vwap": vw["vwap"]}
    out["ema_read"] = (40 if e9 > e21 else -40) + (30 if close > e50 else -30) + ((30 if close > e200 else -30) if e200 is not None else 0)
    out["rsi_read"] = None if r is None else max(-100.0, min(100.0, (r - 50) * 4))
    out["macd_read"] = None if not hist or not at else 100 * dash_t(hist[-1] / (0.5 * at))
    out["vwap_read"] = None if not vw["vwap"] or not at else 100 * dash_t((close - vw["vwap"]) / at)
    out["adx_read"] = a
    return out


def dash_vwap_series(candles: Sequence[Candle], tf: str, session_hour: int = 22) -> list[dict]:
    """VWAP ancorado: sessão (≤ H1, reinicia às session_hour UTC), semana (H4) ou mês (D1); bandas de ±1σ e ±2σ."""
    out: list[dict] = []
    pv = vol = pv2 = 0.0
    anchor = None
    for c in candles:
        if tf in ("H4",):
            key = (c.time - timedelta(days=c.time.weekday())).date()
        elif tf in ("D1", "W1"):
            key = (c.time.year, c.time.month)
        else:
            key = (c.time - timedelta(hours=session_hour)).date()
        if key != anchor:
            anchor, pv, vol, pv2 = key, 0.0, 0.0, 0.0
        tp = (c.high + c.low + c.close) / 3
        v = c.volume or 1.0
        pv += tp * v
        pv2 += tp * tp * v
        vol += v
        mean = pv / vol
        sd = math.sqrt(max(0.0, pv2 / vol - mean * mean))
        out.append({"vwap": mean, "sd": sd})
    return out


def dash_pivots(candles: Sequence[Candle], k: int = 3) -> tuple[list[float], list[float]]:
    highs, lows = [], []
    for i in range(k, len(candles) - k):
        win = candles[i - k:i + k + 1]
        if candles[i].high == max(c.high for c in win):
            highs.append(candles[i].high)
        if candles[i].low == min(c.low for c in win):
            lows.append(candles[i].low)
    return highs, lows


def dash_levels(candles: Sequence[Candle], price: float, atr_: float) -> dict:
    """Suportes (abaixo do preço) e resistências (acima), agrupando pivôs a menos de 0,3 ATR."""
    highs, lows = dash_pivots(candles)
    tol = max(atr_ * 0.3, price * 0.0002)

    def cluster(vals: list[float]) -> list[float]:
        out: list[float] = []
        for v in sorted(vals):
            if out and abs(v - out[-1]) <= tol:
                out[-1] = (out[-1] + v) / 2
            else:
                out.append(v)
        return out

    levels = cluster(highs + lows)
    res = sorted([v for v in levels if v > price + tol * 0.2])[:3]
    sup = sorted([v for v in levels if v < price - tol * 0.2], reverse=True)[:3]
    s1, r1 = swing_levels(candles)
    if not sup and s1 is not None and s1 < price:
        sup = [s1]
    if not res and r1 is not None and r1 > price:
        res = [r1]
    return {"supports": [round(v, 2) for v in sup], "resistances": [round(v, 2) for v in res]}


def dash_fibonacci(candles: Sequence[Candle]) -> Optional[dict]:
    """Retração da maior perna da janela: do extremo mais antigo ao mais recente."""
    if len(candles) < 10:
        return None
    hi_i = max(range(len(candles)), key=lambda i: candles[i].high)
    lo_i = min(range(len(candles)), key=lambda i: candles[i].low)
    hi, lo = candles[hi_i].high, candles[lo_i].low
    if hi <= lo:
        return None
    up = lo_i < hi_i                                   # perna de alta: fundo antes do topo → retrações a partir do topo
    levels = []
    for f in (0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0):
        levels.append({"ratio": f, "price": round(hi - (hi - lo) * f if up else lo + (hi - lo) * f, 2)})
    return {"direction": "alta" if up else "baixa", "high": hi, "low": lo, "levels": levels}


# --------------------------------------------------------------------------- estado do painel
class DashState:
    """Transforma (snapshot, leitura do GOLD BIAS) no JSON do painel. Guarda séries por timeframe para o gráfico."""

    def __init__(self, memory: Optional[BiasMemory] = None, contract_oz: float = DASH_CONTRACT_OZ, local_tz_hours: float = -3.0) -> None:
        self.memory = memory
        self.contract_oz = contract_oz
        self.local_tz_hours = local_tz_hours
        self.snapshot: Optional[MarketSnapshot] = None
        self.reading: Optional[BiasReading] = None
        self.payload: dict = {}
        self.alerts: list[dict] = []
        self.prev_light: Optional[str] = None
        self.prev_score: Optional[float] = None
        self.score_before: Optional[float] = None     # score da leitura anterior (para "IA mudou de X → Y")
        self.status: dict[str, str] = {}
        self.updated: Optional[datetime] = None

    # ---- blocos
    def price_block(self, s: MarketSnapshot) -> dict:
        d1 = s.candles.get("D1") or []
        h1 = s.candles.get("H1") or []
        prev_close = d1[-2].close if len(d1) >= 2 else (h1[-25].close if len(h1) >= 25 else None)
        day = d1[-1] if d1 else None
        hi = day.high if day else (max(c.high for c in h1[-24:]) if h1 else None)
        lo = day.low if day else (min(c.low for c in h1[-24:]) if h1 else None)
        if hi is not None:
            hi, lo = max(hi, s.price), min(lo, s.price)
        return {"price": round(s.price, 2), "change_pct": round((s.price / prev_close - 1) * 100, 2) if prev_close else None,
                "change_abs": round(s.price - prev_close, 2) if prev_close else None, "high": hi, "low": lo,
                "window_change_pct": round(s.price_change_pct, 2), "source": s.price_source or ("mt5" if self.status.get("mt5") == "ok" else "web/sample")}

    def macro_block(self, s: MarketSnapshot, r: BiasReading) -> list[dict]:
        f = r.factor
        items = [
            dash_item("DXY", f("dolar").ratio * 100 if f("dolar").available else None,
                      (f"{s.dxy:.2f} " if s.dxy else "") + (f"({s.dxy_change_pct:+.2f}%)" if s.dxy_change_pct is not None else "")),
            dash_item("Treasury 10Y", -100 * dash_t(s.us10y_change_bp / 8) if s.us10y_change_bp is not None else None,
                      (f"{s.us10y:.2f}% " if s.us10y else "") + (f"({s.us10y_change_bp:+.1f} bp)" if s.us10y_change_bp is not None else "")),
            dash_item("Treasury 2Y", -100 * dash_t(s.us2y_change_bp / 8) if s.us2y_change_bp is not None else None,
                      (f"{s.us2y:.2f}% " if s.us2y else "") + (f"({s.us2y_change_bp:+.1f} bp)" if s.us2y_change_bp is not None else "")),
            dash_item("Treasury 30Y", -100 * dash_t(s.us30y_change_bp / 8) if s.us30y_change_bp is not None else None,
                      (f"{s.us30y:.2f}% " if s.us30y else "") + (f"({s.us30y_change_bp:+.1f} bp)" if s.us30y_change_bp is not None else "")),
            dash_item("Juros reais", f("juros_reais").ratio * 100 if f("juros_reais").available else None,
                      f"{s.real_yield_change_bp:+.1f} bp" if s.real_yield_change_bp is not None else ""),
            dash_item("FED", f("fed").ratio * 100 if f("fed").available else None, r.fed_stance.title() + (f" · {r.fed_shift}" if r.fed_shift else "")),
            dash_item("Inflação", f("inflacao").ratio * 100 if f("inflacao").available else None, ""),
            dash_item("Emprego", f("emprego").ratio * 100 if f("emprego").available else None, ""),
        ]
        return items

    @staticmethod
    def context_block(s: MarketSnapshot, r: BiasReading) -> dict:
        oil_v = None
        if s.oil_change_pct is not None or s.brent_change_pct is not None:
            ch = [x for x in (s.oil_change_pct, s.brent_change_pct) if x is not None]
            oil_v = -100 * dash_t(sum(ch) / len(ch) / 3)          # petróleo subindo → inflação → FED duro → leve pressão
        vix_v = None
        if s.vix is not None:
            vix_v = 100 * dash_t(((s.vix - 18) / 10) + (s.vix_change_pct or 0) / 20)
        vix_mode = "—" if s.vix is None else ("calmo" if s.vix < 15 else "normal" if s.vix < 20 else "tenso" if s.vix < 28 else "estresse")
        return {
            "oil": dash_item("Petróleo", oil_v, " · ".join(x for x in (
                f"WTI {s.wti:.2f}" if s.wti else "", f"({s.oil_change_pct:+.2f}%)" if s.oil_change_pct is not None else "",
                f"Brent {s.brent:.2f}" if s.brent else "", f"({s.brent_change_pct:+.2f}%)" if s.brent_change_pct is not None else "") if x)),
            "vix": dash_item("VIX", vix_v, (f"{s.vix:.1f} · {vix_mode}" if s.vix is not None else "") + (f" · {r.risk_mode}" if r.risk_mode != "INDISPONÍVEL" else "")),
            "geo": {"level": r.geo_level, "icon": r.geo_emoji, "value": s.geopolitical_risk, "change": s.geopolitical_risk_change},
            "economy": r.economy, "risk_mode": r.risk_mode,
        }

    def flow_block(self, s: MarketSnapshot, r: BiasReading) -> list[dict]:
        ci = r.factor("china_india")
        return [
            dash_item("ETF GOLD", 100 * dash_t(s.etf_flow_musd / 300) if s.etf_flow_musd is not None else None,
                      f"{s.etf_flow_musd:+.0f} M USD" if s.etf_flow_musd is not None else ""),
            dash_item("China", 100 * s.china_demand if s.china_demand is not None else (ci.ratio * 100 if ci and ci.available else None),
                      "demanda física" + (f" {s.china_demand:+.2f}" if s.china_demand is not None else "")),
            dash_item("Índia", 100 * s.india_demand if s.india_demand is not None else None, "importações/joias"),
            dash_item("Bancos centrais", 100 * dash_t(s.central_bank_buying_tonnes / 30) if s.central_bank_buying_tonnes is not None else None,
                      f"{s.central_bank_buying_tonnes:+.0f} t" if s.central_bank_buying_tonnes is not None else ""),
            dash_item("COT (especuladores)", 100 * dash_t(s.cot_managed_money_net_change / 15000) if s.cot_managed_money_net_change is not None else None,
                      (f"{s.cot_managed_money_net_change:+.0f} contratos" if s.cot_managed_money_net_change is not None else "")
                      + (f" · p{s.cot_managed_money_percentile:.0f}" if s.cot_managed_money_percentile is not None else "")),
            dash_item("Agressão (fluxo)", 100 * s.order_flow_imbalance if s.order_flow_imbalance is not None else None, "volume comprador − vendedor"),
        ]

    def technical_block(self, s: MarketSnapshot, r: BiasReading) -> dict:
        per_tf = {}
        for tf in ("M15", "H1", "H4", "D1"):
            cs = s.candles.get(tf) or []
            ind = dash_tf_indicators(cs)
            if ind:
                per_tf[tf] = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in ind.items()}
                per_tf[tf]["structure"] = bias_structure(cs)
        ref = per_tf.get("H1") or per_tf.get("M15") or next(iter(per_tf.values()), {})
        adx_v = ref.get("adx")
        items = [
            dash_item("EMA 9/21", ref.get("ema_read"), "9 acima da 21" if ref.get("ema9", 0) > ref.get("ema21", 0) else "9 abaixo da 21" if ref else ""),
            dash_item("RSI", ref.get("rsi_read"), f"{ref['rsi']:.0f}" if ref.get("rsi") is not None else ""),
            dash_item("MACD", ref.get("macd_read"), f"hist {ref['macd_hist']:+.2f}" if ref.get("macd_hist") is not None else ""),
            dash_item("VWAP", ref.get("vwap_read"), ("acima" if ref.get("vwap_read", 0) > 0 else "abaixo") if ref.get("vwap_read") is not None else ""),
            dash_item("ADX", None if adx_v is None else min(100.0, adx_v * 2.5),
                      "" if adx_v is None else f"{adx_v:.0f} · {'tendência' if adx_v >= 25 else 'tendência fraca' if adx_v >= 18 else 'sem tendência'}", directional=False),
        ]
        return {"items": items, "per_tf": per_tf, "vwap": r.vwap, "ema_cross": r.ema_cross, "structure": r.structure}

    @staticmethod
    def news_block(r: BiasReading) -> dict:
        items = [{"headline": n.headline, "source": n.source, "time": n.time.isoformat(), "impact": n.impact, "factor": BIAS_FACTOR_LABELS.get(n.factor, "—"),
                  "credibility": n.credibility, "recency": n.recency, "icon": "🟢" if n.impact > 0 else "🔴" if n.impact < 0 else "🟡"} for n in r.news[:12]]
        # resumo por tema (como no painel de confluência: "🔴 Fed hawkish · 🔴 Dólar forte · 🟡 Geopolítica · 🟢 Demanda física")
        f = r.factor
        themes = []
        for key, pos, neg, neu in (("fed", "Fed dovish", "Fed hawkish", "Fed neutro"), ("dolar", "Dólar fraco", "Dólar forte", "Dólar estável"),
                                   ("geopolitica", "Geopolítica em alta", "Geopolítica aliviando", "Geopolítica estável"),
                                   ("china_india", "Demanda física forte", "Demanda física fraca", "Demanda física estável")):
            fs = f(key)
            if fs is None or not fs.available:
                continue
            v = fs.ratio * 100
            themes.append({"label": pos if v >= 20 else neg if v <= -20 else neu, "icon": dash_icon(v), "value": round(v)})
        return {"items": items, "themes": themes}

    def calendar_block(self, s: MarketSnapshot) -> list[dict]:
        out = []
        for e in sorted(s.events, key=lambda x: x.time):
            mins = (e.time - s.time).total_seconds() / 60
            if mins < -12 * 60 or mins > 7 * 24 * 60:
                continue
            out.append({"name": e.name, "time": e.time.isoformat(), "minutes": round(mins), "impact": e.impact, "consensus": e.consensus,
                        "previous": e.previous, "actual": e.actual, "unit": e.unit, "kind": e.kind, "done": e.actual is not None or mins < 0})
        return out[:15]

    # ---- confluência, semáforo, cérebro, entrada
    def groups(self, macro: list[dict], flow: list[dict], tech: dict, news: dict, s: MarketSnapshot, r: BiasReading) -> dict:
        struct_vals = []
        for tf, w in (("H1", 0.3), ("H4", 0.4), ("D1", 0.3)):
            st = r.structure.get(tf, "")
            v = 100 if ("ALTA" in st or "rompimento de alta" in st) else -100 if ("BAIXA" in st or "rompimento de baixa" in st) else 0 if st else None
            if v is not None:
                struct_vals.append((v, w))
        estrutura = round(sum(v * w for v, w in struct_vals) / sum(w for _, w in struct_vals)) if struct_vals else None
        news_vals = [n["impact"] * n["credibility"] * n["recency"] for n in news["items"] if n["recency"] > 0.2]
        noticias = round(100 * dash_t(sum(news_vals) / 2)) if news_vals else None
        return {"macro": dash_mean(macro), "fluxo": dash_mean(flow), "tecnico": dash_mean(tech["items"]), "noticias": noticias, "estrutura": estrutura}

    @staticmethod
    def confluence(groups: dict, score: float) -> float:
        """% do peso (× intensidade) dos blocos com leitura que aponta na direção do viés."""
        d = 1 if score > 0 else -1 if score < 0 else 0
        if d == 0:
            best = max(((abs(v), v) for v in groups.values() if v is not None), default=(0, 0))
            d = 1 if best[1] > 0 else -1 if best[1] < 0 else 0
        num = den = 0.0
        for k, v in groups.items():
            if v is None or abs(v) < 10:
                continue
            w = DASH_GROUP_WEIGHTS[k] * min(1.0, abs(v) / 60)
            den += w
            if v * d > 0:
                num += w
        return round(100 * num / den) if den else 0.0

    @staticmethod
    def traffic_light(groups: dict, score: float, event_minutes: Optional[float]) -> dict:
        """🟢 COMPRA · 🔴 VENDA · 🟡 AGUARDAR. Compra exige macro, técnico e estrutura positivos, fluxo e notícias sem contrariar e
        score ≥ +40 (a venda é o espelho). Divergência ou evento de alto impacto em ≤ 30 min → AGUARDAR."""
        st = {k: dash_state_of(v) for k, v in groups.items()}
        rows = [{"group": DASH_GROUP_LABELS[k], "icon": dash_icon(groups[k]), "value": groups[k]} for k in DASH_GROUP_LABELS]
        missing = [DASH_GROUP_LABELS[k] for k, v in groups.items() if v is None]

        def side(d: int) -> bool:
            return st["macro"] == d and st["tecnico"] == d and st["estrutura"] == d and st["fluxo"] != -d and st["noticias"] != -d and score * d >= 40

        pos = [DASH_GROUP_LABELS[k] for k, v in st.items() if v > 0]
        neg = [DASH_GROUP_LABELS[k] for k, v in st.items() if v < 0]
        if event_minutes is not None and 0 <= event_minutes <= 30:
            return {"state": "AGUARDAR", "icon": "🟡", "title": "AGUARDAR — EVENTO DE ALTO IMPACTO",
                    "reason": f"dado importante em {event_minutes:.0f} min: esperar a reação do dólar e dos juros", "rows": rows, "missing": missing}
        if side(1):
            return {"state": "COMPRA", "icon": "🟢", "title": "CENÁRIO FAVORÁVEL À COMPRA", "reason": "macro, técnico e estrutura alinhados para alta; fluxo e notícias não contrariam",
                    "rows": rows, "missing": missing}
        if side(-1):
            return {"state": "VENDA", "icon": "🔴", "title": "CENÁRIO FAVORÁVEL À VENDA", "reason": "macro, técnico e estrutura alinhados para baixa; fluxo e notícias não contrariam",
                    "rows": rows, "missing": missing}
        if pos and neg:
            reason = f"divergência: {', '.join(pos)} 🟢 × {', '.join(neg)} 🔴"
        elif abs(score) < 40:
            reason = f"viés sem força (score {score:+.0f}; precisa de ±40)"
        else:
            reason = "falta confirmação de " + ", ".join(DASH_GROUP_LABELS[k] for k in ("macro", "tecnico", "estrutura") if st[k] != (1 if score > 0 else -1))
        return {"state": "AGUARDAR", "icon": "🟡", "title": "AGUARDAR CONFIRMAÇÃO", "reason": reason, "rows": rows, "missing": missing}

    @staticmethod
    def brain(s: MarketSnapshot, r: BiasReading, tech: dict, groups: dict) -> dict:
        """POR QUE A IA ESTÁ PENSANDO ISSO? Fatos em linguagem direta, fator dominante, fator contrário e conclusão."""
        facts: list[str] = []
        if s.dxy_change_pct is not None and abs(s.dxy_change_pct) >= 0.05:
            facts.append("DXY ganhou força." if s.dxy_change_pct > 0 else "DXY perdeu força.")
        if s.us10y_change_bp is not None and abs(s.us10y_change_bp) >= 1:
            facts.append(f"Treasury 10Y {'subiu' if s.us10y_change_bp > 0 else 'caiu'} {abs(s.us10y_change_bp):.1f} bp.")
        if s.real_yield_change_bp is not None and abs(s.real_yield_change_bp) >= 1:
            facts.append(f"Juros reais {'subiram' if s.real_yield_change_bp > 0 else 'caíram'}.")
        if r.fed_stance in ("HAWKISH", "DOVISH"):
            facts.append("Expectativa de política monetária mais restritiva aumentou." if r.fed_stance == "HAWKISH"
                         else "Expectativa de cortes de juros aumentou.")
        if r.fed_shift:
            facts.append(f"Discurso do FED mudou: {r.fed_shift}.")
        ref = tech["per_tf"].get("H1") or {}
        lv = r.levels
        if ref and lv.get("suporte") and s.price < lv["suporte"]:
            facts.append("XAU/USD perdeu suporte técnico.")
        if ref and lv.get("resistencia") and s.price > lv["resistencia"]:
            facts.append("XAU/USD rompeu resistência.")
        for tf, st in r.structure.items():
            if "rompimento" in st:
                facts.append(f"{tf}: {st}.")
        if r.ema_cross.startswith("cruzamento"):
            facts.append("EMA 9 cruzou " + ("acima" if "alta" in r.ema_cross else "abaixo") + " da EMA 21.")
        if ref.get("rsi") is not None and (ref["rsi"] >= 70 or ref["rsi"] <= 30):
            facts.append(f"RSI em {ref['rsi']:.0f} ({'sobrecomprado' if ref['rsi'] >= 70 else 'sobrevendido'}).")
        if s.etf_flow_musd is not None and abs(s.etf_flow_musd) >= 50:
            facts.append(f"ETFs de ouro com {'entradas' if s.etf_flow_musd > 0 else 'saídas'} de {abs(s.etf_flow_musd):.0f} M USD.")
        if r.geo_level in ("ALTO", "EXTREMO"):
            facts.append(f"Risco geopolítico {r.geo_level.lower()}.")
        top_news = [n for n in r.news if abs(n.impact) >= 2][:2]
        facts += [f"Notícia: {n.headline[:90]}" for n in top_news]
        avail = [f for f in r.factors if f.available]
        d = r.direction or (1 if r.score > 0 else -1 if r.score < 0 else 0)
        dom = max(avail, key=lambda f: abs(f.score), default=None)
        contra = max((f for f in avail if d and f.score * d < 0), key=lambda f: abs(f.score), default=None)
        word = {1: "altista", -1: "baixista", 0: "lateral/incerto"}[r.direction]
        conclusion = f"Cenário {word}"
        rsi_h1 = ref.get("rsi")
        if r.direction < 0 and ((rsi_h1 is not None and rsi_h1 <= 35) or (contra is not None and abs(contra.score) >= 0.4 * contra.max_score)):
            conclusion += ", mas com risco de repique"
        elif r.direction > 0 and ((rsi_h1 is not None and rsi_h1 >= 65) or (contra is not None and abs(contra.score) >= 0.4 * contra.max_score)):
            conclusion += ", mas com risco de realização"
        elif r.contradictions:
            conclusion += ", com divergências — confirmar antes de agir"
        return {"bias": r.label, "emoji": r.emoji, "facts": facts[:10] or ["Nenhuma variação relevante no ciclo — mercado sem gatilho."],
                "dominant": None if dom is None else f"{BIAS_FACTOR_LABELS[dom.name]} — {dom.rationale}",
                "contrary": None if contra is None else f"{BIAS_FACTOR_LABELS[contra.name]} — {contra.rationale}",
                "conclusion": conclusion + ".", "opinion": r.opinion, "contradictions": r.contradictions}

    def entry(self, s: MarketSnapshot, r: BiasReading, groups: dict, confluence: float, light: dict, risk_usd: Optional[float] = None) -> dict:
        """POSSO ENTRAR? — checklist, status, entrada/stop/alvo hipotéticos e lote para o risco informado. Não envia ordem."""
        d = 1 if r.score > 0 else -1 if r.score < 0 else 0
        trend_vals = []
        for tf in ("H4", "D1"):
            ind = dash_tf_indicators(s.candles.get(tf) or [])
            if ind:
                trend_vals.append(ind["ema_read"])
        trend = sum(trend_vals) / len(trend_vals) if trend_vals else None
        h1 = s.candles.get("H1") or []
        atr_now = atr(h1) or s.atr or 0.0
        atr_ref = None
        if len(h1) > 80:
            trs = [atr(h1[:i]) for i in range(len(h1) - 60, len(h1), 10)]
            trs = [x for x in trs if x]
            atr_ref = sum(trs) / len(trs) if trs else None
        vol_ratio = atr_now / atr_ref if atr_ref else None
        if vol_ratio is None:
            vol_icon, vol_txt = "⚪", "sem histórico"
        elif vol_ratio > 2.2:
            vol_icon, vol_txt = "🔴", f"extrema ({vol_ratio:.1f}× o normal)"
        elif vol_ratio > 1.5 or vol_ratio < 0.6:
            vol_icon, vol_txt = "🟡", f"{'elevada' if vol_ratio > 1 else 'muito baixa'} ({vol_ratio:.1f}× o normal)"
        else:
            vol_icon, vol_txt = "🟢", f"normal ({vol_ratio:.1f}×)"

        def row(name: str, v: Optional[float], txt: str = "") -> dict:
            aligned = None if v is None or d == 0 else dash_state_of(v) * d
            icon = "⚪" if v is None else ("🟢" if aligned == 1 else "🔴" if aligned == -1 else "🟡") if d else dash_icon(v)
            side = "COMPRA" if d > 0 else "VENDA"
            if not txt and v is not None and d:
                txt = f"confirma a {side}" if aligned == 1 else f"contra a {side}" if aligned == -1 else "neutro"
            return {"name": name, "icon": icon, "value": v, "detail": txt}

        rows = [row("Tendência (H4/D1)", trend), row("Macro", groups["macro"]), row("Notícias", groups["noticias"]),
                row("Fluxo", groups["fluxo"]), row("Técnico", groups["tecnico"]), row("Estrutura", groups["estrutura"]),
                {"name": "Volatilidade", "icon": vol_icon, "value": None, "detail": vol_txt}]
        against = [x["name"] for x in rows if x["icon"] == "🔴"]
        event = r.event
        if d == 0 or abs(r.score) < 40:
            status, icon = "SEM VIÉS SUFICIENTE — NÃO ENTRAR", "🟡"
        elif vol_icon == "🔴" or (trend is not None and dash_state_of(trend) == -d):
            status, icon = "NÃO ENTRAR — " + ("volatilidade extrema" if vol_icon == "🔴" else "tendência maior contra o viés"), "🔴"
        elif light["state"] in ("COMPRA", "VENDA") and confluence >= 70 and not against:
            status, icon = "CENÁRIO CONFIRMADO", "🟢"
        else:
            status, icon = "AGUARDAR CONFIRMAÇÃO", "🟡"
        plan = None
        if d != 0 and atr_now:
            price = s.price
            lv = dash_levels(h1[-120:], price, atr_now) if h1 else {"supports": [], "resistances": []}
            struct = (lv["supports"][0] - 0.25 * atr_now) if d > 0 and lv["supports"] else (lv["resistances"][0] + 0.25 * atr_now) if d < 0 and lv["resistances"] else None
            dist = abs(price - struct) if struct is not None else None
            if dist is None or dist < 0.8 * atr_now or dist > 3 * atr_now:
                stop, how = price - d * 1.5 * atr_now, "1,5 × ATR H1"
            else:
                stop, how = struct, "além do suporte/resistência mais próximo"
            risk_pt = abs(price - stop)
            target = price + d * 2 * risk_pt
            nxt = (lv["resistances"][0] if d > 0 and lv["resistances"] else lv["supports"][0] if d < 0 and lv["supports"] else None)
            plan = {"side": "COMPRA" if d > 0 else "VENDA", "entry": round(price, 2), "stop": round(stop, 2), "stop_rule": how, "target": round(target, 2),
                    "target_rule": "2R (2 × o risco)", "next_level": nxt, "risk_points": round(risk_pt, 2),
                    "risk_per_lot_usd": round(risk_pt * self.contract_oz, 2)}
            if risk_usd:
                lots = risk_usd / (risk_pt * self.contract_oz) if risk_pt else 0.0
                plan["risk_usd"] = risk_usd
                plan["lots"] = math.floor(lots * 100) / 100
        warn = None
        if event is not None:
            warn = f"Evento de alto impacto: {event.name} em {event.minutes:.0f} minutos"
        return {"rows": rows, "confluence": confluence, "status": status, "icon": icon, "plan": plan, "event_warning": warn, "against": against,
                "note": "Apoio à decisão: o painel não envia ordens. Confirme preço, spread e risco na corretora antes de executar."}

    # ---- gráfico
    def chart(self, tf: str, risk_usd: Optional[float] = None) -> dict:
        s, r = self.snapshot, self.reading
        if s is None or r is None:
            return {"tf": tf, "candles": []}
        full = s.candles.get(tf) or []
        if not full:
            return {"tf": tf, "candles": [], "available": [k for k in DASH_TFS if s.candles.get(k)]}
        closes = [c.close for c in full]
        e = {n: ema(closes, n) if len(closes) >= n else [] for n in (9, 21, 50, 200)}
        vw = dash_vwap_series(full, tf, s.session_start[0] if s.session_start else 22)
        start = max(0, len(full) - DASH_BARS)
        vis = full[start:]

        def at(series: list, i: int) -> Optional[float]:
            return round(series[i], 2) if series and i < len(series) and (len(closes) - len(series)) <= i else None

        candles = []
        for i in range(start, len(full)):
            c = full[i]
            v = vw[i]
            candles.append({"t": int(c.time.timestamp()), "o": c.open, "h": c.high, "l": c.low, "c": c.close,
                            "e9": at(e[9], i), "e21": at(e[21], i), "e50": at(e[50], i), "e200": at(e[200], i) if len(closes) >= 200 else None,
                            "vw": round(v["vwap"], 2), "sd": round(v["sd"], 2)})
        atr_tf = atr(full) or s.atr or 1.0
        levels = dash_levels(vis, s.price, atr_tf)
        fib = dash_fibonacci(vis)
        t0, t1 = vis[0].time, vis[-1].time
        events = [{"t": int(ev.time.timestamp()), "name": ev.name, "impact": ev.impact, "future": ev.time > s.time}
                  for ev in s.events if t0 <= ev.time <= t1 + timedelta(hours=12)]
        signals = []
        if self.memory is not None:
            prev = None
            for t, score, label in self.memory.db.execute("SELECT time, score, label FROM bias_predictions ORDER BY time").fetchall():
                tt = datetime.fromisoformat(t)
                if tt < t0:
                    prev = label
                    continue
                if label != prev and ("ALTA" in label or "BAIXA" in label):
                    signals.append({"t": int(tt.timestamp()), "label": label, "score": score, "side": 1 if "ALTA" in label else -1})
                prev = label
        entry = self.payload.get("entry", {}).get("plan") if self.payload else None
        return {"tf": tf, "candles": candles, "levels": levels, "fib": fib, "events": events, "signals": signals[-30:], "entry": entry,
                "price": s.price, "available": [k for k in DASH_TFS if s.candles.get(k)]}

    # ---- alertas
    def buy_sell_alert(self, light: dict, s: MarketSnapshot, r: BiasReading, macro: list[dict], tech: dict) -> Optional[str]:
        """🚨 ALERTA DE COMPRA/VENDA quando o semáforo passa a COMPRA/VENDA — com a lista do que confirmou."""
        prev, now = self.prev_light, light["state"]
        self.prev_light = now
        if now not in ("COMPRA", "VENDA") or prev == now:
            return None
        d = 1 if now == "COMPRA" else -1
        ok = []
        for tf, st in r.structure.items():
            if ("rompimento de alta" in st and d > 0) or ("rompimento de baixa" in st and d < 0):
                ok.append(f"XAU/USD {'rompeu resistência' if d > 0 else 'perdeu suporte'} ({tf})")
        for it in macro[:5]:
            if it["value"] is not None and it["state"] == d:
                ok.append(f"{it['name']} {'favorável' if d > 0 else 'desfavorável'} ao ouro {it['detail']}".strip())
        for it in tech["items"]:
            if it["directional"] and it["value"] is not None and it["state"] == d:
                ok.append(f"{it['name']} confirmou")
        if self.score_before is not None and round(self.score_before) != round(r.score):
            ok.append(f"IA mudou de {self.score_before:+.0f} → {r.score:+.0f}")
        return "\n".join([f"🚨 ALERTA DE {now}", "", *[f"• {x}" for x in ok[:8]], "",
                          "XAU/USD: " + f"{s.price:,.2f}".replace(",", "_").replace(".", ",").replace("_", "."),
                          f"Viés: {r.emoji} {r.label} · confiança {r.confidence:.0f}%", "Apoio à decisão — confira o painel antes de entrar."])

    def add_alert(self, kind: str, text: str, when: datetime) -> None:
        self.alerts.insert(0, {"kind": kind, "time": when.isoformat(), "text": text})
        del self.alerts[40:]

    # ---- montagem
    def update(self, s: MarketSnapshot, r: BiasReading, status: Optional[dict] = None, risk_usd: Optional[float] = None) -> dict:
        self.score_before = self.prev_score
        self.snapshot, self.reading = s, r
        self.status = dict(status or {})
        macro = self.macro_block(s, r)
        ctx = self.context_block(s, r)
        flow = self.flow_block(s, r)
        tech = self.technical_block(s, r)
        news = self.news_block(r)
        groups = self.groups(macro, flow, tech, news, s, r)
        conf = self.confluence(groups, r.score)
        light = self.traffic_light(groups, r.score, r.event.minutes if r.event else None)
        entry = self.entry(s, r, groups, conf, light, risk_usd)
        brain = self.brain(s, r, tech, groups)
        self.updated = datetime.now(timezone.utc)
        self.payload = {
            "time": s.time.isoformat(), "updated": self.updated.isoformat(), "local_tz_hours": self.local_tz_hours,
            "price": self.price_block(s),
            "bias": {"label": r.label, "emoji": r.emoji, "direction": r.direction, "score": r.score, "confidence": r.confidence, "coverage": r.coverage,
                     "horizons": {k: {"label": h.label, "emoji": h.emoji, "score": h.score} for k, h in r.horizons.items()}},
            "macro": macro, "context": ctx, "flow": flow, "technical": tech, "news": news, "calendar": self.calendar_block(s),
            "confluence": {"groups": {k: {"label": DASH_GROUP_LABELS[k], "value": v, "icon": dash_icon(v)} for k, v in groups.items()},
                           "value": conf, "macro": macro[:6], "flow": flow[:4], "technical": tech["items"], "themes": news["themes"]},
            "light": light, "brain": brain, "entry": entry,
            "event": None if r.event is None else {"name": r.event.name, "minutes": r.event.minutes, "if_above": r.event.if_above,
                                                  "if_below": r.event.if_below, "volatility": r.event.volatility},
            "alerts": self.alerts, "status": self.status,
            "disclaimer": "Viés probabilístico, não garantia de movimento. Apoio à decisão — o painel não executa ordens.",
        }
        self.prev_score = r.score
        return self.payload

    def entry_for(self, risk_usd: Optional[float]) -> dict:
        s, r = self.snapshot, self.reading
        if s is None or r is None:
            return {}
        p = self.payload
        groups = {k: v["value"] for k, v in p["confluence"]["groups"].items()}
        return self.entry(s, r, groups, p["confluence"]["value"], p["light"], risk_usd)

    def history(self, limit: int = 60) -> list[dict]:
        """O que a IA disse × o que o ouro fez depois (previsões resolvidas contra o preço real)."""
        if self.memory is None:
            return []
        rows = self.memory.db.execute("""SELECT p.id, p.time, p.price, p.score, p.label, p.confidence, o.horizon, o.move_pct, o.hit
            FROM bias_predictions p LEFT JOIN bias_outcomes o ON o.prediction_id = p.id ORDER BY p.time DESC LIMIT ?""", (limit * 3,)).fetchall()
        out: dict[int, dict] = {}
        for pid, t, price, score, label, conf, h, move, hit in rows:
            d = out.setdefault(pid, {"time": t, "price": price, "score": score, "label": label, "confidence": conf, "outcomes": {}})
            if h:
                d["outcomes"][h] = {"move_pct": move, "hit": bool(hit)}
        return list(out.values())[:limit]


# --------------------------------------------------------------------------- serviço (coleta em segundo plano + HTTP)
class DashService:
    """Loop de coleta/análise em thread + HTTP local. O Telegram recebe o mesmo que o comando `bias` (anti-repetição)."""

    def __init__(self, source: Any, engine: Optional[GoldBiasEngine] = None, memory: Optional[BiasMemory] = None,
                 notifier: Optional[BiasNotifier] = None, sender: Any = None, interval: int = 60, manual_path: Optional[str] = None,
                 contract_oz: float = DASH_CONTRACT_OZ, record_every: int = 300) -> None:
        self.source = source
        self.engine = engine or GoldBiasEngine()
        self.memory = memory
        self.notifier = notifier
        self.sender = sender
        self.interval = interval
        self.manual_path = manual_path
        self.record_every = record_every
        self.state = DashState(memory, contract_oz)
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()   # "Atualizar" acorda a thread de coleta (MT5 sempre acessado pela mesma thread)
        self.last_error = ""
        self.last_record: Optional[datetime] = None
        self.cycles = 0

    def snapshot(self) -> MarketSnapshot:
        return self.source.snapshot() if hasattr(self.source, "snapshot") else self.source.collect()

    def cycle(self) -> dict:
        s = self.snapshot()
        bias_apply_manual(s, self.manual_path)
        r = self.engine.analyze(s)
        status = dict(getattr(self.source, "status", {}) or {})
        with self.lock:
            if self.memory is not None:
                h1 = s.candles.get("H1") or []
                if h1:
                    self.memory.resolve(h1, s.time)
                if self.last_record is None or (s.time - self.last_record).total_seconds() >= self.record_every:
                    self.memory.record(r, "painel")
                    self.last_record = s.time
            payload = self.state.update(s, r, status)
            messages = self.notifier.decide(r) if self.notifier is not None else []
            alert = self.state.buy_sell_alert(payload["light"], s, r, payload["macro"], payload["technical"])
            if alert:
                messages.append(("alerta_entrada", alert))
            for kind, text in messages:
                self.state.add_alert(kind, text, s.time)
            payload["alerts"] = self.state.alerts
            self.cycles += 1
        for _kind, text in messages:
            if self.sender is not None:
                self.sender.send(text)
        return payload

    def run_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.cycle()
                self.last_error = ""
            except Exception as e:  # noqa: BLE001 — o painel continua no ar com o último estado
                self.last_error = f"{type(e).__name__}: {e}"
                print(f"[painel] ciclo falhou: {self.last_error}")
            self.wake_event.wait(self.interval)
            self.wake_event.clear()

    def request_refresh(self, timeout: float = 120.0) -> bool:
        """Pede uma leitura nova à thread de coleta e espera ela terminar."""
        before = self.cycles
        self.wake_event.set()
        end = time.time() + timeout
        while time.time() < end and self.cycles == before and not self.stop_event.is_set():
            time.sleep(0.2)
        return self.cycles != before

    def state_json(self) -> dict:
        with self.lock:
            p = dict(self.state.payload)
        p["service"] = {"cycles": self.cycles, "interval": self.interval, "error": self.last_error}
        return p


def dash_handler(service: DashService) -> type:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # silencioso: a janela mostra só o que importa
            return

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj: Any) -> None:
            self._send(200, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            u = urlparse(self.path)
            q = parse_qs(u.query)

            def num(name: str) -> Optional[float]:
                try:
                    v = float(q.get(name, [""])[0])
                    return v if v > 0 else None
                except ValueError:
                    return None

            if u.path in ("/", "/index.html"):
                self._send(200, DASH_HTML.encode("utf-8"), "text/html; charset=utf-8")
            elif u.path == "/api/state":
                self._json(service.state_json())
            elif u.path == "/api/chart":
                tf = q.get("tf", ["H1"])[0].upper()
                with service.lock:
                    data = service.state.chart(tf if tf in DASH_TFS else "H1")
                self._json(data)
            elif u.path == "/api/entry":
                with service.lock:
                    data = service.state.entry_for(num("risk"))
                self._json(data)
            elif u.path == "/api/history":
                with service.lock:
                    data = service.state.history()
                self._json(data)
            elif u.path == "/api/refresh":
                ok = service.request_refresh()
                self._json({"ok": ok, "error": service.last_error})
            else:
                self._send(404, b"not found", "text/plain")

    return Handler


def dash_serve(service: DashService, host: str = "127.0.0.1", port: int = 8765, open_browser: bool = False,
               wait_first: float = 180.0) -> ThreadingHTTPServer:
    """Sobe a thread de coleta (1ª leitura já nela) e o HTTP. Devolve o servidor (chame .serve_forever())."""
    threading.Thread(target=service.run_loop, name="gold-painel-coleta", daemon=True).start()
    end = time.time() + wait_first
    while service.cycles == 0 and not service.last_error and time.time() < end:
        time.sleep(0.2)
    if service.last_error:
        print(f"[painel] primeira leitura falhou: {service.last_error} — o painel sobe e tenta de novo a cada {service.interval}s")
    srv = ThreadingHTTPServer((host, port), dash_handler(service))
    url = f"http://{host}:{srv.server_address[1]}/"
    print(f"🥇 GOLD MARKET INTELLIGENCE — painel em {url}  (Ctrl+C para sair)")
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    return srv


DASH_HTML = r"""__DASH_HTML__"""
if DASH_HTML == "__DASH_" + "HTML__":            # pacote: lê gold_ai/dashboard.html (no bundle o HTML já vem embutido acima)
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html"), encoding="utf-8") as _fh:
        DASH_HTML = _fh.read()
