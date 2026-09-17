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
    hora TEXT NOT NULL,
    ativo TEXT NOT NULL DEFAULT 'XAUUSD',
    close REAL NOT NULL,
    PRIMARY KEY (hora, ativo)
);
CREATE TABLE IF NOT EXISTS edge_reports (
    data TEXT PRIMARY KEY,
    hora TEXT NOT NULL,
    texto TEXT NOT NULL,
    dados TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS account (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hora TEXT NOT NULL,
    capital REAL NOT NULL,
    pnl REAL,
    nota TEXT
);
CREATE TABLE IF NOT EXISTS reactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evento_id TEXT, ativo TEXT, tipo TEXT, publicado TEXT, direcao_esperada REAL,
    t_primeira REAL, t_confirmacao REAL, t_pleno REAL, mfe REAL, mae REAL, direcao_ok INTEGER,
    lead_usd REAL, lead_yield REAL, horizonte INTEGER, resolucao INTEGER, conhecido_em TEXT,
    UNIQUE(evento_id, ativo)
);
CREATE TABLE IF NOT EXISTS flow_anomalies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE, ativo TEXT, hora TEXT, flow_score INTEGER, atr_move REAL, minutos REAL, volume_ratio REAL,
    persistencia INTEGER, cross_market INTEGER, origem TEXT, assinatura TEXT, leader TEXT, regime TEXT, direcao REAL,
    preco REAL, atr REAL,
    mfe5 REAL, mae5 REAL, mfe15 REAL, mae15 REAL, mfe30 REAL, mae30 REAL, mfe60 REAL, mae60 REAL,
    fechamento60 REAL, resultado TEXT, confirm_min REAL, medido_em TEXT
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
        self._migrate()

    def _migrate(self) -> None:
        """4.0: coluna `ativo` (símbolo) nas tabelas por mercado; bancos antigos = XAUUSD."""
        for table in ("predictions", "trades", "decisions"):
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if "ativo" not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN ativo TEXT DEFAULT 'XAUUSD'")
        pcols = {r["name"] for r in self.conn.execute("PRAGMA table_info(prices)").fetchall()}
        if "ativo" not in pcols:
            # 5.x: preços por mercado — a tabela antiga (hora única) guardava só o primeiro mercado de cada minuto
            self.conn.executescript("""
                CREATE TABLE prices_v2 (hora TEXT NOT NULL, ativo TEXT NOT NULL DEFAULT 'XAUUSD', close REAL NOT NULL, PRIMARY KEY (hora, ativo));
                INSERT OR IGNORE INTO prices_v2 (hora, ativo, close) SELECT hora, 'XAUUSD', close FROM prices;
                DROP TABLE prices;
                ALTER TABLE prices_v2 RENAME TO prices;""")
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(decisions)").fetchall()}
        if "etapa" not in cols:
            self.conn.execute("ALTER TABLE decisions ADD COLUMN etapa TEXT")
        if "bruta" not in cols:
            self.conn.execute("ALTER TABLE decisions ADD COLUMN bruta INTEGER DEFAULT 0")
        self.conn.commit()

    @staticmethod
    def _where_symbol(symbol: Optional[str], prefix: str = "WHERE") -> tuple[str, tuple]:
        return (f" {prefix} ativo=?", (symbol,)) if symbol else ("", ())

    # ------------------------------------------------------------------ registro
    def record(self, a: Assessment, signal_type: Optional[str] = None, atr: Optional[float] = None, horizon_min: int = 240,
               symbol: str = "XAUUSD") -> int:
        from .evaluation import technical_details

        t = a.time.astimezone(timezone.utc)
        direction = a.direction.value
        prob = {"ALTA": a.prob_up, "BAIXA": a.prob_down, "LATERAL": a.prob_flat}[direction]
        fund = {f.name: f.score for f in a.factors}
        cur = self.conn.execute(
            """INSERT INTO predictions (data, hora, sessao, preco, previsao, probabilidade, confianca, score,
               horizonte, estagio, fundamentos, noticias, dolar, juros, fluxo, tecnico, evento, sinal_tipo, nivel_evidencia,
               fatores_ratio, tecnico_detalhe, atr, horizonte_min, ativo)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                t.strftime("%Y-%m-%d"), t.strftime("%H:%M:%S"), session_label(t), a.price, direction, prob,
                a.confidence, a.score, a.horizon, a.premove.stage.value, json.dumps(fund, ensure_ascii=False),
                json.dumps([], ensure_ascii=False), fund.get("dolar"), fund.get("juros_reais"), fund.get("fluxo"),
                fund.get("tecnico"), a.next_event.name if a.next_event else None, signal_type, int(a.evidence_level),
                json.dumps({f.name: round(f.ratio, 3) for f in a.factors if f.available}), json.dumps(technical_details(a)),
                atr, horizon_min, symbol,
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
        """TAXA DE ACERTO por: sessao | hora | previsao | horizonte | estagio | evento | score_bucket | sinal_tipo | ativo."""
        if by == "score_bucket":
            key = "CASE WHEN score>=70 THEN '>=70' WHEN score>=50 THEN '50-69' WHEN score>-50 THEN '-49..49' WHEN score>-70 THEN '-69..-50' ELSE '<=-70' END"
        elif by == "hora":
            key = "substr(hora,1,2)"
        elif by in ("sessao", "previsao", "horizonte", "estagio", "evento", "sinal_tipo", "nivel_evidencia", "ativo"):
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
    def auto_resolve(self, candles: Iterable, now: datetime, default_threshold: float, horizon_min: int = 240,
                     symbol: Optional[str] = None) -> list[tuple[int, Outcome]]:
        """Resolve previsões pendentes usando os candles mais recentes (M1/M5): ACERTO/ERRO quando o preço
        tocar ±limiar (1 ATR da previsão, ou `default_threshold`) dentro do horizonte; LATERAL ao expirar.

        `symbol`: só as previsões DESSE mercado — os candles são de um mercado só; sem o filtro, a previsão do
        EURUSD (1,15) era comparada com o preço do ouro (3 600) e "resolvia" no primeiro candle."""
        cs = sorted(candles, key=lambda c: c.time)
        done: list[tuple[int, Outcome]] = []
        for row in self.pending(symbol):
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
    def open_trade(self, plan, mode: str, prediction_id: Optional[int] = None, horizon_min: int = 240, symbol: str = "XAUUSD") -> int:
        cur = self.conn.execute(
            """INSERT INTO trades (prediction_id, aberta_em, modo, sinal_tipo, direcao, entrada, stop, atr, lote, risco_usd, alvos,
               estrategia, horizonte_min, ativo) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (prediction_id, plan.time.astimezone(timezone.utc).isoformat(), mode, plan.signal_type, plan.direction.value, plan.entry,
             plan.stop, plan.atr, plan.lots, plan.risk_usd, json.dumps(plan.targets), plan.recommended, horizon_min, symbol))
        self.conn.commit()
        return int(cur.lastrowid)

    def open_trades(self, symbol: Optional[str] = None) -> list[sqlite3.Row]:
        w, args = self._where_symbol(symbol, "AND")
        return self.conn.execute(f"SELECT * FROM trades WHERE status IN ('OPEN','MANAGED_CLOSED'){w} ORDER BY id", args).fetchall()

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

    def trades_between(self, t0: datetime, t1: datetime, symbol: Optional[str] = None) -> list[dict]:
        """Operações abertas OU fechadas no intervalo [t0, t1) — base da EFICIÊNCIA DO DIA."""
        conds = ["((aberta_em >= ? AND aberta_em < ?) OR (fechada_em >= ? AND fechada_em < ?))"]
        args: list = [t0.isoformat(), t1.isoformat(), t0.isoformat(), t1.isoformat()]
        if symbol:
            conds.append("ativo = ?"); args.append(symbol)
        rows = self.conn.execute("SELECT * FROM trades WHERE " + " AND ".join(conds) + " ORDER BY id", tuple(args)).fetchall()
        return [dict(r) for r in rows]

    def account_rows(self) -> list[tuple[datetime, float, Optional[float], str]]:
        out = []
        for r in self.conn.execute("SELECT hora, capital, pnl, nota FROM account ORDER BY id").fetchall():
            try:
                t = datetime.fromisoformat(r["hora"])
                t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            out.append((t, float(r["capital"]), r["pnl"], r["nota"] or ""))
        return out

    def neutralize_baseline_syncs(self, min_fraction: float = 0.25) -> int:
        """Reparo de registros antigos: um 'sync broker' que muda ≥ 25% do capital num único ciclo não é resultado de operação —
        é a diferença entre o --equity de partida e o saldo real da conta (versões anteriores gravavam isso como lucro do dia).
        Marca como linha de base (pnl NULL) para não travar a meta diária nem a perda diária após reinício."""
        rows = self.conn.execute("SELECT id, capital, pnl, nota FROM account WHERE nota = 'sync broker' AND pnl IS NOT NULL").fetchall()
        ids = [r["id"] for r in rows if r["capital"] and abs(float(r["pnl"])) >= min_fraction * float(r["capital"])]
        for i in ids:
            self.conn.execute("UPDATE account SET pnl = NULL, nota = 'linha de base do broker (reparado)' WHERE id = ?", (i,))
        if ids:
            self.conn.commit()
        return len(ids)

    def equity_curve(self) -> list[tuple[datetime, float]]:
        return [(datetime.fromisoformat(r["hora"]), r["capital"]) for r in self.conn.execute("SELECT hora, capital FROM account ORDER BY id").fetchall()]

    # ------------------------------------------------------------------ 4.0: LIVE EDGE diário
    def save_edge_report(self, report) -> None:
        self.conn.execute("INSERT OR REPLACE INTO edge_reports (data, hora, texto, dados) VALUES (?,?,?,?)",
                          (report.date, datetime.now(timezone.utc).isoformat(), report.render(), report.to_json()))
        self.conn.commit()

    def last_edge_date(self) -> Optional[str]:
        r = self.conn.execute("SELECT data FROM edge_reports ORDER BY data DESC LIMIT 1").fetchone()
        return r["data"] if r else None

    def edge_history(self) -> list[dict]:
        return [json.loads(r["dados"]) for r in self.conn.execute("SELECT dados FROM edge_reports ORDER BY data").fetchall()]

    def per_market_summary(self) -> list[dict]:
        """4.0: operações, expectancy e win rate por ativo (o que foi vivido)."""
        rows = self.conn.execute("SELECT ativo, COUNT(*) n, AVG(resultado_r) e, SUM(CASE WHEN resultado_r>0 THEN 1 ELSE 0 END) w, SUM(resultado_financeiro) p "
                                 "FROM trades WHERE resultado_r IS NOT NULL GROUP BY ativo ORDER BY e DESC").fetchall()
        return [{"symbol": r["ativo"], "n": r["n"], "expectancy": r["e"] or 0.0, "win_rate": (r["w"] / r["n"]) if r["n"] else 0.0, "pnl": r["p"] or 0.0} for r in rows]

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
            for m in self.per_market_summary():
                lines.append(f"    {m['symbol']}: n={m['n']} E={m['expectancy']:+.2f}R win {m['win_rate']:.0%} {m['pnl']:+,.2f} USD")
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

    def managed_trades(self, symbol: Optional[str] = None) -> list:
        """Reconstrói as operações abertas gerenciadas pelo monitor (ManagedTrade)."""
        from .models import Direction
        from .monitor import ManagedTrade, Thesis
        from .trading import TradePlan

        out = []
        w, args = self._where_symbol(symbol, "AND")
        for r in self.conn.execute(f"SELECT * FROM trades WHERE status='OPEN' AND tese IS NOT NULL{w} ORDER BY id", args).fetchall():
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

    def _simulate_trade_row(self, row: sqlite3.Row, cs: list, now: datetime) -> Optional[dict]:
        """Simula uma operação com candles do SEU mercado; grava o perfil (max R, MAE, 1R…4R, estopada) quando fecha."""
        from .models import Direction
        from .trading import TradePlan, simulate_all

        t0 = datetime.fromisoformat(row["aberta_em"])
        horizon = row["horizonte_min"] or 240
        if not any(c.time > t0 for c in cs):
            return None
        plan = TradePlan(Direction(row["direcao"]), row["entrada"], row["stop"], row["atr"] or 0.0, t0)
        sim = simulate_all(plan, cs, horizon)
        prof = sim["profile"]
        expired = now >= t0 + timedelta(minutes=horizon) or prof.horizon_reached
        all_closed = all(r.exit_reason != "OPEN" for r in sim["details"].values())
        if not (prof.stopped or expired or all_closed):
            return None
        if row["status"] == "MANAGED_CLOSED" and not (prof.stopped or expired):
            return None  # segue acompanhando até o stop inicial ou o horizonte
        self.conn.execute(
            """UPDATE trades SET status='CLOSED', max_r=?, mae_r=?, hit_1r=?, hit_2r=?, hit_3r=?, hit_4r=?, estopada=?, resultados=?, fechada_em=? WHERE id=?""",
            (prof.max_r_before_stop, prof.mae_r, int(prof.hit(1)), int(prof.hit(2)), int(prof.hit(3)), int(prof.hit(4)),
             int(prof.stopped), json.dumps(sim["results"]), now.isoformat(), row["id"]))
        return sim

    def auto_resolve_trades(self, candles: Iterable, now: datetime, symbol: Optional[str] = None) -> list[tuple[int, dict]]:
        """Simula cada operação aberta com os candles reais (todas as estratégias). Fecha quando o stop
        inicial é tocado, quando todas as estratégias saíram, ou ao expirar o horizonte.
        `symbol`: só as operações desse mercado (os candles são de um mercado só)."""
        cs = sorted(candles, key=lambda c: c.time)
        done: list[tuple[int, dict]] = []
        for row in self.open_trades(symbol):
            sim = self._simulate_trade_row(row, cs, now)
            if sim is not None:
                done.append((row["id"], sim))
        if done:
            self.conn.commit()
        return done

    def results_chrono(self, symbol: str, include_shadow: bool = True) -> list[float]:
        """R por operação fechada, em ordem de fechamento (inclui SOMBRA/PAPER quando pedido)."""
        rows = self.conn.execute("SELECT resultado_r, modo FROM trades WHERE ativo=? AND resultado_r IS NOT NULL ORDER BY COALESCE(fechada_em, aberta_em), id", (symbol,)).fetchall()
        return [float(r["resultado_r"]) for r in rows if include_shadow or r["modo"] != "SHADOW"]

    def r_stats(self, symbol: Optional[str] = None):
        from .trading import ExcursionProfile, r_stats

        w, args = self._where_symbol(symbol, "AND")
        rows = self.conn.execute(f"SELECT * FROM trades WHERE status='CLOSED'{w}", args).fetchall()
        recs = []
        for r in rows:
            results = json.loads(r["resultados"] or "{}")
            if r["resultado_r"] is not None:
                results["adaptive"] = r["resultado_r"]
            recs.append({"type": r["sinal_tipo"] or "?", "results": results,
                         "profile": ExcursionProfile(r["max_r"] or 0.0, r["mae_r"] or 0.0, bool(r["estopada"]), False, 0)})
        return r_stats(recs)

    # ------------------------------------------------------------------ 4.0: REACTION ENGINE
    def save_reaction(self, rec) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO reactions (evento_id, ativo, tipo, publicado, direcao_esperada, t_primeira, t_confirmacao, t_pleno, mfe, mae, direcao_ok, "
            "lead_usd, lead_yield, horizonte, resolucao, conhecido_em) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec.event_id, rec.target, rec.kind, rec.published_at.isoformat(), rec.expected_dir, rec.time_to_first, rec.time_to_confirmation, rec.time_to_full_move,
             rec.max_move_atr, rec.max_adverse_atr, None if rec.direction_correct is None else int(rec.direction_correct), rec.lead_times.get("USD"),
             rec.lead_times.get("YIELD"), rec.horizon_min, rec.resolution_min, rec.known_at.isoformat()))
        self.conn.commit()

    def reaction_records(self, symbol: Optional[str] = None) -> list:
        from .reaction import ReactionRecord
        where, params = self._where_symbol(symbol)
        out = []
        for r in self.conn.execute(f"SELECT * FROM reactions {where} ORDER BY publicado", params).fetchall():
            rec = ReactionRecord(r["evento_id"], r["tipo"], datetime.fromisoformat(r["publicado"]), r["ativo"], r["direcao_esperada"], r["t_primeira"],
                                 r["t_confirmacao"], r["t_pleno"], r["mfe"] or 0.0, r["mae"] or 0.0, None if r["direcao_ok"] is None else bool(r["direcao_ok"]),
                                 {"USD": r["lead_usd"], "YIELD": r["lead_yield"]}, r["horizonte"] or 240, r["resolucao"] or 5)
            out.append(rec)
        return out

    # ------------------------------------------------------------------ 5.2: FLOW ANOMALY ledger (registrar → medir → aprender)
    def open_flow_anomaly(self, rec: dict) -> Optional[int]:
        cols = ("event_id", "ativo", "hora", "flow_score", "atr_move", "minutos", "volume_ratio", "persistencia", "cross_market", "origem", "assinatura",
                "leader", "regime", "direcao", "preco", "atr")
        cur = self.conn.execute(f"INSERT OR IGNORE INTO flow_anomalies ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", tuple(rec.get(c) for c in cols))
        self.conn.commit()
        return cur.lastrowid if cur.rowcount else None

    def save_measured_flow_anomaly(self, rec: dict) -> bool:
        """Registro já medido (replay histórico): insere completo; ignora se o event_id já existe."""
        cols = ("event_id", "ativo", "hora", "flow_score", "atr_move", "minutos", "volume_ratio", "persistencia", "cross_market", "origem", "assinatura",
                "leader", "regime", "direcao", "preco", "atr", "mfe5", "mae5", "mfe15", "mae15", "mfe30", "mae30", "mfe60", "mae60", "fechamento60",
                "resultado", "confirm_min", "medido_em")
        cur = self.conn.execute(f"INSERT OR IGNORE INTO flow_anomalies ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", tuple(rec.get(c) for c in cols))
        self.conn.commit()
        return bool(cur.rowcount)

    def recent_flow_anomaly(self, symbol: str, since: datetime) -> bool:
        return self.conn.execute("SELECT 1 FROM flow_anomalies WHERE ativo=? AND hora>=? AND event_id NOT LIKE 'hist_%'", (symbol, since.isoformat())).fetchone() is not None

    def pending_flow_anomalies(self, now: datetime, min_age_min: int = 60) -> list[dict]:
        cutoff = (now - timedelta(minutes=min_age_min)).isoformat()
        return [dict(r) for r in self.conn.execute("SELECT * FROM flow_anomalies WHERE resultado IS NULL AND hora<=? ORDER BY id", (cutoff,)).fetchall()]

    def close_flow_anomaly(self, row_id: int, measures: dict, now: datetime) -> None:
        keys = ("mfe5", "mae5", "mfe15", "mae15", "mfe30", "mae30", "mfe60", "mae60", "fechamento60", "resultado", "confirm_min")
        self.conn.execute(f"UPDATE flow_anomalies SET {', '.join(f'{k}=?' for k in keys)}, medido_em=? WHERE id=?",
                          tuple(measures.get(k) for k in keys) + (now.isoformat(), row_id))
        self.conn.commit()

    def flow_anomaly_rows(self, symbol: Optional[str] = None, measured_only: bool = True) -> list[dict]:
        where, params = self._where_symbol(symbol)
        if measured_only:
            where = (where + " AND " if where else "WHERE ") + "resultado IS NOT NULL"
        return [dict(r) for r in self.conn.execute(f"SELECT * FROM flow_anomalies {where} ORDER BY hora", params).fetchall()]

    def has_reaction(self, event_id: str, symbol: str) -> bool:
        return self.conn.execute("SELECT 1 FROM reactions WHERE evento_id=? AND ativo=?", (event_id, symbol)).fetchone() is not None

    # ------------------------------------------------------------------ 3.0: OPPORTUNITY ENGINE
    def record_decision(self, rec, symbol: str = "XAUUSD", stage: Optional[str] = None, is_raw: bool = False) -> int:
        cur = self.conn.execute(
            "INSERT INTO decisions (hora, preco, score, direcao, acao, motivo, atr, nivel_evidencia, confianca, ativo, etapa, bruta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec.time.isoformat(), rec.price, rec.score, rec.direction, rec.action, rec.reason[:300], rec.atr, rec.evidence_level, rec.confidence, symbol,
             stage, int(is_raw)))
        self.conn.commit()
        return int(cur.lastrowid)

    def store_prices(self, candles: Iterable, symbol: str = "XAUUSD") -> int:
        rows = [(c.time.astimezone(timezone.utc).isoformat(), symbol, c.close) for c in candles]
        if not rows:
            return 0
        self.conn.executemany("INSERT OR IGNORE INTO prices (hora, ativo, close) VALUES (?,?,?)", rows)
        self.conn.commit()
        return len(rows)

    def prices(self, since: Optional[datetime] = None, symbol: Optional[str] = None) -> list[tuple[datetime, float]]:
        """Fechamentos gravados pelo live. `symbol=None` = todos os mercados misturados — só faz sentido em banco de um mercado."""
        conds, args = [], []
        if since:
            conds.append("hora >= ?"); args.append(since.isoformat())
        if symbol:
            conds.append("ativo = ?"); args.append(symbol)
        q = "SELECT hora, close FROM prices" + ((" WHERE " + " AND ".join(conds)) if conds else "") + " ORDER BY hora"
        rows = self.conn.execute(q, tuple(args)).fetchall()
        return [(datetime.fromisoformat(r["hora"]), r["close"]) for r in rows]

    def price_symbols(self) -> list[str]:
        return [r["ativo"] for r in self.conn.execute("SELECT DISTINCT ativo FROM prices ORDER BY ativo").fetchall()]

    def resolve_hypotheticals(self, now: datetime, horizon_min: int = 240, symbol: Optional[str] = None) -> int:
        """Preenche o resultado hipotético (3R, stop 1.2 ATR) das decisões bloqueadas com os preços gravados
        DO MESMO MERCADO da decisão (`symbol=None` = todos os mercados, cada um com os seus preços)."""
        from .models import Candle
        from .opportunity import DecisionRecord, hypothetical_trade

        w, wargs = self._where_symbol(symbol, "AND")
        rows = self.conn.execute(f"SELECT * FROM decisions WHERE resolvido=0 AND acao NOT IN ('ENTRADA','SEM_SINAL') AND direcao IN ('ALTA','BAIXA'){w}", wargs).fetchall()
        if not rows:
            return 0
        candles_by: dict[str, list] = {}
        n = 0
        for r in rows:
            sym = r["ativo"] or "XAUUSD"
            if sym not in candles_by:
                candles_by[sym] = [Candle(t, p, p, p, p, 0.0) for t, p in self.prices(symbol=sym)]
            candles = candles_by[sym]
            t0 = datetime.fromisoformat(r["hora"])
            rec = DecisionRecord(t0, r["preco"], r["score"], r["direcao"], r["acao"], r["motivo"] or "", r["atr"] or 0.0)
            expired = now >= t0 + timedelta(minutes=horizon_min)
            res = hypothetical_trade(rec, candles, horizon_min)
            if res is not None or expired:
                self.conn.execute("UPDATE decisions SET r_hipotetico=?, resolvido=1 WHERE id=?", (res, r["id"]))
                n += 1
        self.conn.commit()
        return n

    def decisions(self, since: Optional[datetime] = None, symbol: Optional[str] = None) -> list:
        from .opportunity import DecisionRecord

        conds, args = [], []
        if since:
            conds.append("hora >= ?"); args.append(since.isoformat())
        if symbol:
            conds.append("ativo = ?"); args.append(symbol)
        q = "SELECT * FROM decisions" + (" WHERE " + " AND ".join(conds) if conds else "") + " ORDER BY id"
        rows = self.conn.execute(q, tuple(args)).fetchall()
        out = []
        for r in rows:
            rec = DecisionRecord(datetime.fromisoformat(r["hora"]), r["preco"], r["score"] or 0.0, r["direcao"] or "LATERAL", r["acao"], r["motivo"] or "",
                                 r["atr"] or 0.0, r["r_hipotetico"], r["nivel_evidencia"] or 0, r["confianca"] or 0.0)
            keys = r.keys()
            if "etapa" in keys and r["etapa"]:
                rec.stage = r["etapa"]
            out.append(rec)
        return out

    def funnel(self, symbol: Optional[str] = None, since: Optional[datetime] = None):
        """FUNIL DE ENTRADA do que foi vivido (uma linha por análise gravada pelo live)."""
        from .opportunity import Funnel

        conds, args = [], []
        if since:
            conds.append("hora >= ?"); args.append(since.isoformat())
        if symbol:
            conds.append("ativo = ?"); args.append(symbol)
        q = "SELECT bruta, etapa, acao FROM decisions" + (" WHERE " + " AND ".join(conds) if conds else "")
        f = Funnel()
        for r in self.conn.execute(q, tuple(args)).fetchall():
            is_raw = bool(r["bruta"]) or r["acao"] == "ENTRADA"
            stage = None if r["acao"] == "ENTRADA" else (r["etapa"] or ("OUTROS" if is_raw else None))
            f.add(is_raw, stage)
        return f

    def opportunity_report(self, horizon_min: int = 240, since: Optional[datetime] = None, symbol: Optional[str] = None):
        from .opportunity import opportunity_report

        decisions = self.decisions(since, symbol)
        syms = self.price_symbols()
        if symbol is None and len(syms) > 1:
            return self._opportunity_report_all(syms, horizon_min, since)
        prices = self.prices(since, symbol)
        w, args = self._where_symbol(symbol)
        entries = [(datetime.fromisoformat(r["aberta_em"]), r["direcao"]) for r in self.conn.execute(f"SELECT aberta_em, direcao FROM trades{w}", args).fetchall()]
        w2, args2 = self._where_symbol(symbol, "AND")
        atrs = [r["atr"] for r in self.conn.execute(f"SELECT atr FROM trades WHERE atr IS NOT NULL{w2}", args2).fetchall()] or [d.atr for d in decisions if d.atr]
        threshold = (sum(atrs) / len(atrs)) if atrs else 9.0
        trade_rows = [{"score": r["score_entrada"] or 0.0, "r": r["resultado_r"]} for r in
                      self.conn.execute(f"SELECT score_entrada, resultado_r FROM trades WHERE resultado_r IS NOT NULL{w2}", args2).fetchall()]
        # decisões bloqueadas com resultado hipotético também alimentam a curva de limiar
        trade_rows += [{"score": d.score, "r": d.hypothetical_r} for d in decisions if d.hypothetical_r is not None]
        return opportunity_report(decisions, prices, entries, threshold, horizon_min, trade_rows)

    def _opportunity_report_all(self, symbols: list[str], horizon_min: int, since: Optional[datetime]):
        """Vários mercados no banco: movimentos e capturas contados mercado a mercado (cada um com o SEU preço) e somados;
        análises, entradas, atribuição e curva de limiar sobre todas as decisões."""
        from .opportunity import OpportunityReport, attribution, threshold_curve

        parts = [self.opportunity_report(horizon_min, since, sym) for sym in symbols]
        decisions = self.decisions(since, None)
        analyzed = [d for d in decisions if d.action != "SEM_SINAL"] or list(decisions)
        n_entries = sum(1 for d in decisions if d.action == "ENTRADA")
        entry_rate = (n_entries / len(analyzed)) if analyzed else None
        n_moves = sum(p.n_moves for p in parts)
        captured = sum(p.n_captured for p in parts)
        capture = (captured / n_moves) if n_moves else None
        trade_rows = [{"score": r["score_entrada"] or 0.0, "r": r["resultado_r"]} for r in
                      self.conn.execute("SELECT score_entrada, resultado_r FROM trades WHERE resultado_r IS NOT NULL").fetchall()]
        trade_rows += [{"score": d.score, "r": d.hypothetical_r} for d in decisions if d.hypothetical_r is not None]
        ref = parts[0]
        overfilter = (entry_rate is not None and len(analyzed) >= 20 and entry_rate < ref.min_entry_rate) or \
                     (capture is not None and n_moves >= 10 and capture < ref.min_capture_rate)
        return OpportunityReport(max(p.period_hours for p in parts), n_moves, captured, capture, len(analyzed), n_entries, entry_rate, overfilter,
                                 attribution([d for d in decisions if d.action != "ENTRADA"]), threshold_curve(trade_rows),
                                 ref.min_entry_rate, ref.min_capture_rate)

    def resolved_records(self, dedupe_episodes: bool = False) -> list[dict]:
        """Previsões resolvidas (ACERTO/ERRO). `dedupe_episodes`: uma por (ativo, direção, hora cheia) — alertas repetidos a cada ciclo
        no mesmo episódio não contam várias vezes (senão 5 episódios viram 55 'casos' e a calibração mente)."""
        rows = self.conn.execute("SELECT * FROM predictions WHERE resultado IN ('ACERTO','ERRO') AND previsao IN ('ALTA','BAIXA') ORDER BY id").fetchall()
        out, seen = [], set()
        for r in rows:
            keys = r.keys()
            sym = r["ativo"] if "ativo" in keys else "XAUUSD"
            if dedupe_episodes:
                k = (sym, r["previsao"], str(r["data"]), str(r["hora"])[:2])
                if k in seen:
                    continue
                seen.add(k)
            out.append({"direction": r["previsao"], "hit": r["resultado"] == "ACERTO", "probability": r["probabilidade"],
                        "factors": json.loads(r["fatores_ratio"] or "{}"), "technical": json.loads(r["tecnico_detalhe"] or "{}"),
                        "lead": r["tempo_ate_reacao_min"], "type": r["sinal_tipo"], "symbol": sym})
        return out

    def calibration_breakdown(self) -> str:
        """Acerto por tipo de sinal e por ativo (bruto e por episódio): onde a probabilidade declarada mente."""
        raw = self.resolved_records()
        dd = self.resolved_records(dedupe_episodes=True)
        lines = [f"  registros resolvidos: {len(raw)} brutos · {len(dd)} episódios (uma previsão por ativo/direção/hora)"]
        for label, recs in (("tipo de sinal", None), ("ativo", None)):
            key = "type" if label == "tipo de sinal" else "symbol"
            groups: dict[str, list] = {}
            for r in dd:
                groups.setdefault(str(r.get(key) or "?"), []).append(r)
            for g, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
                n = len(rs)
                hit = sum(1 for r in rs if r["hit"]) / n
                p = sum(float(r["probability"] or 0.0) for r in rs) / n
                lines.append(f"  {label:<14}{g:<20} n={n:<4} declarada {p:.0%} · observada {hit:.0%}  {'⚠️ inversão' if hit < 0.35 and n >= 10 else ''}")
        return "\n".join(lines)

    def calibration(self, dedupe_episodes: bool = False):
        from .validation import calibration_table
        return calibration_table((r["probability"], r["hit"]) for r in self.resolved_records(dedupe_episodes))

    def scoreboard(self):
        from .validation import factor_scoreboard
        return factor_scoreboard(self.resolved_records())

    def fit_calibrator(self, dedupe_episodes: bool = True):
        from .validation import IsotonicCalibrator
        return IsotonicCalibrator().fit((r["probability"], r["hit"]) for r in self.resolved_records(dedupe_episodes))

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

    def pending(self, symbol: Optional[str] = None) -> list[sqlite3.Row]:
        w, args = self._where_symbol(symbol, "AND")
        return self.conn.execute(f"SELECT * FROM predictions WHERE resultado IS NULL{w} ORDER BY id", args).fetchall()

    # ------------------------------------------------------------------ 5.x: contaminação entre mercados
    CONTAMINATION_RATIO = 0.2   # movimento > 20 % do preço dentro do horizonte = preço de OUTRO mercado (ouro 3 600 × EURUSD 1,15)

    def contaminated_predictions(self) -> list[sqlite3.Row]:
        """Previsões resolvidas com preço de outro mercado: MFE/MAE ou preço final a mais de 20 % do preço da previsão."""
        k = self.CONTAMINATION_RATIO
        return self.conn.execute(
            """SELECT id, ativo, resultado, preco, preco_final, maxima_favoravel, maxima_adversa FROM predictions
               WHERE resultado IN ('ACERTO','ERRO') AND preco > 0 AND (
                     COALESCE(maxima_favoravel, 0) > ? * preco OR COALESCE(maxima_adversa, 0) > ? * preco
                     OR ABS(COALESCE(preco_final, preco) - preco) > ? * preco)""", (k, k, k)).fetchall()

    def contaminated_trades(self, max_minutes: float = 2.0) -> list[sqlite3.Row]:
        """Operações cujo perfil (stop/MFE/MAE) foi simulado com candles de outro mercado: 'estopada' no 1º candle."""
        rows = self.conn.execute("SELECT id, ativo, status, aberta_em, fechada_em, estopada, resultado_r FROM trades WHERE status='CLOSED' AND estopada=1 AND fechada_em IS NOT NULL").fetchall()
        out = []
        for r in rows:
            try:
                dt = (datetime.fromisoformat(r["fechada_em"]) - datetime.fromisoformat(r["aberta_em"])).total_seconds() / 60
            except (TypeError, ValueError):
                continue
            if dt <= max_minutes:
                out.append(r)
        return out

    def repair_cross_market(self, now: datetime, candles_by_symbol: Optional[dict] = None, horizon_min: int = 240) -> dict:
        """Desfaz o que foi resolvido com preço de outro mercado e, se houver candles M1 por mercado, resolve de novo.

        1. previsões contaminadas → voltam a pendentes;  2. operações 'estopadas' no 1º candle → perfil apagado
        (voltam a OPEN/MANAGED_CLOSED para a simulação certa);  3. resultados hipotéticos das decisões → recalculados;
        4. tabela de preços: se o banco tem mais de um mercado, o que foi gravado sem símbolo é descartado."""
        rep: dict = {"previsoes": {}, "operacoes": {}, "decisoes": 0, "precos_descartados": 0, "re_resolvidas": {}, "re_simuladas": {}}
        for r in self.contaminated_predictions():
            rep["previsoes"][r["ativo"]] = rep["previsoes"].get(r["ativo"], 0) + 1
            self.conn.execute("UPDATE predictions SET resultado=NULL, tempo_ate_reacao_min=NULL, maxima_favoravel=NULL, maxima_adversa=NULL, "
                              "preco_final=NULL, resolvido_em=NULL WHERE id=?", (r["id"],))
        for r in self.contaminated_trades():
            rep["operacoes"][r["ativo"]] = rep["operacoes"].get(r["ativo"], 0) + 1
            status = "MANAGED_CLOSED" if r["resultado_r"] is not None else "OPEN"
            self.conn.execute("UPDATE trades SET status=?, max_r=NULL, mae_r=NULL, hit_1r=NULL, hit_2r=NULL, hit_3r=NULL, hit_4r=NULL, "
                              "estopada=NULL, resultados=NULL, fechada_em=NULL WHERE id=?", (status, r["id"]))
        cur = self.conn.execute("UPDATE decisions SET resolvido=0, r_hipotetico=NULL WHERE resolvido=1 AND acao NOT IN ('ENTRADA','SEM_SINAL')")
        rep["decisoes"] = cur.rowcount
        symbols = {r["ativo"] for r in self.conn.execute("SELECT DISTINCT ativo FROM predictions UNION SELECT DISTINCT ativo FROM trades UNION SELECT DISTINCT ativo FROM decisions").fetchall()}
        if len(symbols) > 1 or candles_by_symbol:
            rep["precos_descartados"] = self.conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
            self.conn.execute("DELETE FROM prices")
        self.conn.commit()
        for sym, candles in (candles_by_symbol or {}).items():
            cs = sorted(candles, key=lambda c: c.time)
            if not cs:
                continue
            self.store_prices(cs, sym)
            atrs = [r["atr"] for r in self.conn.execute("SELECT atr FROM predictions WHERE ativo=? AND atr IS NOT NULL", (sym,)).fetchall()]
            default_thr = sorted(atrs)[len(atrs) // 2] if atrs else 0.0
            if default_thr > 0:
                rep["re_resolvidas"][sym] = len(self.auto_resolve(cs, now, default_thr, horizon_min, symbol=sym))
            rep["re_simuladas"][sym] = len(self.auto_resolve_trades(cs, now, symbol=sym))
            self.resolve_hypotheticals(now, horizon_min, symbol=sym)
        return rep

    def close(self) -> None:
        self.conn.close()
