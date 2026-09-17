"""DATA ENGINE — coleta, normaliza e entrega um MarketSnapshot com dados reais.

Cada coletor é isolado: uma falha vira `status[fonte] = erro` e o campo fica None;
o GOLD AI ENGINE reduz a confiança em vez de quebrar. `LiveSource` implementa
`DataSource.snapshot()` e substitui `SampleSource`.
"""


from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

from ..models import MarketSnapshot
from ..technical import _atr
from .http import HttpClient
from .yahoo import YahooCollector
from .fred import FredCollector
from .cftc import CftcCollector
from .news import NewsCollector, aggregate_sentiment, geopolitical_index, load_calendar


@dataclass
class DataEngineConfig:
    xau_symbol: str = "GC=F"          # ou "XAUUSD=X"
    dxy_symbol: str = "DX-Y.NYB"
    us10y_symbol: str = "^TNX"
    us2y_symbol: str = "2YY=F"
    fedfunds_symbol: str = "ZQ=F"     # 30-day Fed Funds futures (100 − preço = taxa implícita)
    vix_symbol: str = "^VIX"
    spx_symbol: str = "^GSPC"
    silver_symbol: str = "SI=F"
    oil_symbol: str = "CL=F"
    btc_symbol: str = "BTC-USD"
    usdcnh_symbol: str = "CNH=X"
    window_minutes: int = 60          # janela das "variações recentes"
    cache_dir: Optional[str] = os.path.join(os.path.expanduser("~"), ".gold_ai_cache")
    feeds: Optional[tuple[str, ...]] = None
    calendar_path: Optional[str] = None
    enable_fred: bool = True
    enable_cot: bool = True
    enable_news: bool = True


class DataEngine:
    def __init__(self, cfg: Optional[DataEngineConfig] = None, http: Optional[HttpClient] = None) -> None:
        self.cfg = cfg or DataEngineConfig()
        self.http = http or HttpClient(cache_dir=self.cfg.cache_dir)
        self.yahoo = YahooCollector(self.http)
        self.fred = FredCollector(self.http)
        self.cftc = CftcCollector(self.http)
        self.news = NewsCollector(self.http, self.cfg.feeds) if self.cfg.feeds else NewsCollector(self.http)
        self.status: dict[str, str] = {}

    # ------------------------------------------------------------------ util
    def _try(self, name: str, fn: Callable[[], None]) -> None:
        try:
            fn()
            self.status[name] = "ok"
        except Exception as e:  # noqa: BLE001 - isolar cada fonte
            self.status[name] = f"erro: {e}"

    def _change(self, symbol: str, tf: str = "M15", pct: bool = True) -> Optional[float]:
        return self.yahoo.change_over(self.yahoo.candles(symbol, tf), self.cfg.window_minutes, pct)

    # ------------------------------------------------------------------ coleta
    def collect(self, now: Optional[datetime] = None) -> MarketSnapshot:
        now = now or datetime.now(timezone.utc)
        s = MarketSnapshot(time=now)
        w = self.cfg.window_minutes

        def xau() -> None:
            s.candles = self.yahoo.all_timeframes(self.cfg.xau_symbol)
            ref = s.candles.get("H1") or s.candles.get("M15") or []
            if not ref:
                raise RuntimeError("sem candles XAU")
            s.price = ref[-1].close
            s.atr = _atr(s.candles["H1"]) or 0.0 if "H1" in s.candles else 0.0
            m5 = s.candles.get("M5") or ref
            s.price_change_pct = self.yahoo.change_over(m5, w) or 0.0
            # proxy de fluxo agressor: volume em candles de alta − de baixa na janela
            recent = [c for c in m5 if (now - c.time).total_seconds() <= w * 60]
            vol = sum(c.volume for c in recent)
            if vol > 0:
                s.order_flow_imbalance = round((sum(c.volume for c in recent if c.close > c.open) - sum(c.volume for c in recent if c.close < c.open)) / vol, 3)
            h1 = s.candles.get("H1") or []
            if len(h1) > 30:
                avg = sum(c.volume for c in h1[-30:-1]) / 29
                s.futures_volume_ratio = round(h1[-1].volume / avg, 2) if avg else None
        self._try("xau", xau)

        def dxy() -> None:
            cs = self.yahoo.candles(self.cfg.dxy_symbol, "M15")
            s.dxy, s.dxy_change_pct = cs[-1].close, self.yahoo.change_over(cs, w)
        self._try("dxy", dxy)

        def yields_() -> None:
            cs = self.yahoo.candles(self.cfg.us10y_symbol, "M15")
            s.us10y = cs[-1].close
            d = self.yahoo.change_over(cs, w, pct=False)
            s.us10y_change_bp = d * 100 if d is not None else None
        self._try("us10y", yields_)

        def us2y() -> None:
            cs = self.yahoo.candles(self.cfg.us2y_symbol, "M15")
            s.us2y = cs[-1].close
        self._try("us2y", us2y)

        def fed() -> None:
            cs = self.yahoo.candles(self.cfg.fedfunds_symbol, "M15")
            d = self.yahoo.change_over(cs, w, pct=False)  # Δpreço; taxa implícita = 100 − preço
            if d is not None:
                implied_bp = -d * 100
                # 25 bp de corte ≈ 100 p.p.: queda de 5 bp na taxa implícita ≈ +20 p.p. de prob. de corte
                s.fed_cut_prob_change_pp = round(max(-100.0, min(100.0, -implied_bp * 4)), 1)
        self._try("fed_funds", fed)

        def fred() -> None:
            if not self.cfg.enable_fred:
                return
            real, d_real = self.fred.last_and_change("DFII10")
            _, d_be = self.fred.last_and_change("T10YIE")
            s.real_yield_10y = real
            s.breakeven_10y_change_bp = d_be * 100 if d_be is not None else None
            if s.us10y_change_bp is not None and d_be is not None:
                # intraday: nominal (Yahoo) − breakeven (FRED, diário)
                s.real_yield_change_bp = round(s.us10y_change_bp - d_be * 100 * (w / 1440), 2)
            elif d_real is not None:
                s.real_yield_change_bp = round(d_real * 100, 2)
            hy, d_hy = self.fred.last_and_change("BAMLH0A0HYM2")
            s.credit_spread_bp = hy * 100 if hy is not None else None
            s.credit_spread_change_bp = d_hy * 100 if d_hy is not None else None
        self._try("fred", fred)

        def risk() -> None:
            cs = self.yahoo.candles(self.cfg.vix_symbol, "M15")
            s.vix, s.vix_change_pct = cs[-1].close, self.yahoo.change_over(cs, w)
            s.equity_change_pct = self._change(self.cfg.spx_symbol)
        self._try("vix_spx", risk)

        def intermarket() -> None:
            s.silver_change_pct = self._change(self.cfg.silver_symbol)
            s.oil_change_pct = self._change(self.cfg.oil_symbol)
            s.btc_change_pct = self._change(self.cfg.btc_symbol)
            s.usdcnh_change_pct = self._change(self.cfg.usdcnh_symbol)
        self._try("intermarket", intermarket)

        def cot() -> None:
            if not self.cfg.enable_cot:
                return
            r = self.cftc.gold()
            s.cot_managed_money_net, s.cot_managed_money_net_change = r.managed_money_net, r.managed_money_net_change
            s.cot_managed_money_percentile, s.cot_commercial_net_change = r.managed_money_percentile, r.commercial_net_change
        self._try("cot", cot)

        def news() -> None:
            if not self.cfg.enable_news:
                return
            items = self.news.collect(now)
            s.news = items[:50]
            s.sentiment, s.sentiment_change = aggregate_sentiment(items, now)
            s.geopolitical_risk, s.geopolitical_risk_change = geopolitical_index(items, now)
            s.events += self.news.released_events()
            if self.news.errors:
                raise RuntimeError(f"{len(items)} notícias; feeds com erro: {len(self.news.errors)}")
        self._try("news", news)

        def calendar() -> None:
            if self.cfg.calendar_path and os.path.exists(self.cfg.calendar_path):
                s.events += load_calendar(self.cfg.calendar_path)
        self._try("calendar", calendar)

        return s

    def coverage(self) -> str:
        ok = [k for k, v in self.status.items() if v == "ok"]
        bad = {k: v for k, v in self.status.items() if v != "ok"}
        lines = [f"DATA ENGINE — fontes ok: {', '.join(ok) or 'nenhuma'}"]
        for k, v in bad.items():
            lines.append(f"  ✗ {k}: {v}")
        return "\n".join(lines)


class LiveSource:
    """DataSource real: substitui SampleSource."""

    def __init__(self, cfg: Optional[DataEngineConfig] = None, http: Optional[HttpClient] = None) -> None:
        self.engine = DataEngine(cfg, http)

    def snapshot(self) -> MarketSnapshot:
        return self.engine.collect()
