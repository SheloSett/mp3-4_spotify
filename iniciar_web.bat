@echo off
title Media ^& Spotify Downloader Pro
color 0b
chcp 65001 > nul
cd /d "%~dp0"

echo ==========================================================
echo       MEDIA ^& SPOTIFY DOWNLOADER PRO (WEB)
echo ==========================================================
echo.

REM Si no existe el entorno virtual, se crea solo e instala las dependencias.
REM Antes el .bat caia a "python run_web.py", que en una PC sin Python
REM instalado (o con solo el alias de la Microsoft Store) fallaba al instante
REM y la app nunca arrancaba.
if not exist ".venv\Scripts\python.exe" (
    echo [*] No se encontro el entorno virtual .venv. Creandolo por primera vez...
    echo     Esto puede tardar unos minutos. Solo pasa una vez.
    echo.
    call :crear_venv
    if errorlevel 1 goto :error_instalacion
    echo.
    echo [OK] Entorno creado e instalado correctamente.
    echo.
)

echo [*] Usando entorno virtual .venv...
".venv\Scripts\python.exe" run_web.py
if errorlevel 1 goto :error_ejecucion
exit /b 0


:crear_venv
REM Opcion 1: uv (descarga su propio Python 3.11, no depende del Python del sistema).
set "UV_EXE="
where uv >nul 2>nul
if not errorlevel 1 set "UV_EXE=uv"
if not defined UV_EXE if exist "%USERPROFILE%\.local\bin\uv.exe" set "UV_EXE=%USERPROFILE%\.local\bin\uv.exe"

if defined UV_EXE (
    echo [*] Creando .venv con uv y Python 3.11...
    "%UV_EXE%" venv --python 3.11 .venv
    if errorlevel 1 exit /b 1
    echo [*] Instalando dependencias...
    "%UV_EXE%" pip install --python ".venv\Scripts\python.exe" -r requirements.txt
    if errorlevel 1 exit /b 1
    exit /b 0
)

REM Opcion 2: Python ya instalado en el sistema (py launcher o python en PATH).
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 (
    echo [*] Creando .venv con el Python del sistema ^(py -3^)...
    py -3 -m venv .venv
    if errorlevel 1 exit /b 1
) else (
    python -c "import sys" >nul 2>nul
    if errorlevel 1 (
        echo [!] No se encontro Python en esta PC.
        echo     Instala Python 3.11 o superior desde https://www.python.org/downloads/
        echo     ^(marca "Add python.exe to PATH"^) o instala uv desde https://docs.astral.sh/uv/
        exit /b 1
    )
    echo [*] Creando .venv con el Python del sistema ^(python^)...
    python -m venv .venv
    if errorlevel 1 exit /b 1
)
echo [*] Instalando dependencias...
".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
exit /b 0


:error_instalacion
echo.
echo [!] No se pudo crear el entorno virtual o instalar las dependencias.
echo     Revisa los mensajes de arriba. Podes reintentar borrando la carpeta .venv
echo     o instalando a mano con:  pip install -r requirements.txt
pause
exit /b 1

:error_ejecucion
echo.
echo [!] Ocurrio un error al iniciar la aplicacion.
echo     Si falta alguna dependencia, borra la carpeta .venv y volve a ejecutar
echo     este archivo para que se reinstale todo.
pause
exit /b 1
