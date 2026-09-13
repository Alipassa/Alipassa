"""DATA ENGINE — 🌎 MERCADO REAL → COLETOR → NORMALIZAÇÃO → MarketSnapshot.

Coletores gratuitos e sem chave: Yahoo Finance (candles/quotes), FRED (juros
reais, breakeven, spreads), CFTC (COT), RSS (notícias) e calendário local.
"""

from .http import DataError, HttpClient
from .engine import DataEngine, DataEngineConfig, LiveSource

__all__ = ["DataEngine", "DataEngineConfig", "DataError", "HttpClient", "LiveSource"]
