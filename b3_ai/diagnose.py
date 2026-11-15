"""Diagnóstico de um clique: Python, MetaTrader 5 e fontes da internet. Grava diagnostico.txt para enviar ao suporte.

    python -m b3_ai diagnostico

Não grava número de conta, senha nem saldo: só o que é preciso para descobrir por que algo não conectou.
"""

from __future__ import annotations

import os
import platform
import struct
import sys
import time


def run(data_dir: str = "dados") -> str:
    L: list[str] = ["DIAGNOSTICO B3 AI — " + time.strftime("%d/%m/%Y %H:%M:%S")]

    def add(ok, name: str, detail: str = "") -> None:
        L.append(f"[{'OK' if ok else 'FALHOU' if ok is False else '--'}] {name}" + (f": {detail}" if detail else ""))

    bits = struct.calcsize("P") * 8
    add(sys.version_info >= (3, 10), "Python", f"{platform.python_version()} · {bits} bits · {platform.system()} {platform.release()}")
    if bits != 64:
        add(False, "Python de 64 bits", "o MetaTrader 5 exige Python de 64 bits — reinstale o Python (Windows installer 64-bit)")
    L.append("")
    L.append("== MetaTrader 5 ==")
    path = os.environ.get("MT5_PATH", "")
    add(None, "MT5_PATH", path or "(não definido: usa o terminal padrão)")
    if path:
        add(os.path.exists(path), "arquivo do terminal existe", path)
    try:
        import MetaTrader5 as mt5  # type: ignore
        add(True, "pacote MetaTrader5", getattr(mt5, "__version__", "?"))
    except Exception as e:  # noqa: BLE001
        mt5 = None
        add(False, "pacote MetaTrader5", f"{type(e).__name__}: {e} — instale com: python -m pip install MetaTrader5")
    if mt5 is not None:
        try:
            ok = mt5.initialize(**({"path": path} if path else {}))
            add(bool(ok), "conexão com o terminal", "" if ok else f"{mt5.last_error()} — abra o MetaTrader 5, faça login e deixe aberto")
            if ok:
                ti, ai = mt5.terminal_info(), mt5.account_info()
                add(bool(getattr(ti, "connected", False)), "terminal conectado ao servidor",
                    f"{getattr(ti, 'name', '?')} · {getattr(ti, 'company', '?')}")
                add(ai is not None, "conta logada", f"servidor {getattr(ai, 'server', '?')}" if ai else "nenhuma conta logada no terminal")
                for key in ("WIN", "WDO"):
                    names = [s.name for s in (mt5.symbols_get(group=f"{key}*") or [])]
                    add(bool(names), f"símbolos {key}* no terminal", f"{len(names)} — " + ", ".join(names[:25]))
                from .data.mt5 import MT5Feed
                feed = MT5Feed(mt5)
                try:
                    snap = feed.snapshot(["IBOV", "DI", "IRON"])
                    add(True, "símbolos escolhidos", str(snap["simbolos"]))
                    add(None, "fuso do servidor (h)", str(feed.offset_h))
                    for key in ("WIN", "WDO"):
                        t = snap["ticks"].get(key) or {}
                        age = time.time() - t.get("ts", 0) if t else None
                        add(bool(t), f"último negócio {key}", f"{t.get('ultimo')} · há {age / 60:.1f} min" if t else "sem tick")
                        add(True, f"barras {key}", " · ".join(f"{tf}: {len(snap[(key, tf)])}" for tf in ("1D", "15", "5")))
                    add(None, "séries extras", ", ".join(snap["extras"]) or "nenhuma")
                except Exception as e:  # noqa: BLE001
                    add(False, "leitura de WIN/WDO", f"{type(e).__name__}: {e}")
                try:
                    t0 = time.time()
                    acoes = feed.stocks(["PETR4", "VALE3", "ITUB4", "BBAS3", "MGLU3", "WEGE3", "ABEV3", "BBDC4"], budget_s=20)
                    add(len(acoes) >= 5, "ações da B3 pelo MetaTrader",
                        f"{len(acoes)} de 8 lidas em {time.time() - t0:.1f}s — " + ", ".join(f"{k} {v['p']:.2f} ({v['c']:+.2f}%)" for k, v in acoes.items()))
                except Exception as e:  # noqa: BLE001
                    add(False, "ações da B3 pelo MetaTrader", f"{type(e).__name__}: {e}")
                for word in ("FERRO", "IRON", "MINERIO", "WSP", "IBOV", "DI1"):
                    names = [s.name for s in (mt5.symbols_get(group=f"*{word}*") or [])]
                    if names:
                        add(None, f"símbolos com '{word}'", ", ".join(names[:12]))
                mt5.shutdown()
        except Exception as e:  # noqa: BLE001
            add(False, "MetaTrader 5", f"{type(e).__name__}: {e}")
    L.append("")
    L.append("== Fontes da internet ==")
    try:
        from . import audit
        from .engine import Engine
        for name, ok, detail in audit.check_sources(Engine(data_dir, fonte="web"))[:-1]:
            add(ok, name, detail)
    except Exception as e:  # noqa: BLE001
        add(False, "fontes", f"{type(e).__name__}: {e}")
    return "\n".join(L) + "\n"
