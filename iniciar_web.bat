@echo off
title Media & Spotify Downloader Pro
color 0b
chcp 65001 > nul
cd /d "%~dp0"

echo ==========================================================
echo       MEDIA & SPOTIFY DOWNLOADER PRO (WEB)
echo ==========================================================
echo.

if exist ".venv\Scripts\python.exe" (
    echo [*] Usando entorno virtual .venv...
    ".venv\Scripts\python.exe" run_web.py
) else (
    echo [*] Usando Python del sistema...
    python run_web.py
)

if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [!] Ocurrio un error al iniciar la aplicacion.
    echo Asegurate de haber instalado los requerimientos con:
    echo pip install -r requirements.txt
    pause
)
