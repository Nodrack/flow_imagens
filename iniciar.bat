@echo off
setlocal
cd /d "%~dp0"
title Fluxo Imagens - inicializacao local

echo [1/4] Verificando Python 3.11 ou superior...
if not exist ".venv\Scripts\python.exe" (
    where py >nul 2>nul
    if not errorlevel 1 (
        py -3 -m venv .venv
    ) else (
        where python >nul 2>nul
        if errorlevel 1 (
            echo ERRO: Python nao encontrado. Instale Python 3.11 ou superior.
            goto :fail
        )
        python -m venv .venv
    )
    if errorlevel 1 (
        echo ERRO: nao foi possivel criar .venv. Confira a instalacao do Python.
        goto :fail
    )
)

".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
    echo ERRO: o ambiente .venv precisa de Python 3.11 ou superior.
    goto :fail
)

echo [2/4] Verificando dependencias fixadas...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo ERRO: instalacao de dependencias falhou. Verifique rede, proxy ou pip.
    goto :fail
)

echo [3/4] Verificando integridade das dependencias...
".venv\Scripts\python.exe" -m pip check
if errorlevel 1 (
    echo ERRO: pip encontrou dependencias incompativeis.
    goto :fail
)

echo [4/4] Procurando uma porta local livre...
set "PORT="
for /f %%P in ('powershell -NoProfile -Command "$p=8501; while (Get-NetTCPConnection -State Listen -LocalPort $p -ErrorAction SilentlyContinue) { $p++ }; $p"') do set "PORT=%%P"
if not defined PORT (
    echo ERRO: nao foi possivel selecionar uma porta local.
    goto :fail
)

echo Iniciando Streamlit somente em localhost na porta %PORT%...
echo URL: http://127.0.0.1:%PORT%
".venv\Scripts\python.exe" -m streamlit run app.py --server.address 127.0.0.1 --server.port %PORT%
if errorlevel 1 (
    echo ERRO: Streamlit nao iniciou. Confirme se a porta 8501 esta livre.
    goto :fail
)
goto :eof

:fail
echo.
echo Diagnostico: consulte a mensagem acima; nenhuma geracao ou transferencia foi iniciada.
pause
exit /b 1