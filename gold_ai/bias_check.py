"""CHECKUP do GOLD BIAS + PAINEL — "está 100 % fazendo a sua função?"

Confere cada peça, de ponta a ponta, com ✅ (ok) · ⚠️ (funciona com limitação) · ❌ (não funciona) e o que fazer:
configuração (.env) → MetaTrader 5 (conexão, símbolo, preço ao vivo, candles, fuso) → dados da internet (dólar, juros, Fed,
VIX, petróleo, COT, notícias) → análise da IA (cobertura dos fatores) → memória (previsão × resultado) → painel (ao vivo)
→ Telegram (mensagem de teste). "Funcionando" ≠ "acertando": o acerto só se mede com o tempo (bias --mode stats).

    python market_ai_engine_v6.py bias --mode checkup            # sem enviar nada
    python market_ai_engine_v6.py bias --mode checkup --send     # inclui mensagem de teste no Telegram
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .bias import BIAS_FACTOR_LABELS, BiasMemory, GoldBiasEngine, bias_apply_manual, bias_fmt_price
from .telegram import TelegramSender, find_env_file, load_env_file

CHECK_WEB_LABELS: dict[str, str] = {
    "xau": "Ouro (Yahoo, reserva do MT5)", "dxy": "Dólar (DXY)", "us10y": "Treasury 10Y", "us2y": "Treasury 2Y",
    "extended": "Treasury 30Y · WTI · Brent", "fed_funds": "Expectativa do FED (Fed Funds)", "fred": "Juros reais (FRED)",
    "vix_spx": "VIX · S&P 500", "intermarket": "Prata · petróleo · BTC · yuan", "cot": "COT (CFTC, semanal)",
    "news": "Notícias (RSS)", "calendar": "Calendário econômico", "news_engine": "Leitura das notícias",
}
CHECK_OPTIONAL_WEB = ("cot", "calendar", "news_engine", "xau")   # falha nestes não impede a função principal


@dataclass
class CheckItem:
    group: str
    name: str
    status: str            # ✅ ⚠️ ❌
    detail: str = ""
    action: str = ""


@dataclass
class CheckReport:
    items: list[CheckItem] = field(default_factory=list)

    def add(self, group: str, name: str, status: str, detail: str = "", action: str = "") -> None:
        self.items.append(CheckItem(group, name, status, detail, action))

    @property
    def errors(self) -> list[CheckItem]:
        return [i for i in self.items if i.status == "❌"]

    @property
    def warnings(self) -> list[CheckItem]:
        return [i for i in self.items if i.status == "⚠️"]

    def render(self) -> str:
        lines = ["🩺 CHECKUP — GOLD BIAS + PAINEL", f"{datetime.now():%d/%m/%Y %H:%M}", ""]
        group = None
        for i in self.items:
            if i.group != group:
                group = i.group
                lines += ["", f"── {group} ──"]
            lines.append(f"{i.status} {i.name}" + (f": {i.detail}" if i.detail else ""))
            if i.action:
                lines.append(f"     → {i.action}")
        ok = sum(1 for i in self.items if i.status == "✅")
        lines += ["", "═" * 60, f"RESULTADO: {ok} ✅ · {len(self.warnings)} ⚠️ · {len(self.errors)} ❌"]
        if not self.errors and not self.warnings:
            lines.append("🟢 TUDO FUNCIONANDO — o robô está fazendo 100 % da sua função.")
        elif not self.errors:
            lines.append("🟢 FUNCIONANDO — os ⚠️ só reduzem a confiança da IA; nada impede o uso.")
        else:
            lines.append("🔴 HÁ PEÇAS PARADAS — resolva os ❌ acima (a ação está embaixo de cada um).")
        lines += ["", "Lembrete: funcionar ≠ acertar. O acerto da IA aparece com o tempo em:  bias --mode stats"]
        return "\n".join(lines)


def check_config(rep: CheckReport, env: dict[str, str]) -> None:
    g = "1. CONFIGURAÇÃO (.env)"
    path = find_env_file()
    if not path:
        rep.add(g, "Arquivo .env", "❌", "não encontrado nesta pasta",
                "coloque o .env na MESMA pasta do market_ai_engine_v6.py (aceita .env, .env.txt, env ou env.txt)")
        return
    rep.add(g, "Arquivo .env", "✅", os.path.basename(path))
    token = env.get("TELEGRAM_BOT_TOKEN") or env.get("TOKEN_TELEGRAM") or ""
    chat = env.get("TELEGRAM_CHAT_ID") or env.get("CHAT_ID") or ""
    if not token or "xxxx" in token or ":" not in token:
        rep.add(g, "Token do Telegram", "❌", "vazio ou ainda o exemplo", "preencha TOKEN_TELEGRAM com o token do @BotFather")
    else:
        rep.add(g, "Token do Telegram", "✅", token.split(":")[0] + ":••••••")
    if not chat or chat == "123456789" or not chat.lstrip("-").isdigit():
        rep.add(g, "Chat ID do Telegram", "❌", "vazio ou ainda o exemplo", "preencha CHAT_ID (número; peça ao @userinfobot)")
    else:
        rep.add(g, "Chat ID do Telegram", "✅", chat)
    rep.add(g, "Símbolo do ouro (MT5_SYMBOL)", "✅" if env.get("MT5_SYMBOL") else "⚠️", env.get("MT5_SYMBOL") or "não definido — usando XAUUSD",
            "" if env.get("MT5_SYMBOL") else "se a sua corretora usa outro nome (XAUUSD.m, GOLD…), defina MT5_SYMBOL no .env")


def check_mt5(rep: CheckReport, env: dict[str, str], client_factory: Optional[Any] = None) -> Optional[float]:
    """Conecta, lê o tick e os candles; confere se o último candle é recente (mercado aberto) e o fuso. Devolve o preço."""
    g = "2. METATRADER 5"
    try:
        from .data.mt5 import TF_TO_MT5, MT5Client, MT5Config
        cfg = MT5Config.from_env(env)
        client = client_factory(cfg) if client_factory else MT5Client(cfg)
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        rep.add(g, "Pacote MetaTrader5", "❌", msg,
                "no Prompt de Comando: pip install MetaTrader5" if "pacote" in msg or "MetaTrader5" in msg else "veja a mensagem acima")
        return None
    rep.add(g, "Pacote MetaTrader5", "✅", "instalado")
    try:
        client.connect()
    except Exception as e:  # noqa: BLE001
        rep.add(g, "Conexão com o terminal", "❌", str(e), "abra o MetaTrader 5, faça login na conta e rode o checkup de novo")
        return None
    rep.add(g, "Conexão com o terminal", "✅", f"símbolo {cfg.symbol}")
    price = None
    try:
        bid, ask = client.tick()
        price = (bid + ask) / 2
        spread = ask - bid
        rep.add(g, "Preço ao vivo (tick)", "✅", f"bid {bias_fmt_price(bid)} · ask {bias_fmt_price(ask)} · spread {spread:.2f}")
    except Exception as e:  # noqa: BLE001
        rep.add(g, "Preço ao vivo (tick)", "❌", str(e), f"confira se '{cfg.symbol}' está na Observação do Mercado e se o nome no .env é igual")
    counts, missing = {}, []
    for tf in TF_TO_MT5:
        try:
            counts[tf] = len(client.candles(tf))
        except Exception:  # noqa: BLE001
            missing.append(tf)
    if missing or not counts:
        rep.add(g, "Candles M1…W1", "❌", "sem " + ", ".join(missing or list(TF_TO_MT5)), "abra o gráfico do ouro no MT5 uma vez para baixar o histórico")
    else:
        few = [tf for tf, n in counts.items() if n < 200]
        rep.add(g, "Candles M1…W1", "⚠️" if few else "✅", " · ".join(f"{tf} {n}" for tf, n in counts.items()),
                ("pouco histórico em " + ", ".join(few) + " (EMA 200 precisa de 200 candles)") if few else "")
    try:
        m1 = client.candles("M1", 3)
        age = (datetime.now(timezone.utc) - m1[-1].time).total_seconds() / 60 if m1 else None
        note = getattr(client, "offset_note", "")
        off = getattr(client, "server_offset_hours", 0.0)
        if age is None:
            rep.add(g, "Dados atualizando", "❌", "sem candle M1", "abra o gráfico M1 do ouro no MT5")
        elif -2 <= age <= 3:
            rep.add(g, "Dados atualizando", "✅", f"último candle M1 há {max(age, 0):.0f} min · fuso do servidor {off:+.0f}h ({note or 'ok'})")
        elif datetime.now(timezone.utc).weekday() >= 5 or age > 60 * 20:
            rep.add(g, "Dados atualizando", "⚠️", f"último candle há {age / 60:.1f} h — mercado fechado (fim de semana/feriado)",
                    "normal fora do horário do ouro; teste de novo com o mercado aberto")
        else:
            rep.add(g, "Dados atualizando", "❌", f"último candle M1 há {age:.0f} min com o mercado aberto (fuso {off:+.0f}h)",
                    "confira a conexão do MT5 (canto inferior direito) ou defina MT5_UTC_OFFSET_HOURS no .env")
    except Exception as e:  # noqa: BLE001
        rep.add(g, "Dados atualizando", "⚠️", str(e))
    try:
        client.close()
    except Exception:  # noqa: BLE001
        pass
    return price


def check_web_and_analysis(rep: CheckReport, snapshot: Any, status: dict[str, str], mt5_price: Optional[float]) -> None:
    g = "3. DADOS DA INTERNET"
    if not status:
        rep.add(g, "Coleta web", "⚠️", "não executada")
    for key, label in CHECK_WEB_LABELS.items():
        st = status.get(key)
        if st is None:
            continue
        if st == "ok":
            rep.add(g, label, "✅")
        elif key in CHECK_OPTIONAL_WEB or "usando último" in st:
            rep.add(g, label, "⚠️", st.replace("erro: ", ""), "opcional — a IA segue sem isso" if key != "calendar" else "")
        else:
            rep.add(g, label, "❌" if "rede" in st or "timed out" in st or "URLError" in st else "⚠️", st.replace("erro: ", ""),
                    "verifique a internet/firewall/antivírus para o Python")
    web_down = [k for k in ("dxy", "us10y", "vix_spx") if status.get(k, "ok") != "ok"]
    if len(web_down) == 3:
        rep.add(g, "Internet para o Python", "❌", "dólar, juros e VIX falharam juntos",
                "a internet do Python está bloqueada (firewall, antivírus ou proxy); libere python.exe")

    g = "4. ANÁLISE DA IA"
    r = GoldBiasEngine().analyze(snapshot)
    cov = r.coverage
    missing = [BIAS_FACTOR_LABELS[f.name] for f in r.factors if not f.available]
    if cov >= 0.8:
        st = "✅"
    elif cov >= 0.5:
        st = "⚠️"
    else:
        st = "❌"
    rep.add(g, "Fatores com dado", st, f"{cov:.0%} do peso" + (f" · sem dado: {', '.join(missing)}" if missing else ""),
            "" if st == "✅" else "Inflação/Emprego só têm dado em dia de CPI/Payroll; China/Índia: preencha dados/manual.json")
    rep.add(g, "Leitura atual", "✅", f"{r.emoji} {r.label} · score {r.score:+.0f} · confiança {r.confidence:.0f}%")
    if mt5_price and snapshot.price and abs(snapshot.price / mt5_price - 1) > 0.01:
        rep.add(g, "Preço usado na análise", "⚠️", f"{bias_fmt_price(snapshot.price)} × MT5 {bias_fmt_price(mt5_price)}", "a análise não está com o preço da corretora")
    elif snapshot.price:
        rep.add(g, "Preço usado na análise", "✅", f"{bias_fmt_price(snapshot.price)} ({snapshot.price_source or 'web'})")
    else:
        rep.add(g, "Preço usado na análise", "❌", "sem preço", "MT5 e internet falharam — veja os itens 2 e 3")


def check_memory(rep: CheckReport, db_path: Optional[str]) -> None:
    g = "5. MEMÓRIA (previsão × resultado)"
    if not db_path:
        rep.add(g, "Banco de previsões", "⚠️", "desligado (--db vazio)")
        return
    if not os.path.exists(db_path):
        rep.add(g, "Banco de previsões", "⚠️", f"{db_path} ainda não existe", "é criado na 1ª leitura do painel ou do rodar_bias.bat")
        return
    try:
        mem = BiasMemory(db_path)
        n = mem.db.execute("SELECT COUNT(*) FROM bias_predictions").fetchone()[0]
        last = mem.db.execute("SELECT MAX(time) FROM bias_predictions").fetchone()[0]
        res = mem.db.execute("SELECT COUNT(*), SUM(hit) FROM bias_outcomes WHERE horizon = '1d'").fetchone()
        mem.close()
    except Exception as e:  # noqa: BLE001
        rep.add(g, "Banco de previsões", "❌", str(e), f"o arquivo {db_path} pode estar corrompido: renomeie-o e deixe o robô criar outro")
        return
    rep.add(g, "Banco de previsões", "✅", f"{n} leituras gravadas" + (f" · última {last[:16].replace('T', ' ')} UTC" if last else ""))
    done, hits = res[0] or 0, res[1] or 0
    if done < 30:
        rep.add(g, "Aprendizado (acerto em 1 dia)", "⚠️", f"{done} previsões avaliadas — ainda pouco para medir",
                "deixe rodando alguns dias; depois veja: bias --mode stats")
    else:
        rep.add(g, "Aprendizado (acerto em 1 dia)", "✅", f"{hits}/{done} = {hits / done:.0%}")


def check_panel(rep: CheckReport, port: int = 8765, opener: Any = None) -> None:
    g = "6. PAINEL"
    op = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        live = json.loads(op.open(f"http://127.0.0.1:{port}/api/live", timeout=5).read())
        st = json.loads(op.open(f"http://127.0.0.1:{port}/api/state", timeout=5).read())
    except Exception:  # noqa: BLE001
        rep.add(g, "Painel aberto", "⚠️", f"não está rodando em http://127.0.0.1:{port}", "para usar o painel, dê 2 cliques em rodar_painel.bat")
        return
    rep.add(g, "Painel aberto", "✅", f"http://127.0.0.1:{port}")
    p = live.get("price") or {}
    if p.get("live") and p.get("time"):
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(p["time"])).total_seconds()
        rep.add(g, "Tempo real (MT5 ao vivo)", "✅" if age <= 15 else "❌", f"última leitura há {age:.0f} s",
                "" if age <= 15 else "o painel parou de ler o MT5: feche e abra o rodar_painel.bat")
    else:
        rep.add(g, "Tempo real (MT5 ao vivo)", "⚠️", "painel sem MT5 ao vivo", "abra o painel com --source mt5 (o rodar_painel.bat já faz isso)")
    svc = st.get("service") or {}
    if svc.get("error"):
        rep.add(g, "Análise do painel", "❌", svc["error"], "veja a mensagem; se persistir, me envie um print")
    elif svc.get("cycles"):
        rep.add(g, "Análise do painel", "✅", f"{svc['cycles']} análise(s) feitas · a cada {svc.get('interval')} s")
    else:
        rep.add(g, "Análise do painel", "⚠️", "1ª análise ainda em andamento", "aguarde alguns minutos")


def check_telegram(rep: CheckReport, send: bool, sender: Optional[Any] = None) -> None:
    g = "7. TELEGRAM"
    tg = sender or TelegramSender(quiet=True)
    if tg.dry_run:
        rep.add(g, "Envio", "❌", "sem token/chat no .env", "veja o item 1")
        return
    if not send:
        rep.add(g, "Envio", "⚠️", "não testado", "rode com --send para receber uma mensagem de teste")
        return
    ok = tg.send("🩺 CHECKUP GOLD BIAS: mensagem de teste. Se você está lendo isto, o Telegram está funcionando ✅")
    rep.add(g, "Mensagem de teste", "✅" if ok else "❌", "enviada — confira no seu Telegram" if ok else "o Telegram recusou",
            "" if ok else "confira o token, o chat_id e se você já mandou /start para o seu bot")


def run_checkup(source: Any, db_path: Optional[str], manual_path: Optional[str], send: bool = False, port: int = 8765,
                mt5: bool = True, client_factory: Optional[Any] = None) -> CheckReport:
    rep = CheckReport()
    env = load_env_file()
    check_config(rep, env)
    mt5_price = check_mt5(rep, env, client_factory) if mt5 else None
    snap = source.snapshot() if hasattr(source, "snapshot") else source.collect()
    bias_apply_manual(snap, manual_path)
    status = dict(getattr(source, "status", {}) or {})
    de = getattr(source, "data_engine", None)
    if de is not None:
        status.update(de.status)
    check_web_and_analysis(rep, snap, status, mt5_price)
    check_memory(rep, db_path)
    check_panel(rep, port)
    check_telegram(rep, send)
    return rep
