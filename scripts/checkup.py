"""CHECKUP — como o robô está AGORA, analisando o mercado de janeiro até hoje.

Uso (na pasta do robô, com o MT5 aberto e o .env configurado):
    python scripts\\checkup.py
    python scripts\\checkup.py --start 2026-01-01 --markets XAUUSD,US500,EURUSD,USDJPY,WTI

Roda, em sequência, as provas que já existem no market_ai_engine_v6.py e junta tudo em um único arquivo
checkup_<data>.txt. Cada etapa é independente: se uma falhar, as outras continuam e a falha fica registrada.

Etapas:
  1. estimate      — walk-forward fora da amostra desde janeiro: retorno, drawdown, operações, expectancy por mercado
  2. autotune      — parâmetros que o passado escolhe (piso × confirmações × sinal × filtro de lateral), OOS por mercado
  3. matrix        — confirmações 1→5 × posições simultâneas 1→4 (escolhe na 1ª metade, confere na 2ª)
  4. edge          — o que o robô VIVEU no demo (LIVE EDGE: OOS por construção)
  5. dia           — eficiência de ontem e de hoje (o que viu, fez e deixou na mesa)
  6. doctor        — saúde do sistema (dados, fontes, memória)
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

ENGINE = "market_ai_engine_v6.py"
REPORT = ""


def run(step: str, cmd: list[str], out: list[str], timeout_min: int = 240) -> None:
    """Roda uma etapa mostrando a saída NA HORA (linha a linha) e guardando tudo para o relatório."""
    head = f"\n{'=' * 100}\n=== {step} ===\n{'=' * 100}\n$ {' '.join(cmd)}\n"
    print(head, flush=True)
    out.append(head)
    t0 = time.time()
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    lines: list[str] = []
    timed_out = False
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             encoding="utf-8", errors="replace", bufsize=1, env=env)
        assert p.stdout is not None
        deadline = t0 + timeout_min * 60
        for line in p.stdout:
            try:
                print(line, end="", flush=True)
            except UnicodeEncodeError:          # console sem UTF-8: mostra sem os emojis, arquivo guarda tudo
                print(line.encode("ascii", "replace").decode(), end="", flush=True)
            lines.append(line)
            if time.time() > deadline:
                timed_out = True
                p.kill()
                break
        rc = p.wait()
    except OSError as e:
        lines.append(f"\n[erro ao iniciar: {e}]\n")
        rc = -1
    if timed_out:
        tail = f"\n[{step}: TEMPO ESGOTADO após {timeout_min} min]\n"
    else:
        tail = f"\n[{step}: código {rc} em {time.time() - t0:.0f}s]\n"
    print(tail, flush=True)
    out.append("".join(lines) + tail)
    if REPORT:                                  # grava o parcial: Ctrl+C depois não perde o que já rodou
        with open(REPORT, "w", encoding="utf-8") as f:
            f.write("".join(out))


def main() -> int:
    ap = argparse.ArgumentParser(description="checkup do robô: janeiro → hoje")
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--markets", default="XAUUSD,US500,EURUSD,USDJPY,WTI")
    ap.add_argument("--csv-dir", default=None, help="pasta com <SYM>_h1.csv (senão Yahoo, como a pipeline)")
    ap.add_argument("--events", default=os.path.join("dados", "noticias_historicas.csv"))
    ap.add_argument("--db", default="gold_ai.db", help="memória do live (gold_ai.db na pasta do robô)")
    ap.add_argument("--equity", type=float, default=50000.0)
    ap.add_argument("--risk", type=float, default=3.0)
    ap.add_argument("--skip", default="", help="etapas a pular, ex.: matrix,autotune")
    args = ap.parse_args()

    if not os.path.exists(ENGINE):
        print(f"{ENGINE} não está nesta pasta — rode a partir da pasta do robô (cd \"C:\\Users\\Alisson\\Desktop\\IA OURO E PETROLEO\")")
        return 1
    end = args.end or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    skip = {s.strip().lower() for s in args.skip.split(",") if s.strip()}
    py = sys.executable
    base = [py, ENGINE]
    common = ["--start", args.start, "--end", end, "--markets", args.markets, "--events", args.events]
    if args.csv_dir:
        common += ["--csv-dir", args.csv_dir]
    global REPORT
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    report = REPORT = f"checkup_{stamp}.txt"
    print(f"relatório parcial em {report} (atualizado ao fim de cada etapa); etapas longas: estimate, autotune, matrix, false-signals\n"
          f"para uma resposta rápida: python checkup.py --skip autotune,matrix,false-signals", flush=True)
    out: list[str] = [f"CHECKUP MARKET AI ENGINE · {stamp} · {args.start} → {end} · mercados {args.markets}\n"]

    if "estimate" not in skip:
        run("1/6 ESTIMATIVA walk-forward fora da amostra (retorno, DD, operações, expectancy por mercado)",
            base + ["estimate"] + common + ["--equity", str(args.equity), "--risk", str(args.risk), "--news-mode", "full"], out)
    if "autotune" not in skip:
        run("2/6 AUTOTUNE (piso × confirmações × sinal × filtro de lateral) — o passado escolhe, o OOS confere",
            base + ["autotune"] + common + ["--equity", str(args.equity), "--risk", str(args.risk), "--out", os.path.join("dados", "parametros.json")], out)
    if "matrix" not in skip:
        run("3/6 MATRIZ confirmações 1→5 × posições 1→4 (escolhe na 1ª metade, confere na 2ª)",
            base + ["matrix"] + common + ["--equity", str(args.equity), "--risk", str(args.risk), "--out", "matriz.txt"], out)
    if "false-signals" not in skip:
        run("3b/6 FALSE SIGNAL FILTER — onde o robô erra fora da amostra (mercado × sessão × regime × evento)",
            base + ["false-signals"] + common + ["--out", os.path.join("dados", "falsos_sinais.json"), "--txt", "falsos_sinais.txt"], out)
    if "edge" not in skip:
        run("4/6 LIVE EDGE — o que o robô viveu no demo", base + ["edge", "--db", args.db, "--markets", args.markets], out, timeout_min=10)
    if "dia" not in skip:
        today = datetime.now(timezone.utc)
        for d in (today - timedelta(days=1), today):
            run(f"5/6 EFICIÊNCIA DO DIA {d:%d/%m}", base + ["dia", "--db", args.db, "--day", d.strftime("%Y-%m-%d"), "--markets", args.markets], out, timeout_min=5)
    if "doctor" not in skip:
        run("6/6 DOCTOR — saúde do sistema", base + ["doctor", "--db", args.db], out, timeout_min=10)

    with open(report, "w", encoding="utf-8") as f:
        f.write("".join(out))
    print(f"\n✅ checkup completo → {report}  (mande este arquivo)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
