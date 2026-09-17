"""FALSE SIGNAL FILTER (5.2) — onde o robô costuma errar, medido fora da amostra, e o veto só com amostra.

Entrada: as operações fora da amostra do walk-forward (mesmas linhas do Exit Lab / Edge Bank: R por saída, MFE, MAE, score,
confiança, regime, tipo de evento, hora) e, quando existem, as operações vividas. Para cada contexto — mercado, sessão (UTC),
regime, tipo de evento, direção, e mercado × sessão / mercado × regime — mede n, acerto, expectancy e compara vencedoras × perdedoras
(MFE, MAE, |score|, confiança). Um contexto é FALSO SINAL RECORRENTE quando n ≥ MIN_N, expectancy ≤ 0 e acerto ≤ 45%: entra em
`dados/falsos_sinais.json` e o live veta a entrada nesse contexto ("FALSO SINAL — mercado×sessão perdeu em N casos OOS").
Regra: só o histórico com amostra veta; um dia ruim não vira regra."""
from __future__ import annotations

import json
import os
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Sequence

MIN_N = 20
NEG_WIN = 0.45


def session_of(t: datetime) -> str:
    h = t.astimezone(timezone.utc).hour
    return "Ásia 00–07" if h < 7 else "Londres 07–13" if h < 13 else "NY 13–21" if h < 21 else "fecho 21–24"


@dataclass
class ContextStat:
    dimension: str
    value: str
    n: int = 0
    wins: int = 0
    r_sum: float = 0.0
    mfe_w: list = field(default_factory=list)
    mfe_l: list = field(default_factory=list)
    mae_w: list = field(default_factory=list)
    mae_l: list = field(default_factory=list)
    score_w: list = field(default_factory=list)
    score_l: list = field(default_factory=list)
    conf_w: list = field(default_factory=list)
    conf_l: list = field(default_factory=list)

    @property
    def expectancy(self) -> float:
        return self.r_sum / self.n if self.n else 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else 0.0

    @property
    def verdict(self) -> str:
        if self.n < MIN_N:
            return "⚪ amostra"
        if self.expectancy <= 0 and self.win_rate <= NEG_WIN:
            return "🔴 falso sinal recorrente"
        if self.expectancy <= 0:
            return "🟡 negativo"
        return "🟢 positivo"

    def add(self, r: float, mfe: float, mae: float, score: float, conf: float) -> None:
        self.n += 1
        self.r_sum += r
        if r > 0:
            self.wins += 1
            self.mfe_w.append(mfe); self.mae_w.append(mae); self.score_w.append(abs(score)); self.conf_w.append(conf)
        else:
            self.mfe_l.append(mfe); self.mae_l.append(mae); self.score_l.append(abs(score)); self.conf_l.append(conf)

    def row(self) -> str:
        med = lambda xs: (f"{statistics.median(xs):.2f}" if xs else "—")  # noqa: E731
        return (f"  {self.dimension:<16}{self.value[:22]:<24}{self.n:>5}{self.win_rate:>7.0%}{self.expectancy:>+8.2f}R"
                f"{med(self.mfe_w):>7}{med(self.mfe_l):>7}{med(self.mae_w):>7}{med(self.mae_l):>7}{med(self.score_w):>7}{med(self.score_l):>7}  {self.verdict}")


@dataclass
class FalseSignalReport:
    start: str
    end: str
    strategy: str
    stats: list[ContextStat] = field(default_factory=list)
    n_rows: int = 0

    def negatives(self) -> list[ContextStat]:
        return [s for s in self.stats if s.verdict.startswith("🔴")]

    def render(self) -> str:
        L = [f"🚫 FALSE SIGNAL FILTER — onde o robô erra, fora da amostra · {self.start} → {self.end} · {self.n_rows} operações OOS · saída {self.strategy}",
             f"  {'dimensão':<16}{'contexto':<24}{'n':>5}{'acerto':>7}{'expect.':>9}{'MFE✓':>7}{'MFE✗':>7}{'MAE✓':>7}{'MAE✗':>7}{'|sc|✓':>7}{'|sc|✗':>7}  veredito"]
        order = {"mercado": 0, "sessão": 1, "regime": 2, "evento": 3, "direção": 4, "mercado×sessão": 5, "mercado×regime": 6, "mercado×evento": 7}
        for s in sorted(self.stats, key=lambda x: (order.get(x.dimension, 9), -x.n)):
            L.append(s.row())
        neg = self.negatives()
        L.append("")
        if neg:
            L.append(f"  🔴 CONTEXTOS VETADOS NO LIVE ({len(neg)}): " + " · ".join(f"{s.dimension}={s.value} (n={s.n}, E={s.expectancy:+.2f}R, acerto {s.win_rate:.0%})" for s in neg))
        else:
            L.append(f"  nenhum contexto com n ≥ {MIN_N}, expectancy ≤ 0 e acerto ≤ {NEG_WIN:.0%} — nada a vetar por enquanto (só amostra decide)")
        # padrões que antecedem perdas: comparação global vencedoras × perdedoras
        allw = [x for s in self.stats if s.dimension == "mercado" for x in s.mae_w]
        alll = [x for s in self.stats if s.dimension == "mercado" for x in s.mae_l]
        sw = [x for s in self.stats if s.dimension == "mercado" for x in s.score_w]
        sl = [x for s in self.stats if s.dimension == "mercado" for x in s.score_l]
        cw = [x for s in self.stats if s.dimension == "mercado" for x in s.conf_w]
        cl = [x for s in self.stats if s.dimension == "mercado" for x in s.conf_l]
        if allw and alll:
            L.append(f"  vencedoras × perdedoras: MAE mediano {statistics.median(allw):.2f} × {statistics.median(alll):.2f} ATR · |score| {statistics.median(sw):.0f} × {statistics.median(sl):.0f}"
                     f" · confiança {statistics.median(cw):.0f} × {statistics.median(cl):.0f}")
            if statistics.median(alll) > 0 and statistics.median(allw) < 0.6 * statistics.median(alll):
                L.append("  leitura: perdedoras andam contra cedo (MAE alto) — candidato a stop mais curto/saída rápida no Exit Lab, não a filtro novo")
        L.append("  regra: veta só n ≥ 20 com expectancy ≤ 0 e acerto ≤ 45% · MFE/MAE em ATR (stop inicial) · o Exit Lab decide a saída; este quadro decide onde NÃO entrar")
        return "\n".join(L)

    def to_dict(self) -> dict:
        return {"generated": datetime.now(timezone.utc).isoformat(), "start": self.start, "end": self.end, "min_n": MIN_N, "neg_win": NEG_WIN,
                "negatives": [{"dimension": s.dimension, "value": s.value, "n": s.n, "expectancy": round(s.expectancy, 3), "win_rate": round(s.win_rate, 3)}
                              for s in self.negatives()],
                "contexts": [{"dimension": s.dimension, "value": s.value, "n": s.n, "expectancy": round(s.expectancy, 3), "win_rate": round(s.win_rate, 3)}
                             for s in self.stats]}

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=1)


def build_report(rows_by_symbol: dict, strategy: str = "adaptive", default: str = "3R", start: str = "", end: str = "") -> FalseSignalReport:
    rep = FalseSignalReport(start, end, strategy)
    stats: dict[tuple[str, str], ContextStat] = {}

    def add(dim: str, val: str, r: float, mfe: float, mae: float, score: float, conf: float) -> None:
        stats.setdefault((dim, val), ContextStat(dim, val)).add(r, mfe, mae, score, conf)

    for sym, rows in rows_by_symbol.items():
        for row in rows:
            r = row["results"].get(strategy, row["results"].get(default))
            if r is None:
                continue
            r = float(r)
            prof = row.get("profile")
            mfe = float(getattr(prof, "max_r_before_stop", 0.0) or 0.0)
            mae = float(getattr(prof, "mae_r", 0.0) or 0.0)
            score = float(row.get("score", 0.0) or 0.0)
            conf = float(row.get("confidence", 0.0) or 0.0)
            sess = session_of(row["time"])
            regime = str(row.get("regime", "") or "?").upper().split(" ")[0] or "?"
            kind = str(row.get("event_kind", "") or "sem evento")
            direction = str(row.get("direction", "?"))
            rep.n_rows += 1
            add("mercado", sym, r, mfe, mae, score, conf)
            add("sessão", sess, r, mfe, mae, score, conf)
            add("regime", regime, r, mfe, mae, score, conf)
            add("evento", kind, r, mfe, mae, score, conf)
            add("direção", direction, r, mfe, mae, score, conf)
            add("mercado×sessão", f"{sym} {sess}", r, mfe, mae, score, conf)
            add("mercado×regime", f"{sym} {regime}", r, mfe, mae, score, conf)
            add("mercado×evento", f"{sym} {kind}", r, mfe, mae, score, conf)
    rep.stats = list(stats.values())
    return rep


def load_negatives(path: str) -> list[dict]:
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return list(json.load(f).get("negatives", []))


def live_veto(negatives: Sequence[dict], symbol: str, now: datetime, regime: str, event_kind: str = "") -> Optional[str]:
    """Contexto atual do mercado × contextos vetados. Só mercado×sessão, mercado×regime e mercado×evento vetam (o mercado sozinho
    é grau D pelo ciclo de vida; sessão/regime globais são leitura, não veto)."""
    sess = session_of(now)
    reg = str(regime or "?").upper().split(" ")[0]
    keys = {("mercado×sessão", f"{symbol} {sess}"), ("mercado×regime", f"{symbol} {reg}")}
    if event_kind:
        keys.add(("mercado×evento", f"{symbol} {event_kind}"))
    for n in negatives:
        if (n.get("dimension"), n.get("value")) in keys:
            return f"FALSO SINAL — {n['dimension']} {n['value']} perdeu fora da amostra (n={n['n']}, E={n['expectancy']:+.2f}R, acerto {n['win_rate']:.0%})"
    return None
