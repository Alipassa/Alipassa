"""Alertas de movimento forte: probabilidade ≥ 90 % (ALERTA) ou ≥ 75 % (ATENÇÃO) de alta ou de queda.

Fontes do alerta, todas medidas no histórico real:
1. qualquer linha do quadro de probabilidades que passe do limiar;
2. choque no exterior acontecendo agora (VIX disparando, S&P caindo forte, dólar disparando...) cuja frequência
   histórica de alta/queda do mini índice no mesmo pregão passe do limiar.

Envio opcional ao Telegram: variáveis TELEGRAM_BOT_TOKEN (ou TOKEN_TELEGRAM) e TELEGRAM_CHAT_ID (ou CHAT_ID).
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

from . import model as M, stats as st

ALERTA = float(os.environ.get("B3AI_ALERTA", "0.90"))
ATENCAO = float(os.environ.get("B3AI_ATENCAO", "0.75"))

# choques acompanhados: (série, direção, descrição)
SHOCKS = [("VIX", +1, "VIX (índice do medo) disparando"), ("VIX", -1, "VIX (índice do medo) despencando"),
          ("ES", -1, "S&P 500 futuro em queda forte"), ("ES", +1, "S&P 500 futuro em alta forte"),
          ("DXY", +1, "dólar global (DXY) em alta forte"), ("US10Y", +1, "juro americano de 10 anos em alta forte"),
          ("BRENT", -1, "petróleo Brent em queda forte"), ("IRON", -1, "minério de ferro em queda forte"),
          ("CROSS", +1, "dólar disparando contra o real"), ("CROSS", -1, "dólar despencando contra o real"),
          ("EEM", -1, "emergentes (EEM) em queda forte"), ("EEM", +1, "emergentes (EEM) em alta forte")]


def level(p: float) -> str:
    return "ALERTA" if p >= ALERTA else "ATENÇÃO" if p >= ATENCAO else ""


def from_rows(ativos: dict) -> list[dict]:
    out = []
    for k, a in ativos.items():
        for r in a["prob"]:
            if r.get("tipo") == "sessao":
                continue      # "Fecho de hoje" só repete que o dia já andou; fica no quadro, não vira aviso no topo
            p, lado = (r["p_alta"], 1) if r["p_alta"] >= r["p_queda"] else (r["p_queda"], -1)
            lv = level(p)
            distante = "a próxima abertura é só em" in (r.get("nota") or "")
            if distante and lv == "ALERTA":
                lv = "ATENÇÃO"           # abertura daqui a dias: o exterior ainda vai andar, o número vai mudar
            if not lv:
                continue
            verbo = "ALTA" if lado > 0 else "QUEDA"
            out.append({"nivel": lv, "lado": lado, "ativo": k, "prob": p, "horizonte": r["horizonte"],
                        "titulo": f"{a['nome']}: {p * 100:.0f}% de chance de {verbo} — {r['horizonte']}"
                                  + (f" ({r['alvo'].split(' × ')[0].replace('abertura de ', '')})" if r.get("tipo") == "abertura" else ""),
                        "texto": f"{r['alvo']}. {r['confianca']}." + (f" {r['nota']}." if r.get("nota") else ""),
                        "chave": f"{k}|{r['horizonte']}|{lado}|{a['data']}"})
    return out


def from_shocks(key: str, nome: str, d: "M.DailyData", live: bool, cache: dict, thr: float = 1.5) -> list[dict]:
    """Choques no exterior em curso (`live`: pregão aberto ou exterior com barra mais nova que a do alvo)."""
    out = []
    if not live:
        return out
    for fk, direction, desc in SHOCKS:
        series = d.x.get(fk)
        if not series:
            continue
        now = d.fresh.get(fk, series[-1])
        sd = st.std(series[-250:])
        if not sd or now * direction <= thr * sd:
            continue
        ck = (key, fk, direction)
        if ck not in cache:
            cache[ck] = M.event_study(d, series, direction, thr)
        ev = cache[ck]
        if not ev:
            continue
        p, lado = (ev["dia_pct_alta"], 1) if ev["dia_pct_alta"] >= 0.5 else (1 - ev["dia_pct_alta"], -1)
        lv = level(p)
        if not lv:
            continue
        verbo = "alta" if lado > 0 else "queda"
        out.append({"nivel": lv, "lado": lado, "ativo": key, "prob": p, "horizonte": "choque no exterior",
                    "titulo": f"{nome}: {desc} — em {p * 100:.0f}% das vezes o pregão foi de {verbo}",
                    "texto": f"Em {ev['n']} pregões com esse choque, o {nome} fez em média {ev['dia_media']:+.2f}% no dia e "
                             f"{ev['seguinte_media']:+.2f}% no seguinte (alta em {ev['seguinte_pct_alta'] * 100:.0f}% deles).",
                    "chave": f"{key}|choque|{fk}|{direction}|{d.dates[-1]}"})
    return out


def strongest_shock(items: list[dict]) -> list[dict]:
    """Vários choques costumam dizer a mesma coisa (dólar, VIX e emergentes andam juntos): fica só o mais forte por ativo,
    com os demais citados em uma linha."""
    best: dict[str, dict] = {}
    rest: dict[str, list[str]] = {}
    for x in sorted(items, key=lambda x: -x["prob"]):
        if x["ativo"] not in best:
            best[x["ativo"]] = dict(x)
        else:
            rest.setdefault(x["ativo"], []).append(x["titulo"].split(": ", 1)[-1].split(" — ")[0])
    for k, x in best.items():
        if rest.get(k):
            x["texto"] += " Também: " + "; ".join(rest[k]) + "."
    return list(best.values())


def telegram_config() -> tuple[str, str]:
    return (os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TOKEN_TELEGRAM") or "",
            os.environ.get("TELEGRAM_CHAT_ID") or os.environ.get("CHAT_ID") or "")


def send_telegram(text: str) -> bool:
    token, chat = telegram_config()
    if not token or not chat:
        return False
    try:
        body = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data=body, timeout=10) as r:
            return bool(json.loads(r.read()).get("ok"))
    except Exception:  # noqa: BLE001 — alerta nunca derruba o ciclo
        return False
