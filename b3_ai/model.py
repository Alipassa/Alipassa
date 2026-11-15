"""Modelos estatísticos — tudo medido fora da amostra, nada de probabilidade inventada.

1. CONFRONTO (fair value): regressão ridge móvel do retorno diário do alvo (WIN ou WDO) sobre os retornos dos
   fatores internacionais do mesmo dia → "o exterior implica X %, o Brasil fez Y %, descolamento Z".
2. DIÁRIO / SEMANAL: regressão logística sobre o estado no fechamento (exterior implícito, descolamento, momento,
   RSI, distância da média, VIX, câmbio/bolsa cruzados, regime de volatilidade) → P(alta) em 1 e 5 pregões.
3. INTRADAY: regressão logística em barras de 5 min (momento do WIN, liderança do S&P futuro, dólar, VWAP, RSI,
   gap, hora) → P(alta) em 5, 10, 30 e 60 minutos.

Cada modelo passa por walk-forward (treina no passado, prevê o trecho seguinte) e é CALIBRADO pelo que acertou
fora da amostra: p_final = base + k · (p_modelo − base), com k ∈ [0, 1] estimado nas previsões OOS. Sem poder
preditivo comprovado, k → 0 e a probabilidade volta para a taxa-base (~50 %). O painel mostra acerto e amostra OOS.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import stats as st

DAY_SHIFT = 8 * 3600      # ts + 8 h → data do pregão (futuros americanos abrem na noite anterior)
BRT = timezone(timedelta(hours=-3))

FEATURES_D = ["exterior no dia", "descolamento Brasil × exterior", "momento 5 pregões", "RSI 14", "distância da média 20",
              "nível do VIX", "ativo cruzado (dólar ↔ bolsa)", "regime de volatilidade"]
FEATURES_I = ["momento curto (1 barra)", "momento médio (3 barras)", "momento longo (12 barras)", "S&P futuro curto",
              "S&P futuro médio", "S&P futuro longo", "dólar (3 barras)", "distância do VWAP", "RSI 14 intraday",
              "gap de abertura", "hora do pregão"]


def bar_date(ts: float) -> str:
    return datetime.fromtimestamp(ts + DAY_SHIFT, tz=timezone.utc).strftime("%Y-%m-%d")


def daily_closes(bars: list[tuple]) -> dict[str, float]:
    return {bar_date(b[0]): b[4] for b in bars}


# ============================================================================= calibração / avaliação
def evaluate(preds: list[float], ys: list[int], base: float) -> dict:
    """Métricas fora da amostra + inclinação de calibração k (quanto do desvio do modelo é real)."""
    n = len(preds)
    if n < 30:
        return {"n": n, "k": 0.0, "hit": None, "skill": None, "hit_conv": None, "n_conv": 0}
    num = sum((p - base) * (y - base) for p, y in zip(preds, ys))
    den = sum((p - base) ** 2 for p in preds)
    k = st.clamp(num / den, 0.0, 1.0) if den > 1e-9 else 0.0
    hits = sum(1 for p, y in zip(preds, ys) if (p >= 0.5) == (y == 1))
    brier = st.mean([(p - y) ** 2 for p, y in zip(preds, ys)])
    brier0 = st.mean([(base - y) ** 2 for y in ys])
    cal = [base + k * (p - base) for p in preds]
    conv = [(p, y) for p, y in zip(cal, ys) if abs(p - 0.5) >= 0.05]
    hit_conv = sum(1 for p, y in conv if (p >= 0.5) == (y == 1)) / len(conv) if len(conv) >= 15 else None
    return {"n": n, "k": round(k, 3), "hit": round(hits / n, 4), "skill": round(1 - brier / brier0, 4) if brier0 else None,
            "hit_conv": round(hit_conv, 4) if hit_conv is not None else None, "n_conv": len(conv)}


def walk_forward(xs: list[list[float]], ys: list[int], start: int, step: int, lam: float) -> tuple[list[float], list[int]]:
    preds: list[float] = []
    outs: list[int] = []
    i = start
    while i < len(xs):
        w = st.logistic_fit(xs[:i], ys[:i], lam=lam)
        j = min(i + step, len(xs))
        preds += [st.logistic_predict(w, r) for r in xs[i:j]]
        outs += ys[i:j]
        i = j
    return preds, outs


def fit_calibrated(xs: list[list[float]], ys: list[int], names: list[str], lam: float, start_frac: float = 0.5,
                   folds: int = 4, embargo: int = 0) -> Optional[dict]:
    """Ajusta, valida em walk-forward e devolve o modelo com a calibração OOS."""
    n = len(xs)
    if n < 200:
        return None
    start = max(150, int(n * start_frac))
    step = max(20, (n - start) // folds + 1)
    if embargo:   # alvos sobrepostos (5 pregões): descarta as últimas amostras de cada treino
        preds, outs = [], []
        i = start
        while i < n:
            w = st.logistic_fit(xs[:i - embargo], ys[:i - embargo], lam=lam)
            j = min(i + step, n)
            preds += [st.logistic_predict(w, r) for r in xs[i:j]]
            outs += ys[i:j]
            i = j
    else:
        preds, outs = walk_forward(xs, ys, start, step, lam)
    base = st.mean(ys)
    ev = evaluate(preds, outs, base)
    return {"w": st.logistic_fit(xs, ys, lam=lam), "base": base, "eval": ev, "names": names, "n_train": n}


def apply_model(model: dict, row: list[float]) -> dict:
    raw = st.logistic_predict(model["w"], row)
    base, k = model["base"], model["eval"]["k"]
    p = st.clamp(base + k * (raw - base), 0.05, 0.95)
    contrib = sorted(((model["names"][j], model["w"][j + 1] * v * k) for j, v in enumerate(row)), key=lambda x: -abs(x[1]))
    return {"p_up": p, "p_raw": raw, "base": base,
            "drivers": [{"nome": nm, "efeito": round(c, 4)} for nm, c in contrib[:4] if abs(c) > 1e-4]}


def classify(p_up: float, ev: dict) -> dict:
    """Rótulo de decisão: só há viés quando a probabilidade se afasta de 50 % E o modelo provou algo fora da amostra."""
    proven = bool(ev.get("k", 0) >= 0.15 and (ev.get("hit") or 0) > 0.515 and ev.get("n", 0) >= 100)
    if p_up >= 0.60:
        label = "ALTA FORTE"
    elif p_up >= 0.55:
        label = "VIÉS DE ALTA"
    elif p_up <= 0.40:
        label = "QUEDA FORTE"
    elif p_up <= 0.45:
        label = "VIÉS DE QUEDA"
    else:
        label = "NEUTRO"
    return {"sinal": label, "validado": proven,
            "confianca": "validada fora da amostra" if proven else "sem vantagem estatística comprovada"}


def bands(p_up: float, sigma: float) -> dict:
    """Faixas de retorno (normal com média deslocada para bater com p_up)."""
    z = st.phi_inv(p_up)
    up_strong = st.phi(z - 1)
    down_strong = st.phi(-1 - z)
    return {"alta_forte": up_strong, "alta": p_up - up_strong, "queda": (1 - p_up) - down_strong,
            "queda_forte": down_strong, "sigma_pct": sigma * 100}


# ============================================================================= modelo diário (confronto + 1d/5d)
class DailyData:
    """Séries diárias alinhadas às datas de pregão do alvo."""

    def __init__(self, target_bars: list[tuple], factor_bars: dict[str, list[tuple]], kinds: dict[str, str]) -> None:
        self.bars = target_bars
        self.dates = [bar_date(b[0]) for b in target_bars]
        self.close = [b[4] for b in target_bars]
        n = len(self.close)
        self.ret = [0.0] + [self.close[i] / self.close[i - 1] - 1 for i in range(1, n)]
        self.factor_keys: list[str] = []
        self.x: dict[str, list[float]] = {}
        self.fdate: dict[str, str] = {}
        self.fresh: dict[str, float] = {}   # retorno das barras do fator POSTERIORES ao último pregão do alvo
        for key, bars in factor_bars.items():
            if len(bars) < 300:
                continue
            fc = daily_closes(bars)
            fdates = sorted(fc)
            if fdates[0] > self.dates[max(0, n - 700)]:
                continue   # histórico curto demais para a regressão
            aligned: list[Optional[float]] = []
            j, last = 0, None
            for d in self.dates:
                while j < len(fdates) and fdates[j] <= d:
                    last = fc[fdates[j]]
                    j += 1
                aligned.append(last)
            col = [0.0] * n
            for i in range(1, n):
                a, b = aligned[i - 1], aligned[i]
                if a is not None and b is not None and a:
                    col[i] = (b - a) / 100.0 if kinds.get(key) == "yield" else b / a - 1
            self.factor_keys.append(key)
            self.x[key] = col
            self.fdate[key] = fdates[-1]
            if fdates[-1] > self.dates[-1] and aligned[-1]:
                lastv = fc[fdates[-1]]
                self.fresh[key] = (lastv - aligned[-1]) / 100.0 if kinds.get(key) == "yield" else lastv / aligned[-1] - 1


def rolling_betas(d: DailyData, window: int = 250, every: int = 5, lam: float = 60.0) -> tuple[list[float], dict[str, float], float]:
    """Exterior implícito ponto-a-ponto (betas só com dados anteriores a cada dia). Devolve (implícito[], betas atuais, R²)."""
    n = len(d.ret)
    keys = d.factor_keys
    implied = [0.0] * n
    beff: dict[str, float] = {k: 0.0 for k in keys}
    if not keys:
        return implied, beff, 0.0
    for t in range(window + 1, n + 1):   # t = n calcula os betas "de hoje" (usados no confronto ao vivo)
        if (t - window - 1) % every == 0 or t == n:
            lo = t - window
            sds = {k: st.std(d.x[k][lo:t]) or 1e-9 for k in keys}
            xs = [[d.x[k][i] / sds[k] for k in keys] for i in range(lo, t)]
            b = st.ridge(xs, d.ret[lo:t], lam=lam)
            beff = {k: b[j] / sds[k] for j, k in enumerate(keys)}
        if t < n:
            implied[t] = sum(beff[k] * d.x[k][t] for k in keys)
    lo = max(window + 1, n - 250)
    res = [d.ret[i] - implied[i] for i in range(lo, n)]
    var = st.std(d.ret[lo:n]) ** 2
    r2 = 1 - (st.std(res) ** 2) / var if var else 0.0
    return implied, beff, st.clamp(r2, 0.0, 1.0)


def daily_features(d: DailyData, implied: list[float], vix: list[float], cross_ret: list[float]) -> tuple[list[Optional[list[float]]], list[float]]:
    n = len(d.close)
    rsi = st.rsi(d.close, 14)
    sma20 = st.sma(d.close, 20)
    feats: list[Optional[list[float]]] = [None] * n
    sig = [0.0] * n
    for t in range(300, n):
        s = st.std(d.ret[t - 60:t]) or 1e-9
        sig[t] = s
        s250 = st.std(d.ret[t - 250:t]) or 1e-9
        vwin = vix[t - 250:t]
        vsd = st.std(vwin) or 1e-9
        cs = st.std(cross_ret[t - 60:t]) or 1e-9
        r5 = d.close[t] / d.close[t - 5] - 1
        feats[t] = [
            st.clamp(implied[t] / s, -4, 4),
            st.clamp((d.ret[t] - implied[t]) / s, -4, 4),
            st.clamp(r5 / (s * math.sqrt(5)), -4, 4),
            (rsi[t] - 50) / 50,
            st.clamp((d.close[t] / (sma20[t] or d.close[t]) - 1) / (s * math.sqrt(20)), -4, 4),
            st.clamp((vix[t] - st.mean(vwin)) / vsd, -4, 4) if vix[t] else 0.0,
            st.clamp(cross_ret[t] / cs, -4, 4),
            st.clamp(st.std(d.ret[t - 20:t]) / s250 - 1, -2, 3),
        ]
    return feats, sig


def aligned_levels(d: DailyData, bars: list[tuple]) -> list[float]:
    fc = daily_closes(bars) if bars else {}
    fdates = sorted(fc)
    out, j, last = [], 0, 0.0
    for dt in d.dates:
        while j < len(fdates) and fdates[j] <= dt:
            last = fc[fdates[j]]
            j += 1
        out.append(last)
    return out


def build_daily(d: DailyData, vix_bars: list[tuple], cross: DailyData, last_complete: bool) -> dict:
    """Treina o confronto e os modelos de 1 e 5 pregões para um alvo. `last_complete`: a última barra é um pregão fechado."""
    implied, betas, r2 = rolling_betas(d)
    vix = aligned_levels(d, vix_bars)
    cmap = dict(zip(cross.dates, cross.ret))
    cross_ret = [cmap.get(dt, 0.0) for dt in d.dates]
    feats, sig = daily_features(d, implied, vix, cross_ret)
    n = len(d.close)
    end = n if last_complete else n - 1          # índice (exclusivo) do último pregão fechado
    models = {}
    for h, lam in ((1, 20.0), (5, 40.0)):
        xs, ys = [], []
        for t in range(300, end - h):
            if feats[t] is None:
                continue
            xs.append(feats[t])
            ys.append(1 if d.close[t + h] > d.close[t] else 0)
        models[h] = fit_calibrated(xs, ys, FEATURES_D, lam, start_frac=0.45, folds=8, embargo=h - 1)
    # análogos: mesmo sinal de exterior e de descolamento → o que aconteceu no pregão seguinte
    analog = None
    now = feats[n - 1]
    if now:
        hits = tot = 0
        for t in range(300, end - 1):
            f = feats[t]
            if f and (f[0] > 0) == (now[0] > 0) and (f[1] > 0) == (now[1] > 0):
                tot += 1
                hits += 1 if d.close[t + 1] > d.close[t] else 0
        if tot >= 30:
            analog = {"n": tot, "p_up": hits / tot}
    return {"implied": implied, "betas": betas, "r2": r2, "feats": feats, "sigma": sig, "models": models, "analog": analog,
            "resid_sd": st.std([d.ret[i] - implied[i] for i in range(max(301, n - 250), end)]) or 1e-9}


# ============================================================================= modelo intraday (5 min)
# horizonte → (timeframe das barras em minutos, nº de barras à frente)
HORIZONS_I = {"5 min": (5, 1), "10 min": (5, 2), "30 min": (15, 2), "1 hora": (15, 4)}


def intraday_features(win: list[tuple], es: list[tuple], wdo: list[tuple]) -> dict:
    n = len(win)
    ts = [b[0] for b in win]
    close = [b[4] for b in win]

    def align(bars: list[tuple]) -> list[Optional[float]]:
        out: list[Optional[float]] = []
        j, last = 0, None
        for t in ts:
            while j < len(bars) and bars[j][0] <= t:
                last = bars[j][4]
                j += 1
            out.append(last)
        return out

    esc, wdc = align(es), align(wdo)
    new_sess = [i == 0 or ts[i] - ts[i - 1] > 3600 for i in range(n)]
    r = [0.0] * n     # retorno de 5 min dentro do pregão (gap de abertura fica à parte)
    re_ = [0.0] * n
    rd = [0.0] * n
    gap = [0.0] * n
    for i in range(1, n):
        if new_sess[i]:
            gap[i] = win[i][1] / close[i - 1] - 1
            r[i] = close[i] / win[i][1] - 1
        else:
            gap[i] = gap[i - 1]
            r[i] = close[i] / close[i - 1] - 1
            if esc[i] and esc[i - 1]:
                re_[i] = esc[i] / esc[i - 1] - 1
            if wdc[i] and wdc[i - 1]:
                rd[i] = wdc[i] / wdc[i - 1] - 1
    rsi = st.rsi(close, 14)
    feats: list[Optional[list[float]]] = [None] * n
    sig = [0.0] * n
    pv = vv = 0.0
    k_open = 0
    for i in range(n):
        if new_sess[i]:
            pv = vv = 0.0
            k_open = i
        vol = win[i][5] or 1.0
        pv += (win[i][2] + win[i][3] + win[i][4]) / 3 * vol
        vv += vol
        if i < 320:
            continue
        s = st.std(r[i - 300:i]) or 1e-9
        se = st.std(re_[i - 300:i]) or 1e-9
        sd = st.std(rd[i - 300:i]) or 1e-9
        sig[i] = s
        vwap = pv / vv
        feats[i] = [
            st.clamp(r[i] / s, -5, 5),
            st.clamp(sum(r[i - 2:i + 1]) / (s * 1.732), -5, 5),
            st.clamp(sum(r[i - 11:i + 1]) / (s * 3.464), -5, 5),
            st.clamp(re_[i] / se, -5, 5),
            st.clamp(sum(re_[i - 2:i + 1]) / (se * 1.732), -5, 5),
            st.clamp(sum(re_[i - 11:i + 1]) / (se * 3.464), -5, 5),
            st.clamp(sum(rd[i - 2:i + 1]) / (sd * 1.732), -5, 5),
            st.clamp((close[i] / vwap - 1) / (s * 3.464), -5, 5),
            (rsi[i] - 50) / 50,
            st.clamp(gap[i] / (s * 10.5), -4, 4),
            st.clamp((i - k_open) / 110.0, 0, 1.2) - 0.5,
        ]
    return {"feats": feats, "sigma": sig, "ts": ts, "close": close, "new_sess": new_sess}


def build_intraday(win: list[tuple], es: list[tuple], wdo: list[tuple], tf_min: int, horizons: dict[str, int]) -> Optional[dict]:
    """Modelos intraday num timeframe (`tf_min` minutos por barra); `horizons` = {nome: barras à frente}.

    Barras sem S&P futuro alinhado entram com os fatores do S&P zerados (o histórico público dele é mais curto).
    """
    if len(win) < 800:
        return None
    f = intraday_features(win, es, wdo)
    feats, ts, close = f["feats"], f["ts"], f["close"]
    n = len(win)
    sec = tf_min * 60
    models = {}
    for name, h in horizons.items():
        xs, ys = [], []
        for i in range(n - h):
            if feats[i] is None or ts[i + h] - ts[i] > h * sec + 900 or close[i + h] == close[i]:
                continue
            xs.append(feats[i])
            ys.append(1 if close[i + h] > close[i] else 0)
        models[name] = fit_calibrated(xs, ys, FEATURES_I, lam=30.0, start_frac=0.5, folds=4, embargo=h - 1)
    return {"models": models, "f": f, "tf": tf_min}
