"""DOCTOR — "tudo está funcionando como deveria?" e "qual a eficiência?" num painel só.

Cada camada recebe ✅ (ok) · ⚠️ (funciona com limitação) · ❌ (não funciona) e a ação correspondente. Depois, a leitura de
eficiência: o que já foi provado (fora da amostra / vivido) e o que ainda é hipótese. Não inventa número: sem dado, diz "sem dado".
"""

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional


@dataclass
class Check:
    layer: str
    status: str          # ✅ ⚠️ ❌
    detail: str
    action: str = ""

    def row(self) -> str:
        return f"{self.status} {self.layer:<26} {self.detail}" + (f"\n      → {self.action}" if self.action else "")


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list)
    efficiency: list[str] = field(default_factory=list)

    def add(self, layer: str, status: str, detail: str, action: str = "") -> None:
        self.checks.append(Check(layer, status, detail, action))

    @property
    def score(self) -> tuple[int, int, int]:
        return (sum(1 for c in self.checks if c.status == "✅"), sum(1 for c in self.checks if c.status == "⚠️"), sum(1 for c in self.checks if c.status == "❌"))

    def render(self) -> str:
        ok, warn, bad = self.score
        lines = [f"🩺 MARKET AI DOCTOR — {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC · ✅ {ok} · ⚠️ {warn} · ❌ {bad}", ""]
        lines += [c.row() for c in self.checks]
        lines += ["", "📏 EFICIÊNCIA — o que está PROVADO e o que ainda é hipótese"] + [f"  {e}" for e in self.efficiency]
        lines += ["", "LEGENDA: ✅ funciona · ⚠️ funciona com limitação (ação sugerida) · ❌ não funciona (ação obrigatória)"]
        return "\n".join(lines)


def _age(path: str) -> str:
    if not os.path.exists(path):
        return "ausente"
    h = (datetime.now(timezone.utc) - datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc)).total_seconds() / 3600
    return f"há {h:.0f} h" if h < 48 else f"há {h / 24:.0f} dias"


def _csv_rows(path: str) -> int:
    try:
        with open(path, encoding="utf-8") as f:
            return max(0, sum(1 for _ in f) - 1)
    except OSError:
        return 0


def run_doctor(env: dict, db_path: str = "gold_ai.db", events_path: str = os.path.join("dados", "noticias_historicas.csv"),
               markets: tuple[str, ...] = ("XAUUSD", "US500", "EURUSD", "USDJPY", "WTI"), start: Optional[date] = None, end: Optional[date] = None,
               mt5_probe: Optional[Callable[[], tuple[bool, str]]] = None, telegram_probe: Optional[Callable[[], tuple[bool, str]]] = None,
               data_dir: str = "dados") -> DoctorReport:
    rep = DoctorReport()
    start = start or date(2026, 1, 1)
    end = end or datetime.now(timezone.utc).date()

    # 1) .env
    from .telegram import find_env_file
    envf = find_env_file()
    keys = ("TOKEN_TELEGRAM", "CHAT_ID", "MT5_PATH", "RISK_PER_TRADE", "MAX_DAILY_LOSS", "FRED_API_KEY")
    missing = [k for k in keys if not env.get(k)]
    if not envf:
        rep.add(".env", "❌", "não encontrado na pasta", "salve o .env na mesma pasta do market_ai_engine_v6.py")
    elif missing:
        rep.add(".env", "⚠️", f"{envf} · faltam: {', '.join(missing)}", "preencha as chaves que faltam")
    else:
        rep.add(".env", "✅", f"{envf} · risco {env.get('RISK_PER_TRADE')}% · perda diária {env.get('MAX_DAILY_LOSS')}% · meta {env.get('DAILY_TARGET', '0')}%")

    # 2) Telegram
    if telegram_probe:
        ok, msg = telegram_probe()
        rep.add("Telegram", "✅" if ok else "❌", msg, "" if ok else "confira TOKEN_TELEGRAM/CHAT_ID; envie /start ao bot")
    else:
        rep.add("Telegram", "✅" if env.get("TOKEN_TELEGRAM") and env.get("CHAT_ID") else "⚠️", "credenciais presentes (não testado)" if env.get("TOKEN_TELEGRAM") else "sem credenciais: modo dry-run (alertas só no console)")

    # 3) MT5
    if mt5_probe:
        ok, msg = mt5_probe()
        rep.add("MetaTrader 5", "✅" if ok else "❌", msg, "" if ok else "abra o terminal, faça login (demo/real) e deixe aberto; ou MT5_LOGIN/MT5_PASSWORD/MT5_SERVER no .env")
    else:
        rep.add("MetaTrader 5", "⚠️", "não testado nesta execução", "rode `doctor --mt5` com o terminal aberto")

    # 4) Banco de eventos
    if not os.path.exists(events_path):
        rep.add("Banco de eventos", "❌", f"{events_path} ausente", "rode `history fetch-alfred` e `history fetch-gdelt`")
        cov = None
    else:
        from .history import coverage, load_history
        hist = load_history(events_path)
        cov = coverage(hist, start, end)
        st = "✅" if cov.macro_pct >= 0.8 and cov.news_pct >= 0.8 else "⚠️" if cov.macro_pct >= 0.8 or cov.news_pct >= 0.8 else "❌"
        rep.add("Banco de eventos", st, f"{len(hist)} registros · MACRO {cov.macro_pct:.0%} das semanas · NEWS {cov.news_pct:.0%} dos dias · revisões {cov.revisions} · {_age(events_path)}",
                "" if st == "✅" else "complete com `history fetch-alfred` (macro) / `history fetch-gdelt` (news)")

    # 5) Preços de alta resolução
    for sym in markets:
        t, m = os.path.join(data_dir, f"{sym}_ticks.csv"), os.path.join(data_dir, f"{sym}_m1.csv")
        nt, nm = _csv_rows(t) if os.path.exists(t) else 0, _csv_rows(m) if os.path.exists(m) else 0
        if nt and nm:
            rep.add(f"Alta resolução {sym}", "✅", f"ticks {nt:,} · M1 {nm:,}")
        elif nt or nm:
            rep.add(f"Alta resolução {sym}", "⚠️", f"ticks {nt:,} · M1 {nm:,}", "falta uma das resoluções: `history prices` (dukascopy/mt5)")
        else:
            rep.add(f"Alta resolução {sym}", "❌", "sem ticks nem M1", "`history prices --markets ...` (Dukascopy) ou `--source mt5`")
    usd = os.path.join(data_dir, "USDX_ticks.csv")
    rep.add("Líder USD (USDX)", "✅" if os.path.exists(usd) and _csv_rows(usd) else "⚠️", f"ticks {_csv_rows(usd):,}" if os.path.exists(usd) else "ausente",
            "" if os.path.exists(usd) else "sem líder USD não há lead-lag nem prova do relógio: `history prices --extra USDX`")

    # 6) Veredito do relógio
    edge_path = os.path.join(data_dir, "reaction_edge.json")
    if os.path.exists(edge_path):
        with open(edge_path, encoding="utf-8") as f:
            edge = json.load(f)
        strong = [s for s, v in edge.items() if v.get("verdict") == "🟢"]
        incon = [s for s, v in edge.items() if v.get("verdict") == "⚪"]
        st = "✅" if strong else "⚠️"
        rep.add("REACTION EDGE", st, f"{_age(edge_path)} · " + " · ".join(f"{s} {v.get('verdict')} n={v.get('n')} {v.get('net_atr', 0):+.2f}ATR" for s, v in edge.items()),
                "" if strong else ("amostra insuficiente: mais eventos (Dukascopy jan→set) ou aguardar o live" if incon else "nenhum ativo com edge provado: o relógio fica como evidência, não opera"))
    else:
        rep.add("REACTION EDGE", "❌", "reaction_edge.json ausente", "rode `reaction learn --tf BOTH ...` (ou rodar_tudo.bat)")

    # 7) Memória do live
    if not os.path.exists(db_path):
        rep.add("Memória do live", "⚠️", f"{db_path} ausente — o live ainda não rodou", "inicie `rodar_live.bat` (PAPER)")
    else:
        from .memory import PredictionMemory
        mem = PredictionMemory(db_path)
        try:
            n_pred = mem.conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
            n_res = mem.conn.execute("SELECT COUNT(*) FROM predictions WHERE resultado IS NOT NULL").fetchone()[0]
            n_tr = mem.conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            n_closed = mem.conn.execute("SELECT COUNT(*) FROM trades WHERE resultado_r IS NOT NULL").fetchone()[0]
            n_dec = mem.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
            last = mem.conn.execute("SELECT MAX(hora) FROM decisions").fetchone()[0]
            n_react = mem.conn.execute("SELECT COUNT(*) FROM reactions").fetchone()[0]
            eq = mem.last_equity()
            fresh = ""
            age_h = 1e9
            if last:
                try:
                    age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() / 3600
                    fresh = f" · última análise há {age_h:.1f} h"
                except ValueError:
                    fresh = ""
            st = "✅" if n_dec and last and age_h < 2 else "⚠️" if n_dec else "❌"
            rep.add("Memória do live", st, f"análises {n_dec:,} · previsões {n_pred} (resolvidas {n_res}) · operações {n_tr} (fechadas {n_closed}) · reações {n_react} · capital {eq if eq is not None else 'n/d'}{fresh}",
                    "" if st == "✅" else "o live não está rodando (ou parou): `rodar_live.bat`")
            per = mem.per_market_summary()
            if per:
                rep.efficiency.append("VIVIDO (PAPER/LIVE, fora da amostra por construção): " + " · ".join(f"{r['symbol']} n={r['n']} {r['expectancy']:+.2f}R acerto {r['win_rate']:.0%}" for r in per))
                rep.efficiency.append("  → o LIVE EDGE só é conclusivo com ≥ 30 operações fechadas por mercado (`edge`)")
            else:
                rep.efficiency.append("VIVIDO: nenhuma operação fechada ainda — a eficiência real só aparece com o live rodando dias/semanas em PAPER")
        finally:
            mem.close()

    # 8) Resultados dos testes históricos
    for name, what in (("prova.txt", "PROVA do relógio (TICK/M1, Δ CLOCK−INGÊNUA, veredito por ativo)"), ("teste_ab.txt", "TESTE A/B preço × macro × news"),
                       ("estimativa_news.txt", "estimativa de lucro OOS com notícias")):
        if os.path.exists(name):
            rep.add(f"Resultado {name}", "✅", f"{what} · {_age(name)}")
        else:
            rep.add(f"Resultado {name}", "⚠️", f"{what} · ainda não gerado", "rodar_tudo.bat")
    rep.efficiency += [
        "HISTÓRICO (walk-forward OOS, mesmo piso): leia em teste_ab.txt a expectancy e a captura de Preço só × +Macro × +Macro+News — a informação tem de CRIAR entradas com expectancy ≥ referência",
        "RELÓGIO: leia em prova.txt a tabela CLOCK − INGÊNUA por ativo × atraso — só Δ > 0 com n ≥ 20 e concordância TICK × M1 vale como edge; ⚪ é 'ainda não sei'",
        "LUCRO: estimativa_news.txt dá retorno, drawdown e P(lucro) por bootstrap — é estimativa OOS, não promessa; compare com a versão sem --events",
        "META +10%/dia com risco 3%: exige +3,33R líquidos no dia; com a expectancy medida até agora isso é exceção, não rotina — a meta é trava para proteger o dia bom",
    ]
    return rep
