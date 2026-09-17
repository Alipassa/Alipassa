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
import time
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
    risk_ladder: tuple = ()              # ESCADA (5.2): (base, operacional 30 OOS, validado 50 OOS) em %; vazio = proporcional à base (×1, ×4/3, ×5/3)
    risk_ladder_max_pct: float = 5.0     # teto absoluto da escada
    sample_risk_pct: float = 1.0         # HIERARQUIA: risco do grau C (inconclusivo) para formar amostra; 0 = não opera em C
    max_cost_r: float = 0.25             # CUSTO LÍQUIDO: spread + slippage + comissão acima desta fração do stop (R) → descarta
    commission_per_lot: float = 0.0      # USD por lote, ida e volta (0 = corretora sem comissão / já no spread)
    prob_shrink_uncalibrated: float = 0.5   # sem calibrador: p usada = 50% + (p declarada − 50%) × este fator

    def ladder(self) -> tuple[float, float, float]:
        if self.risk_ladder:
            xs = [float(x) for x in self.risk_ladder][:3]
            while len(xs) < 3:
                xs.append(xs[-1])
            return tuple(min(x, self.risk_ladder_max_pct) for x in xs)
        b = float(self.risk_per_trade_pct)
        return (b, min(round(b * 4 / 3, 2), self.risk_ladder_max_pct), min(round(b * 5 / 3, 2), self.risk_ladder_max_pct))

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "GuardLimits":
        base = RiskLimits.from_env(env)
        g = cls(**base.__dict__)
        g.max_drawdown_pct = float(env.get("MAX_DRAWDOWN", g.max_drawdown_pct))
        g.min_rr_to_structure = float(env.get("MIN_RR_TO_STRUCTURE", g.min_rr_to_structure))
        g.daily_target_pct = float(env.get("DAILY_TARGET", g.daily_target_pct) or 0.0)
        g.risk_ladder_max_pct = float(env.get("RISK_LADDER_MAX", g.risk_ladder_max_pct))
        g.sample_risk_pct = float(env.get("SAMPLE_RISK_PCT", g.sample_risk_pct))
        g.max_cost_r = float(env.get("MAX_COST_R", g.max_cost_r))
        g.commission_per_lot = float(env.get("COMMISSION_PER_LOT", g.commission_per_lot))
        g.prob_shrink_uncalibrated = float(env.get("PROB_SHRINK_UNCALIBRATED", g.prob_shrink_uncalibrated))
        raw = str(env.get("RISK_LADDER", "") or "").strip()
        if raw:
            g.risk_ladder = tuple(float(x) for x in raw.split(",") if x.strip())
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
    synced: bool = False              # primeira leitura do broker = linha de base (não é resultado do dia)

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

    def restore(self, rows: list, now: datetime) -> None:
        """Após reinício: reconstrói pico, resultado do dia e as travas (perda diária / meta) a partir da tabela `account`.
        Linhas (hora, capital, pnl[, nota]); linhas de base do broker têm pnl None e não contam."""
        if not rows:
            return
        self.peak_equity = max(self.peak_equity, max(r[1] for r in rows))
        self.roll_day(now)
        day = now.strftime("%Y-%m-%d")
        self.daily_pnl = round(sum((r[2] or 0.0) for r in rows if (r[0].strftime("%Y-%m-%d") if r[0] else "") == day), 2)
        self.blocks(now)

    def sync_equity(self, broker_equity: float, t: datetime) -> None:
        """Em LIVE o capital vem do broker; a variação entra como resultado do dia.
        A PRIMEIRA leitura só define a linha de base (capital real da conta): a diferença para o --equity de partida
        não é lucro nem perda — sem isso, 10 000 → 50 000 viraria "meta diária atingida" no primeiro ciclo."""
        self.roll_day(t)
        if not self.synced:
            self.synced = True
            self.equity = round(broker_equity, 2)
            self.peak_equity = max(self.peak_equity, self.equity)
            return
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
                f"dia {self.daily_pnl:+.2f} · risco base {self.limits.risk_per_trade_pct}% = {self.risk_usd:.2f} USD (por mercado: ver hierarquia de edge)" + meta
                + (" · 🚨 TRADING STOP" if self.trading_stop else "") + (" · 🎯 META ATINGIDA" if self.target_reached else ""))


def size_lots(limits: GuardLimits, risk_usd: float, stop_distance: float, point_value_usd: Optional[float] = None) -> tuple[float, float]:
    """(lote, risco real em USD). CAPITAL + RISCO + STOP + CONTRATO — nada mais.
    `point_value_usd` (USD por 1.0 de preço por lote) vem do MarketSpec no 4.0; padrão = contract_size (XAUUSD)."""
    per_lot = stop_distance * (point_value_usd if point_value_usd else limits.contract_size)
    if per_lot <= 0:
        return 0.0, 0.0
    lots = min(limits.max_lot, risk_usd / per_lot)
    lots = round(int(lots / limits.lot_step + 1e-9) * limits.lot_step, 3)
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
    offset_path: Optional[str] = None            # persistência do ponteiro: um reinício não reexecuta comandos antigos
    started_at: float = field(default_factory=time.time)
    MAX_AGE_SEC: int = 120                       # comando mais velho que isto (antes da partida) é descartado

    def __post_init__(self) -> None:
        if self.offset_path and os.path.exists(self.offset_path):
            try:
                with open(self.offset_path, encoding="utf-8") as f:
                    self.offset = max(self.offset, int(f.read().strip() or 0))
            except (OSError, ValueError):
                pass

    def _save_offset(self) -> None:
        if not self.offset_path:
            return
        try:
            os.makedirs(os.path.dirname(self.offset_path) or ".", exist_ok=True)
            with open(self.offset_path, "w", encoding="utf-8") as f:
                f.write(str(self.offset))
        except OSError:
            pass

    def filter_updates(self, updates: list, now: Optional[float] = None) -> list[str]:
        """Avança o ponteiro por TODAS as atualizações; devolve só comandos do chat certo e recentes (≤ MAX_AGE_SEC antes da partida)."""
        now = now if now is not None else time.time()
        cmds: list[str] = []
        for upd in updates:
            self.offset = max(self.offset, int(upd["update_id"]) + 1)
            msg = upd.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != str(self.chat_id):
                continue
            date = float(msg.get("date") or now)
            if date < self.started_at - self.MAX_AGE_SEC:
                continue                                                      # relíquia de antes do reinício: ignora
            text = (msg.get("text") or "").strip()
            if text.startswith("/"):
                cmds.append(text.upper())
        self._save_offset()
        return cmds

    def poll(self) -> list[str]:  # pragma: no cover - rede
        if not self.token:
            return []
        url = f"https://api.telegram.org/bot{self.token}/getUpdates?" + urllib.parse.urlencode({"offset": self.offset, "timeout": 0})
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310
                data = json.loads(resp.read().decode())
        except Exception:  # noqa: BLE001
            return []
        return self.filter_updates(data.get("result", []))

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
            elif c.startswith("/FLOW"):
                actions.append("FLOW")
            elif c.startswith("/DIA"):
                actions.append("DIA")
            elif c.startswith("/CLOSE"):
                if "CONFIRM" in c or self.pending_close:
                    self.pending_close = False
                    actions.append("CLOSE_CONFIRMED")
                else:
                    self.pending_close = True
                    actions.append("CLOSE_REQUESTED")
        return actions
