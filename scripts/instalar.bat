@echo off
REM ============================================================================
REM  GOLD AI ENGINE 3.0 - INSTALACAO (1 vez). Coloque na MESMA pasta de gold_ai_engine_v3.py.
REM  1) instala o pacote MetaTrader5 do Python   2) cria o .env se nao existir e abre para voce preencher
REM  3) roda a verificacao: Telegram (manda mensagem de teste) + MetaTrader 5 (conecta e le o preco do ouro)
REM ============================================================================
cd /d "%~dp0"
chcp 65001 > nul
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
python --version || (echo Python nao encontrado. Instale em https://www.python.org/downloads/ marcando "Add python.exe to PATH" & pause & exit /b 1)
python -m pip install --upgrade MetaTrader5
if not exist .env (
    if exist .env.example (copy .env.example .env > nul) else (echo TOKEN_TELEGRAM=> .env & echo CHAT_ID=>> .env & echo MT5_PATH=C:\Program Files\MetaTrader 5\terminal64.exe>> .env & echo MT5_SYMBOL=XAUUSD>> .env)
    echo.
    echo Preencha TOKEN_TELEGRAM, CHAT_ID e MT5_PATH no arquivo .env que vai abrir agora. Salve e feche o Bloco de Notas.
    notepad .env
)
echo.
echo Abra o MetaTrader 5 e faca login antes de continuar.
pause
python gold_ai_engine_v3.py setup
pause
