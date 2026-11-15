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
    """Lê CHAVE=valor do .env (sem sobrescrever o que já está no ambiente): ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN..."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _engine(args: argparse.Namespace) -> Engine:
    return Engine(data_dir=args.dados, with_news=not args.sem_noticias)


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


def cmd_analise_ia(args: argparse.Namespace) -> int:
    e = _engine(args)
    e.cycle()
    r = e.run_ai()
    print(r.get("texto") or f"analista com IA indisponível: {r.get('erro')}")
    return 0 if r.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="b3_ai", description="B3 AI — mercado brasileiro × exterior, probabilidades do mini índice e do mini dólar")
    p.add_argument("--dados", default="dados", help="pasta do banco local (padrão: dados)")
    p.add_argument("--sem-noticias", action="store_true", help="não busca notícias")
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
    ia = sub.add_parser("analise-ia", help="análise escrita pelo Claude a partir dos dados (precisa de ANTHROPIC_API_KEY)")
    ia.set_defaults(func=cmd_analise_ia)
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
