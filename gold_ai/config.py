"""Configuração do motor: pesos, limiares e horizontes (Diretriz §19, §21, §27, §28, §37)."""

from __future__ import annotations

from dataclasses import dataclass, field


# Peso máximo de cada fator no GOLD AI SCORE. A soma é 100, de modo que o
# score final fica naturalmente em -100..+100 (Diretriz §19).
DEFAULT_WEIGHTS: dict[str, int] = {
    "dolar": 15,
    "juros_reais": 18,
    "fed": 12,
    "inflacao": 8,
    "geopolitica": 10,
    "fluxo": 7,
    "cot": 5,
    "opcoes": 6,
    "tecnico": 13,
    "sentimento": 6,
}

# Agrupamento dos fatores em famílias INDEPENDENTES para o filtro de
# confirmações (Diretriz §27): um sinal forte exige >= 3 famílias alinhadas.
FACTOR_FAMILIES: dict[str, tuple[str, ...]] = {
    "macro": ("fed", "inflacao"),
    "juros": ("juros_reais",),
    "dolar": ("dolar",),
    "fluxo": ("fluxo", "cot", "opcoes"),
    "tecnico": ("tecnico",),
    "sentimento": ("sentimento",),
    "geopolitica": ("geopolitica",),
}

# Pesos dos timeframes na nota técnica agregada (Diretriz §18).
TIMEFRAME_WEIGHTS: dict[str, float] = {
    "M1": 0.04,
    "M5": 0.08,
    "M15": 0.10,
    "M30": 0.12,
    "H1": 0.16,
    "H4": 0.20,
    "D1": 0.20,
    "W1": 0.10,
}

TIMEFRAME_GROUPS: dict[str, tuple[str, ...]] = {
    "microestrutura": ("M1", "M5", "M15"),
    "intraday": ("M30", "H1"),
    "swing": ("H4", "D1"),
    "macrotendencia": ("D1", "W1"),
}

# Horizontes de previsão (Diretriz §21).
HORIZONS: dict[str, str] = {
    "curtissimo": "5–30 min",
    "curto": "30 min–4h",
    "intraday": "4–24h",
    "swing": "1–5 dias",
    "macro": "1–4 semanas",
}


@dataclass
class EngineConfig:
    weights: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))

    # Classificação por score (Diretriz §28).
    strong_buy: int = 70
    buy: int = 50
    sell: int = -50
    strong_sell: int = -70

    # Filtro contra falsos sinais (Diretriz §27).
    min_confirmations: int = 3
    # Contribuição mínima (fração do peso máximo) para uma família "confirmar".
    family_confirmation_ratio: float = 0.35

    # Pré-movimento (Diretriz §14, §22): fundamentos fortes e preço ainda não confirmou.
    premove_fundamental_threshold: float = 35.0  # score fundamental (sem técnico) em -100..+100
    premove_price_confirm_ratio: float = 0.35     # nota técnica abaixo disso = "não confirmou"
    chase_atr_multiple: float = 2.0               # movimento > N ATR = estágio 3 (não perseguir)

    # Reversão (Diretriz §16, §26).
    reversal_risk_threshold: float = 60.0

    # Probabilidades: inclinação da logística que converte score em prob.
    prob_slope: float = 0.045
    lateral_base: float = 0.12

    # Anti-spam (Diretriz §37).
    min_score_change_to_alert: int = 20
    min_seconds_between_alerts: int = 900

    # Risco sistêmico excepcional (Diretriz §11, §37 item 7).
    exceptional_systemic_risk: float = 75.0

    # Janela (min) antes de evento de alto impacto em que a confiança é penalizada.
    event_window_minutes: int = 90
