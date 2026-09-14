"""GOLD AI ENGINE 3.0 — RISK GUARD · KILL SWITCH · PERFORMANCE ENGINE · COMANDOS.

Regras fundamentais (literalmente na especificação):
  • A IA nunca poderá aumentar o risco percentual da conta para recuperar perdas.
  • Nenhuma nova posição será aberta enquanto existir posição ativa no mesmo ativo.
  • O lote nasce de CAPITAL + RISCO + STOP + CONTRATO — nunca da confiança.
  • Perda diária ≥ MAX_DAILY_LOSS ou drawdown ≥ MAX_DRAWDOWN → 🚨 TRADING STOP.
  • TRADING_ENABLED=false (ou arquivo kill switch, ou /STOP) bloqueia novas entradas.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from .trading import RiskLimits


class TradingMode(str, Enum):
    PAPER = "PAPER"           # 🟢 tudo simulado (padrão)
    AUTHORIZE = "AUTHORIZE"   # 🟡 monta a operação e pede autorização
    SEMI_LIVE = "SEMI_LIVE"   # 🟠 entra por regras pré-autorizadas; ações críticas pedem confirmação
    LIVE = "LIVE"             # 🔴 execução totalmente automática


@dataclass
class GuardLimits(RiskLimits):
    max_drawdown_pct: float = 10.0
    min_rr_to_structure: float = 2.0     # se a resistência/suporte forte estiver antes disto (em R), não há expectativa
    daily_target_pct: float = 0.0        # META DIÁRIA (0 = desligada): ao atingir, sem novas entradas até o dia seguinte — trava, não obrigação

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "GuardLimits":
        base = RiskLimits.from_env(env)
        g = cls(**base.__dict__)
        g.max_drawdown_pct = float(env.get("MAX_DRAWDOWN", g.max_drawdown_pct))
        g.min_rr_to_structure = float(env.get("MIN_RR_TO_STRUCTURE", g.min_rr_to_structure))
        g.daily_target_pct = float(env.get("DAILY_TARGET", g.daily_target_pct) or 0.0)
        return g


@dataclass
class KillSwitch:
    """TRADING_ENABLED no ambiente/.env, arquivo sentinela e comandos /STOP /PAUSE /RESUME."""

    enabled_env: bool = True
    file_path: Optional[str] = None
    stopped: bool = False     # /STOP → sem novas entradas
    paused: bool = False      # /PAUSE → sem novas entradas nem gestão automática crítica

    @classmethod
    def from_env(cls, env: dict[str, str], file_path: Optional[str] = None) -> "KillSwitch":
        val = str(env.get("TRADING_ENABLED", "true")).strip().lower()
        return cls(enabled_env=val in ("1", "true", "yes", "on"), file_path=file_path)

    def new_entries_allowed(self) -> tuple[bool, str]:
        if not self.enabled_env:
            return False, "TRADING_ENABLED=false"
        if self.file_path and os.path.exists(self.file_path):
            return False, f"kill switch ativo ({self.file_path})"
        if self.paused:
            return False, "sistema pausado (/PAUSE)"
        if self.stopped:
            return False, "novas entradas bloqueadas (/STOP)"
        return True, "ok"


@dataclass
class PerformanceEngine:
    """Capital → risco financeiro permitido → lote. O percentual nunca muda; o valor cresce com o capital."""

    limits: GuardLimits
    equity: float
    peak_equity: float = 0.0
    day: Optional[str] = None
    daily_pnl: float = 0.0
    trading_stop: bool = False
    target_reached: bool = False      # 🎯 meta diária atingida: protege o ganho (sem novas entradas hoje)
    history: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.peak_equity = max(self.peak_equity, self.equity)

    def roll_day(self, t: datetime) -> None:
        d = t.strftime("%Y-%m-%d")
        if d != self.day:
            self.day, self.daily_pnl, self.trading_stop, self.target_reached = d, 0.0, False, False

    @property
    def daily_target_usd(self) -> Optional[float]:
        return round(self.equity_start_of_day() * self.limits.daily_target_pct / 100.0, 2) if self.limits.daily_target_pct > 0 else None

    @property
    def daily_pct(self) -> float:
        base = self.equity_start_of_day()
        return round(100.0 * self.daily_pnl / base, 2) if base else 0.0

    def r_to_target(self) -> Optional[float]:
        """Quantos R líquidos faltam para a meta com o risco atual (informativo — a meta nunca força entrada)."""
        if self.daily_target_usd is None or self.risk_usd <= 0:
            return None
        return round(max(0.0, (self.daily_target_usd - self.daily_pnl) / self.risk_usd), 2)

    @property
    def risk_usd(self) -> float:
        """Risco financeiro por operação = capital × RISK_PER_TRADE. Único ponto de cálculo — nunca ajustado por confiança ou perdas."""
        return round(self.equity * self.limits.risk_per_trade_pct / 100.0, 2)

    @property
    def drawdown_pct(self) -> float:
        return round(100.0 * (self.peak_equity - self.equity) / self.peak_equity, 2) if self.peak_equity else 0.0

    def record_result(self, pnl_usd: float, t: datetime, note: str = "") -> None:
        self.roll_day(t)
        self.equity = round(self.equity + pnl_usd, 2)
        self.peak_equity = max(self.peak_equity, self.equity)
        self.daily_pnl = round(self.daily_pnl + pnl_usd, 2)
        self.history.append({"time": t.isoformat(), "pnl": pnl_usd, "equity": self.equity, "note": note})
        if self.daily_pnl <= -self.equity_start_of_day() * self.limits.max_daily_loss_pct / 100.0:
            self.trading_stop = True
        if self.daily_target_usd is not None and self.daily_pnl >= self.daily_target_usd:
            self.target_reached = True

    def equity_start_of_day(self) -> float:
        return self.equity - self.daily_pnl

    def sync_equity(self, broker_equity: float, t: datetime) -> None:
        """Em LIVE o capital vem do broker; a variação entra como resultado do dia."""
        self.roll_day(t)
        delta = round(broker_equity - self.equity, 2)
        if abs(delta) > 0.005:
            self.record_result(delta, t, "sync broker")

    def blocks(self, t: datetime) -> list[str]:
        self.roll_day(t)
        out: list[str] = []
        if self.trading_stop or self.daily_pnl <= -self.equity_start_of_day() * self.limits.max_daily_loss_pct / 100.0:
            self.trading_stop = True
            out.append(f"🚨 TRADING STOP — perda diária {self.daily_pnl:+.2f} USD atingiu {self.limits.max_daily_loss_pct:.1f}% do capital")
        if self.drawdown_pct >= self.limits.max_drawdown_pct:
            out.append(f"🚨 drawdown {self.drawdown_pct:.1f}% ≥ MAX_DRAWDOWN {self.limits.max_drawdown_pct:.1f}%")
        if self.daily_target_usd is not None and (self.target_reached or self.daily_pnl >= self.daily_target_usd):
            self.target_reached = True
            out.append(f"🎯 META DIÁRIA ATINGIDA — dia {self.daily_pnl:+.2f} USD ({self.daily_pct:+.1f}% ≥ {self.limits.daily_target_pct:.0f}%): "
                       "sem novas entradas hoje; posições abertas seguem com o monitor")
        return out

    def render(self) -> str:
        meta = ""
        if self.daily_target_usd is not None:
            r = self.r_to_target()
            meta = (f" · 🎯 meta {self.limits.daily_target_pct:.0f}% = {self.daily_target_usd:,.2f} USD ({self.daily_pct:+.1f}% hoje"
                    + (f", faltam ≈ {r:.2f}R" if r else ", ATINGIDA") + ")")
        return (f"💼 CAPITAL {self.equity:,.2f} USD · pico {self.peak_equity:,.2f} · drawdown {self.drawdown_pct:.1f}% · "
                f"dia {self.daily_pnl:+.2f} · risco/operação {self.limits.risk_per_trade_pct}% = {self.risk_usd:.2f} USD" + meta
                + (" · 🚨 TRADING STOP" if self.trading_stop else "") + (" · 🎯 META ATINGIDA" if self.target_reached else ""))


def size_lots(limits: GuardLimits, risk_usd: float, stop_distance: float, point_value_usd: Optional[float] = None) -> tuple[float, float]:
    """(lote, risco real em USD). CAPITAL + RISCO + STOP + CONTRATO — nada mais.
    `point_value_usd` (USD por 1.0 de preço por lote) vem do MarketSpec no 4.0; padrão = contract_size (XAUUSD)."""
    per_lot = stop_distance * (point_value_usd if point_value_usd else limits.contract_size)
    if per_lot <= 0:
        return 0.0, 0.0
    lots = min(limits.max_lot, risk_usd / per_lot)
    lots = round(int(lots / limits.lot_step + 1e-9) * limits.lot_step, 2)
    if lots < limits.min_lot:
        return 0.0, 0.0
    return lots, round(lots * per_lot, 2)


@dataclass
class TelegramCommands:
    """Lê /STOP /PAUSE /RESUME /STATUS /CLOSE (com confirmação) via getUpdates. Sem token → inativo."""

    token: Optional[str]
    chat_id: Optional[str]
    offset: int = 0
    pending_close: bool = False
    last_cmds: list[str] = field(default_factory=list)

    def poll(self) -> list[str]:  # pragma: no cover - rede
        if not self.token:
            return []
        url = f"https://api.telegram.org/bot{self.token}/getUpdates?" + urllib.parse.urlencode({"offset": self.offset, "timeout": 0})
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
                data = json.loads(resp.read().decode())
        except Exception:  # noqa: BLE001
            return []
        cmds: list[str] = []
        for upd in data.get("result", []):
            self.offset = max(self.offset, int(upd["update_id"]) + 1)
            msg = upd.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != str(self.chat_id):
                continue
            text = (msg.get("text") or "").strip()
            if text.startswith("/"):
                cmds.append(text.upper())
        return cmds

    def apply(self, cmds: list[str], ks: KillSwitch) -> list[str]:
        """Aplica ao kill switch; devolve ações que o loop deve executar: STATUS, CLOSE_CONFIRMED."""
        actions: list[str] = []
        self.last_cmds = list(cmds)
        for c in cmds:
            if c.startswith("/EDGE"):
                actions.append("EDGE")
            elif c.startswith("/STOP"):
                ks.stopped = True
                actions.append("STOP")
            elif c.startswith("/PAUSE"):
                ks.paused = True
                actions.append("PAUSE")
            elif c.startswith("/RESUME"):
                ks.stopped = ks.paused = False
                actions.append("RESUME")
            elif c.startswith("/STATUS"):
                actions.append("STATUS")
            elif c.startswith("/CLOSE"):
                if "CONFIRM" in c or self.pending_close:
                    self.pending_close = False
                    actions.append("CLOSE_CONFIRMED")
                else:
                    self.pending_close = True
                    actions.append("CLOSE_REQUESTED")
        return actions
