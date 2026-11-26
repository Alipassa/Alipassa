"""Regras da B3 para a execução do cruzamento no mini índice (WIN).

Validação (dados da XP, 2022–2026, custo B3 completo de 13 pontos por operação, swing — a posição pode dormir):
    WIN M15 · SMA 9×20 · stop 2 ATR · sai e vira a mão no cruzamento contrário
    +229 ATR · 4 de 5 anos positivos · pior ano −27 · +0,09 ATR/op
    robustez: 22 de 25 combinações de médias positivas (regra fixada antes: ≥ 18 e o 9×20 positivo → aprovado)
    day trade (zerando às 17h50): 17 de 25 — reprovado; WDO: reprovado em tudo.

Como funciona a execução:
    • a ANÁLISE usa a série contínua (WIN@ na XP), que não tem buracos na troca de vencimento;
    • a ORDEM vai no contrato vigente (ex.: WINZ26), escolhido pelo vencimento informado pelo MT5;
    • ROLAGEM: faltando ROLL_DAYS dias para o vencimento, a posição é fechada no contrato velho e reaberta no novo,
      do mesmo lado e com a mesma distância de stop (é o que a série contínua do teste supõe);
    • ordens só entre 09h05 e 18h15 (Brasília); fora disso a IA espera (o candle não é "gasto").
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Iterable, Optional

MONTHS = "FGHJKMNQUVXZ"                       # código do mês do vencimento (F = jan … Z = dez)
CONTINUOUS = re.compile(r"^(WIN|WDO|IND|DOL)(@|\$|@N|\$N|@D|\$D|FUT)$")
CONTRACT = re.compile(r"^(WIN|WDO|IND|DOL)([FGHJKMNQUVXZ])(\d{2})$")
BRT = _dt.timezone(_dt.timedelta(hours=-3))  # Brasília, sem horário de verão desde 2019
SESSION_BRT = (9 * 60 + 5, 18 * 60 + 15)     # janela para enviar ordens (pregão 09h00–18h25, longe do leilão)
ROLL_DAYS = 3                                 # rola a posição faltando 3 dias corridos para o vencimento


def root_of(symbol: str) -> Optional[str]:
    """'WIN' para WIN@, WIN$N, WINZ26… — None fora da B3."""
    s = (symbol or "").upper()
    m = CONTINUOUS.match(s) or CONTRACT.match(s)
    return m.group(1) if m else None


def is_continuous(symbol: str) -> bool:
    return bool(CONTINUOUS.match((symbol or "").upper()))


def expiry_guess(code: str) -> Optional[float]:
    """Vencimento aproximado (UTC, fim do pregão) quando o MT5 não informa:
    índice (WIN/IND): quarta-feira mais próxima do dia 15 do mês · dólar (WDO/DOL): 1º dia útil do mês (sem feriados)."""
    m = CONTRACT.match((code or "").upper())
    if not m:
        return None
    root, month, year = m.group(1), MONTHS.index(m.group(2)) + 1, 2000 + int(m.group(3))
    if root in ("WIN", "IND"):
        d15 = _dt.date(year, month, 15)
        delta = (2 - d15.weekday() + 3) % 7 - 3            # quarta-feira (weekday 2) mais próxima: −3…+3 dias
        day = d15 + _dt.timedelta(days=delta)
    else:
        day = _dt.date(year, month, 1)
        while day.weekday() >= 5:
            day += _dt.timedelta(days=1)
    return _dt.datetime(day.year, day.month, day.day, 18, 30, tzinfo=BRT).timestamp()


def contract_code(root: str, when_utc: float) -> str:
    """Código do contrato cujo vencimento é o primeiro depois de `when_utc` (índice: meses pares; dólar: todo mês)."""
    d = _dt.datetime.fromtimestamp(when_utc, tz=BRT)
    months = range(2, 13, 2) if root in ("WIN", "IND") else range(1, 13)
    for k in range(0, 26):
        y, m = d.year + (d.month - 1 + k) // 12, (d.month - 1 + k) % 12 + 1
        if m not in months:
            continue
        code = f"{root}{MONTHS[m - 1]}{y % 100:02d}"
        exp = expiry_guess(code)
        if exp and exp > when_utc:
            return code
    raise ValueError(f"sem contrato para {root}")


def pick_contract(root: str, contracts: Iterable[tuple[str, float]], now: float, roll_days: float = ROLL_DAYS) -> str:
    """Contrato para operar AGORA: o de vencimento mais próximo que ainda está a mais de `roll_days` dias.
    `contracts` = [(nome, vencimento UTC ou 0)] vindos do MT5; sem lista (ou sem datas) usa o calendário de expiry_guess."""
    limit = now + roll_days * 86400
    best: Optional[tuple[float, str]] = None
    for name, exp in contracts:
        if root_of(name) != root or not CONTRACT.match(name.upper()):
            continue
        exp = float(exp or 0) or (expiry_guess(name) or 0)
        if exp > limit and (best is None or exp < best[0]):
            best = (exp, name)
    return best[1] if best else contract_code(root, limit)


def needs_roll(symbol: str, contracts: Iterable[tuple[str, float]], now: float, roll_days: float = ROLL_DAYS) -> bool:
    """A posição está num contrato a ≤ roll_days do vencimento?"""
    exp = next((float(e or 0) for n, e in contracts if n.upper() == symbol.upper()), 0.0) or (expiry_guess(symbol) or 0)
    return bool(exp) and exp <= now + roll_days * 86400


def session_open(now_utc: float) -> bool:
    """Dentro da janela de envio de ordens (seg–sex, 09h05–18h15 de Brasília)? Feriados: a corretora recusa e a IA tenta depois."""
    d = _dt.datetime.fromtimestamp(now_utc, tz=BRT)
    m = d.hour * 60 + d.minute
    return d.weekday() < 5 and SESSION_BRT[0] <= m <= SESSION_BRT[1]
