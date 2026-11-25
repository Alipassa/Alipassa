"""Configuração local: lê o `.env` da raiz do projeto (o mesmo do resto do repositório) e acha o terminal MT5.

O ambiente do Windows tem prioridade sobre o `.env`. Sem MT5_PATH em nenhum dos dois, usa o terminal da Pepperstone
no caminho padrão, se existir.
"""

from __future__ import annotations

import os
from typing import Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_MT5_PATHS = (
    r"C:\Program Files\Pepperstone MetaTrader 5\terminal64.exe",
    r"C:\Program Files\MetaTrader 5\terminal64.exe",
)


def load_env(path: Optional[str] = None) -> dict:
    """KEY=VALUE por linha (# comenta; aspas opcionais). Não sobrescreve o que já está no ambiente."""
    path = path or os.path.join(ROOT, ".env")
    loaded: dict = {}
    if not os.path.exists(path):
        return loaded
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            elif " #" in v:
                v = v.split(" #", 1)[0].rstrip()
            if k and k not in os.environ:
                os.environ[k] = v
                loaded[k] = v
    return loaded


def clean_path(p: Optional[str]) -> Optional[str]:
    """Aceita o caminho como o Windows copia: com aspas, só a pasta, ou apontando para o .exe."""
    if not p:
        return None
    p = p.strip().strip('"').strip("'").strip()
    if p and not p.lower().endswith(".exe"):
        p = os.path.join(p, "terminal64.exe") if "\\" in p or "/" in p else p
    return p or None


def mt5_path(exists=os.path.exists) -> Optional[str]:
    p = clean_path(os.environ.get("MT5_PATH") or os.environ.get("CAMINHO_MT5"))
    if p:
        return p
    return next((d for d in DEFAULT_MT5_PATHS if exists(d)), None)
