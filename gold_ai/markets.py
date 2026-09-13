"""MARKET AI ENGINE 4.0 — registro de mercados.

Cérebro único, múltiplos mercados: os fatores macro são calculados uma vez (na convenção do ouro,
"+ = favorável à alta") e cada mercado declara o SINAL com que cada fator o afeta.
    XAUUSD: dólar ↓ → +, juros reais ↓ → +, geopolítica ↑ → +
    EURUSD: dólar ↓ → +, juros reais ↓ → +, geopolítica ↑ (risk-off, USD) → −
    US500 : juros ↓ → +, Fed dovish → +, geopolítica ↑ → −, risco sistêmico → −
    USDJPY: dólar ↑ → +, juros ↑ → + (o oposto do ouro), geopolítica ↑ (JPY refúgio) → −
    WTI   : dólar ↓ → +, geopolítica ↑ → +, crescimento → +
Fatores sem relação conhecida ficam com sinal 0 (indisponíveis para aquele mercado) — nada de
inventar edge. O técnico e o fluxo vêm sempre dos candles do próprio mercado.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Grupos de exposição: (bucket, direção da exposição quando o mercado SOBE)
# USD_SHORT = o mercado sobe quando o dólar cai; RISK_ON = sobe com apetite a risco; OIL = petróleo.


@dataclass(frozen=True)
class MarketSpec:
    symbol: str                        # nome canônico (XAUUSD, EURUSD, US500, USDJPY, WTI)
    yahoo: str                         # símbolo Yahoo para candles
    mt5: str                           # símbolo típico no MT5 (ajustável por .env: MT5_SYMBOL_<symbol>)
    contract_size: float               # unidades por lote (USD por 1.0 de preço por lote = contract_size × valor do ponto)
    point_value_usd: float             # USD por 1.0 de variação de preço por lote padrão
    factor_signs: dict[str, int]       # fator → +1 / −1 / 0
    exposures: dict[str, float]        # bucket → peso (+ = mesma direção do mercado)
    cftc_code: Optional[str] = None    # COT (disaggregated/legacy) do próprio mercado
    typical_spread: float = 0.0        # em unidades de preço (custo de execução)
    min_stop_atr: float = 0.6
    max_stop_atr: float = 2.5
    session_hours_utc: tuple[int, int] = (0, 24)   # janela de liquidez principal
    phase: int = 1

    def risk_usd_per_lot(self, stop_distance: float) -> float:
        return stop_distance * self.point_value_usd


GOLD_SIGNS = {"dolar": 1, "juros_reais": 1, "fed": 1, "inflacao": 1, "geopolitica": 1, "fluxo": 1, "cot": 1, "opcoes": 1, "sentimento": 1, "tecnico": 1}

MARKETS: dict[str, MarketSpec] = {
    "XAUUSD": MarketSpec("XAUUSD", "GC=F", "XAUUSD", 100.0, 100.0, GOLD_SIGNS, {"USD_SHORT": 0.6, "SAFE_HAVEN": 0.8}, "088691", 0.30),
    "EURUSD": MarketSpec("EURUSD", "EURUSD=X", "EURUSD", 100000.0, 100000.0,
                         {"dolar": 1, "juros_reais": 1, "fed": 1, "inflacao": 1, "geopolitica": -1, "fluxo": 1, "cot": 1, "opcoes": 0, "sentimento": 0, "tecnico": 1},
                         {"USD_SHORT": 1.0, "RISK_ON": 0.3}, "099741", 0.00008),
    "US500": MarketSpec("US500", "ES=F", "US500", 1.0, 1.0,
                        {"dolar": 0, "juros_reais": 1, "fed": 1, "inflacao": 1, "geopolitica": -1, "fluxo": 1, "cot": 0, "opcoes": 0, "sentimento": 0, "tecnico": 1},
                        {"RISK_ON": 1.0}, "13874A", 0.4, session_hours_utc=(13, 21)),
    "USDJPY": MarketSpec("USDJPY", "JPY=X", "USDJPY", 100000.0, 680.0,   # ≈ 100000 / 147 USD por 1.0 de preço
                         {"dolar": -1, "juros_reais": -1, "fed": -1, "inflacao": -1, "geopolitica": -1, "fluxo": 1, "cot": 1, "opcoes": 0, "sentimento": 0, "tecnico": 1},
                         {"USD_SHORT": -1.0, "RISK_ON": 0.5}, "097741", 0.012),
    "WTI": MarketSpec("WTI", "CL=F", "XTIUSD", 1000.0, 1000.0,
                      {"dolar": 1, "juros_reais": 0, "fed": 1, "inflacao": 0, "geopolitica": 1, "fluxo": 1, "cot": 1, "opcoes": 0, "sentimento": 0, "tecnico": 1},
                      {"OIL": 1.0, "USD_SHORT": 0.3, "RISK_ON": 0.3}, "067651", 0.03),
    # FASE 2
    "NAS100": MarketSpec("NAS100", "NQ=F", "NAS100", 1.0, 1.0,
                         {"dolar": 0, "juros_reais": 1, "fed": 1, "inflacao": 1, "geopolitica": -1, "fluxo": 1, "cot": 0, "opcoes": 0, "sentimento": 0, "tecnico": 1},
                         {"RISK_ON": 1.0}, None, 1.5, session_hours_utc=(13, 21), phase=2),
    "GBPUSD": MarketSpec("GBPUSD", "GBPUSD=X", "GBPUSD", 100000.0, 100000.0,
                         {"dolar": 1, "juros_reais": 1, "fed": 1, "inflacao": 1, "geopolitica": -1, "fluxo": 1, "cot": 1, "opcoes": 0, "sentimento": 0, "tecnico": 1},
                         {"USD_SHORT": 1.0, "RISK_ON": 0.4}, "096742", 0.00012, phase=2),
    # FASE 3 (não provar volatilidade como falso edge antes de generalizar)
    "BTCUSD": MarketSpec("BTCUSD", "BTC-USD", "BTCUSD", 1.0, 1.0,
                         {"dolar": 1, "juros_reais": 1, "fed": 1, "inflacao": 0, "geopolitica": 0, "fluxo": 1, "cot": 0, "opcoes": 0, "sentimento": 0, "tecnico": 1},
                         {"RISK_ON": 1.0, "USD_SHORT": 0.3}, None, 30.0, phase=3),
}

PHASE1 = ("EURUSD", "US500", "XAUUSD", "USDJPY", "WTI")

# Correlação estática de referência entre retornos (usada até haver correlação empírica suficiente).
DEFAULT_CORRELATION: dict[tuple[str, str], float] = {
    ("EURUSD", "GBPUSD"): 0.75, ("EURUSD", "XAUUSD"): 0.35, ("EURUSD", "USDJPY"): -0.30, ("EURUSD", "US500"): 0.20, ("EURUSD", "WTI"): 0.15,
    ("GBPUSD", "XAUUSD"): 0.30, ("GBPUSD", "USDJPY"): -0.25, ("GBPUSD", "US500"): 0.25,
    ("XAUUSD", "USDJPY"): -0.35, ("XAUUSD", "US500"): 0.05, ("XAUUSD", "WTI"): 0.20,
    ("USDJPY", "US500"): 0.35, ("USDJPY", "WTI"): 0.10,
    ("US500", "WTI"): 0.30, ("US500", "NAS100"): 0.90, ("US500", "BTCUSD"): 0.40, ("NAS100", "BTCUSD"): 0.45,
}


def correlation(a: str, b: str, table: Optional[dict[tuple[str, str], float]] = None) -> float:
    if a == b:
        return 1.0
    table = table or DEFAULT_CORRELATION
    return table.get((a, b), table.get((b, a), 0.0))


def get_market(symbol: str) -> MarketSpec:
    try:
        return MARKETS[symbol.upper()]
    except KeyError as e:
        raise KeyError(f"mercado desconhecido: {symbol} (disponíveis: {', '.join(MARKETS)})") from e
