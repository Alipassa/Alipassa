"""Fontes de inteligência (Diretriz §3). `DataSource` é a interface; `SampleSource`
gera cenários sintéticos para demonstração e testes."""

from .base import DataSource
from .sample import SampleSource, make_candles

__all__ = ["DataSource", "SampleSource", "make_candles"]
