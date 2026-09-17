"""GOLD AI ENGINE — Centro Global de Inteligência do Ouro (XAU/USD).

Motor probabilístico de antecipação: transforma mundo → macro → mercado →
comportamento → previsão em SCORE (-100..+100), PROBABILIDADE e CONFIANÇA,
com detecção de pré-movimento, reversão, filtro anti-falso-sinal, anti-spam,
registro de previsões e aprendizado — e, na 3.0, execução no MetaTrader 5
com gestão de risco, kill switch e monitor da operação.
"""

from .models import (
    Assessment,
    Candle,
    Direction,
    EconomicEvent,
    EvidenceLevel,
    FactorScore,
    MarketSnapshot,
    NewsItem,
    Signal,
    SignalType,
    Stage,
)
from .engine import GoldAIEngine
from .config import EngineConfig

__all__ = [
    "Assessment",
    "Candle",
    "Direction",
    "EconomicEvent",
    "EngineConfig",
    "EvidenceLevel",
    "FactorScore",
    "GoldAIEngine",
    "MarketSnapshot",
    "NewsItem",
    "Signal",
    "SignalType",
    "Stage",
]

__version__ = "3.0.0"
