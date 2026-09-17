"""DIRECTION DIAGNOSTIC — por que o robô perdeu? (5.x)

Três perguntas, três ferramentas, nenhuma mexe na estratégia:

1. `direction_report`  — cada operação OOS do walk-forward é classificada:
     ACERTO        resultado > 0
     SAÍDA RUIM    chegou a ≥ 1R e terminou ≤ 0 — o sinal estava certo, a saída não
     ENTRADA RUIM  andou a favor (0,3R–1R) e depois estopou — sinal certo, timing/stop errado
     INVERTIDO     foi direto contra (< 0,3R a favor antes do stop) — o sinal estava do lado errado
     EXPIROU       nem 1R nem stop no horizonte
   e, por mercado, o "poder OOS" de cada fator: alinhamento médio nos ACERTOS − nos INVERTIDOS.

2. `factor_matrix`     — para cada mercado × fator: E com o sinal como está, INVERTIDO e REMOVIDO, em blocos de tempo.
   "INVERTIDO?" só quando a inversão ganha do base em ≥ 3 de 4 blocos com n ≥ 20. Parâmetros fixos (padrão): não há
   escolha pelo resultado além da própria bandeira — e a bandeira NÃO é aplicada: é candidato para o walk-forward confirmar.

3. `reality_check`     — o mesmo backtest sobre o preço da corretora e sobre o Yahoo: se o edge muda de sinal ao trocar
   a fonte, EDGE NÃO CONFIRMADO (a vantagem pode existir só no dataset).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

CATEGORIES = ("ACERTO", "SAÍDA RUIM", "ENTRADA RUIM", "INVERTIDO", "EXPIROU")


def classify_trade(row: dict, strategy: str = "adaptive") -> str:
    r = (row.get("results") or {}).get(strategy)
    prof = row.get("profile")
    if r is None or prof is None:
        return "SEM RESULTADO"
    if r > 0:
        return "ACERTO"
    if prof.max_r_before_stop >= 1.0:
        return "SAÍDA RUIM"
    if prof.stopped:
        return "INVERTIDO" if prof.max_r_before_stop < 0.3 else "ENTRADA RUIM"
    return "EXPIROU"


@dataclass
class DirectionStats:
    symbol: str
    counts: dict = field(default_factory=dict)      # categoria → n
    r_sum: dict = field(default_factory=dict)       # categoria → Σ R
    factor_power: list = field(default_factory=list)  # (fator, poder, n_acertos, n_invertidos)
    n: int = 0
    has_factors: bool = False

    def render(self) -> str:
        lines = [f"{self.symbol}: {self.n} operações OOS"]
        if not self.n:
            return lines[0]
        for c in CATEGORIES:
            k = self.counts.get(c, 0)
            if k:
                lines.append(f"  {c:<13} {k:>4}  {k / self.n:>4.0%}   R médio {self.r_sum.get(c, 0.0) / k:+.2f}")
        inv = self.counts.get("INVERTIDO", 0)
        lose = self.n - self.counts.get("ACERTO", 0)
        if lose:
            lines.append(f"  das perdas: {inv / lose:.0%} foram direto contra (INVERTIDO) · "
                         f"{self.counts.get('ENTRADA RUIM', 0) / lose:.0%} timing · {self.counts.get('SAÍDA RUIM', 0) / lose:.0%} saída")
        if self.has_factors and self.factor_power:
            lines.append("  poder OOS por fator (alinhamento médio nos ACERTOS − nos INVERTIDOS; negativo = o fator apontava para o lado errado):")
            for name, power, na, ni in self.factor_power:
                flag = " ⚠️" if power < -0.5 and na + ni >= 10 else ""
                lines.append(f"    {name:<12} {power:+.2f}  (acertos {na} · invertidos {ni}){flag}")
        elif not self.has_factors:
            lines.append("  (fatores por operação ausentes no cache do walk-forward: rode com --no-cache para tê-los)")
        return "\n".join(lines)


def direction_report(rows: list[dict], symbol: str, strategy: str = "adaptive") -> DirectionStats:
    st = DirectionStats(symbol)
    align: dict[str, dict[str, list[float]]] = {}
    for row in rows:
        cat = classify_trade(row, strategy)
        if cat == "SEM RESULTADO":
            continue
        st.n += 1
        st.counts[cat] = st.counts.get(cat, 0) + 1
        st.r_sum[cat] = st.r_sum.get(cat, 0.0) + float(row["results"][strategy])
        facs = row.get("factors")
        if facs:
            st.has_factors = True
            sign = 1.0 if row.get("direction") == "ALTA" else -1.0
            if cat in ("ACERTO", "INVERTIDO"):
                for name, score in facs.items():
                    align.setdefault(name, {"ACERTO": [], "INVERTIDO": []})[cat].append(sign * float(score))
    for name, d in align.items():
        a, i = d["ACERTO"], d["INVERTIDO"]
        if not a and not i:
            continue
        ma = sum(a) / len(a) if a else 0.0
        mi = sum(i) / len(i) if i else 0.0
        st.factor_power.append((name, round(ma - mi, 2), len(a), len(i)))
    st.factor_power.sort(key=lambda t: t[1])
    return st


# --------------------------------------------------------------------------- matriz fator × mercado
@dataclass
class VariantResult:
    label: str
    e_blocks: list          # E por bloco (None se n = 0)
    n_blocks: list
    r_all: list

    @property
    def n(self) -> int:
        return len(self.r_all)

    @property
    def e(self) -> float:
        return sum(self.r_all) / self.n if self.n else 0.0

    @property
    def win(self) -> float:
        return sum(1 for r in self.r_all if r > 0) / self.n if self.n else 0.0


def _run_blocks(bt, cfg, n_blocks: int, strategy: str) -> VariantResult:
    xau = bt.frame.xau
    lo, hi = bt.warmup, len(xau)
    edges = [lo + (hi - lo) * k // n_blocks for k in range(n_blocks + 1)]
    e_blocks, n_blocks_, r_all = [], [], []
    for k in range(n_blocks):
        res = bt.run(edges[k], edges[k + 1], cfg=cfg)
        rs = [float(row["results"][strategy]) for row in res.trade_rows if (row.get("results") or {}).get(strategy) is not None]
        e_blocks.append(sum(rs) / len(rs) if rs else None)
        n_blocks_.append(len(rs))
        r_all += rs
    return VariantResult("", e_blocks, n_blocks_, r_all)


def verdict(base: VariantResult, inv: VariantResult, rem: VariantResult, min_n: int = 20) -> str:
    if base.n < min_n:
        return "amostra < %d" % min_n
    wins = sum(1 for eb, ei in zip(base.e_blocks, inv.e_blocks) if eb is not None and ei is not None and ei > eb)
    if inv.e > base.e + 0.10 and wins >= max(3, len(base.e_blocks) - 1):
        return f"INVERTIDO? ({wins}/{len(base.e_blocks)} blocos)"
    if rem.e > base.e + 0.05:
        return "ATRAPALHA"
    if rem.e < base.e - 0.05:
        return "AJUDA"
    return "neutro"


@dataclass
class FactorMatrix:
    symbol: str
    base: VariantResult
    rows: list = field(default_factory=list)   # (fator, sinal, inv, rem, veredito)

    def render(self) -> str:
        b = self.base
        lines = [f"{self.symbol} · base (parâmetros padrão, sinais como estão): n={b.n} · win {b.win:.0%} · E {b.e:+.2f}R · "
                 f"por bloco {' '.join(('n/d' if e is None else f'{e:+.2f}') for e in b.e_blocks)}",
                 f"  {'fator':<12} {'sinal':>5} {'invertido':>16} {'removido':>12}  veredito"]
        for name, sign, inv, rem, vd in self.rows:
            wins = sum(1 for eb, ei in zip(b.e_blocks, inv.e_blocks) if eb is not None and ei is not None and ei > eb)
            lines.append(f"  {name:<12} {sign:>+5d} {inv.e:>+7.2f}R n={inv.n:<3} {wins}/{len(b.e_blocks)} {rem.e:>+7.2f}R n={rem.n:<3}  {vd}")
        lines.append("  leitura: INVERTIDO? = a inversão ganha do base em quase todos os blocos com n ≥ 20 — candidato, NÃO aplicado; "
                     "AJUDA/ATRAPALHA = o que muda ao remover o fator. Confirmar no walk-forward antes de qualquer mudança.")
        return "\n".join(lines)


def _with_signs(cfg, signs: dict):
    """Cópia da configuração com outros sinais de fator (sem `dataclasses.replace`, que o bundle não importa)."""
    import copy
    new = copy.copy(cfg)
    new.factor_signs = dict(signs)
    return new


def factor_matrix(frame, symbol: str, base_cfg, n_blocks: int = 4, step: int = 1, horizon_min: int = 240, strategy: str = "adaptive",
                  log: Optional[Callable[[str], None]] = None, factors: Optional[list[str]] = None) -> FactorMatrix:
    from .evaluation import Backtester

    bt = Backtester(frame, base_cfg, step=step, horizon_min=horizon_min)
    base = _run_blocks(bt, base_cfg, n_blocks, strategy)
    base.label = "base"
    if log:
        log(f"  {symbol} base: n={base.n} E={base.e:+.2f}R")
    fm = FactorMatrix(symbol, base)
    names = factors or [k for k, v in base_cfg.factor_signs.items() if v]
    for name in names:
        sign = int(base_cfg.factor_signs.get(name, 1) or 1)
        inv_cfg = _with_signs(base_cfg, {**base_cfg.factor_signs, name: -sign})
        rem_cfg = _with_signs(base_cfg, {**base_cfg.factor_signs, name: 0})
        inv = _run_blocks(bt, inv_cfg, n_blocks, strategy)
        rem = _run_blocks(bt, rem_cfg, n_blocks, strategy)
        vd = verdict(base, inv, rem)
        fm.rows.append((name, sign, inv, rem, vd))
        if log:
            log(f"  {symbol} {name}: invertido E={inv.e:+.2f}R n={inv.n} · removido E={rem.e:+.2f}R n={rem.n} → {vd}")
    return fm


# --------------------------------------------------------------------------- corretora × Yahoo
@dataclass
class RealityRow:
    symbol: str
    broker: Optional[VariantResult]
    yahoo: Optional[VariantResult]

    @property
    def verdict(self) -> str:
        b, y = self.broker, self.yahoo
        if b is None or y is None:
            return "sem uma das fontes"
        if b.n < 20 or y.n < 20:
            return "inconclusivo (amostra < 20 em uma fonte)"
        if (b.e > 0) != (y.e > 0):
            return "EDGE NÃO CONFIRMADO — muda de sinal com a fonte de preço"
        if abs(b.e - y.e) > 0.30:
            return "EDGE FRÁGIL — mesma direção, mas > 0,30R de diferença"
        return "CONFIRMADO nas duas fontes" if b.e > 0 else "negativo nas duas fontes"

    def render(self) -> str:
        f = lambda v: "n/d" if v is None else f"n={v.n:<3} win {v.win:>4.0%} E {v.e:+.2f}R"  # noqa: E731
        return f"{self.symbol:<7} corretora {f(self.broker)}   ·   Yahoo {f(self.yahoo)}   →  {self.verdict}"


def reality_check(frames_broker: dict, frames_yahoo: dict, cfg_for: Callable[[str], object], n_blocks: int = 4, step: int = 1,
                  horizon_min: int = 240, strategy: str = "adaptive", log: Optional[Callable[[str], None]] = None) -> list[RealityRow]:
    from .evaluation import Backtester

    out = []
    for sym in sorted(set(frames_broker) | set(frames_yahoo)):
        cfg = cfg_for(sym)
        res = {}
        for src, frames in (("broker", frames_broker), ("yahoo", frames_yahoo)):
            fr = frames.get(sym)
            if fr is None or len(fr.xau) < 260:
                res[src] = None
                continue
            res[src] = _run_blocks(Backtester(fr, cfg, step=step, horizon_min=horizon_min), cfg, n_blocks, strategy)
            if log:
                log(f"  {sym} {src}: n={res[src].n} E={res[src].e:+.2f}R")
        out.append(RealityRow(sym, res["broker"], res["yahoo"]))
    return out


def render_reality(rows: list[RealityRow]) -> str:
    lines = ["🧪 BROKER REALITY CHECK — o mesmo backtest (parâmetros padrão) sobre o preço da corretora e sobre o Yahoo"]
    lines += ["  " + r.render() for r in rows]
    lines.append("  regra: uma vantagem que só existe numa fonte de preço não é vantagem — EDGE NÃO CONFIRMADO trava a promoção de risco.")
    return "\n".join(lines)
