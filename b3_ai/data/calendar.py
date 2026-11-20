"""Agenda dos eventos que mexem com o mercado mundial e, por tabela, com o brasileiro.

Fontes reais: calendário oficial do Fed (reuniões do FOMC, com as datas passadas para medir o efeito histórico),
Banco Central (próxima decisão do Copom — a série da Selic meta é publicada até o dia da reunião) e o arquivo opcional
`dados/agenda.json` para o que você quiser acrescentar (CPI, PCE, balanços...). O payroll entra pela regra da
"primeira sexta-feira do mês" e vem marcado como data estimada.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta

from .http import DataError, fetch, get_json

FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
MONTHS = {m: i + 1 for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August", "September",
                                          "October", "November", "December"])}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})

WHY = {
    "fomc": "Decisão de juros dos EUA: define o custo do dinheiro no mundo. Juro mais alto ou discurso duro fortalece o dólar e tira "
            "fluxo de emergentes (pressão no mini índice, alta no mini dólar); corte ou tom brando faz o contrário. "
            "Sai por volta das 16h de Brasília, com a B3 aberta; a entrevista do presidente do Fed vem 30 min depois.",
    "copom": "Decisão da Selic: sai depois do fechamento (após 18h30), então o efeito aparece na abertura do pregão seguinte, "
             "principalmente nos juros futuros, bancos, varejo e construção.",
    "payroll": "Emprego dos EUA (payroll): dado forte adia cortes de juros do Fed (dólar e Treasuries sobem); fraco faz o oposto. "
               "Sai às 10h30 de Brasília e costuma dar o tom do dia.",
}


def _easter(y: int) -> date:
    """Domingo de Páscoa (algoritmo gregoriano anônimo)."""
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l_ = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l_) // 451
    month = (h + l_ - 7 * m + 114) // 31
    return date(y, month, (h + l_ - 7 * m + 114) % 31 + 1)


def b3_holidays(y: int) -> set[str]:
    """Dias sem pregão na B3: feriados nacionais (inclui 20/11, Consciência Negra, desde 2024), Carnaval (segunda e terça),
    Sexta-feira Santa, Corpus Christi, 24/12 e 31/12. Datas extras (ex.: feriado novo) em B3AI_FERIADOS=AAAA-MM-DD,AAAA-MM-DD.
    Confira o calendário oficial da B3 para o ano: esta lista segue a regra geral."""
    e = _easter(y)
    days = {date(y, 1, 1), date(y, 4, 21), date(y, 5, 1), date(y, 9, 7), date(y, 10, 12), date(y, 11, 2), date(y, 11, 15),
            date(y, 12, 25), date(y, 12, 24), date(y, 12, 31), e - timedelta(days=48), e - timedelta(days=47), e - timedelta(days=2),
            e + timedelta(days=60)}
    if y >= 2024:
        days.add(date(y, 11, 20))
    out = {d.isoformat() for d in days}
    out |= {x.strip() for x in os.environ.get("B3AI_FERIADOS", "").split(",") if x.strip().startswith(str(y))}
    return out


def is_b3_day(d: date) -> bool:
    return d.weekday() < 5 and d.isoformat() not in b3_holidays(d.year)


def parse_fomc(html_text: str) -> list[dict]:
    """Datas de decisão (último dia de cada reunião) de todos os anos listados na página."""
    out = []
    heads = [(m.start(), int(m.group(1))) for m in re.finditer(r"(\d{4}) FOMC Meetings", html_text)]
    for i, (pos, year) in enumerate(heads):
        nxt = min([p for p, _ in heads if p > pos] or [len(html_text)])
        seg = html_text[pos:nxt]
        months = re.findall(r"fomc-meeting__month[^>]*>\s*(?:<strong>)?\s*([A-Za-z/]+)", seg)
        days = re.findall(r"fomc-meeting__date[^>]*>\s*([^<]+)", seg)
        for mo, dy in zip(months, days):
            if "unscheduled" in dy.lower() or "notation" in dy.lower():
                continue
            nums = re.findall(r"\d+", dy)
            mname = mo.split("/")[-1].strip()
            if not nums or mname not in MONTHS:
                continue
            try:
                out.append({"data": date(year, MONTHS[mname], int(nums[-1])).isoformat(), "projecoes": "*" in dy})
            except ValueError:
                continue
    return sorted(out, key=lambda x: x["data"])


def fetch_fomc() -> list[dict]:
    return parse_fomc(fetch(FOMC_URL, timeout=25, retries=1))


def next_copom() -> str | None:
    rows = get_json("https://api.bcb.gov.br/dados/serie/bcdata.sgs.432/dados/ultimos/1?formato=json", timeout=12, retries=1)
    try:
        d = datetime.strptime(rows[-1]["data"], "%d/%m/%Y").date()
    except (KeyError, IndexError, ValueError, TypeError):
        return None
    return d.isoformat() if d >= date.today() else None


def first_fridays(start: date, months: int = 3) -> list[str]:
    out = []
    y, m = start.year, start.month
    for _ in range(months):
        d = date(y, m, 1)
        d += timedelta(days=(4 - d.weekday()) % 7)
        if d.month == 1 and d.day == 1:
            d += timedelta(days=7)         # 1º de janeiro é feriado nos EUA: o payroll sai na sexta seguinte
        if d >= start:
            out.append(d.isoformat())
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def load_manual(data_dir: str) -> list[dict]:
    path = os.path.join(data_dir, "agenda.json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            rows = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    out = []
    for r in rows if isinstance(rows, list) else rows.get("eventos", []):
        if isinstance(r, dict) and r.get("data") and r.get("evento"):
            out.append({"data": str(r["data"])[:10], "hora": r.get("hora", ""), "pais": r.get("pais", ""), "evento": r["evento"],
                        "impacto": r.get("impacto", "alto"), "porque": r.get("porque", ""), "fonte": "dados/agenda.json",
                        "estimada": False, "tipo": "manual"})
    return out


def build(data_dir: str, horizon_days: int = 60) -> dict:
    """{"eventos": próximos eventos ordenados, "fomc_passados": datas para o estudo histórico, "erros": [...]}"""
    today = date.today()
    limit = (today + timedelta(days=horizon_days)).isoformat()
    events, errors, past = [], [], []
    try:
        for f in fetch_fomc():
            if f["data"] < today.isoformat():
                past.append(f["data"])
            elif f["data"] <= limit:
                events.append({"data": f["data"], "hora": "≈16:00", "pais": "EUA", "tipo": "fomc", "impacto": "muito alto",
                               "evento": "Decisão de juros dos EUA (Fed/FOMC)" + (" + projeções econômicas" if f["projecoes"] else ""),
                               "porque": WHY["fomc"], "fonte": "Federal Reserve", "estimada": False})
    except DataError as e:
        errors.append(f"agenda do Fed: {e}")
    try:
        c = next_copom()
        if c and c <= limit:
            events.append({"data": c, "hora": "após 18:30", "pais": "Brasil", "tipo": "copom", "impacto": "muito alto",
                           "evento": "Decisão da Selic (Copom)", "porque": WHY["copom"], "fonte": "Banco Central", "estimada": False})
    except DataError as e:
        errors.append(f"agenda do Copom: {e}")
    for d in first_fridays(today):
        if d <= limit:
            events.append({"data": d, "hora": "10:30", "pais": "EUA", "tipo": "payroll", "impacto": "alto",
                           "evento": "Payroll — emprego dos EUA", "porque": WHY["payroll"],
                           "fonte": "regra da 1ª sexta-feira (confira a data oficial)", "estimada": True})
    events += [e for e in load_manual(data_dir) if today.isoformat() <= e["data"] <= limit]
    for e in events:
        e["dias"] = (date.fromisoformat(e["data"]) - today).days
    events.sort(key=lambda e: (e["data"], e.get("hora", "")))
    return {"eventos": events, "fomc_passados": past, "erros": errors}
