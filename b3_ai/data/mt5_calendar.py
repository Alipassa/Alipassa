"""Calendário econômico do MetaTrader, exportado pelo robô B3AI_Calendario (b3_ai/mql5/B3AI_Calendario.mq5).

O robô roda dentro do terminal (somente leitura) e grava a cada 30 s o arquivo `b3ai_calendario.csv` na pasta comum dos
terminais MetaTrader (Terminal\\Common\\Files). Este módulo lê esse arquivo: eventos de importância moderada e alta de EUA,
Brasil, Zona do Euro e China, com horário, valor esperado, anterior e — depois da divulgação — o valor divulgado.

`python -m b3_ai instalar-calendario` copia o robô para a pasta do seu MetaTrader e o compila.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Optional

FILE = "b3ai_calendario.csv"
PAISES = {"US": "EUA", "BR": "Brasil", "EU": "Zona do Euro", "CN": "China"}
IMPACTO = {1: 1, 2: -1}          # ENUM_CALENDAR_EVENT_IMPACT: 1 = positivo para a moeda do país, 2 = negativo
MQ5 = os.path.join(os.path.dirname(os.path.dirname(__file__)), "mql5", "B3AI_Calendario.mq5")


def candidate_paths(common_dir: Optional[str] = None) -> list[str]:
    out = []
    if os.environ.get("B3AI_CALENDARIO"):
        out.append(os.environ["B3AI_CALENDARIO"])
    if common_dir:
        out.append(os.path.join(common_dir, "Files", FILE))
    if os.environ.get("APPDATA"):
        out.append(os.path.join(os.environ["APPDATA"], "MetaQuotes", "Terminal", "Common", "Files", FILE))
    return out


def _f(x: str) -> Optional[float]:
    x = (x or "").strip()
    if not x:
        return None
    try:
        return float(x.replace(",", "."))
    except ValueError:
        return None


def parse(text: str) -> dict:
    lines = [ln for ln in text.replace("﻿", "").splitlines() if ln.strip()]
    gerado = None
    events = []
    for ln in lines:
        parts = ln.split(";")
        if parts[0] == "gerado_utc":
            gerado = float(parts[1]) if len(parts) > 1 and parts[1].strip() else None
            continue
        if parts[0] == "utc" or len(parts) < 14:
            continue
        try:
            ts = float(parts[0])
        except ValueError:
            continue
        events.append({"ts": ts, "pais": parts[1], "pais_nome": PAISES.get(parts[1], parts[1]), "moeda": parts[2],
                       "importancia": int(parts[3] or 0), "nome": parts[4].strip(), "divulgado": _f(parts[5]),
                       "esperado": _f(parts[6]), "anterior": _f(parts[7]), "anterior_revisado": _f(parts[8]),
                       "impacto": int(parts[9] or 0), "unidade": int(parts[10] or 0), "multiplicador": int(parts[11] or 0),
                       "evento_id": parts[12], "valor_id": parts[13].strip()})
    events.sort(key=lambda e: e["ts"])
    return {"gerado": gerado, "eventos": events}


def read(common_dir: Optional[str] = None) -> dict:
    """{"ok", "eventos", "gerado", "idade_s", "caminho", "erro"} — nunca levanta exceção."""
    tried = candidate_paths(common_dir)
    for path in tried:
        if not os.path.exists(path):
            continue
        try:
            raw = open(path, "rb").read()
            text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8", errors="replace")
            out = parse(text)
        except OSError as e:
            return {"ok": False, "eventos": [], "caminho": path, "erro": f"não consegui ler o calendário: {e}"}
        idade = time.time() - (out["gerado"] or os.path.getmtime(path))
        out.update({"ok": True, "caminho": path, "idade_s": idade,
                    "erro": "" if idade < 600 else f"calendário do MetaTrader parado há {idade / 60:.0f} min — o robô B3AI_Calendario está num gráfico aberto?"})
        return out
    return {"ok": False, "eventos": [], "caminho": tried[0] if tried else "",
            "erro": "calendário do MetaTrader não encontrado — rode instalar_calendario.bat e coloque o robô B3AI_Calendario num gráfico"}


def fmt(v: Optional[float], unidade: int = 0) -> str:
    """Valor do calendário em texto curto (unidade 1 = %, demais sem símbolo)."""
    if v is None:
        return "—"
    s = f"{v:,.2f}".rstrip("0").rstrip(".") if abs(v) < 1e5 else f"{v:,.0f}"
    s = s.replace(",", "X").replace(".", ",").replace("X", ".")
    return s + ("%" if unidade == 1 else "")


def effect_on(e: dict) -> dict:
    """Efeito esperado do dado divulgado no mini dólar e no mini índice, pelo impacto que o calendário atribui à moeda do país.
    USD forte → dólar sobe no mundo e aqui (WDO ↑, WIN ↓). BRL forte → real valoriza (WDO ↓, WIN ↑). Outras moedas: sem direção."""
    s = IMPACTO.get(e.get("impacto", 0), 0)
    if not s or e.get("moeda") not in ("USD", "BRL"):
        return {"WDO": 0, "WIN": 0}
    if e["moeda"] == "USD":
        return {"WDO": s, "WIN": -s}
    return {"WDO": -s, "WIN": s}


def install(data_dir: Optional[str], install_dir: Optional[str]) -> list[str]:
    """Copia o robô para <pasta de dados>\\MQL5\\Experts e compila com o MetaEditor que vem junto com o terminal."""
    msgs = []
    if not data_dir:
        return ["não encontrei a pasta de dados do MetaTrader (o terminal está aberto e logado?)"]
    dst_dir = os.path.join(data_dir, "MQL5", "Experts")
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, "B3AI_Calendario.mq5")
    shutil.copyfile(MQ5, dst)
    msgs.append(f"robô copiado para {dst}")
    editor = os.path.join(install_dir or "", "metaeditor64.exe")
    if install_dir and os.path.exists(editor):
        log = os.path.join(dst_dir, "B3AI_Calendario.log")
        subprocess.run([editor, f"/compile:{dst}", f"/log:{log}"], timeout=120, check=False)
        ok = os.path.exists(dst[:-4] + ".ex5")
        msgs.append("compilado com sucesso" if ok else f"a compilação não gerou o .ex5 — veja {log} ou abra o arquivo no MetaEditor (F4) e aperte F7")
    else:
        msgs.append("não achei o metaeditor64.exe: abra o MetaEditor (F4 no MetaTrader), abra Experts\\B3AI_Calendario.mq5 e aperte F7")
    return msgs
