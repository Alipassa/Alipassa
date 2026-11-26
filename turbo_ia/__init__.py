"""TURBO IA — painel local: botão "Iniciar IA" analisa o gráfico e executa no MetaTrader 5.

    python -m turbo_ia                 # abre http://127.0.0.1:8765 (modo PAPER se o MT5 não estiver disponível)
    python -m turbo_ia --broker mt5    # usa o terminal MT5 aberto/logado (conta DEMO por padrão)
"""

__version__ = "1.0.0"

# No Windows, com a saída redirecionada para arquivo, o Python usa cp1252 e quebra em "×", "≈", "✔"…: força UTF-8.
import sys as _sys

for _stream in (_sys.stdout, _sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
