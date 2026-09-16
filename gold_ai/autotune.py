"""AUTOTUNE (5.2) — a IA procura os parâmetros do funil no passado e o robô só os adota com amostra.

Walk-forward por mercado: em cada bloco cronológico, a grade (piso de vantagem, confirmações mínimas, limiar de sinal,
probabilidade mínima) é avaliada só no TREINO (blocos anteriores); a melhor combinação pelo objetivo expectancy·√n é então
aplicada no bloco de TESTE, que ela nunca viu. A soma dos testes é o resultado fora da amostra da POLÍTICA de escolha — o que
uma IA que escolhe parâmetros no passado teria rendido de fato. A combinação mais votada nos blocos vira a recomendação.

O arquivo `dados/parametros.json` guarda, por mercado: parâmetros, n fora da amostra, expectancy OOS da política e do padrão,
tier do ciclo de vida e `apply` (True só com n ≥ 20 casos OOS e expectancy ≥ padrão). O live lê o arquivo e aplica apenas o
que está marcado; o resto fica em sombra (registrado, não operado). Regra central preservada: o histórico decide o parâmetro,
nunca uma regra arbitrária nem a impaciência."""
from __future__ import annotations

import json
import os
import statistics
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from itertools import product
from typing import Callable, Optional, Sequence

from .config import EngineConfig
from .evaluation import Backtester, BacktestResult
from .lifecycle import tier
from .sweep import FloorMetrics, _metrics, objective

MIN_APPLY = 20            # candidato: n OOS mínimo para o live adotar
DEFAULT_GRID: dict[str, Sequence] = {"min_edge_score": (15.0, 25.0, 35.0), "min_confirmations": (2, 3), "signal_score": (40, 50)}
PARAM_KEYS = ("min_edge_score", "min_confirmations", "signal_score", "min_edge_probability", "min_edge_confidence")


def cfg_with(base: EngineConfig, params: dict) -> EngineConfig:
    d = {**base.__dict__, "weights": dict(base.weights), "factor_signs": dict(base.factor_signs)}
    for k, v in params.items():
        if k == "signal_score":
            d["buy"], d["sell"] = int(v), -int(v)
        elif k in PARAM_KEYS:
            d[k] = v
    return EngineConfig(**d)


def default_params(cfg: EngineConfig) -> dict:
    return {"min_edge_score": float(cfg.min_edge_score), "min_confirmations": int(cfg.min_confirmations), "signal_score": int(cfg.buy)}


def _label(p: dict) -> str:
    return " · ".join(f"{k.replace('min_edge_score', 'piso').replace('min_confirmations', 'conf').replace('signal_score', 'sinal').replace('min_edge_probability', 'prob')} {v:g}"
                      for k, v in p.items())


@dataclass
class FoldPick:
    fold: int
    params: dict
    train: FloorMetrics
    test: FloorMetrics


@dataclass
class MarketTune:
    market: str
    picks: list[FoldPick] = field(default_factory=list)
    policy_oos: Optional[FloorMetrics] = None        # soma dos blocos de teste com o parâmetro escolhido no treino
    default_oos: Optional[FloorMetrics] = None       # mesmos blocos com o parâmetro padrão
    recommended: dict = field(default_factory=dict)  # combinação mais votada
    default: dict = field(default_factory=dict)
    oos_results: list = field(default_factory=list)  # R das operações OOS da política, em ordem cronológica (semente do ciclo de vida)

    @property
    def n_oos(self) -> int:
        return self.policy_oos.n if self.policy_oos else 0

    @property
    def tier(self) -> str:
        return tier(self.n_oos)

    @property
    def apply(self) -> bool:
        if self.policy_oos is None or self.n_oos < MIN_APPLY:
            return False
        if self.default_oos is not None and self.default_oos.n >= 5 and self.policy_oos.expectancy < self.default_oos.expectancy:
            return False
        return self.policy_oos.expectancy > 0

    def to_dict(self) -> dict:
        m = lambda x: None if x is None else {"n": x.n, "expectancy": round(x.expectancy, 3), "win_rate": round(x.win_rate, 3),  # noqa: E731
                                              "profit_factor": (None if x.profit_factor in (None, float("inf")) else round(x.profit_factor, 2)), "max_dd_pct": round(x.max_dd_pct, 2)}
        return {"params": self.recommended, "default": self.default, "n_oos": self.n_oos, "tier": self.tier, "apply": self.apply,
                "policy_oos": m(self.policy_oos), "default_oos": m(self.default_oos), "oos_results": [round(x, 3) for x in self.oos_results],
                "folds": [{"fold": p.fold, "params": p.params, "train_n": p.train.n, "train_expectancy": round(p.train.expectancy, 3),
                           "test_n": p.test.n, "test_expectancy": round(p.test.expectancy, 3)} for p in self.picks]}

    def render(self) -> str:
        lines = [f"{self.market}: recomendado {_label(self.recommended)}  (padrão {_label(self.default)})"]
        for p in self.picks:
            lines.append(f"  bloco {p.fold}: treino escolheu {_label(p.params)} (n={p.train.n}, E={p.train.expectancy:+.2f}R) → teste n={p.test.n} E={p.test.expectancy:+.2f}R")
        if self.policy_oos is not None:
            d = self.default_oos
            lines.append(f"  POLÍTICA OOS: n={self.policy_oos.n} E={self.policy_oos.expectancy:+.2f}R acerto {self.policy_oos.win_rate:.0%} DD {self.policy_oos.max_dd_pct:.1f}%"
                         + (f"   ·   PADRÃO OOS: n={d.n} E={d.expectancy:+.2f}R" if d is not None else ""))
        verdict = ("🟢 ADOTAR no live" if self.apply else
                   f"⚪ SOMBRA — n OOS {self.n_oos} < {MIN_APPLY}" if self.n_oos < MIN_APPLY else "🟡 SOMBRA — não supera o padrão fora da amostra")
        lines.append(f"  tier {self.tier} · {verdict}")
        return "\n".join(lines)


def autotune_market(market: str, bt: Backtester, grid: Optional[dict] = None, n_folds: int = 4, train_folds: int = 2,
                    strategy: str = "adaptive", equity: float = 10000.0, risk_pct: float = 0.5, log: Optional[Callable[[str], None]] = None) -> MarketTune:
    grid = grid or DEFAULT_GRID
    base = bt.cfg
    combos = [dict(zip(grid.keys(), vals)) for vals in product(*grid.values())]
    dflt = default_params(base)
    tune = MarketTune(market, default=dflt)
    n = len(bt.frame.xau)
    fold_len = (n - bt.warmup) // (n_folds + train_folds)
    policy_runs: list[BacktestResult] = []
    default_runs: list[BacktestResult] = []
    for k in range(n_folds):
        train_end = bt.warmup + (train_folds + k) * fold_len
        train_start = train_end - train_folds * fold_len
        test_end = min(n, train_end + fold_len)
        table = []
        for p in combos:
            r = bt.run(train_start, train_end, cfg_with(base, p))
            table.append((p, _metrics([r], p.get("min_edge_score", base.min_edge_score), strategy, equity, risk_pct)))
            if log:
                log(f"{market} bloco {k + 1} treino {_label(p)}: n={table[-1][1].n} E={table[-1][1].expectancy:+.2f}R")
        best_p, best_m = max(table, key=lambda t: objective(t[1]))
        if objective(best_m) <= 0:                                   # sem edge no treino → mantém o padrão, não escolhe ao acaso
            best_p, best_m = next(((p, m) for p, m in table if p == dflt), (dflt, best_m))
        test = bt.run(train_end, test_end, cfg_with(base, best_p))
        policy_runs.append(test)
        default_runs.append(test if best_p == dflt else bt.run(train_end, test_end, cfg_with(base, dflt)))
        tune.picks.append(FoldPick(k + 1, dict(best_p), best_m, _metrics([test], best_p.get("min_edge_score", 0.0), strategy, equity, risk_pct)))
    tune.policy_oos = _metrics(policy_runs, float("nan"), strategy, equity, risk_pct)
    rows = sorted((r for res in policy_runs for r in res.trade_rows), key=lambda r: r["time"])
    tune.oos_results = [float(r["results"].get(strategy, r["results"].get("3R"))) for r in rows if r["results"].get(strategy, r["results"].get("3R")) is not None]
    tune.default_oos = _metrics(default_runs, float("nan"), strategy, equity, risk_pct)
    # recomendação: mais votada nos blocos; empate decidido pelo que a escolha RENDEU nos blocos de teste (R somado), não pela ordem
    votes = Counter(json.dumps(p.params, sort_keys=True) for p in tune.picks)
    earned: dict[str, float] = {}
    for p in tune.picks:
        key = json.dumps(p.params, sort_keys=True)
        earned[key] = earned.get(key, 0.0) + p.test.expectancy * p.test.n
    tune.recommended = json.loads(max(votes, key=lambda k: (votes[k], earned.get(k, 0.0)))) if votes else dict(dflt)
    return tune


@dataclass
class TuneReport:
    start: str
    end: str
    markets: list[MarketTune] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"generated": datetime.now(timezone.utc).isoformat(), "start": self.start, "end": self.end, "min_apply": MIN_APPLY,
                "markets": {t.market: t.to_dict() for t in self.markets}}

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=1)

    def render(self) -> str:
        head = [f"🧠 AUTOTUNE — parâmetros do funil escolhidos no PASSADO, avaliados fora da amostra · {self.start} → {self.end}",
                "grade: piso de vantagem × confirmações mínimas × limiar de sinal; escolha só no treino de cada bloco; política = o que a escolha rendeu no teste", ""]
        body = [t.render() for t in self.markets]
        tail = ["", f"REGRA: o live adota um parâmetro só com n OOS ≥ {MIN_APPLY} (candidato) e expectancy ≥ padrão; abaixo disso fica em SOMBRA.",
                "       O ciclo de vida continua valendo depois: 3 perdas alertam, 5 suspendem e revalidam; nada é apagado."]
        return "\n".join(head + ["\n".join([b, ""]) for b in body] + tail)


def load_params(path: str) -> dict:
    """{mercado: {"params", "apply", "n_oos", "tier", ...}} — vazio se o arquivo não existe."""
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("markets", {})


def apply_params(cfg: EngineConfig, entry: Optional[dict]) -> tuple[EngineConfig, str]:
    """Aplica ao EngineConfig do mercado só se `apply` for verdadeiro; devolve (cfg, nota para o log)."""
    if not entry:
        return cfg, "padrão (sem autotune)"
    p = entry.get("params") or {}
    if not entry.get("apply"):
        return cfg, f"SOMBRA: mantém padrão — {_label(p)} tem n OOS {entry.get('n_oos', 0)} ({entry.get('tier', '?')})"
    return cfg_with(cfg, p), f"APRENDIDO: {_label(p)} (n OOS {entry.get('n_oos', 0)}, {entry.get('tier', '?')})"
