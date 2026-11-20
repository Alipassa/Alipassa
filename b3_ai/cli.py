"""Linha de comando do B3 AI.

    python -m b3_ai painel                 # abre o painel em http://127.0.0.1:8766
    python -m b3_ai relatorio              # grava relatorios/relatorio_AAAAMMDD_HHMM.md e .html
    python -m b3_ai analise                # imprime o quadro de probabilidades no terminal
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime

from . import __version__, report
from .engine import Engine
from .model import BRT


def load_env(path: str = ".env") -> None:
    """Lê CHAVE=valor do .env (sem sobrescrever o que já está no ambiente): TELEGRAM_BOT_TOKEN, B3AI_ALERTA..."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _engine(args: argparse.Namespace) -> Engine:
    return Engine(data_dir=args.dados, with_news=not args.sem_noticias, fonte=args.fonte)


def cmd_painel(args: argparse.Namespace) -> int:
    from .server import serve
    serve(_engine(args), host=args.host, port=args.port, interval=args.intervalo, open_browser=not args.sem_navegador)
    return 0


def cmd_relatorio(args: argparse.Namespace) -> int:
    s = _engine(args).cycle()
    os.makedirs(args.saida, exist_ok=True)
    stamp = datetime.fromtimestamp(s["atualizado"], tz=BRT).strftime("%Y%m%d_%H%M")
    base = os.path.join(args.saida, f"relatorio_{stamp}")
    with open(base + ".md", "w", encoding="utf-8") as f:
        f.write(report.markdown(s))
    with open(base + ".html", "w", encoding="utf-8") as f:
        f.write(report.to_html(s))
    print(f"relatório gravado em {base}.md e {base}.html")
    return 0


def cmd_analise(args: argparse.Namespace) -> int:
    s = _engine(args).cycle()
    print(f"B3 AI {__version__} — pregão {s['pregao']} · mercado {'ABERTO' if s['mercado_aberto'] else 'FECHADO'}\n")
    for a in s["ativos"].values():
        print(f"{a['nome']}: {a['preco']:,.1f} ({a['var']:+.2f}%)")
        print(f"  {'horizonte':<14}{'alvo':<32}{'ALTA':>6}{'QUEDA':>7}  sinal")
        for r in a["prob"]:
            print(f"  {r['horizonte']:<14}{r['alvo']:<32}{r['p_alta'] * 100:>5.0f}%{r['p_queda'] * 100:>6.0f}%  {r['sinal']}"
                  f"{'' if r['validado'] else '  (sem vantagem comprovada)'}")
        print()
    for line in s["leitura"]:
        print("•", line)
    for w in s["avisos"]:
        print("aviso:", w)
    print("\n" + s["aviso_legal"])
    return 0


def cmd_auditoria(args: argparse.Namespace) -> int:
    from . import audit
    e = _engine(args)
    text = audit.run(e)
    os.makedirs(args.saida, exist_ok=True)
    path = os.path.join(args.saida, "auditoria_" + datetime.now(tz=BRT).strftime("%Y%m%d_%H%M") + ".md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print(f"\nauditoria gravada em {path}")
    return 0


def cmd_diagnostico(args: argparse.Namespace) -> int:
    from . import diagnose
    text = diagnose.run(args.dados)
    with open("diagnostico.txt", "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print("gravado em diagnostico.txt — envie esse arquivo (ou cole o texto) para análise")
    return 0


def cmd_simulacao(args: argparse.Namespace) -> int:
    from . import simulate
    text = simulate.run(_engine(args))
    os.makedirs(args.saida, exist_ok=True)
    path = os.path.join(args.saida, "simulacao_" + datetime.now(tz=BRT).strftime("%Y%m%d_%H%M") + ".md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print(f"\nsimulação gravada em {path}")
    return 0


def cmd_extremos(args: argparse.Namespace) -> int:
    from . import simulate
    text = simulate.run_extremes(_engine(args))
    os.makedirs(args.saida, exist_ok=True)
    path = os.path.join(args.saida, "extremos_" + datetime.now(tz=BRT).strftime("%Y%m%d_%H%M") + ".md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print(f"\nteste gravado em {path}")
    return 0


def cmd_gap(args: argparse.Namespace) -> int:
    from . import simulate
    text = simulate.run_gap(_engine(args))
    os.makedirs(args.saida, exist_ok=True)
    path = os.path.join(args.saida, "gap_" + datetime.now(tz=BRT).strftime("%Y%m%d_%H%M") + ".md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print(f"\nestudo gravado em {path}")
    return 0


def cmd_gap_ativos(args: argparse.Namespace) -> int:
    from . import simulate
    text = simulate.run_gap_assets(_engine(args), args.acoes)
    os.makedirs(args.saida, exist_ok=True)
    path = os.path.join(args.saida, "gap_ativos_" + datetime.now(tz=BRT).strftime("%Y%m%d_%H%M") + ".md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print(f"\nestudo gravado em {path}")
    return 0


def cmd_leiloes(args: argparse.Namespace) -> int:
    from . import bc_watch
    e = _engine(args)
    e.cycle()
    text = bc_watch.report(e.mem.bc_events(500))
    print(text)
    return 0


def cmd_instalar_calendario(args: argparse.Namespace) -> int:
    from .data import mt5 as mt5feed, mt5_calendar as mcal
    feed = mt5feed.MT5Feed()
    try:
        feed.connect()
        p = feed.paths()
    except Exception as e:  # noqa: BLE001
        print(f"não consegui falar com o MetaTrader: {e}")
        return 1
    for m in mcal.install(p.get("dados"), p.get("instalacao")):
        print("-", m)
    print("\nFalta um passo, no MetaTrader: na janela Navegador, em Consultor Expert (Experts), clique com o botão direito e escolha")
    print("'Atualizar'; depois arraste B3AI_Calendario para qualquer gráfico e clique OK. No canto do gráfico deve aparecer")
    print("'B3 AI - calendario exportado: N eventos'. O robô só lê o calendário: não envia ordens.")
    r = mcal.read(p.get("comum"))
    print(f"\nArquivo do calendário: {r['caminho']} — " + (f"{len(r['eventos'])} eventos" if r["ok"] else r["erro"]))
    feed.close()
    return 0


def cmd_horizontes(args: argparse.Namespace) -> int:
    from . import horizons
    text = horizons.run(_engine(args))
    os.makedirs(args.saida, exist_ok=True)
    path = os.path.join(args.saida, "horizontes_" + datetime.now(tz=BRT).strftime("%Y%m%d_%H%M") + ".md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    print(text)
    print(f"\nteste gravado em {path}")
    return 0


def cmd_opiniao(args: argparse.Namespace) -> int:
    e = _engine(args)
    e.cycle()
    r = e.run_ai()
    print(r.get("texto") or f"opinião indisponível: {r.get('erro')}")
    return 0 if r.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="b3_ai", description="B3 AI — mercado brasileiro × exterior, probabilidades do mini índice e do mini dólar")
    p.add_argument("--dados", default="dados", help="pasta do banco local (padrão: dados)")
    p.add_argument("--sem-noticias", action="store_true", help="não busca notícias")
    p.add_argument("--fonte", default="", choices=["", "web", "mt5"],
                   help="web = cotações públicas (~15 min de atraso) · mt5 = WIN/WDO em tempo real pelo MetaTrader 5 (Windows)")
    sub = p.add_subparsers(dest="cmd")
    pn = sub.add_parser("painel", help="painel no navegador")
    pn.add_argument("--host", default="127.0.0.1")
    pn.add_argument("--port", type=int, default=8766)
    pn.add_argument("--intervalo", type=int, default=60, help="segundos entre atualizações (mínimo 20)")
    pn.add_argument("--sem-navegador", action="store_true")
    pn.set_defaults(func=cmd_painel)
    rl = sub.add_parser("relatorio", help="grava o relatório do mini índice e do mini dólar")
    rl.add_argument("--saida", default="relatorios")
    rl.set_defaults(func=cmd_relatorio)
    an = sub.add_parser("analise", help="quadro de probabilidades no terminal")
    an.set_defaults(func=cmd_analise)
    au = sub.add_parser("auditoria", help="testa as fontes e mede os acertos fora da amostra")
    au.add_argument("--saida", default="relatorios")
    au.set_defaults(func=cmd_auditoria)
    dg = sub.add_parser("diagnostico", help="confere Python, MetaTrader 5 e fontes; grava diagnostico.txt")
    dg.set_defaults(func=cmd_diagnostico)
    sm = sub.add_parser("simulacao", help="simula as estratégias com custos: dá lucro ou não?")
    sm.add_argument("--saida", default="relatorios")
    sm.set_defaults(func=cmd_simulacao)
    ex = sub.add_parser("extremos", help="testa os extremos de compra e venda como entrada, com filtros e custos")
    ex.add_argument("--saida", default="relatorios")
    ex.set_defaults(func=cmd_extremos)
    gp = sub.add_parser("gap", help="estudo de uso da estratégia de devolução de gap (tamanho, direção, saída, stop, entrada)")
    gp.add_argument("--saida", default="relatorios")
    gp.set_defaults(func=cmd_gap)
    ga = sub.add_parser("gap-ativos", help="testa a devolução de gap nas ações mais negociadas, no índice cheio e no micro S&P")
    ga.add_argument("--saida", default="relatorios")
    ga.add_argument("--acoes", type=int, default=30, help="quantas ações (as mais negociadas)")
    ga.set_defaults(func=cmd_gap_ativos)
    lb = sub.add_parser("leiloes", help="registro de intervenções do Banco Central e movimentos bruscos, com o que o mercado fez depois")
    lb.set_defaults(func=cmd_leiloes)
    ic = sub.add_parser("instalar-calendario", help="copia e compila no MetaTrader o robô que exporta o calendário econômico")
    ic.set_defaults(func=cmd_instalar_calendario)
    hz = sub.add_parser("horizontes", help="qual prazo acerta mais: 5 min, 10 min, 30 min ou 1 hora")
    hz.add_argument("--saida", default="relatorios")
    hz.set_defaults(func=cmd_horizontes)
    op = sub.add_parser("opiniao", help="opinião do painel sobre os dados atuais (local, sem custo)")
    op.set_defaults(func=cmd_opiniao)
    args = p.parse_args(argv)
    load_env()
    if not getattr(args, "func", None):
        p.print_help()
        return 2
    try:
        return args.func(args)
    except Exception as e:  # noqa: BLE001
        print(f"erro: {e}", file=sys.stderr)
        return 1
