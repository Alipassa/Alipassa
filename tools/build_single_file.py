#!/usr/bin/env python3
"""Gera o entrypoint único `market_ai_engine_v5.py` a partir do pacote `gold_ai/`.

Uso:  python tools/build_single_file.py
O arquivo gerado é o ÚNICO bundle suportado; versões anteriores (v1/v2) foram removidas
para evitar execução acidental da versão errada. Bundles v1/v2/v3 foram removidos.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "market_ai_engine_v5.py"
VERSION = "5.0.0"

ORDER = ["config", "models", "technical", "factors", "premove", "events", "evidence", "signals", "memory", "telegram", "engine", "report",
         "sources/sample", "data/http", "data/yahoo", "data/fred", "data/cftc", "data/news", "data/engine", "data/mt5", "data/dukascopy", "trading", "monitor",
         "markets", "news_engine", "reaction", "reaction_hires", "flow_anomaly", "history", "data/history_sources", "execution", "guard", "validation", "evaluation", "opportunity", "selector", "edge_report", "estimate", "sweep", "ablation", "doctor", "live_engine", "data/multi", "market_engine", "cli"]

HEADER = f'''#!/usr/bin/env python3
"""MARKET AI ENGINE 5.0 — informação explícita (news/macro) + informação IMPLÍCITA (fluxo anômalo) · reação temporal · propagação entre ativos.

Gerado por tools/build_single_file.py a partir do pacote gold_ai/ (versão {VERSION}).
Equivalente a `python -m gold_ai`. Único bundle suportado (v1–v4 removidos). 5.0 = núcleo 4.0 + FLOW ANOMALY ENGINE + REACTION ENGINE.

5.0 — FLOW ANOMALY ENGINE: "existe um movimento que revela uma informação que ainda não conhecemos?" FLOW SCORE 0–100,
assinaturas A/B/C, origem A–E (NUNCA 'compra de banco central': fluxo institucional provável, origem desconhecida), evento
IMPLÍCITO no REACTION ENGINE → relógio nos atrasados → LEAD-LAG → PRESSÃO LATENTE → PRE-MOVE → OPPORTUNITY → ASSET SELECTOR.
    python market_ai_engine_v5.py flow --markets XAUUSD,US500,EURUSD,USDJPY,WTI   # FLOW SCORE agora + propagação

REGRA CENTRAL: maximizar o aproveitamento das oportunidades estatisticamente válidas, a expectancy e o potencial de ganho,
mantendo o risco controlado — sem sacrificar captura de oportunidades em busca de uma taxa de acerto artificialmente alta.
Alvo = acerto + captura + expectancy + ganho + controle de drawdown (docs/MISSAO.md).

"Analisar vários mercados simultaneamente e operar somente aquele que apresentar a melhor vantagem
estatística disponível naquele momento, respeitando risco, correlação, qualidade dos dados e custo de
execução." A IA não precisa operar ouro; precisa encontrar onde existe vantagem.

XAUUSD · EURUSD · US500 · USDJPY · WTI (fase 1) → 📡 DATA ENGINE (macro uma vez + candles por mercado)
→ 🧠 PREDICTION ENGINE (cérebro único; cada mercado declara o sinal de cada fator)
→ 🔥 OPPORTUNITY ENGINE → 🏆 ASSET SELECTOR (histórico ajustado à amostra × oportunidade atual × decay)
→ 📐 PORTFOLIO EXPOSURE (correlação; mesma aposta três vezes ≠ diversificação) → RISK ENGINE (capital único)
→ TRADE ENGINE → MT5 → confirmação → 🔄 TRADE MONITOR 24/7 → ADAPTIVE EXIT → resultado → capital

🌎 MUNDO → 📡 DATA ENGINE (Yahoo · FRED · CFTC · RSS · calendário · MetaTrader 5)
→ MARKET SNAPSHOT → 🧠 PREDICTION ENGINE (score · probabilidade · confiança · pré-movimento)
→ CADEIA DE RACIOCÍNIO · NÍVEL DE EVIDÊNCIA · VANTAGEM ESTATÍSTICA ("NÃO SEI")
→ DECISION ENGINE → 🧾 TRADE ENGINE (stop inteligente · 1R–4R · MAX PROFIT ENGINE · viabilidade)
→ RISK ENGINE (capital → risco % fixo → lote; TRADING STOP; drawdown; kill switch)
→ EXECUTION ENGINE (MT5 → broker → confirmação real · EXECUTION MISMATCH)
→ 🔄 TRADE MONITOR (TRADE/THESIS/EXIT SCORE · PROFIT POTENTIAL · MANTER/PROTEGER/REDUZIR/ESTENDER/ENCERRAR)
→ RESULTADO → SQLite → PERFORMANCE → NOVO CAPITAL → NOVO LOTE
→ VALIDATION (anti look-ahead · walk-forward · calibração · score por fator · lead time · MFE/MAE)

Modos: 🟢 PAPER (padrão) · 🟡 AUTHORIZE · 🟠 SEMI-LIVE · 🔴 LIVE (exige --authorize)
Comandos Telegram: /STOP /PAUSE /RESUME /STATUS /CLOSE (com /CLOSE CONFIRM)

Uso (4.0, multi-mercado):
    python market_ai_engine_v5.py markets                                              # ranking agora, não opera
    python market_ai_engine_v5.py edge                                                 # 🚨 LIVE EDGE — o teste definitivo (o que foi vivido)
    python market_ai_engine_v5.py estimate --start 2026-01-01 --markets EURUSD,US500,XAUUSD,USDJPY,WTI --equity 10000   # estimativa de lucro OOS
    python market_ai_engine_v5.py sweep --start 2026-01-01 --market US500        # piso de vantagem escolhido no treino de cada fold
    python market_ai_engine_v5.py history fetch-alfred|fetch-te|fetch-gdelt      # banco histórico point-in-time de eventos/notícias
    python market_ai_engine_v5.py compare-news --start 2026-01-01 --markets US500,XAUUSD   # Preço só × +Macro (A) × +Macro+News (B)
    python market_ai_engine_v5.py live --markets EURUSD,US500,XAUUSD,USDJPY,WTI --source mt5 --mode paper --send
    python market_ai_engine_v5.py validate --markets EURUSD,US500,XAUUSD,USDJPY,WTI [--csv-dir dados/]
Uso (3.0, um mercado):
    python market_ai_engine_v5.py live --source mt5 --mode paper|authorize|semi-live|live [--authorize] --send
    python market_ai_engine_v5.py status | stats | validate | simulate | calibrate | backtest | metrics | demo | event

Credenciais e limites no .env (ver .env.example). Sem dependências externas (MetaTrader5 opcional, Windows).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import random
import re
import sqlite3
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, fields
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Optional, Protocol, Sequence

try:  # MetaTrader5 só existe no Windows com o terminal instalado
    import MetaTrader5 as _mt5  # type: ignore
except Exception:  # noqa: BLE001
    _mt5 = None

__version__ = "{VERSION}"
'''

SKIP = re.compile(r"^\s*(from (\.|\.\.)[\w.]* import|from __future__|import (argparse|csv|hashlib|json|math|os|random|re|sqlite3|statistics|sys|time|"
                  r"urllib\.error|urllib\.parse|urllib\.request)$|import xml\.etree|from dataclasses import|from datetime import|from email\.utils import|"
                  r"from enum import|from types import|from typing import)")

FOOTER = '''

# ============================================================================
# INTERFACE DE FONTE DE DADOS
# ============================================================================

class DataSource(Protocol):
    """Qualquer objeto com snapshot() -> MarketSnapshot (LiveSource, MT5Source, SampleSource ou o seu)."""

    def snapshot(self) -> MarketSnapshot:  # pragma: no cover
        ...


if __name__ == "__main__":
    sys.exit(main())
'''


def strip_imports(text: str) -> str:
    out, multi = [], False
    for ln in text.splitlines():
        if multi:
            if ln.strip().endswith(")"):
                multi = False
            continue
        if SKIP.match(ln):
            if "(" in ln and ")" not in ln:   # import multilinha (parêntese aberto)
                multi = True
            continue
        out.append(ln)
    return "\n".join(out).strip("\n")


def build() -> str:
    parts = [HEADER]
    for name in ORDER:
        body = strip_imports((ROOT / "gold_ai" / f"{name}.py").read_text(encoding="utf-8"))
        if name == "technical":
            body += "\n\n\n_atr = atr  # alias usado pelo Data Engine, MT5 e avaliação"
        if name == "factors":
            body = body.replace('def _clip(x: float, lo: float, hi: float) -> float:\n    return max(lo, min(hi, x))\n\n\n', "")
        if name == "data/mt5":
            body = re.sub(r"try:  # pragma: no cover.*?_mt5 = None\n", "", body, flags=re.S)
        if name == "trading":
            body = body.replace("class TradingMode(str, Enum):", "class TradingMode22(str, Enum):  # modos do 2.2 (PositionManager); o 3.0 usa guard.TradingMode")
            body = body.replace("mode: TradingMode = TradingMode.PAPER", "mode: TradingMode22 = TradingMode22.PAPER")
            body = body.replace("def __init__(self, mode: TradingMode,", "def __init__(self, mode: TradingMode22,")
            body = body.replace("self.mode == TradingMode.", "self.mode == TradingMode22.")
        if name == "cli":
            body = body.replace('if __name__ == "__main__":\n    sys.exit(main())', "")
        title = name.replace("sources/", "").replace("data/", "DATA · ").upper()
        parts.append(f"\n\n# {'=' * 76}\n# {title}\n# {'=' * 76}\n\n{body}\n")
    parts.append(FOOTER)
    return "".join(parts)


if __name__ == "__main__":
    text = build()
    compile(text, str(OUT), "exec")  # falha cedo se o bundle não for Python válido
    OUT.write_text(text, encoding="utf-8")
    print(f"{OUT.name}: {len(text.splitlines())} linhas, versão {VERSION}")
    if re.search(r"^\s*from \.", text, re.M):
        print("ATENÇÃO: sobraram imports relativos", file=sys.stderr)
        sys.exit(1)
