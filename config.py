# -*- coding: utf-8 -*-
import os
import shutil
import sys
import platform
from pathlib import Path
from dotenv import load_dotenv, set_key, dotenv_values

# Cargar variables de entorno desde .env
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"

if not ENV_PATH.exists():
    example_path = BASE_DIR / ".env.example"
    if example_path.exists():
        shutil.copy(example_path, ENV_PATH)
    else:
        with open(ENV_PATH, "w", encoding="utf-8") as f:
            f.write("SPOTIFY_CLIENT_ID=\nSPOTIFY_CLIENT_SECRET=\nPORT=8000\nHOST=0.0.0.0\nDOWNLOAD_DIR=descargas\nDEFAULT_AUDIO_BITRATE=192\nDEFAULT_VIDEO_QUALITY=best\n")

# SEGURIDAD / DEPLOY: antes esta linea era override=True.
# Se comento porque override=True hace que el archivo .env le GANE a las
# variables de entorno reales de la plataforma (Render, Docker, etc).
# Como el Dockerfile copia el .env local dentro de la imagen, tus credenciales
# de desarrollo pisaban las que configuras en el dashboard de Render.
# Con override=False manda el entorno real y el .env queda solo como fallback local.
# load_dotenv(dotenv_path=ENV_PATH, override=True)
load_dotenv(dotenv_path=ENV_PATH, override=False)


# =====================================================================
# HELPERS DE SEGURIDAD DE RUTAS
# =====================================================================

def safe_resolve_under(base_dir: Path, relative_path: str) -> Path:
    """
    Resuelve 'relative_path' dentro de 'base_dir' y garantiza que el resultado
    NO se escape de esa carpeta (proteccion contra path traversal).

    Por que existe: antes se validaba con str(target).startswith(str(base_dir)),
    que es inseguro porque compara TEXTO, no jerarquia de carpetas.
    Ejemplo del agujero que tenia:
        base   = C:\\...\\Mp3_py\\descargas
        target = C:\\...\\Mp3_py\\descargas_privado\\algo.mp3
    Ese target "empieza con" la base como string, asi que pasaba el filtro
    aunque este FUERA de la carpeta de descargas.
    Path.is_relative_to() compara por segmentos de ruta y no se deja enganar.

    Returns:
        Path resuelto si es seguro, o None si intenta salirse de base_dir.
    """
    base_resolved = base_dir.resolve()
    try:
        target = (base_resolved / relative_path).resolve()
    except (OSError, ValueError):
        # Rutas invalidas para el SO (caracteres nulos, nombres reservados, etc.)
        return None

    if not target.is_relative_to(base_resolved):
        return None
    return target


def _sanitize_download_dir(raw_value: str) -> str:
    """
    Valida el valor de DOWNLOAD_DIR para que siempre sea una subcarpeta
    del proyecto y nunca una ruta arbitraria del disco.

    Por que existe: DOWNLOAD_DIR se usa como 'BASE_DIR / download_dir'.
    En pathlib, si el segundo operando es ABSOLUTO descarta la base por completo:
        Path("C:/proyecto") / "C:/Windows"  ->  C:/Windows
        Path("/app") / "/etc"               ->  /etc
    Como /api/config permitia escribir este valor, alguien podia apuntar la app
    a cualquier carpeta del sistema y despues listarla o borrarla con
    /api/files y /api/files/delete. Aca lo bloqueamos.
    """
    value = (raw_value or "").strip().strip('"').strip("'")
    if not value:
        return "descargas"

    candidate = Path(value)
    # Rechazar rutas absolutas y unidades de Windows (C:\, D:\, \\servidor\...)
    if candidate.is_absolute() or candidate.drive or value.startswith(("/", "\\")):
        return "descargas"
    # Rechazar cualquier intento de subir de nivel
    if ".." in candidate.parts:
        return "descargas"

    return value


def get_config():
    """Obtiene la configuración actual desde .env y variables del entorno."""
    # Recargar para reflejar cambios en caliente.
    # NOTA: igual que arriba, override=False para no pisar el entorno real.
    # load_dotenv(dotenv_path=ENV_PATH, override=True)
    load_dotenv(dotenv_path=ENV_PATH, override=False)
    env_vals = dotenv_values(ENV_PATH)

    spotify_client_id = os.getenv("SPOTIFY_CLIENT_ID", env_vals.get("SPOTIFY_CLIENT_ID", "")).strip()
    spotify_client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", env_vals.get("SPOTIFY_CLIENT_SECRET", "")).strip()
    # Antes: int(...) directo. Si PORT venia vacio (PORT= en el .env) la app
    # crasheaba al arrancar con ValueError. Ahora cae al default 8000.
    # port = int(os.getenv("PORT", env_vals.get("PORT", "8000")))
    try:
        port = int((os.getenv("PORT") or env_vals.get("PORT") or "8000").strip())
    except ValueError:
        port = 8000

    host = os.getenv("HOST", env_vals.get("HOST", "0.0.0.0"))

    # Antes se usaba el valor crudo. Ahora pasa por el validador para que
    # nunca pueda apuntar fuera de la carpeta del proyecto.
    # download_dir = os.getenv("DOWNLOAD_DIR", env_vals.get("DOWNLOAD_DIR", "descargas")).strip()
    download_dir = _sanitize_download_dir(
        os.getenv("DOWNLOAD_DIR", env_vals.get("DOWNLOAD_DIR", "descargas"))
    )

    default_bitrate = os.getenv("DEFAULT_AUDIO_BITRATE", env_vals.get("DEFAULT_AUDIO_BITRATE", "192")).strip()
    default_video_quality = os.getenv("DEFAULT_VIDEO_QUALITY", env_vals.get("DEFAULT_VIDEO_QUALITY", "best")).strip()

    # Password opcional para proteger toda la app con HTTP Basic Auth.
    # Vacio = sin proteccion (modo local). Ver el middleware en app.py.
    app_username = (os.getenv("APP_USERNAME", env_vals.get("APP_USERNAME", "admin")) or "admin").strip()
    app_password = (os.getenv("APP_PASSWORD", env_vals.get("APP_PASSWORD", "")) or "").strip()

    # Remedios para cuando YouTube bloquea la IP del servidor (tipico en Render
    # y otros hostings). Opcionales; vacios = sin cookies / sin proxy.
    # Ver .env.example y downloader_core._preparar_cookies_ytdlp.
    ytdlp_cookies_file = (os.getenv("YTDLP_COOKIES_FILE", env_vals.get("YTDLP_COOKIES_FILE", "")) or "").strip()
    ytdlp_cookies_b64 = (os.getenv("YTDLP_COOKIES_B64", env_vals.get("YTDLP_COOKIES_B64", "")) or "").strip()
    ytdlp_proxy = (os.getenv("YTDLP_PROXY", env_vals.get("YTDLP_PROXY", "")) or "").strip()

    return {
        "spotify_client_id": spotify_client_id,
        "spotify_client_secret": spotify_client_secret,
        "has_spotify_credentials": bool(spotify_client_id and spotify_client_secret),
        "port": port,
        "host": host,
        "download_dir": download_dir,
        "default_audio_bitrate": default_bitrate,
        "default_video_quality": default_video_quality,
        "app_username": app_username,
        "app_password": app_password,
        "auth_enabled": bool(app_password),
        "ytdlp_cookies_file": ytdlp_cookies_file,
        "ytdlp_cookies_b64": ytdlp_cookies_b64,
        "ytdlp_proxy": ytdlp_proxy,
    }


def update_config(updates: dict):
    """Actualiza variables en el archivo .env de forma segura."""
    for key, value in updates.items():
        key_upper = key.upper()
        if value is not None:
            clean_value = str(value).strip()

            # SEGURIDAD: DOWNLOAD_DIR se valida antes de escribirlo.
            # Sin esto, escribir DOWNLOAD_DIR="C:\" o "/" convertia a
            # /api/files y /api/files/delete en un explorador (y borrador)
            # de todo el disco del servidor.
            if key_upper == "DOWNLOAD_DIR":
                clean_value = _sanitize_download_dir(clean_value)

            set_key(str(ENV_PATH), key_upper, clean_value)
            os.environ[key_upper] = clean_value

    # Recargar dotenv.
    # Aca SI usamos override=True a proposito: acabamos de escribir el .env
    # nosotros mismos y queremos que ese cambio se aplique en caliente.
    load_dotenv(dotenv_path=ENV_PATH, override=True)
    return get_config()


def get_download_path(custom_subfolder: str = None) -> Path:
    """Retorna la ruta absoluta de la carpeta de descargas."""
    cfg = get_config()
    base_download = BASE_DIR / cfg["download_dir"]
    if custom_subfolder:
        clean_sub = "".join(c for c in custom_subfolder if c.isalnum() or c in (" ", "-", "_", ".")).strip()

        # El filtro de arriba permite el caracter ".", asi que un subfolder
        # llamado ".." pasaba el filtro y "descargas/.." terminaba escribiendo
        # en la raiz del proyecto. Un nombre de solo puntos tampoco es valido
        # en Windows, asi que lo descartamos.
        if not clean_sub or set(clean_sub) <= {"."}:
            clean_sub = ""

        # Verificacion final: el destino tiene que quedar dentro de descargas.
        target = safe_resolve_under(base_download, clean_sub) if clean_sub else base_download
        if target is None:
            target = base_download
    else:
        target = base_download

    target.mkdir(parents=True, exist_ok=True)
    return target


def buscar_ffmpeg():
    """
    Localiza la carpeta que contiene ffmpeg.exe y ffprobe.exe
    o retorna None si ya está en PATH o no se encuentra.
    """
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return None

    import glob
    patrones = [
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\ffmpeg-*\bin"),
        os.path.expandvars(r"%ProgramData%\chocolatey\bin"),
        r"C:\ffmpeg\bin",
        r"/usr/bin",
        r"/usr/local/bin",
    ]

    for patron in patrones:
        for carpeta in glob.glob(patron):
            tiene_ffmpeg = os.path.isfile(os.path.join(carpeta, "ffmpeg.exe")) or os.path.isfile(os.path.join(carpeta, "ffmpeg"))
            tiene_ffprobe = os.path.isfile(os.path.join(carpeta, "ffprobe.exe")) or os.path.isfile(os.path.join(carpeta, "ffprobe"))
            if tiene_ffmpeg and tiene_ffprobe:
                return carpeta

    return None


def get_system_diagnostics():
    """Obtiene el diagnóstico del sistema (ffmpeg, yt-dlp, python, espacio)."""
    cfg = get_config()
    try:
        from yt_dlp.version import __version__ as ytdlp_ver
    except Exception:
        ytdlp_ver = "Desconocida"

    ffmpeg_in_path = bool(shutil.which("ffmpeg"))
    ffmpeg_custom = buscar_ffmpeg()
    ffmpeg_ok = ffmpeg_in_path or bool(ffmpeg_custom)

    try:
        total, used, free = shutil.disk_usage(str(BASE_DIR))
        free_gb = round(free / (1024 ** 3), 2)
        total_gb = round(total / (1024 ** 3), 2)
    except Exception:
        free_gb, total_gb = 0, 0

    return {
        "ffmpeg_installed": ffmpeg_ok,
        "ffmpeg_location": "En PATH del sistema" if ffmpeg_in_path else (ffmpeg_custom if ffmpeg_custom else "No encontrado"),
        "ytdlp_version": ytdlp_ver,
        "python_version": platform.python_version(),
        "os_name": platform.system() + " " + platform.release(),
        "disk_free_gb": free_gb,
        "disk_total_gb": total_gb,
        "downloads_dir_exists": (BASE_DIR / cfg["download_dir"]).exists(),
        # Para comprobar desde /api/diagnostics que el servidor (p. ej. Render)
        # tomo las cookies o el proxy anti-bloqueo de YouTube.
        "youtube_cookies_configured": bool(cfg["ytdlp_cookies_file"] or cfg["ytdlp_cookies_b64"]),
        "youtube_proxy_configured": bool(cfg["ytdlp_proxy"]),
    }
