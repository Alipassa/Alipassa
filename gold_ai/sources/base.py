"""Interface de fonte de dados.

Para conectar dados reais (MT5, APIs de notícias, FRED, CFTC/COT, CME, ETFs),
implemente `snapshot()` devolvendo um `MarketSnapshot` preenchido. Campos
ausentes podem ficar `None`: o motor reduz a confiança em vez de falhar.
"""

from __future__ import annotations

from typing import Protocol

from ..models import MarketSnapshot


class DataSource(Protocol):
    def snapshot(self) -> MarketSnapshot:  # pragma: no cover - interface
        ...
