@echo off
REM ============================================================================
REM  GOLD BIAS ENGINE - vies do ouro 24/7 no Telegram (reinicia sozinho se cair)
REM  Coloque na MESMA pasta de market_ai_engine_v6.py e do .env. MT5 aberto e logado.
REM  NAO opera: so analisa (MT5 + macro + noticias + tecnico) e envia ao Telegram quando algo muda.
REM  Relatorio da manha:  python market_ai_engine_v6.py bias --mode manha --send
REM  Fechamento do dia:   python market_ai_engine_v6.py bias --mode fechamento --send
REM  Acerto x erro:       python market_ai_engine_v6.py bias --mode stats
REM  Saida nesta janela e em logs\bias.log. Para desligar feche a janela.
REM ============================================================================
cd /d "%~dp0"
chcp 65001 > nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
if not exist logs mkdir logs
:loop
echo [%date% %time%] iniciando GOLD BIAS
python market_ai_engine_v6.py --log-file logs\bias.log bias --source mt5 --send --interval 300 --verbose
echo [%date% %time%] bias terminou (codigo %errorlevel%) - reiniciando em 30s
timeout /t 30 /nobreak > nul
goto loop
