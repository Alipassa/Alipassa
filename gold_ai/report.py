"""Saída interna de cada ciclo (Diretriz §36)."""

from __future__ import annotations

from typing import Optional

from .models import Assessment


def render_report(a: Assessment) -> str:
    f = {x.name: x for x in a.factors}

    def line(name: str, label: str) -> str:
        x = f.get(name)
        if x is None or not x.available:
            return f"{label}: ⚪ n/d"
        return f"{label}: {x.emoji} {x.score:+.0f}/{x.max_score:.0f} — {x.rationale}"

    tech = " | ".join(f"{r.timeframe}:{r.trend[0]}({r.score:+.2f})" for r in a.technical if "dados insuficientes" not in r.notes)
    lines = [
        "GOLD AI",
        "",
        f"Hora: {a.time:%Y-%m-%d %H:%M} UTC",
        f"Preço: {a.price:.2f}",
        f"Tendência: {a.trend.value}",
        f"Score: {a.score:+.0f}",
        f"Probabilidade de alta: {a.prob_up:.0%}",
        f"Probabilidade de baixa: {a.prob_down:.0%}",
        f"Probabilidade lateral: {a.prob_flat:.0%}",
        f"Confiança: {a.confidence:.0f}/100",
        f"Horizonte: {a.horizon}",
        "",
        line("dolar", "Dólar"),
        line("juros_reais", "Juros reais"),
        line("fed", "Fed"),
        line("inflacao", "Inflação"),
        line("geopolitica", "Geopolítica"),
        line("fluxo", "Fluxo"),
        line("cot", "COT"),
        line("opcoes", "Opções"),
        line("sentimento", "Sentimento"),
        line("tecnico", "Técnico"),
        f"  timeframes: {tech or 'n/d'}",
        "",
        f"Sentimento global: {a.sentiment_label.value}",
        f"Risco sistêmico: {a.systemic_risk:.0f}/100",
        f"Pressão dominante: {a.dominant_pressure}",
        f"Pré-movimento: {a.premove.stage.value} ({a.premove.direction.value}, prob. {a.premove.probability:.0%}, confirmação técnica {a.premove.price_confirmation:.0%}, {a.premove.move_in_atr:+.1f} ATR)",
        f"Risco de reversão: {a.reversal.risk:.0f}/100" + (f" — {'; '.join(a.reversal.evidence)}" if a.reversal.evidence else ""),
        f"Evento próximo: {a.next_event.name + ' ' + a.next_event.time.strftime('%H:%M') + ' UTC' if a.next_event else 'nenhum na janela'}",
        f"Confirmações: {', '.join(a.confirmations) or 'nenhuma'}",
        f"Evidência: {a.evidence_level.label}",
        "",
        f"STATUS: {a.edge_status}",
        "",
        a.chain,
        "",
        f"Conclusão: {a.conclusion}",
    ]
    return "\n".join(lines)


def render_dashboard(a: Assessment, expected_lead_min: Optional[float] = None) -> str:
    """Painel GOLD MARKET PREDICTION SYSTEM (caixa de largura fixa)."""
    from .signals import classify
    from .config import EngineConfig

    f = {x.name: x for x in a.factors}

    def lab(name: str) -> str:
        x = f.get(name)
        if x is None or not x.available:
            return "N/D"
        return "ALTISTA" if x.ratio >= 0.3 else "BAIXISTA" if x.ratio <= -0.3 else "NEUTRO"

    p_dom = max(a.prob_up, a.prob_down)
    decision = classify(a.score, EngineConfig()).value
    if a.premove.stage.value == "PRÉ-MOVIMENTO" and len(a.confirmations) >= 3:
        decision = "GOLD PRE-MOVE"
    if not a.has_edge:
        decision = "NÃO SEI — SEM SINAL"
    status = a.premove.latent_pressure or a.dominant_pressure
    status_emoji = "🟢" if "COMPRADORA" in status else "🔴" if "VENDEDORA" in status else "🟡"
    lead = f"{expected_lead_min:.0f} min (histórico)" if expected_lead_min else "n/d (sem histórico)"
    rows = [
        ("REGIME", a.regime), ("SCORE", f"{a.score:+.0f}"), ("PROBABILIDADE", f"{p_dom:.0%}"), ("CONFIANÇA", f"{a.confidence:.0f}"),
        None,
        ("PRE-MOVE", a.premove.direction.value if a.premove.stage.value == "PRÉ-MOVIMENTO" else a.premove.stage.value),
        ("LEAD TIME", lead), ("EVIDÊNCIA", f"NÍVEL {int(a.evidence_level)}"),
        None,
        ("DXY", lab("dolar")), ("REAL YIELD", lab("juros_reais")), ("FED", lab("fed")), ("FLOW", lab("fluxo")),
        ("COT", lab("cot")), ("TECHNICAL", lab("tecnico")), ("NEWS", lab("sentimento") if f.get("sentimento") and f["sentimento"].available else "UNKNOWN"), ("GEO", lab("geopolitica")),
        None,
        ("STATUS", f"{status_emoji} {status}"),
        None,
        ("DECISÃO", decision),
    ]
    width = 44
    out = ["┌" + "─" * width + "┐", "│" + "GOLD AI ENGINE".center(width) + "│", "├" + "─" * width + "┤"]
    for r in rows:
        if r is None:
            out.append("│" + " " * width + "│")
            continue
        k, v = r
        line = f" {k:<13}→ {v}"
        out.append("│" + line[:width].ljust(width) + "│")
    out.append("└" + "─" * width + "┘")
    return "\n".join(out)
