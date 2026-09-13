"""Banco de previsões, feedback e aprendizado (Diretriz §29, §30).

PREVISÃO → MOVIMENTO REAL → COMPARAÇÃO → ERRO → APRENDIZADO.
Implementado em SQLite (stdlib) para funcionar em qualquer ambiente.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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
    fatores_ratio TEXT,
    tecnico_detalhe TEXT,
    atr REAL,
    horizonte_min INTEGER DEFAULT 240,
    resultado TEXT,
    tempo_ate_reacao_min REAL,
    maxima_favoravel REAL,
    maxima_adversa REAL,
    preco_final REAL,
    resolvido_em TEXT
);
CREATE INDEX IF NOT EXISTS idx_pred_resultado ON predictions(resultado);
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prediction_id INTEGER,
    aberta_em TEXT NOT NULL,
    modo TEXT NOT NULL,
    sinal_tipo TEXT,
    direcao TEXT NOT NULL,
    entrada REAL NOT NULL,
    stop REAL NOT NULL,
    atr REAL,
    lote REAL,
    risco_usd REAL,
    alvos TEXT,
    estrategia TEXT,
    horizonte_min INTEGER DEFAULT 240,
    status TEXT DEFAULT 'OPEN',
    max_r REAL, mae_r REAL,
    hit_1r INTEGER, hit_2r INTEGER, hit_3r INTEGER, hit_4r INTEGER,
    estopada INTEGER,
    resultados TEXT,
    fechada_em TEXT,
    tese TEXT,
    estado TEXT,
    motivo_saida TEXT,
    resultado_r REAL,
    gerenciada_em TEXT,
    capital REAL,
    risco_pct REAL,
    ticket INTEGER,
    preco_execucao REAL,
    sl_real REAL,
    tp_real REAL,
    slippage REAL,
    execucao TEXT,
    resultado_financeiro REAL,
    tempo_operacao_min REAL,
    lead_time_min REAL,
    score_entrada REAL,
    probabilidade REAL,
    confianca REAL
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hora TEXT NOT NULL,
    preco REAL, score REAL, direcao TEXT, acao TEXT, motivo TEXT, atr REAL,
    nivel_evidencia INTEGER, confianca REAL,
    r_hipotetico REAL, resolvido INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS prices (
    hora TEXT PRIMARY KEY,
    close REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hora TEXT NOT NULL,
    capital REAL NOT NULL,
    pnl REAL,
    nota TEXT
);
CREATE TABLE IF NOT EXISTS trade_monitor (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id INTEGER NOT NULL,
    hora TEXT NOT NULL,
    preco REAL, r_atual REAL,
    trade_score REAL, thesis_score REAL, exit_score REAL, profit_potential REAL,
    acao TEXT, nota TEXT
);
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
    def record(self, a: Assessment, signal_type: Optional[str] = None, atr: Optional[float] = None, horizon_min: int = 240) -> int:
        from .evaluation import technical_details

        t = a.time.astimezone(timezone.utc)
        direction = a.direction.value
        prob = {"ALTA": a.prob_up, "BAIXA": a.prob_down, "LATERAL": a.prob_flat}[direction]
        fund = {f.name: f.score for f in a.factors}
        cur = self.conn.execute(
            """INSERT INTO predictions (data, hora, sessao, preco, previsao, probabilidade, confianca, score,
               horizonte, estagio, fundamentos, noticias, dolar, juros, fluxo, tecnico, evento, sinal_tipo, nivel_evidencia,
               fatores_ratio, tecnico_detalhe, atr, horizonte_min)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                t.strftime("%Y-%m-%d"), t.strftime("%H:%M:%S"), session_label(t), a.price, direction, prob,
                a.confidence, a.score, a.horizon, a.premove.stage.value, json.dumps(fund, ensure_ascii=False),
                json.dumps([], ensure_ascii=False), fund.get("dolar"), fund.get("juros_reais"), fund.get("fluxo"),
                fund.get("tecnico"), a.next_event.name if a.next_event else None, signal_type, int(a.evidence_level),
                json.dumps({f.name: round(f.ratio, 3) for f in a.factors if f.available}), json.dumps(technical_details(a)),
                atr, horizon_min,
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

    # ------------------------------------------------------------------ 2.1: resolução automática no loop live
    def auto_resolve(self, candles: Iterable, now: datetime, default_threshold: float, horizon_min: int = 240) -> list[tuple[int, Outcome]]:
        """Resolve previsões pendentes usando os candles mais recentes (M1/M5): ACERTO/ERRO quando o preço
        tocar ±limiar (1 ATR da previsão, ou `default_threshold`) dentro do horizonte; LATERAL ao expirar."""
        cs = sorted(candles, key=lambda c: c.time)
        done: list[tuple[int, Outcome]] = []
        for row in self.pending():
            start = datetime.fromisoformat(f"{row['data']}T{row['hora']}").replace(tzinfo=timezone.utc)
            horizon = row["horizonte_min"] or horizon_min
            path = [(c.time, c.close) for c in cs if start < c.time <= start + timedelta(minutes=horizon)]
            if not path:
                continue
            thr = row["atr"] or default_threshold
            out = self.evaluate_path(row["previsao"], row["preco"], path, start, thr)
            expired = now >= start + timedelta(minutes=horizon)
            if out.result in ("ACERTO", "ERRO") or expired:
                self.conn.execute(
                    """UPDATE predictions SET resultado=?, tempo_ate_reacao_min=?, maxima_favoravel=?, maxima_adversa=?,
                       preco_final=?, resolvido_em=? WHERE id=?""",
                    (out.result, out.time_to_reaction_min, out.mfe, out.mae, out.final_price, now.isoformat(), row["id"]))
                done.append((row["id"], out))
        if done:
            self.conn.commit()
        return done

    # ------------------------------------------------------------------ 2.2: operações simuladas
    def open_trade(self, plan, mode: str, prediction_id: Optional[int] = None, horizon_min: int = 240) -> int:
        cur = self.conn.execute(
            """INSERT INTO trades (prediction_id, aberta_em, modo, sinal_tipo, direcao, entrada, stop, atr, lote, risco_usd, alvos,
               estrategia, horizonte_min) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (prediction_id, plan.time.astimezone(timezone.utc).isoformat(), mode, plan.signal_type, plan.direction.value, plan.entry,
             plan.stop, plan.atr, plan.lots, plan.risk_usd, json.dumps(plan.targets), plan.recommended, horizon_min))
        self.conn.commit()
        return int(cur.lastrowid)

    def open_trades(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM trades WHERE status IN ('OPEN','MANAGED_CLOSED') ORDER BY id").fetchall()

    # ------------------------------------------------------------------ 3.0: execução, capital, resultado
    def save_execution(self, trade_id: int, report, capital: float, risk_pct: float, assessment=None) -> None:
        self.conn.execute(
            """UPDATE trades SET ticket=?, preco_execucao=?, sl_real=?, tp_real=?, slippage=?, execucao=?, capital=?, risco_pct=?,
               score_entrada=?, probabilidade=?, confianca=? WHERE id=?""",
            (report.ticket if report else None, report.fill_price if report else None, report.real_sl if report else None,
             report.real_tp if report else None, report.slippage if report else None, report.render() if report else None, capital, risk_pct,
             assessment.score if assessment else None, max(assessment.prob_up, assessment.prob_down) if assessment else None,
             assessment.confidence if assessment else None, trade_id))
        self.conn.commit()

    def save_financial_result(self, trade_id: int, pnl_usd: float, minutes: Optional[float], lead_time_min: Optional[float] = None) -> None:
        self.conn.execute("UPDATE trades SET resultado_financeiro=?, tempo_operacao_min=?, lead_time_min=? WHERE id=?", (pnl_usd, minutes, lead_time_min, trade_id))
        self.conn.commit()

    def record_equity(self, t: datetime, equity: float, pnl: Optional[float] = None, note: str = "") -> None:
        self.conn.execute("INSERT INTO account (hora, capital, pnl, nota) VALUES (?,?,?,?)", (t.isoformat(), equity, pnl, note))
        self.conn.commit()

    def last_equity(self) -> Optional[float]:
        r = self.conn.execute("SELECT capital FROM account ORDER BY id DESC LIMIT 1").fetchone()
        return float(r["capital"]) if r else None

    def equity_curve(self) -> list[tuple[datetime, float]]:
        return [(datetime.fromisoformat(r["hora"]), r["capital"]) for r in self.conn.execute("SELECT hora, capital FROM account ORDER BY id").fetchall()]

    def performance_summary(self) -> str:
        rows = self.conn.execute("SELECT resultado_financeiro AS p, resultado_r AS r, modo FROM trades WHERE resultado_financeiro IS NOT NULL").fetchall()
        curve = self.equity_curve()
        if not rows and not curve:
            return "📈 PERFORMANCE — sem operações com resultado financeiro"
        pnl = sum(r["p"] for r in rows)
        wins = [r["p"] for r in rows if r["p"] > 0]
        lines = ["📈 PERFORMANCE ENGINE"]
        if curve:
            lines.append(f"  capital inicial {curve[0][1]:,.2f} → atual {curve[-1][1]:,.2f} USD ({(curve[-1][1] / curve[0][1] - 1) * 100:+.2f}%)")
        if rows:
            lines.append(f"  operações {len(rows)} · resultado {pnl:+,.2f} USD · win rate {len(wins) / len(rows):.0%} · média {pnl / len(rows):+,.2f} USD")
            by_mode = {}
            for r in rows:
                by_mode.setdefault(r["modo"], []).append(r["p"])
            for m, v in by_mode.items():
                lines.append(f"    {m}: n={len(v)} {sum(v):+,.2f} USD")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 2.3: trade monitor
    def save_thesis(self, trade_id: int, thesis, state: dict) -> None:
        self.conn.execute("UPDATE trades SET tese=?, estado=? WHERE id=?", (json.dumps(thesis.to_dict()), json.dumps(state), trade_id))
        self.conn.commit()

    def save_state(self, trade_id: int, state: dict) -> None:
        self.conn.execute("UPDATE trades SET estado=? WHERE id=?", (json.dumps(state), trade_id))
        self.conn.commit()

    def log_monitor(self, trade_id: int, reading) -> None:
        self.conn.execute(
            "INSERT INTO trade_monitor (trade_id, hora, preco, r_atual, trade_score, thesis_score, exit_score, profit_potential, acao, nota) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (trade_id, reading.time.isoformat(), reading.price, reading.current_r, reading.trade_score, reading.thesis_score,
             reading.exit_score, reading.profit_potential, reading.action, reading.note))
        self.conn.commit()

    def close_managed(self, trade_id: int, result_r: float, reason: str, t: datetime, state: dict) -> None:
        """Fechada pelo monitor; continua sendo acompanhada até o horizonte para medir o que ficou na mesa."""
        self.conn.execute("UPDATE trades SET status='MANAGED_CLOSED', resultado_r=?, motivo_saida=?, gerenciada_em=?, estado=? WHERE id=?",
                          (result_r, reason, t.isoformat(), json.dumps(state), trade_id))
        self.conn.commit()

    def managed_trades(self) -> list:
        """Reconstrói as operações abertas gerenciadas pelo monitor (ManagedTrade)."""
        from .models import Direction
        from .monitor import ManagedTrade, Thesis
        from .trading import TradePlan

        out = []
        for r in self.conn.execute("SELECT * FROM trades WHERE status='OPEN' AND tese IS NOT NULL ORDER BY id").fetchall():
            plan = TradePlan(Direction(r["direcao"]), r["entrada"], r["stop"], r["atr"] or 0.0, datetime.fromisoformat(r["aberta_em"]),
                             targets=json.loads(r["alvos"] or "{}"), recommended=r["estrategia"] or "3R", signal_type=r["sinal_tipo"] or "", lots=r["lote"], risk_usd=r["risco_usd"])
            tr = ManagedTrade(r["id"], plan, Thesis.from_dict(json.loads(r["tese"]))).load_state(json.loads(r["estado"] or "{}"))
            last = self.conn.execute("SELECT hora FROM trade_monitor WHERE trade_id=? ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
            if last:
                from .monitor import MonitorReading
                tr.history.append(MonitorReading(datetime.fromisoformat(last["hora"]), plan.entry, 0.0, 0.0, 0.0, 0.0, 0.0, "MANTER"))
            out.append(tr)
        return out

    def monitor_history(self, trade_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM trade_monitor WHERE trade_id=? ORDER BY id", (trade_id,)).fetchall()

    def exit_learning_rows(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM trades WHERE motivo_saida IS NOT NULL").fetchall()
        out = []
        for r in rows:
            hist = self.monitor_history(r["id"])
            thesis0 = json.loads(r["tese"])["score"] if r["tese"] else None
            last = hist[-1] if hist else None
            out.append({"exit_reason": r["motivo_saida"], "result_r": r["resultado_r"] or 0.0,
                        "max_r_after": r["max_r"] if r["status"] == "CLOSED" else None,
                        "min_r_after": (-(r["mae_r"] or 0.0)) if r["status"] == "CLOSED" else None,
                        "thesis_at_exit": last["thesis_score"] if last else None,
                        "drop_at_exit": (thesis0 - last["trade_score"]) if (last and thesis0 is not None) else None})
        return out

    def exit_learning(self) -> str:
        from .monitor import exit_learning
        return exit_learning(self.exit_learning_rows())

    def auto_resolve_trades(self, candles: Iterable, now: datetime) -> list[tuple[int, dict]]:
        """Simula cada operação aberta com os candles reais (todas as estratégias). Fecha quando o stop
        inicial é tocado, quando todas as estratégias saíram, ou ao expirar o horizonte."""
        from .models import Direction
        from .trading import TradePlan, simulate_all

        cs = sorted(candles, key=lambda c: c.time)
        done: list[tuple[int, dict]] = []
        for row in self.open_trades():
            t0 = datetime.fromisoformat(row["aberta_em"])
            horizon = row["horizonte_min"] or 240
            if not any(c.time > t0 for c in cs):
                continue
            plan = TradePlan(Direction(row["direcao"]), row["entrada"], row["stop"], row["atr"] or 0.0, t0)
            sim = simulate_all(plan, cs, horizon)
            prof = sim["profile"]
            expired = now >= t0 + timedelta(minutes=horizon) or prof.horizon_reached
            all_closed = all(r.exit_reason != "OPEN" for r in sim["details"].values())
            if prof.stopped or expired or all_closed:
                if row["status"] == "MANAGED_CLOSED" and not (prof.stopped or expired):
                    continue  # segue acompanhando até o stop inicial ou o horizonte
                self.conn.execute(
                    """UPDATE trades SET status='CLOSED', max_r=?, mae_r=?, hit_1r=?, hit_2r=?, hit_3r=?, hit_4r=?, estopada=?, resultados=?, fechada_em=? WHERE id=?""",
                    (prof.max_r_before_stop, prof.mae_r, int(prof.hit(1)), int(prof.hit(2)), int(prof.hit(3)), int(prof.hit(4)),
                     int(prof.stopped), json.dumps(sim["results"]), now.isoformat(), row["id"]))
                done.append((row["id"], sim))
        if done:
            self.conn.commit()
        return done

    def r_stats(self):
        from .trading import ExcursionProfile, r_stats

        rows = self.conn.execute("SELECT * FROM trades WHERE status='CLOSED'").fetchall()
        recs = []
        for r in rows:
            results = json.loads(r["resultados"] or "{}")
            if r["resultado_r"] is not None:
                results["adaptive"] = r["resultado_r"]
            recs.append({"type": r["sinal_tipo"] or "?", "results": results,
                         "profile": ExcursionProfile(r["max_r"] or 0.0, r["mae_r"] or 0.0, bool(r["estopada"]), False, 0)})
        return r_stats(recs)

    # ------------------------------------------------------------------ 3.0: OPPORTUNITY ENGINE
    def record_decision(self, rec) -> int:
        cur = self.conn.execute(
            "INSERT INTO decisions (hora, preco, score, direcao, acao, motivo, atr, nivel_evidencia, confianca) VALUES (?,?,?,?,?,?,?,?,?)",
            (rec.time.isoformat(), rec.price, rec.score, rec.direction, rec.action, rec.reason[:300], rec.atr, rec.evidence_level, rec.confidence))
        self.conn.commit()
        return int(cur.lastrowid)

    def store_prices(self, candles: Iterable) -> int:
        rows = [(c.time.astimezone(timezone.utc).isoformat(), c.close) for c in candles]
        if not rows:
            return 0
        self.conn.executemany("INSERT OR IGNORE INTO prices (hora, close) VALUES (?,?)", rows)
        self.conn.commit()
        return len(rows)

    def prices(self, since: Optional[datetime] = None) -> list[tuple[datetime, float]]:
        q = "SELECT hora, close FROM prices" + (" WHERE hora >= ?" if since else "") + " ORDER BY hora"
        rows = self.conn.execute(q, (since.isoformat(),) if since else ()).fetchall()
        return [(datetime.fromisoformat(r["hora"]), r["close"]) for r in rows]

    def resolve_hypotheticals(self, now: datetime, horizon_min: int = 240) -> int:
        """Preenche o resultado hipotético (3R, stop 1.2 ATR) das decisões bloqueadas com os preços gravados."""
        from .models import Candle
        from .opportunity import DecisionRecord, hypothetical_trade

        rows = self.conn.execute("SELECT * FROM decisions WHERE resolvido=0 AND acao NOT IN ('ENTRADA','SEM_SINAL') AND direcao IN ('ALTA','BAIXA')").fetchall()
        if not rows:
            return 0
        prices = self.prices()
        candles = [Candle(t, p, p, p, p, 0.0) for t, p in prices]
        n = 0
        for r in rows:
            t0 = datetime.fromisoformat(r["hora"])
            rec = DecisionRecord(t0, r["preco"], r["score"], r["direcao"], r["acao"], r["motivo"] or "", r["atr"] or 0.0)
            expired = now >= t0 + timedelta(minutes=horizon_min)
            res = hypothetical_trade(rec, candles, horizon_min)
            if res is not None or expired:
                self.conn.execute("UPDATE decisions SET r_hipotetico=?, resolvido=1 WHERE id=?", (res, r["id"]))
                n += 1
        self.conn.commit()
        return n

    def decisions(self, since: Optional[datetime] = None) -> list:
        from .opportunity import DecisionRecord

        q = "SELECT * FROM decisions" + (" WHERE hora >= ?" if since else "") + " ORDER BY id"
        rows = self.conn.execute(q, (since.isoformat(),) if since else ()).fetchall()
        return [DecisionRecord(datetime.fromisoformat(r["hora"]), r["preco"], r["score"] or 0.0, r["direcao"] or "LATERAL", r["acao"], r["motivo"] or "",
                               r["atr"] or 0.0, r["r_hipotetico"], r["nivel_evidencia"] or 0, r["confianca"] or 0.0) for r in rows]

    def opportunity_report(self, horizon_min: int = 240, since: Optional[datetime] = None):
        from .opportunity import opportunity_report

        decisions = self.decisions(since)
        prices = self.prices(since)
        entries = [(datetime.fromisoformat(r["aberta_em"]), r["direcao"]) for r in self.conn.execute("SELECT aberta_em, direcao FROM trades").fetchall()]
        atrs = [r["atr"] for r in self.conn.execute("SELECT atr FROM trades WHERE atr IS NOT NULL").fetchall()] or [d.atr for d in decisions if d.atr]
        threshold = (sum(atrs) / len(atrs)) if atrs else 9.0
        trade_rows = [{"score": r["score_entrada"] or 0.0, "r": r["resultado_r"]} for r in
                      self.conn.execute("SELECT score_entrada, resultado_r FROM trades WHERE resultado_r IS NOT NULL").fetchall()]
        # decisões bloqueadas com resultado hipotético também alimentam a curva de limiar
        trade_rows += [{"score": d.score, "r": d.hypothetical_r} for d in decisions if d.hypothetical_r is not None]
        return opportunity_report(decisions, prices, entries, threshold, horizon_min, trade_rows)

    def resolved_records(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM predictions WHERE resultado IN ('ACERTO','ERRO') AND previsao IN ('ALTA','BAIXA')").fetchall()
        return [{"direction": r["previsao"], "hit": r["resultado"] == "ACERTO", "probability": r["probabilidade"],
                 "factors": json.loads(r["fatores_ratio"] or "{}"), "technical": json.loads(r["tecnico_detalhe"] or "{}"),
                 "lead": r["tempo_ate_reacao_min"], "type": r["sinal_tipo"]} for r in rows]

    def calibration(self):
        from .validation import calibration_table
        return calibration_table((r["probability"], r["hit"]) for r in self.resolved_records())

    def scoreboard(self):
        from .validation import factor_scoreboard
        return factor_scoreboard(self.resolved_records())

    def fit_calibrator(self):
        from .validation import IsotonicCalibrator
        return IsotonicCalibrator().fit((r["probability"], r["hit"]) for r in self.resolved_records())

    def lead_time_stats(self) -> dict:
        """⏱️ lead time das previsões que acertaram, por tipo de sinal."""
        rows = self.conn.execute("SELECT sinal_tipo, tempo_ate_reacao_min FROM predictions WHERE resultado='ACERTO' AND tempo_ate_reacao_min IS NOT NULL").fetchall()
        by: dict[str, list[float]] = {}
        for r in rows:
            by.setdefault(r["sinal_tipo"] or "?", []).append(r["tempo_ate_reacao_min"])
        allv = [v for vs in by.values() for v in vs]
        return {"media": (sum(allv) / len(allv)) if allv else None, "n": len(allv),
                "por_tipo": {k: sum(v) / len(v) for k, v in by.items()}}

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
