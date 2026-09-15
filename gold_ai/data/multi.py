"""MARKET AI ENGINE 4.0 — dados multi-mercado sobre o Data Engine existente.

Macro (dólar, juros, Fed, inflação, geopolítica, risco sistêmico, notícias, COT do ouro) é coletada UMA vez;
cada mercado recebe os próprios candles/preço/ATR/fluxo (Yahoo ou MT5) e o COT do próprio contrato quando houver.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from ..markets import MARKETS, MarketSpec, get_market
from ..models import MarketSnapshot
from ..technical import atr as _atr
from .engine import DataEngine, DataEngineConfig
from .http import HttpClient
from .yahoo import YahooCollector


@dataclass
class MarketSnapshotSet:
    time: datetime
    base: MarketSnapshot                       # snapshot macro (convenção do ouro)
    by_symbol: dict[str, MarketSnapshot] = field(default_factory=dict)
    status: dict[str, str] = field(default_factory=dict)
    data_quality: dict[str, float] = field(default_factory=dict)
    identified: list = field(default_factory=list)     # eventos identificados pelo NEWS ENGINE neste ciclo


MACRO_FIELDS = ("dxy", "dxy_change_pct", "us2y", "us10y", "us10y_change_bp", "real_yield_10y", "real_yield_change_bp", "breakeven_10y_change_bp",
                "fed_cut_prob_change_pp", "fed_tone", "inflation_surprise_sigma", "inflation_trend", "geopolitical_risk", "geopolitical_risk_change",
                "vix", "vix_change_pct", "credit_spread_bp", "credit_spread_change_bp", "equity_change_pct", "bank_stress", "sentiment", "sentiment_change",
                "silver_change_pct", "oil_change_pct", "btc_change_pct", "usdcnh_change_pct", "news", "events")


def derive_market_snapshot(base: MarketSnapshot, spec: MarketSpec, candles: dict, now: datetime, window_minutes: int = 60,
                           cot: Optional[dict] = None, identified=None) -> MarketSnapshot:
    """Snapshot do mercado: macro compartilhada + candles/preço/ATR/fluxo próprios."""
    s = MarketSnapshot(time=now)
    for f in MACRO_FIELDS:
        setattr(s, f, copy.copy(getattr(base, f)))
    s.candles = candles
    ref = candles.get("H1") or candles.get("M15") or []
    if ref:
        s.price = ref[-1].close
        s.atr = (_atr(candles["H1"]) or 0.0) if "H1" in candles else 0.0
        fine = candles.get("M5") or ref
        cutoff = fine[-1].time.timestamp() - window_minutes * 60
        prev = next((c for c in reversed(fine[:-1]) if c.time.timestamp() <= cutoff), fine[0])
        s.price_change_pct = (fine[-1].close / prev.close - 1) * 100 if prev.close else 0.0
        recent = [c for c in fine if (now - c.time).total_seconds() <= window_minutes * 60]
        vol = sum(c.volume for c in recent)
        if vol > 0:
            s.order_flow_imbalance = round((sum(c.volume for c in recent if c.close > c.open) - sum(c.volume for c in recent if c.close < c.open)) / vol, 3)
    if spec.symbol == "XAUUSD":
        s.cot_managed_money_net, s.cot_managed_money_net_change = base.cot_managed_money_net, base.cot_managed_money_net_change
        s.cot_managed_money_percentile, s.cot_commercial_net_change = base.cot_managed_money_percentile, base.cot_commercial_net_change
        s.etf_flow_musd, s.put_call_ratio, s.implied_vol_change_pct = base.etf_flow_musd, base.put_call_ratio, base.implied_vol_change_pct
        s.gamma_wall_above, s.gamma_wall_below = base.gamma_wall_above, base.gamma_wall_below
    elif cot:
        s.cot_managed_money_net, s.cot_managed_money_net_change = cot.get("net"), cot.get("change")
        s.cot_managed_money_percentile, s.cot_commercial_net_change = cot.get("percentile"), cot.get("commercial_change")
        s.cot_age_days, s.cot_report_date = cot.get("age_days"), cot.get("report_date")
    if spec.symbol == "XAUUSD":
        s.cot_age_days, s.cot_report_date = base.cot_age_days, base.cot_report_date
    # NEWS ENGINE por mercado: identificação → importância → surpresa → direção esperada → reação → pressão latente
    if identified is not None:
        from ..news_engine import NewsEngine
        na = NewsEngine().assess(spec.symbol, s, identified, now)
        s.news_pressure, s.news_status, s.news_chain = na.pressure, na.status, na.chain
    return s


def data_quality(s: MarketSnapshot, spec: MarketSpec) -> float:
    """Fração dos fatores relevantes para o mercado com dado disponível."""
    checks = {
        "dolar": s.dxy_change_pct is not None, "juros_reais": s.real_yield_change_bp is not None or s.us10y_change_bp is not None,
        "fed": s.fed_cut_prob_change_pp is not None or s.fed_tone is not None, "inflacao": s.inflation_surprise_sigma is not None,
        "geopolitica": s.geopolitical_risk is not None, "fluxo": s.order_flow_imbalance is not None, "cot": s.cot_managed_money_net_change is not None,
        "opcoes": s.put_call_ratio is not None, "sentimento": s.sentiment is not None, "tecnico": bool(s.candles.get("H1")),
    }
    relevant = [k for k, sign in spec.factor_signs.items() if sign != 0]
    if not relevant:
        return 0.0
    return round(sum(1 for k in relevant if checks.get(k)) / len(relevant), 2)


class MultiMarketData:
    """Coleta macro uma vez e candles por mercado (Yahoo por padrão; MT5 quando `mt5_client` é fornecido)."""

    def __init__(self, symbols: tuple[str, ...], cfg: Optional[DataEngineConfig] = None, http: Optional[HttpClient] = None,
                 mt5_client: Any = None, mt5_symbol_map: Optional[dict[str, str]] = None) -> None:
        self.specs = [get_market(s) for s in symbols]
        self.engine = DataEngine(cfg, http)
        self.yahoo = YahooCollector(self.engine.http)
        self.mt5 = mt5_client
        self.mt5_symbol_map = mt5_symbol_map or {}
        self.status: dict[str, str] = {}

    @classmethod
    def symbol_map_from_env(cls, env: dict[str, str]) -> dict[str, str]:
        return {sym: env[f"MT5_SYMBOL_{sym}"] for sym in MARKETS if f"MT5_SYMBOL_{sym}" in env}

    def market_candles(self, spec: MarketSpec) -> dict:
        if self.mt5 is not None:
            from .mt5 import TF_TO_MT5
            orig = self.mt5.cfg.symbol
            self.mt5.cfg.symbol = self.mt5_symbol_map.get(spec.symbol, spec.mt5)
            try:
                if not self.mt5.connected:
                    self.mt5.connect()
                if not self.mt5.mt5.symbol_select(self.mt5.cfg.symbol, True):
                    self.status[f"mt5:{spec.symbol}"] = f"símbolo {self.mt5.cfg.symbol} indisponível na corretora → Yahoo (confira MT5_SYMBOL_{spec.symbol} no .env)"
                    return self.yahoo.all_timeframes(spec.yahoo)
                return {tf: cs for tf in TF_TO_MT5 if (cs := self.mt5.candles(tf))}
            except Exception as e:  # noqa: BLE001
                self.status[f"mt5:{spec.symbol}"] = f"MT5 falhou ({str(e)[:60]}) → Yahoo"
                return self.yahoo.all_timeframes(spec.yahoo)
            finally:
                self.mt5.cfg.symbol = orig
        return self.yahoo.all_timeframes(spec.yahoo)

    def market_cot(self, spec: MarketSpec, now: datetime) -> Optional[dict]:
        if not spec.cftc_code or spec.symbol == "XAUUSD" or not self.engine.cfg.enable_cot:
            return None
        data = self.engine.cot_with_cache(spec.cftc_code, now)
        if data is None:
            self.status[f"cot:{spec.symbol}"] = "erro: indisponível e sem cache"
            return None
        if data.get("from_cache"):
            self.status[f"cot:{spec.symbol}"] = f"último válido {data['report_date']} ({data['age_days']:.0f} dias)"
        return data

    def collect(self, now: Optional[datetime] = None) -> MarketSnapshotSet:
        now = now or datetime.now(timezone.utc)
        base = self.engine.collect(now)          # macro + XAU
        self.status = dict(self.engine.status)
        out = MarketSnapshotSet(now, base)
        from ..news_engine import EventIdentifier
        identified = EventIdentifier().identify(base.news, base.events, now)
        self.identified = identified
        out.identified = identified
        for spec in self.specs:
            try:
                candles = base.candles if (self.mt5 is None and spec.symbol == "XAUUSD" and base.candles) else self.market_candles(spec)   # com MT5, o ouro é o SPOT do broker
                if not candles:
                    raise RuntimeError("sem candles")
                s = derive_market_snapshot(base, spec, candles, now, self.engine.cfg.window_minutes, self.market_cot(spec, now), identified)
                out.by_symbol[spec.symbol] = s
                out.data_quality[spec.symbol] = data_quality(s, spec)
                self.status[spec.symbol] = "ok"
            except Exception as e:  # noqa: BLE001
                self.status[spec.symbol] = f"erro: {e}"
        out.status = dict(self.status)
        return out

    def coverage(self) -> str:
        from ..news_engine import render_feed_health
        ok = [k for k, v in self.status.items() if v == "ok"]
        bad = [f"  ✗ {k}: {v}" for k, v in self.status.items() if v != "ok"]
        lines = [f"MULTI-MARKET DATA — ok: {', '.join(ok) or 'nenhum'}"] + bad
        if self.engine.cfg.enable_news:
            lines.append(render_feed_health(self.engine.news.health))
            n = len(getattr(self, "identified", []))
            lines.append(f"NEWS ENGINE: {n} evento(s) identificado(s) na janela" + ("" if n else " → NEWS = UNKNOWN em todos os mercados (peso reduzido, não negativo)"))
        return "\n".join(lines)
