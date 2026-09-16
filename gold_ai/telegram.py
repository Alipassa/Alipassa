"""Formatação e envio de alertas para o Telegram (Diretriz §23–§26)."""

from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from typing import Optional

from .models import Assessment, Direction, Signal, SignalType
from .config import HORIZONS


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _layer_line(a: Assessment, direction: Direction) -> list[str]:
    sign = 1.0 if direction == Direction.ALTA else -1.0

    def flag(names: tuple[str, ...]) -> str:
        fs = [f for f in a.factors if f.name in names and f.available]
        if not fs:
            return "⚪"
        r = sum(f.score for f in fs) / sum(f.max_score for f in fs)
        return "🟢" if sign * r >= 0.3 else "🔴" if sign * r <= -0.3 else "🟡"

    return [
        f"FUNDAMENTOS → {flag(('dolar', 'juros_reais', 'fed', 'inflacao', 'geopolitica'))}",
        f"FLUXO → {flag(('fluxo', 'cot', 'opcoes'))}",
        f"TÉCNICO → {flag(('tecnico',))}",
        f"SENTIMENTO → {flag(('sentimento',))}",
    ]


def _price_digits(price: float) -> int:
    """Casas decimais pelo nível do preço: FX (< 10) 5 · JPY/petróleo (< 1000) 3 · ouro/índices 2."""
    return 5 if price < 10 else 3 if price < 1000 else 2


def _zone(a: Assessment) -> list[str]:
    z = a.zone
    d = _price_digits(float(getattr(a, "price", 0.0) or 0.0))
    fmt = lambda v: f"{v:.{d}f}" if v is not None else "n/d"  # noqa: E731
    lines = []
    if z.get("entry_low") is not None:
        lines.append(f"Entrada: {fmt(z.get('entry_low'))}–{fmt(z.get('entry_high'))}")
    lines.append(f"Suporte: {fmt(z.get('support'))}")
    lines.append(f"Resistência: {fmt(z.get('resistance'))}")
    lines.append(f"Invalidação: {fmt(z.get('invalidation'))}")
    return lines


MARKET_LABEL = {"XAUUSD": ("GOLD", "XAU/USD"), "US500": ("US500", "S&P 500 (US500)"), "EURUSD": ("EURUSD", "EUR/USD"),
                "USDJPY": ("USDJPY", "USD/JPY"), "WTI": ("WTI", "Petróleo WTI"), "NAS100": ("NAS100", "Nasdaq 100"), "XAGUSD": ("SILVER", "XAG/USD")}


def signal_label(sig_type, symbol: str = "XAUUSD") -> str:
    """Nome do sinal com o rótulo do mercado: 'GOLD WATCH' → 'EURUSD WATCH' para EURUSD (o enum guarda o nome histórico GOLD)."""
    name = MARKET_LABEL.get(str(symbol).upper(), (str(symbol).upper(), ""))[0]
    value = getattr(sig_type, "value", str(sig_type))
    return value.replace("GOLD ", f"{name} ", 1) if value.startswith("GOLD ") else value


def format_signal(sig: Signal, symbol: str = "XAUUSD") -> str:
    a = sig.assessment
    d = sig.direction
    prob = a.prob_up if d == Direction.ALTA else a.prob_down if d == Direction.BAIXA else a.prob_flat
    name, pair = MARKET_LABEL.get(symbol.upper(), (symbol.upper(), symbol.upper()))
    digits = _price_digits(a.price)
    price = f"Preço: {a.price:.{digits}f}"

    if sig.type == SignalType.WATCH:
        lines = [f"⚠️ {name} WATCH", pair, price, "",
                 f"Possível movimento de {'ALTA' if d == Direction.ALTA else 'BAIXA'}.", "",
                 f"Probabilidade: {_pct(prob)}", f"Confiança: {a.confidence:.0f}/100", f"Evidência: {a.evidence_level.label}",
                 f"Horizonte: {a.horizon}", "", "Fatores em observação:", *[f"• {r}" for r in sig.reasons[:4]], "",
                 "Ainda não há operação. Aguardando confirmação."]
        return "\n".join(lines)

    if sig.type == SignalType.PRE_MOVE:
        emoji = "🟢 ALTA" if d == Direction.ALTA else "🔴 BAIXA"
        lines = [f"⚠️ {name} PRE-MOVE — POSSÍVEL {'ALTA' if d == Direction.ALTA else 'BAIXA'}", pair, price, "",
                 "A IA detectou mudança em:", *[f"• {r}" for r in sig.reasons], "",
                 "mas o preço ainda não confirmou.", "",
                 f"Probabilidade de movimento: {_pct(a.premove.probability)}", f"Direção: {emoji}",
                 f"Status: {a.premove.stage.value}", f"Confiança: {a.confidence:.0f}/100", f"Horizonte: {a.horizon}", "",
                 f"Evidência: {a.evidence_level.label}", "",
                 "Antecipação", *_layer_line(a, d), "", "Situação", f"{a.premove.latent_pressure or a.premove.stage.value}", "",
                 "Zona de atenção", *_zone(a), "", a.chain]
        return "\n".join(lines)

    if sig.type == SignalType.REVERSAL:
        trend = a.reversal.current_trend.value
        lines = [f"🔄 {name} REVERSAL ALERT", pair, price, "",
                 f"Ouro está em tendência de {trend}, porém foram detectados sinais de {'distribuição' if trend == 'ALTA' else 'acumulação'}.", "",
                 "Indicadores:", *[f"• {e}" for e in a.reversal.evidence], "",
                 f"Resultado: 🔴 RISCO DE REVERSÃO ({a.reversal.risk:.0f}/100)", f"Score atual: {a.score:+.0f}", f"Horizonte: {a.horizon}"]
        if a.premove.stage.value == "PRÉ-MOVIMENTO" and a.premove.direction != a.reversal.current_trend:
            lines += ["", f"⚠️ {a.premove.latent_pressure}: fundamentos já apontam {a.premove.direction.value.lower()} (prob. {a.premove.probability:.0%}); preço ainda não confirmou."]
        lines += ["", "Zona de atenção", *_zone(a)]
        return "\n".join(lines)

    if sig.type == SignalType.RISK:
        lines = [f"🚨 {name} SYSTEMIC RISK", pair, price, "",
                 f"RISCO SISTÊMICO: {a.systemic_risk:.0f}/100", "",
                 "Sinais de stress detectados (VIX, spreads, bolsas, bancos).",
                 "Reação do ouro pode ser não-linear: liquidação inicial (venda forçada) seguida de fluxo de proteção.", "",
                 f"Score atual: {a.score:+.0f} | Prob. alta {_pct(a.prob_up)} | Prob. baixa {_pct(a.prob_down)}"]
        return "\n".join(lines)

    buy = sig.type in (SignalType.BUY, SignalType.STRONG_BUY)
    confirmed = sig.trigger == "confirmação de movimento"
    head = (f"🟢 {name} SIGNAL" if buy else f"🔴 {name} SIGNAL") if confirmed else (f"🚨 {name} AI ALERT" if buy else f"🔴 {name} AI ALERT")
    bias = "🟢 COMPRA" if buy else "🔴 VENDA"
    if sig.type in (SignalType.STRONG_BUY, SignalType.STRONG_SELL):
        bias += " (FORTE)"
    lines = [head, pair, price, "", bias, *(["PRE-MOVE CONFIRMADO"] if confirmed else []),
             f"Score: {a.score:+.0f}", f"Probabilidade: {_pct(prob)}",
             f"Confiança: {a.confidence:.0f}/100", f"Evidência: {a.evidence_level.label}", f"Horizonte: {a.horizon}", "",
             "Motivos", *[f"• {r}" for r in sig.reasons], "",
             "Antecipação", *_layer_line(a, d), "",
             "Situação", f"{a.premove.latent_pressure or a.dominant_pressure}",
             f"Estágio: {a.premove.stage.value}", "",
             "Zona de atenção", *_zone(a)]
    if a.reversal.risk >= 40:
        lines += ["", f"⚠️ Risco de reversão: {a.reversal.risk:.0f}/100"]
    if a.next_event:
        lines += ["", f"⚠️ Evento próximo: {a.next_event.name} — {a.next_event.time:%H:%M} UTC ({a.next_event.impact})"]
    lines += ["", f"Gatilho: {sig.trigger}"]
    return "\n".join(lines)


ENV_CANDIDATES = (".env", ".env.txt", "env", "env.txt")


def env_file_candidates(path: str = ".env") -> list[str]:
    """Onde o .env é procurado: pasta atual e pasta do script, com os nomes que o Windows costuma dar ao arquivo."""
    if path != ".env":
        return [path]
    dirs = [os.getcwd()]
    script_dir = os.path.dirname(os.path.abspath(sys.argv[0])) if sys.argv and sys.argv[0] else ""
    if script_dir and script_dir != dirs[0]:
        dirs.append(script_dir)
    return [os.path.join(d, name) for d in dirs for name in ENV_CANDIDATES]


def find_env_file(path: str = ".env") -> Optional[str]:
    for cand in env_file_candidates(path):
        if os.path.isfile(cand):
            return cand
    return None


def load_env_file(path: str = ".env") -> dict[str, str]:
    """Lê um .env simples (CHAVE=valor, aspas opcionais). Nunca versionar esse arquivo.
    Aceita .env / .env.txt / env / env.txt na pasta atual ou na pasta do script."""
    out: dict[str, str] = {}
    found = find_env_file(path)
    if not found:
        return out
    with open(found, encoding="utf-8-sig") as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#") or "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


class TelegramSender:
    """Envio via Bot API (stdlib). Credenciais por argumento, variável de ambiente
    (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID, ou TOKEN_TELEGRAM / CHAT_ID) ou arquivo .env.
    Sem token/chat_id, apenas imprime (modo dry-run)."""

    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None, dry_run: bool = False, env_file: str = ".env",
                 quiet: bool = False) -> None:
        env = {**load_env_file(env_file), **os.environ}
        self.token = token or env.get("TELEGRAM_BOT_TOKEN") or env.get("TOKEN_TELEGRAM")
        self.chat_id = str(chat_id or env.get("TELEGRAM_CHAT_ID") or env.get("CHAT_ID") or "") or None
        self.dry_run = dry_run or not (self.token and self.chat_id)
        self.quiet = quiet
        self.sent: list[str] = []

    def send(self, text: str) -> bool:
        if self.dry_run:
            self.sent.append(text)
            if not self.quiet:
                print("\n[TELEGRAM dry-run]\n" + text + "\n")
            return True
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": self.chat_id, "text": text, "disable_web_page_preview": "true"}).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310
            body = json.loads(resp.read().decode())
            return bool(body.get("ok"))


def format_decision(decision) -> str:
    """Mensagem do TRADE SIMULATOR / gestor de posição (2.2)."""
    return "🧾 GOLD AI TRADE\n" + decision.render()


def format_monitor(tr, reading) -> str:
    from .monitor import render_monitor
    return "📡 " + render_monitor(tr, reading)


# --------------------------------------------------------------------------- 3.0: TELEGRAM TRADE MANAGER
def format_entry(plan, assessment, mode: str, execution=None, symbol: str = "XAUUSD") -> str:
    side = f"🟢 BUY {symbol}" if plan.direction.value == "ALTA" else f"🔴 SELL {symbol}"
    tp = plan.targets.get(plan.recommended) or plan.targets.get("3R")
    rr = plan.recommended[0] if plan.recommended[:1].isdigit() else "3"
    lines = ["🚨 MARKET AI", "", side, "", f"Score: {assessment.score:+.0f}", f"Probabilidade: {max(assessment.prob_up, assessment.prob_down):.0%}",
             f"Confiança: {assessment.confidence:.0f}", "", f"Entrada: {plan.entry:.2f}", f"Stop: {plan.stop:.2f}", f"TP: {tp:.2f}" if tp else "TP: trailing",
             "", f"R:R = 1:{rr}", "", f"Lote: {plan.lots:.2f}" if plan.lots else "Lote: n/d", f"Risco: {plan.risk_usd:.2f} USD" if plan.risk_usd else "",
             "", "PRE-MOVE CONFIRMADO" if "CONFIRM" in plan.signal_type.upper() or "BUY" in plan.signal_type or "SELL" in plan.signal_type else plan.signal_type,
             f"Modo: {mode}"]
    if execution is not None:
        lines += ["", execution.render()]
    return "\n".join(x for x in lines if x is not None)


def format_protection(tr, reading) -> str:
    return "\n".join(["🛡️ GOLD AI", "", f"+{reading.current_r:.1f}R atingido", "", f"{1 - tr.remaining:.0%} realizado", "",
                       f"Stop: {'BREAK EVEN' if abs(tr.stop_r) < 1e-9 else f'{tr.stop_r:+.2f}R'}", "", f"{tr.remaining:.0%} restante:", "TRAILING", "", reading.note])


def format_scenario_change(tr, reading) -> str:
    from types import SimpleNamespace
    first = tr.history[0] if len(tr.history) > 1 else SimpleNamespace(thesis_score=100.0, exit_score=0.0)
    return "\n".join(["⚠️ GOLD AI", "", "CENÁRIO ALTERADO", "", f"Score:\n{tr.thesis.score:+.0f} → {reading.trade_score:+.0f}", "",
                       f"Thesis:\n{first.thesis_score:.0f} → {reading.thesis_score:.0f}", "", f"Exit:\n{first.exit_score:.0f} → {reading.exit_score:.0f}", "",
                       "AÇÃO:", "🔴 ENCERRAR" if reading.action == "ENCERRAR" else f"🟠 {reading.action}", "", f"Motivo:\n{tr.close_reason or reading.note}"])


def format_result(tr, pnl_usd=None, prediction_correct=None, lead_time_min=None, minutes=None) -> str:
    lines = ["🏆 GOLD AI" if (tr.result_r or 0) > 0 else "📉 GOLD AI", "", "TRADE ENCERRADO", "", f"Resultado:\n{tr.result_r:+.2f}R"]
    if pnl_usd is not None:
        lines.append(f"{pnl_usd:+,.2f} USD")
    reason = {"TESE INVALIDADA": "Saída adaptativa (tese invalidada)", "EXIT SCORE": "Saída adaptativa", "STOP": "Stop", "TRAILING/PROTEÇÃO": "Trailing / proteção",
              "HORIZON": "Horizonte", "FIM": "Fim do período", "BROKER": "Fechada no broker", "MANUAL": "Encerramento manual (/CLOSE)"}.get(tr.close_reason, tr.close_reason)
    lines += ["", f"Motivo:\n{reason}"]
    if prediction_correct is not None:
        lines += ["", f"Previsão:\n{'CORRETA' if prediction_correct else 'INCORRETA'}"]
    if lead_time_min is not None:
        lines += ["", f"Lead time:\n{lead_time_min:.0f} minutos"]
    if minutes is not None:
        lines += ["", f"Duração:\n{minutes:.0f} minutos"]
    return "\n".join(lines)


def format_status(perf, ks, managed, mode: str) -> str:
    allowed, why = ks.new_entries_allowed()
    lines = ["📋 GOLD AI STATUS", f"Modo: {mode}", f"Novas entradas: {'✅' if allowed else '⛔ ' + why}", perf.render(), f"Posições sob monitor: {len(managed)}"]
    for tr in managed:
        last = tr.history[-1] if tr.history else None
        lines.append(f"  #{tr.trade_id:05d} {tr.thesis.direction.value} entrada {tr.plan.entry:.2f} stop {tr.price_at_r(tr.stop_r):.2f} "
                     + (f"{last.current_r:+.2f}R {last.action}" if last else ""))
    return "\n".join(lines)
