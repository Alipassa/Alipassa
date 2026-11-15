"""MetaTrader 5 — WIN e WDO em TEMPO REAL direto do terminal da corretora (somente leitura: nunca envia ordens).

Requer Windows, o MetaTrader 5 da corretora aberto e logado, e o pacote oficial:
    pip install MetaTrader5

Ative com `python -m b3_ai painel --fonte mt5` (ou B3AI_FONTE=mt5 no .env). Variáveis opcionais:
    MT5_PATH   caminho do terminal64.exe (só se houver mais de um MetaTrader instalado)
    MT5_WIN    símbolo do mini índice na corretora (padrão: descobre sozinho — WIN@, WIN$N, WIN$...)
    MT5_WDO    símbolo do mini dólar
    MT5_UTC_OFFSET_HOURS   fuso do servidor (B3: -3, detectado pelo último tick quando o pregão está aberto)
    MT5_<ATIVO>            qualquer outra série do painel pelo MetaTrader, se a corretora tiver o símbolo. Ex.:
                           MT5_IRON=<símbolo do minério de ferro>, MT5_BRENT=..., MT5_ES=WSP$N. Ibovespa (IBOV) e
                           DI futuro (DI1$N) são procurados sozinhos.

A biblioteca oficial do MetaTrader para Python entrega preços, barras e ticks; ela NÃO dá acesso às notícias do
terminal, por isso as notícias do painel continuam vindo dos feeds públicos.

O ProfitChart/XPTrader (Nelogica) NÃO é MetaTrader e não oferece esta interface: é preciso o terminal MetaTrader 5.
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional

from .http import DataError

try:  # pragma: no cover — só existe no Windows com o terminal instalado
    import MetaTrader5 as _mt5  # type: ignore
except Exception:  # noqa: BLE001
    _mt5 = None

# XP: "WIN@" (série contínua por vencimento, ajuste proporcional). Outras corretoras: "WIN$N", "WIN$"...
CANDIDATES = {"WIN": ["WIN@", "WIN$N", "WIN$", "WIN@N", "WIN$D", "WINFUT"], "WDO": ["WDO@", "WDO$N", "WDO$", "WDO@N", "WDO$D", "WDOFUT"]}
EXTRAS = {"IBOV": ["IBOV"], "DI": ["DI1@", "DI1$N", "DI1$", "DI1@N"]}      # procurados sozinhos; os demais só por MT5_<ATIVO>
TF = {"1D": "TIMEFRAME_D1", "5": "TIMEFRAME_M5", "15": "TIMEFRAME_M15"}
BARS = {"1D": 1800, "5": 20000, "15": 12000}       # o terminal entrega muito mais histórico intraday que o feed público


class MT5Feed:
    def __init__(self, mt5: Any = None, env: Optional[dict] = None) -> None:
        self.mt5 = mt5 or _mt5
        self.env = env if env is not None else os.environ
        self.connected = False
        self.symbols: dict[str, str] = {}
        self.offset_h = -3.0
        self.note = ""

    # ------------------------------------------------------------------ conexão
    def connect(self) -> None:
        if self.mt5 is None:
            raise DataError("pacote MetaTrader5 não instalado — no Windows: pip install MetaTrader5")
        kw = {"path": self.env["MT5_PATH"]} if self.env.get("MT5_PATH") else {}
        if not self.mt5.initialize(**kw):
            raise DataError(f"MetaTrader 5 não respondeu ({self.mt5.last_error()}): abra o terminal da corretora, faça login e deixe-o "
                            "aberto; se houver mais de um MetaTrader instalado, informe o caminho do terminal64.exe em MT5_PATH")
        self.connected = True
        for key in ("WIN", "WDO"):
            self.symbols[key] = self._find_symbol(key)
        self.offset_h = self._server_offset()

    def _find_symbol(self, key: str) -> str:
        forced = self.env.get(f"MT5_{key}")
        for sym in ([forced] if forced else []) + CANDIDATES[key]:
            if sym and self.mt5.symbol_select(sym, True) and self._has_bars(sym):
                return sym
        # nada dos nomes conhecidos: pega o contrato do grupo com o tick mais recente (o vencimento em negociação)
        best, best_t = "", 0.0
        for info in self.mt5.symbols_get(group=f"{key}*") or []:
            name = getattr(info, "name", "")
            if not self.mt5.symbol_select(name, True):
                continue
            t = float(getattr(self.mt5.symbol_info_tick(name), "time", 0) or 0)
            if t > best_t and self._has_bars(name):
                best, best_t = name, t
        if not best:
            raise DataError(f"não achei o símbolo do {key} no MetaTrader — informe o nome exato em MT5_{key} (ex.: {CANDIDATES[key][0]})")
        return best

    def _has_bars(self, sym: str) -> bool:
        r = self.mt5.copy_rates_from_pos(sym, self.mt5.TIMEFRAME_D1, 0, 5)
        return r is not None and len(r) > 0

    def _server_offset(self) -> float:
        forced = self.env.get("MT5_UTC_OFFSET_HOURS")
        if forced not in (None, ""):
            return float(forced)
        tick = self.mt5.symbol_info_tick(self.symbols["WIN"])
        ts = float(getattr(tick, "time", 0) or 0)
        raw = (ts - time.time()) / 3600.0
        if ts and abs(raw - round(raw)) <= 0.25 and abs(raw) <= 14:      # tick fresco: o carimbo revela o fuso do servidor
            return float(round(raw))
        return -3.0                                                      # pregão fechado: servidores da B3 usam horário de Brasília

    # ------------------------------------------------------------------ leitura
    def bars(self, key: str, tf: str) -> list[tuple]:
        """(ts UTC, o, h, l, c, volume) em ordem cronológica, no mesmo formato do feed público."""
        rates = self.mt5.copy_rates_from_pos(self.symbols[key], getattr(self.mt5, TF[tf]), 0, BARS[tf])
        if rates is None or len(rates) == 0:
            raise DataError(f"MetaTrader sem barras de {self.symbols[key]} ({tf}): {self.mt5.last_error()}")
        shift = self.offset_h * 3600
        out = []
        for r in rates:
            vol = float(r["real_volume"]) or float(r["tick_volume"])
            out.append((float(r["time"]) - shift, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), vol))
        return out

    def last_price(self, key: str) -> Optional[dict]:
        t = self.mt5.symbol_info_tick(self.symbols[key])
        if t is None:
            return None
        last = float(getattr(t, "last", 0) or 0) or (float(getattr(t, "bid", 0) or 0) + float(getattr(t, "ask", 0) or 0)) / 2
        return {"ultimo": last, "bid": float(getattr(t, "bid", 0) or 0), "ask": float(getattr(t, "ask", 0) or 0),
                "ts": float(getattr(t, "time", 0) or 0) - self.offset_h * 3600} if last else None

    def extras(self, keys: list[str]) -> dict[str, list[tuple]]:
        """Séries diárias de outros ativos do painel que existam no terminal: {chave: barras}. Falhas são ignoradas."""
        out: dict[str, list[tuple]] = {}
        for key in keys:
            forced = self.env.get(f"MT5_{key}")
            for sym in ([forced] if forced else []) + EXTRAS.get(key, []):
                try:
                    if not sym or not self.mt5.symbol_select(sym, True):
                        continue
                    self.symbols[key] = sym
                    bars = self.bars(key, "1D")
                    if len(bars) >= 300:            # histórico curto demais não serve para a regressão
                        out[key] = bars
                        break
                except Exception:  # noqa: BLE001
                    pass
                self.symbols.pop(key, None)
        return out

    def snapshot(self, extra_keys: Optional[list[str]] = None) -> dict:
        """{("WIN","1D"): barras, ("WIN","5"): ..., "ticks": {...}, "simbolos": {...}} — reconecta se o terminal caiu."""
        if not self.connected:
            self.connect()
        try:
            out: dict = {(k, tf): self.bars(k, tf) for k in ("WIN", "WDO") for tf in TF}
            out["ticks"] = {k: self.last_price(k) for k in ("WIN", "WDO")}
            out["extras"] = self.extras(extra_keys or [])
        except Exception:
            self.close()
            raise
        out["simbolos"] = dict(self.symbols)
        return out

    def close(self) -> None:
        if self.connected:
            try:
                self.mt5.shutdown()
            except Exception:  # noqa: BLE001
                pass
        self.connected = False
