"""Gerador de cenários sintéticos para demonstração/testes (sem dependências)."""


from __future__ import annotations

from ..config import TF_MINUTES

import random
from datetime import datetime, timedelta, timezone

from ..models import Candle, EconomicEvent, MarketSnapshot, NewsItem



def make_candles(tf: str, n: int, start_price: float, drift: float, vol: float, end: datetime, seed: int = 7, volume_trend: float = 0.0) -> list[Candle]:
    """Série de candles com deriva (`drift` por candle, em USD) e volatilidade `vol` (USD)."""
    rnd = random.Random(seed + sum(ord(ch) * (i + 1) for i, ch in enumerate(tf)))  # determinístico entre processos
    step = timedelta(minutes=TF_MINUTES[tf])
    price = start_price
    out: list[Candle] = []
    for i in range(n):
        t = end - step * (n - 1 - i)
        o = price
        c = o + drift + rnd.gauss(0, vol)
        h = max(o, c) + abs(rnd.gauss(0, vol * 0.5))
        l = min(o, c) - abs(rnd.gauss(0, vol * 0.5))
        v = max(10.0, 1000 * (1 + volume_trend * (i / n)) + rnd.gauss(0, 150))
        out.append(Candle(t, round(o, 2), round(h, 2), round(l, 2), round(c, 2), round(v, 0)))
        price = c
    return out


def _candles_all(price: float, end: datetime, drift_per_hour: float, vol: float, seed: int, volume_trend: float = 0.0) -> dict[str, list[Candle]]:
    out: dict[str, list[Candle]] = {}
    for tf, mins in TF_MINUTES.items():
        n = 260 if mins <= 240 else 120
        drift = drift_per_hour * mins / 60
        v = vol * (mins / 60) ** 0.5
        # start so that the series ends near `price`
        start = price - drift * n
        cs = make_candles(tf, n, start, drift, v, end, seed, volume_trend)
        shift = price - cs[-1].close
        for c in cs:
            c.open, c.high, c.low, c.close = c.open + shift, c.high + shift, c.low + shift, c.close + shift
        out[tf] = cs
    return out


class SampleSource:
    """Cenários: 'premove_alta', 'confirmacao_alta', 'venda', 'neutro', 'reversao', 'sistemico', 'pre_evento'."""

    def __init__(self, scenario: str = "premove_alta", price: float = 2650.0, seed: int = 7, now: datetime | None = None) -> None:
        self.scenario = scenario
        self.price = price
        self.seed = seed
        self.now = now or datetime(2026, 9, 14, 13, 0, tzinfo=timezone.utc)

    def snapshot(self) -> MarketSnapshot:
        now, p = self.now, self.price
        sc = self.scenario
        s = MarketSnapshot(time=now, price=p, atr=9.0)

        if sc == "premove_alta":
            # fundamentos viraram; preço lateral (§14)
            s.candles = _candles_all(p, now, drift_per_hour=0.0, vol=3.0, seed=self.seed, volume_trend=0.6)
            s.price_change_pct = 0.05
            s.dxy, s.dxy_change_pct = 103.2, -0.35
            s.us10y, s.us10y_change_bp, s.real_yield_change_bp = 4.05, -6.0, -5.0
            s.fed_cut_prob_change_pp, s.fed_tone = 12.0, 0.3
            s.inflation_surprise_sigma = -1.0
            s.geopolitical_risk, s.geopolitical_risk_change = 55, 4
            s.etf_flow_musd, s.order_flow_imbalance, s.open_interest_change_pct = 220.0, 0.35, 2.1
            s.cot_managed_money_net_change, s.cot_managed_money_percentile = 8000, 55
            s.put_call_ratio, s.implied_vol_change_pct = 1.05, 3.0
            s.sentiment, s.sentiment_change = 0.35, 0.2
            s.vix, s.vix_change_pct, s.credit_spread_bp = 15.0, 2.0, 330
            s.news = [NewsItem("CPI dos EUA abaixo do esperado; núcleo desacelera", "BLS", now, "macro", gold_impact=0.7, priced_in=0.3,
                               interpretation="aumenta probabilidade de corte do Fed → yields ↓ → dólar ↓ → suporte ao ouro")]
        elif sc == "confirmacao_alta":
            s.candles = _candles_all(p, now, drift_per_hour=1.6, vol=3.0, seed=self.seed, volume_trend=0.5)
            s.price_change_pct = 0.35
            s.dxy_change_pct, s.real_yield_change_bp, s.us10y_change_bp = -0.4, -6.0, -7.0
            s.fed_cut_prob_change_pp, s.fed_tone = 10.0, 0.3
            s.inflation_surprise_sigma = -0.8
            s.geopolitical_risk, s.geopolitical_risk_change = 50, 2
            s.etf_flow_musd, s.order_flow_imbalance, s.open_interest_change_pct = 260.0, 0.4, 2.5
            s.cot_managed_money_net_change, s.cot_managed_money_percentile = 9000, 60
            s.put_call_ratio = 1.0
            s.sentiment = 0.4
            s.vix, s.credit_spread_bp = 14.5, 320
        elif sc == "venda":
            s.candles = _candles_all(p, now, drift_per_hour=-1.4, vol=3.0, seed=self.seed)
            s.price_change_pct = -0.3
            s.dxy_change_pct, s.real_yield_change_bp, s.us10y_change_bp = 0.45, 7.0, 8.0
            s.fed_cut_prob_change_pp, s.fed_tone = -10.0, -0.4
            s.inflation_surprise_sigma = 1.2
            s.geopolitical_risk, s.geopolitical_risk_change = 40, -3
            s.etf_flow_musd, s.order_flow_imbalance = -180.0, -0.35
            s.cot_managed_money_net_change, s.cot_managed_money_percentile = -7000, 45
            s.put_call_ratio = 0.7
            s.sentiment, s.sentiment_change = -0.4, -0.2
            s.vix, s.credit_spread_bp = 13.0, 310
        elif sc == "reversao":
            # tendência de alta com distribuição (§16)
            s.candles = _candles_all(p, now, drift_per_hour=1.2, vol=3.0, seed=self.seed, volume_trend=-0.6)
            s.price_change_pct = 0.2
            s.dxy_change_pct, s.real_yield_change_bp = 0.3, 5.0
            s.fed_cut_prob_change_pp, s.fed_tone = -8.0, -0.3
            s.etf_flow_musd, s.order_flow_imbalance = -200.0, -0.4
            s.cot_managed_money_net_change, s.cot_managed_money_percentile = 2000, 94
            s.sentiment = 0.7
            s.vix, s.credit_spread_bp = 14.0, 310
        elif sc == "sistemico":
            s.candles = _candles_all(p, now, drift_per_hour=-2.0, vol=6.0, seed=self.seed, volume_trend=1.0)
            s.price_change_pct = -0.8
            s.dxy_change_pct, s.real_yield_change_bp = 0.6, -4.0
            s.vix, s.vix_change_pct, s.credit_spread_bp, s.credit_spread_change_bp = 34.0, 45.0, 620, 60
            s.equity_change_pct, s.bank_stress = -3.5, 70
            s.geopolitical_risk, s.geopolitical_risk_change = 60, 5
            s.etf_flow_musd, s.order_flow_imbalance = 80.0, -0.2
            s.sentiment = -0.3
        elif sc == "pre_evento":
            s.candles = _candles_all(p, now, drift_per_hour=0.2, vol=2.5, seed=self.seed)
            s.price_change_pct = 0.05
            s.dxy_change_pct, s.real_yield_change_bp = -0.1, -1.0
            s.fed_cut_prob_change_pp = 2.0
            s.cot_managed_money_percentile, s.put_call_ratio = 85, 0.95
            s.sentiment = 0.2
            s.vix, s.credit_spread_bp = 15.5, 330
            s.events = [EconomicEvent("CPI EUA", now + timedelta(minutes=30), "MUITO ALTO", consensus=0.3, previous=0.2, kind="cpi", unit="%")]
        else:  # neutro
            s.candles = _candles_all(p, now, drift_per_hour=0.0, vol=3.0, seed=self.seed)
            s.price_change_pct = 0.0
            s.dxy_change_pct, s.real_yield_change_bp = 0.05, 0.5
            s.fed_cut_prob_change_pp, s.fed_tone = 0.0, 0.0
            s.etf_flow_musd, s.order_flow_imbalance = 10.0, 0.02
            s.sentiment = 0.0
            s.vix, s.credit_spread_bp = 15.0, 330
        return s
