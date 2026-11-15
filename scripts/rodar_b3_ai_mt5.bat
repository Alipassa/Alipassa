@echo off
REM ============================================================================
REM  B3 AI - painel com WIN e WDO em TEMPO REAL pelo MetaTrader 5 da corretora
REM  Antes: abra o MetaTrader 5 e faca login. Somente leitura: NUNCA envia ordens.
REM  Abre http://127.0.0.1:8766 sozinho. Para desligar feche esta janela.
REM ============================================================================
cd /d "%~dp0\.."
chcp 65001 > nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
if not defined MT5_PATH set "MT5_PATH=C:\Program Files\Mt5xp\terminal64.exe"
if not exist dados mkdir dados
python -c "import MetaTrader5" 2> nul
if errorlevel 1 (
  echo Instalando o pacote MetaTrader5 - so na primeira vez...
  python -m pip install MetaTrader5
)
:loop
python -m b3_ai --fonte mt5 painel --intervalo 20
echo [%date% %time%] painel terminou (codigo %errorlevel%) - reiniciando em 15s
timeout /t 15 /nobreak > nul
goto loop
