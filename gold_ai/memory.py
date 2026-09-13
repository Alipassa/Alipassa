"""Banco de previsões, feedback e aprendizado (Diretriz §29, §30).

PREVISÃO → MOVIMENTO REAL → COMPARAÇÃO → ERRO → APRENDIZADO.
Implementado em SQLite (stdlib) para funcionar em qualquer ambiente.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Optional

from .models import Assessment, Direction

SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    data TEXT NOT NULL,
    hora TEXT NOT NULL,
    sessao TEXT NOT NULL,
    preco REAL NOT NULL,
    previsao TEXT NOT NULL,
    probabilidade REAL NOT NULL,
    confianca REAL NOT NULL,
    score REAL NOT NULL,
    horizonte TEXT NOT NULL,
    estagio TEXT NOT NULL,
    fundamentos TEXT NOT NULL,
    noticias TEXT NOT NULL,
    dolar REAL, juros REAL, fluxo REAL, tecnico REAL,
    evento TEXT,
    sinal_tipo TEXT,
    nivel_evidencia INTEGER DEFAULT 0,
    resultado TEXT,
    tempo_ate_reacao_min REAL,
    maxima_favoravel REAL,
    maxima_adversa REAL,
    preco_final REAL,
    resolvido_em TEXT
);
CREATE INDEX IF NOT EXISTS idx_pred_resultado ON predictions(resultado);
"""


def session_label(t: datetime) -> str:
    """Sessão de mercado pela hora UTC."""
    h = t.astimezone(timezone.utc).hour
    if 0 <= h < 7:
        return "ASIA"
    if 7 <= h < 13:
        return "LONDRES"
    if 13 <= h < 17:
        return "LONDRES/NY"
    if 17 <= h < 22:
        return "NY"
    return "PÓS-NY"


@dataclass
class Outcome:
    result: str                   # ACERTO | ERRO | LATERAL
    time_to_reaction_min: Optional[float]
    mfe: float                    # máxima excursão favorável (USD)
    mae: float                    # máxima excursão adversa (USD)
    final_price: float


class PredictionMemory:
    def __init__(self, path: str = "gold_ai.db") -> None:
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ registro
    def record(self, a: Assessment, signal_type: Optional[str] = None) -> int:
        t = a.time.astimezone(timezone.utc)
        direction = a.direction.value
        prob = {"ALTA": a.prob_up, "BAIXA": a.prob_down, "LATERAL": a.prob_flat}[direction]
        fund = {f.name: f.score for f in a.factors}
        cur = self.conn.execute(
            """INSERT INTO predictions (data, hora, sessao, preco, previsao, probabilidade, confianca, score,
               horizonte, estagio, fundamentos, noticias, dolar, juros, fluxo, tecnico, evento, sinal_tipo, nivel_evidencia)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                t.strftime("%Y-%m-%d"), t.strftime("%H:%M:%S"), session_label(t), a.price, direction, prob,
                a.confidence, a.score, a.horizon, a.premove.stage.value, json.dumps(fund, ensure_ascii=False),
                json.dumps([], ensure_ascii=False), fund.get("dolar"), fund.get("juros_reais"), fund.get("fluxo"),
                fund.get("tecnico"), a.next_event.name if a.next_event else None, signal_type, int(a.evidence_level),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    # ------------------------------------------------------------------ feedback
    @staticmethod
    def evaluate_path(direction: str, entry: float, path: Iterable[tuple[datetime, float]], start: datetime, threshold: float) -> Outcome:
        """Compara a previsão com o movimento real.

        `threshold` (USD) define o que conta como movimento (ex.: 1 ATR). ACERTO se
        o preço tocar entry ± threshold primeiro na direção prevista; ERRO se tocar
        primeiro na direção contrária; LATERAL se não tocar nenhum.
        """
        sign = 1.0 if direction == "ALTA" else -1.0 if direction == "BAIXA" else 0.0
        mfe, mae, final = 0.0, 0.0, entry
        result, ttr = "LATERAL", None
        for t, p in path:
            final = p
            excursion = (p - entry) * sign if sign else abs(p - entry)
            mfe = max(mfe, excursion)
            mae = max(mae, -excursion if sign else 0.0)
            if sign and result == "LATERAL":
                if excursion >= threshold:
                    result, ttr = "ACERTO", (t - start).total_seconds() / 60
                elif excursion <= -threshold:
                    result, ttr = "ERRO", (t - start).total_seconds() / 60
        if not sign:
            result = "ACERTO" if mfe < threshold else "ERRO"
        return Outcome(result, ttr, round(mfe, 2), round(mae, 2), final)

    def resolve(self, prediction_id: int, path: Iterable[tuple[datetime, float]], threshold: float) -> Outcome:
        row = self.conn.execute("SELECT * FROM predictions WHERE id=?", (prediction_id,)).fetchone()
        if row is None:
            raise KeyError(prediction_id)
        start = datetime.fromisoformat(f"{row['data']}T{row['hora']}").replace(tzinfo=timezone.utc)
        out = self.evaluate_path(row["previsao"], row["preco"], path, start, threshold)
        self.conn.execute(
            """UPDATE predictions SET resultado=?, tempo_ate_reacao_min=?, maxima_favoravel=?, maxima_adversa=?,
               preco_final=?, resolvido_em=? WHERE id=?""",
            (out.result, out.time_to_reaction_min, out.mfe, out.mae, out.final_price,
             datetime.now(timezone.utc).isoformat(), prediction_id),
        )
        self.conn.commit()
        return out

    # ------------------------------------------------------------------ aprendizado
    def accuracy(self, by: str = "sessao") -> list[dict]:
        """TAXA DE ACERTO por: sessao | hora | previsao | horizonte | estagio | evento | score_bucket | sinal_tipo."""
        if by == "score_bucket":
            key = "CASE WHEN score>=70 THEN '>=70' WHEN score>=50 THEN '50-69' WHEN score>-50 THEN '-49..49' WHEN score>-70 THEN '-69..-50' ELSE '<=-70' END"
        elif by == "hora":
            key = "substr(hora,1,2)"
        elif by in ("sessao", "previsao", "horizonte", "estagio", "evento", "sinal_tipo", "nivel_evidencia"):
            key = by
        else:
            raise ValueError(by)
        rows = self.conn.execute(
            f"""SELECT {key} AS chave, COUNT(*) AS n,
                       SUM(CASE WHEN resultado='ACERTO' THEN 1 ELSE 0 END) AS acertos,
                       AVG(tempo_ate_reacao_min) AS tempo_medio,
                       AVG(maxima_favoravel) AS mfe_medio, AVG(maxima_adversa) AS mae_medio
                FROM predictions WHERE resultado IS NOT NULL GROUP BY chave ORDER BY n DESC"""
        ).fetchall()
        return [
            {"chave": r["chave"], "n": r["n"], "acertos": r["acertos"], "taxa": round(r["acertos"] / r["n"], 3) if r["n"] else 0.0,
             "tempo_medio_min": r["tempo_medio"], "mfe_medio": r["mfe_medio"], "mae_medio": r["mae_medio"]}
            for r in rows
        ]

    def factor_power(self) -> list[dict]:
        """Quais fatores mais discriminam ACERTO vs ERRO (aprendizado §30).
        Mede a média do score do fator, alinhado à direção prevista, nos acertos e nos erros."""
        rows = self.conn.execute("SELECT previsao, fundamentos, resultado FROM predictions WHERE resultado IN ('ACERTO','ERRO')").fetchall()
        acc: dict[str, dict[str, list[float]]] = {}
        for r in rows:
            sign = 1.0 if r["previsao"] == "ALTA" else -1.0 if r["previsao"] == "BAIXA" else 0.0
            for name, val in json.loads(r["fundamentos"]).items():
                acc.setdefault(name, {"ACERTO": [], "ERRO": []})[r["resultado"]].append(sign * val)
        out = []
        for name, d in acc.items():
            ma = sum(d["ACERTO"]) / len(d["ACERTO"]) if d["ACERTO"] else 0.0
            me = sum(d["ERRO"]) / len(d["ERRO"]) if d["ERRO"] else 0.0
            out.append({"fator": name, "media_acertos": round(ma, 2), "media_erros": round(me, 2), "poder": round(ma - me, 2), "n": len(d["ACERTO"]) + len(d["ERRO"])})
        return sorted(out, key=lambda x: -x["poder"])

    def metrics(self, path: Iterable[tuple[datetime, float]], threshold: float, horizon_min: int = 240):
        """Precisão, recall, MFE/MAE, lead time e GOLD LEAD SCORE das previsões direcionais registradas,
        confrontadas com o caminho real do preço (evaluation.evaluate)."""
        from .evaluation import SignalRecord, evaluate

        rows = self.conn.execute("SELECT * FROM predictions WHERE previsao IN ('ALTA','BAIXA') ORDER BY id").fetchall()
        sigs = [SignalRecord(datetime.fromisoformat(f"{r['data']}T{r['hora']}").replace(tzinfo=timezone.utc), r["previsao"],
                             r["sinal_tipo"] or "", r["preco"], threshold, r["nivel_evidencia"] or 0, r["probabilidade"], r["confianca"]) for r in rows]
        return evaluate(sigs, list(path), threshold, horizon_min)

    def pending(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM predictions WHERE resultado IS NULL ORDER BY id").fetchall()

    def close(self) -> None:
        self.conn.close()
