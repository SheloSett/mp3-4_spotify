# -*- coding: utf-8 -*-
import os
import re
import time
import uuid
import base64
import shutil
import zipfile
import asyncio
import tempfile
import urllib.parse
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
import yt_dlp
import requests

from config import get_config, get_download_path, buscar_ffmpeg, BASE_DIR, safe_resolve_under

# Administrador de tareas en memoria
TASKS: Dict[str, Dict[str, Any]] = {}

# Cuanto tiempo se conservan las tareas ya terminadas antes de descartarlas.
# Sin esto el diccionario TASKS crecia para siempre: cada descarga dejaba su
# registro en memoria y nunca se liberaba (fuga de memoria en usos largos).
TASK_RETENTION_SECONDS = 3600  # 1 hora


class DescargaCancelada(Exception):
    """Excepcion propia para abortar una descarga de yt-dlp desde el hook."""
    pass


def _limpiar_tareas_viejas():
    """Descarta tareas terminadas hace mas de TASK_RETENTION_SECONDS."""
    ahora = time.time()
    terminadas = ("completed", "error", "cancelled")
    vencidas = [
        tid for tid, t in TASKS.items()
        if t.get("status") in terminadas and (ahora - t.get("updated_at", ahora)) > TASK_RETENTION_SECONDS
    ]
    for tid in vencidas:
        TASKS.pop(tid, None)


def format_duration(seconds: Optional[int]) -> str:
    """Convierte segundos a formato MM:SS o HH:MM:SS."""
    if not seconds or seconds <= 0:
        return "00:00"
    seconds = int(seconds)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def sanitize_filename(name: str) -> str:
    """Limpia caracteres no permitidos en nombres de archivo."""
    if not name:
        return "archivo"
    # Eliminar caracteres prohibidos en Windows y Unix
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', name).strip()
    return cleaned if cleaned else "audio"


def create_task(task_type: str, title: str = "Iniciando descarga...") -> str:
    """Crea un registro de tarea para seguimiento en tiempo real."""
    # Aprovechamos cada tarea nueva para purgar las viejas ya terminadas.
    _limpiar_tareas_viejas()

    task_id = str(uuid.uuid4())
    TASKS[task_id] = {
        "id": task_id,
        "type": task_type,
        "title": title,
        "status": "queued",  # queued, downloading, converting, completed, error, cancelled
        "progress": 0.0,
        "speed": "--",
        "eta": "--",
        "current_item": "",
        "completed_items": 0,
        "total_items": 1,
        "file_path": None,
        "file_name": None,
        "file_size": None,
        "error": None,
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    return task_id


def update_task(task_id: str, **kwargs):
    """Actualiza los campos de una tarea."""
    if task_id in TASKS:
        TASKS[task_id].update(kwargs)
        TASKS[task_id]["updated_at"] = time.time()


def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    """Obtiene el estado de una tarea."""
    return TASKS.get(task_id)


# =====================================================================
# OPCIONES COMUNES DE YT-DLP (ffmpeg, cookies, proxy, avisos)
# =====================================================================

class _LoggerYtdlp:
    """
    Logger minimo para yt-dlp que GUARDA los avisos en vez de descartarlos.

    Por que existe: las tres llamadas a yt-dlp usaban quiet/no_warnings, asi
    que cuando YouTube rechazaba la peticion el usuario veia solo el error
    final generico ("Failed to extract any player response... report this
    issue on GitHub") y nunca el aviso previo que explica la causa real
    ("Sign in to confirm you're not a bot", "HTTP Error 429", etc.).
    Con los avisos a mano se arma un mensaje util en _explicar_error_ytdlp.
    """

    def __init__(self):
        self.avisos: List[str] = []

    def debug(self, msg):
        # yt-dlp manda por aca tambien el progreso de descarga; no interesa.
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        texto = str(msg).strip()
        if texto and texto not in self.avisos:
            self.avisos.append(texto)
            # Tambien al log del servidor, que es lo unico visible en Render.
            print(f"[yt-dlp] {texto}")

    def error(self, msg):
        # El error ademas llega como excepcion; con loguearlo alcanza.
        print(f"[yt-dlp] {msg}")


# Cache de la copia escribible de las cookies (ver _preparar_cookies_ytdlp).
_COOKIES_CACHE: Dict[str, Any] = {"clave": None, "ruta": None}


def _preparar_cookies_ytdlp(cfg: Dict[str, Any]) -> Optional[str]:
    """
    Devuelve la ruta a un cookies.txt (formato Netscape) listo para yt-dlp,
    o None si no hay cookies configuradas.

    Por que existe: cuando la app corre en un hosting (Render, etc.) YouTube
    bloquea la IP del servidor y yt-dlp falla con "Failed to extract any
    player response" o "Sign in to confirm you're not a bot", aunque en la PC
    del usuario funcione perfecto. La solucion documentada por yt-dlp es
    pasarle las cookies de una sesion de YouTube iniciada en un navegador.

    Se aceptan dos fuentes (ver .env.example):
      - YTDLP_COOKIES_FILE: ruta a un cookies.txt. En Render se sube como
        "Secret File" y queda montado en /etc/secrets/<nombre>.
      - YTDLP_COOKIES_B64: el contenido del cookies.txt en base64, para
        hostings que no tienen archivos secretos.

    yt-dlp REESCRIBE el archivo de cookies al terminar (guarda las cookies
    renovadas) y /etc/secrets es de solo lectura, asi que siempre se trabaja
    sobre una copia en la carpeta temporal del sistema.
    """
    origen_archivo = (cfg.get("ytdlp_cookies_file") or "").strip()
    origen_b64 = (cfg.get("ytdlp_cookies_b64") or "").strip()

    if origen_archivo:
        src = Path(origen_archivo)
        if not src.is_file():
            print(f"[cookies] YTDLP_COOKIES_FILE apunta a un archivo inexistente: {src}")
            return None
        try:
            contenido = src.read_bytes()
            clave = ("file", str(src), src.stat().st_mtime)
        except OSError as e:
            print(f"[cookies] No se pudo leer {src}: {e}")
            return None
    elif origen_b64:
        try:
            contenido = base64.b64decode("".join(origen_b64.split()))
        except Exception as e:
            print(f"[cookies] YTDLP_COOKIES_B64 no es base64 valido: {e}")
            return None
        clave = ("b64", len(origen_b64), hash(origen_b64))
    else:
        return None

    # Reutilizar la copia si el origen no cambio: yt-dlp va guardando ahi las
    # cookies renovadas y pisarlas en cada llamada las haria perder.
    ruta_cache = _COOKIES_CACHE["ruta"]
    if _COOKIES_CACHE["clave"] == clave and ruta_cache and os.path.isfile(ruta_cache):
        return ruta_cache

    try:
        destino_dir = Path(tempfile.gettempdir()) / "mp3_spotify_downloader"
        destino_dir.mkdir(parents=True, exist_ok=True)
        destino = destino_dir / "youtube_cookies.txt"
        destino.write_bytes(contenido)
    except OSError as e:
        print(f"[cookies] No se pudo preparar la copia escribible de las cookies: {e}")
        return None

    _COOKIES_CACHE["clave"] = clave
    _COOKIES_CACHE["ruta"] = str(destino)
    print(f"[cookies] Cookies de YouTube cargadas desde {'YTDLP_COOKIES_FILE' if origen_archivo else 'YTDLP_COOKIES_B64'}.")
    return str(destino)


def _opciones_base_ytdlp(logger: Optional[_LoggerYtdlp] = None) -> Dict[str, Any]:
    """
    Opciones compartidas por TODAS las llamadas a yt-dlp de la app.

    Antes cada funcion armaba su propio diccionario y repetia la logica de
    ffmpeg_location. Ahora ademas se suman cookies y proxy, que son la
    solucion para cuando YouTube bloquea la IP de un servidor (ver
    _preparar_cookies_ytdlp y _explicar_error_ytdlp).
    """
    cfg = get_config()
    opts: Dict[str, Any] = {
        'quiet': True,
        # Los avisos NO se silencian: van al logger, que los guarda para
        # explicar el error si la extraccion termina fallando.
        'no_warnings': False,
    }
    if logger is not None:
        opts['logger'] = logger

    carpeta_ffmpeg = buscar_ffmpeg()
    if carpeta_ffmpeg:
        opts['ffmpeg_location'] = carpeta_ffmpeg

    cookies = _preparar_cookies_ytdlp(cfg)
    if cookies:
        opts['cookiefile'] = cookies

    if cfg.get("ytdlp_proxy"):
        opts['proxy'] = cfg["ytdlp_proxy"]

    return opts


# Fragmentos (en minusculas) que delatan que YouTube rechazo al servidor.
_PISTAS_BLOQUEO_YOUTUBE = (
    "failed to extract any player response",
    "all player responses are invalid",
    "confirm you're not a bot",
    "confirm you’re not a bot",
    "sign in to confirm",
    "ip is likely being blocked",
    "http error 429",
)


def _explicar_error_ytdlp(e: Exception, avisos: Optional[List[str]] = None) -> str:
    """
    Convierte una excepcion de yt-dlp en un mensaje que le sirva al usuario.

    yt-dlp termina con un texto generico que pide "reportar el issue en
    GitHub", pero la causa real casi siempre esta en un aviso anterior. Y si
    la app corre en un hosting (Render) el motivo mas comun es que YouTube
    bloquea las IPs de datacenter: en la PC anda y en el servidor no. Para
    ese caso se explica que hacer (cookies o proxy) en vez de mandar a GitHub.
    """
    avisos = avisos or []
    mensaje = str(e).strip()
    # El pedido de reportar en GitHub no es algo que el usuario pueda resolver
    # y tapa la causa, asi que se recorta.
    mensaje = re.sub(
        r";?\s*please report this issue on\s+https://github\.com/yt-dlp/yt-dlp/issues.*$",
        "", mensaje, flags=re.IGNORECASE | re.DOTALL,
    ).strip()

    texto_completo = (mensaje + " " + " ".join(avisos)).lower()
    if any(p in texto_completo for p in _PISTAS_BLOQUEO_YOUTUBE):
        cfg = get_config()
        if cfg.get("ytdlp_cookies_file") or cfg.get("ytdlp_cookies_b64"):
            consejo = (
                "YouTube rechazó la petición aunque hay cookies cargadas: probablemente "
                "vencieron. Exportalas de nuevo desde el navegador y, si sigue pasando, "
                "sumá un proxy residencial en YTDLP_PROXY."
            )
        else:
            consejo = (
                "YouTube bloqueó la IP del servidor; es normal en hostings como Render "
                "aunque en tu PC funcione. Solución: cargar las cookies de una sesión de "
                "YouTube (YTDLP_COOKIES_FILE o YTDLP_COOKIES_B64) o salir por un proxy "
                "residencial (YTDLP_PROXY). Ver .env.example."
            )
        detalle = avisos[-1] if avisos else mensaje
        return f"{consejo} Detalle: {detalle[:220]}"

    if avisos:
        return f"{mensaje} | Aviso de yt-dlp: {avisos[-1][:220]}"
    return mensaje


# =====================================================================
# METADATOS Y ANÁLISIS DE ENLACES
# =====================================================================

def detect_url_type(url: str) -> str:
    """Detecta si la URL es de Spotify, YouTube o genérica."""
    url_lower = url.lower().strip()
    if "spotify.com" in url_lower or "spotify:" in url_lower:
        if "playlist" in url_lower:
            return "spotify_playlist"
        elif "album" in url_lower:
            return "spotify_album"
        elif "track" in url_lower:
            return "spotify_track"
        return "spotify"
    elif "youtube.com" in url_lower or "youtu.be" in url_lower:
        return "youtube"
    else:
        return "generic_video"


def get_spotify_client():
    """Inicializa cliente de Spotipy usando credenciales de config / .env."""
    cfg = get_config()
    client_id = cfg.get("spotify_client_id")
    client_secret = cfg.get("spotify_client_secret")

    if not client_id or not client_secret:
        return None

    try:
        import spotipy
        from spotipy.oauth2 import SpotifyClientCredentials
        auth_manager = SpotifyClientCredentials(
            client_id=client_id,
            client_secret=client_secret
        )
        return spotipy.Spotify(auth_manager=auth_manager)
    except Exception as e:
        print(f"Error al inicializar Spotipy: {e}")
        return None


def get_spotify_info(url: str) -> Dict[str, Any]:
    """Extrae metadatos de una playlist, álbum o canción de Spotify."""
    sp = get_spotify_client()
    if not sp:
        return {
            "success": False,
            "error": "Credenciales de Spotify no configuradas. Configura tu SPOTIFY_CLIENT_ID y SPOTIFY_CLIENT_SECRET en Ajustes o en el archivo .env.",
            "needs_credentials": True
        }

    try:
        url_type = detect_url_type(url)

        # 1. Pistas individuales
        if "track" in url_type:
            match = re.search(r'track/([a-zA-Z0-9]+)', url)
            if not match:
                return {"success": False, "error": "URL de canción de Spotify inválida."}
            track_id = match.group(1)
            t = sp.track(track_id)

            artists = ", ".join([a["name"] for a in t.get("artists", [])])
            image = t["album"]["images"][0]["url"] if t["album"].get("images") else None
            duration_ms = t.get("duration_ms", 0)

            return {
                "success": True,
                "type": "track",
                "id": track_id,
                "title": t["name"],
                "subtitle": artists,
                "cover_image": image,
                "album_name": t["album"]["name"],
                "release_date": t["album"].get("release_date"),
                "total_tracks": 1,
                "tracks": [{
                    "index": 1,
                    "id": track_id,
                    "title": t["name"],
                    "artists": artists,
                    "duration_ms": duration_ms,
                    "duration_str": format_duration(duration_ms // 1000),
                    "album": t["album"]["name"],
                    # Se usa para incrustar la portada dentro del MP3.
                    "cover_url": image
                }]
            }

        # 2. Álbumes
        elif "album" in url_type:
            match = re.search(r'album/([a-zA-Z0-9]+)', url)
            if not match:
                return {"success": False, "error": "URL de álbum de Spotify inválida."}
            album_id = match.group(1)
            album = sp.album(album_id)

            artists = ", ".join([a["name"] for a in album.get("artists", [])])
            image = album["images"][0]["url"] if album.get("images") else None
            
            tracks_data = []
            results = album.get("tracks", {})
            items = results.get("items", [])
            
            for idx, item in enumerate(items, 1):
                t_artists = ", ".join([a["name"] for a in item.get("artists", [])])
                dur_ms = item.get("duration_ms", 0)
                tracks_data.append({
                    "index": idx,
                    "id": item.get("id", str(idx)),
                    "title": item["name"],
                    "artists": t_artists,
                    "duration_ms": dur_ms,
                    "duration_str": format_duration(dur_ms // 1000),
                    "album": album["name"],
                    # Todas las pistas de un album comparten la misma portada.
                    "cover_url": image
                })

            return {
                "success": True,
                "type": "album",
                "id": album_id,
                "title": album["name"],
                "subtitle": f"Álbum de {artists}",
                "cover_image": image,
                "release_date": album.get("release_date"),
                "total_tracks": len(tracks_data),
                "tracks": tracks_data
            }

        # 3. Playlists
        else:
            match = re.search(r'playlist/([a-zA-Z0-9]+)', url)
            if not match:
                return {"success": False, "error": "URL de playlist de Spotify inválida."}
            playlist_id = match.group(1)
            pl = sp.playlist(playlist_id)

            image = pl["images"][0]["url"] if pl.get("images") else None
            owner = pl.get("owner", {}).get("display_name", "Desconocido")

            tracks_data = []
            results = sp.playlist_tracks(playlist_id)
            items = results.get("items", [])

            while results.get("next"):
                results = sp.next(results)
                items.extend(results.get("items", []))

            for idx, entry in enumerate(items, 1):
                item = entry.get("track")
                if not item or not item.get("name"):
                    continue
                t_artists = ", ".join([a["name"] for a in item.get("artists", [])])
                dur_ms = item.get("duration_ms", 0)
                album_name = item.get("album", {}).get("name", "")

                # En una playlist cada cancion puede venir de un album distinto,
                # asi que la portada se toma de cada pista y no de la coleccion.
                album_images = item.get("album", {}).get("images") or []
                track_cover = album_images[0]["url"] if album_images else image

                tracks_data.append({
                    "index": idx,
                    "id": item.get("id", str(idx)),
                    "title": item["name"],
                    "artists": t_artists,
                    "duration_ms": dur_ms,
                    "duration_str": format_duration(dur_ms // 1000),
                    "album": album_name,
                    "cover_url": track_cover
                })

            return {
                "success": True,
                "type": "playlist",
                "id": playlist_id,
                "title": pl["name"],
                "subtitle": f"Playlist de {owner}",
                "cover_image": image,
                "description": pl.get("description", ""),
                "total_tracks": len(tracks_data),
                "tracks": tracks_data
            }

    except Exception as e:
        return {"success": False, "error": f"Error al consultar Spotify: {str(e)}"}


def get_youtube_info(url: str) -> Dict[str, Any]:
    """Extrae información y formatos disponibles de YouTube o URL genérica."""
    logger = _LoggerYtdlp()
    ydl_opts = _opciones_base_ytdlp(logger)
    ydl_opts.update({
        'skip_download': True,
        'noplaylist': True,
    })

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if 'entries' in info and info['entries']:
                info = info['entries'][0]

            title = info.get("title", "Video")
            duration = info.get("duration", 0)
            thumbnail = info.get("thumbnail")
            uploader = info.get("uploader") or info.get("channel", "Desconocido")
            view_count = info.get("view_count", 0)

            # Formatos de video y audio disponibles
            return {
                "success": True,
                "type": "youtube",
                "title": title,
                "subtitle": uploader,
                "duration": duration,
                "duration_str": format_duration(duration),
                "cover_image": thumbnail,
                "view_count": f"{view_count:,}" if view_count else None,
                "url": url,
                "audio_formats": [
                    {"label": "MP3 Ultra HD (320 kbps)", "bitrate": "320", "ext": "mp3"},
                    {"label": "MP3 Alta Calidad (256 kbps)", "bitrate": "256", "ext": "mp3"},
                    {"label": "MP3 Estándar (192 kbps)", "bitrate": "192", "ext": "mp3"},
                    {"label": "MP3 Ligero (128 kbps)", "bitrate": "128", "ext": "mp3"},
                ],
                "video_formats": [
                    {"label": "MP4 Mejor Calidad (1080p/Auto)", "quality": "best", "ext": "mp4"},
                    {"label": "MP4 HD (720p)", "quality": "720", "ext": "mp4"},
                    {"label": "MP4 SD (480p)", "quality": "480", "ext": "mp4"},
                ]
            }
    except Exception as e:
        return {"success": False, "error": f"Error al extraer información: {_explicar_error_ytdlp(e, logger.avisos)}"}


def analyze_url(url: str) -> Dict[str, Any]:
    """Analiza cualquier URL y retorna la información correspondiente."""
    url_type = detect_url_type(url)
    if "spotify" in url_type:
        return get_spotify_info(url)
    else:
        return get_youtube_info(url)


# =====================================================================
# MOTORES DE DESCARGA
# =====================================================================

def make_ytdlp_progress_hook(task_id: str):
    """Crea un callback para actualizar el progreso de yt-dlp en tiempo real."""
    def hook(d):
        if task_id not in TASKS:
            return

        # CANCELACION REAL:
        # Antes el boton "Cancelar" solo cambiaba el estado de la tarea, pero
        # nadie lo miraba durante una descarga de YouTube: el archivo se seguia
        # bajando entero igual. yt-dlp llama a este hook constantemente, asi que
        # es el lugar correcto para abortar. Al lanzar la excepcion, yt-dlp corta
        # la descarga y la atrapamos en download_youtube_media.
        if TASKS[task_id].get("status") == "cancelled":
            raise DescargaCancelada("Descarga cancelada por el usuario.")

        status = d.get('status')
        if status == 'downloading':
            total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            downloaded = d.get('downloaded_bytes', 0)
            
            progress = 0.0
            if total_bytes > 0:
                progress = round((downloaded / total_bytes) * 100, 1)

            speed_val = d.get('speed')
            speed_str = "--"
            if speed_val:
                speed_mb = speed_val / (1024 * 1024)
                speed_str = f"{speed_mb:.1f} MB/s"

            eta_val = d.get('eta')
            eta_str = f"{eta_val}s" if eta_val is not None else "--"

            update_task(
                task_id,
                status="downloading",
                progress=progress,
                speed=speed_str,
                eta=eta_str
            )
        elif status == 'finished':
            update_task(
                task_id,
                status="converting",
                progress=99.0,
                speed="Procesando...",
                eta="Finalizando..."
            )
    return hook


def download_youtube_media(
    task_id: str,
    url: str,
    media_type: str = "audio",  # 'audio' o 'video'
    audio_bitrate: str = "192",
    video_quality: str = "best",
    custom_subfolder: Optional[str] = None
) -> Optional[str]:
    """
    Descarga video o audio de YouTube en la calidad especificada.
    """
    dest_folder = get_download_path(custom_subfolder)
    logger = _LoggerYtdlp()

    ydl_opts: Dict[str, Any] = _opciones_base_ytdlp(logger)
    ydl_opts.update({
        'noplaylist': True,
        'progress_hooks': [make_ytdlp_progress_hook(task_id)],
    })

    if media_type == "audio":
        ydl_opts['format'] = 'bestaudio/best'
        ydl_opts['outtmpl'] = str(dest_folder / '%(title)s.%(ext)s')
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': str(audio_bitrate),
        }]
    else:
        # Modo Video MP4
        if video_quality == "720":
            ydl_opts['format'] = 'bestvideo[height<=720]+bestaudio/best[height<=720]/best'
        elif video_quality == "480":
            ydl_opts['format'] = 'bestvideo[height<=480]+bestaudio/best[height<=480]/best'
        else:
            ydl_opts['format'] = 'bestvideo+bestaudio/best'

        ydl_opts['outtmpl'] = str(dest_folder / '%(title)s.%(ext)s')

        # merge_output_format le dice a yt-dlp que junte video+audio
        # directamente en un contenedor MP4 (remux: solo copia los streams,
        # tarda segundos). Sin esta linea yt-dlp mezclaba en MKV y despues el
        # postprocesador FFmpegVideoConvertor RE-CODIFICABA todo el video a
        # MP4, que en un video largo son varios minutos con la barra clavada
        # en 99%. El convertor queda como respaldo por si el remux no aplica.
        ydl_opts['merge_output_format'] = 'mp4'
        ydl_opts['postprocessors'] = [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4',
        }]

    update_task(task_id, status="downloading", progress=5.0, title="Iniciando descarga...")

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if 'entries' in info and info['entries']:
                info = info['entries'][0]

            final_filepath = None
            descargas = info.get('requested_downloads')
            if descargas:
                final_filepath = descargas[0].get('filepath')

            if not final_filepath:
                ext = "mp3" if media_type == "audio" else "mp4"
                final_filepath = os.path.splitext(ydl.prepare_filename(info))[0] + f".{ext}"

            file_size_mb = 0.0
            if os.path.isfile(final_filepath):
                file_size_mb = round(os.path.getsize(final_filepath) / (1024 * 1024), 2)

            file_name = os.path.basename(final_filepath)
            
            # Construir URL relativa para descarga directa en el navegador.
            # quote() es necesario porque los titulos de YouTube traen de todo:
            # un "#" cortaba la URL en el navegador (lo toma como ancla) y un
            # "?" convertia el resto del nombre en query string, asi que el
            # link de descarga fallaba con 404. safe="/" preserva las carpetas.
            rel_path = os.path.relpath(final_filepath, BASE_DIR / get_config()["download_dir"]).replace("\\", "/")
            download_url = f"/api/files/download/{urllib.parse.quote(rel_path, safe='/')}"

            update_task(
                task_id,
                status="completed",
                progress=100.0,
                file_path=final_filepath,
                file_name=file_name,
                file_size=f"{file_size_mb} MB",
                download_url=download_url,
                title=f"Descarga completa: {file_name}"
            )
            return final_filepath

    except DescargaCancelada:
        # Cancelacion pedida por el usuario: no es un error, no mostrar alerta roja.
        update_task(
            task_id,
            status="cancelled",
            progress=0.0,
            speed="--",
            eta="--",
            title="Descarga cancelada por el usuario."
        )
        return None

    except Exception as e:
        err_msg = _explicar_error_ytdlp(e, logger.avisos)
        update_task(task_id, status="error", error=err_msg, title="Error en la descarga")
        return None


def embed_mp3_metadata(
    filepath: str,
    title: str,
    artist: str,
    album: str = "",
    cover_url: Optional[str] = None
) -> bool:
    """
    Escribe las etiquetas ID3 (titulo, artista, album) y la caratula dentro
    del MP3 descargado.

    Por que existe: mutagen estaba declarado en requirements.txt pero no se
    usaba en ningun lado. Resultado: los MP3 bajados de una playlist quedaban
    "pelados" - sin titulo, artista, album ni portada. En el celular o en
    cualquier reproductor aparecian como "Pista desconocida" y sin orden.
    Como los datos reales ya los tenemos de la API de Spotify, los escribimos
    nosotros en vez de confiar en el titulo del video de YouTube.
    """
    try:
        from mutagen.mp3 import MP3
        from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC, ID3NoHeaderError

        try:
            audio = ID3(filepath)
        except ID3NoHeaderError:
            # El MP3 recien convertido puede no tener contenedor ID3 todavia.
            audio = ID3()

        audio.add(TIT2(encoding=3, text=title))
        audio.add(TPE1(encoding=3, text=artist))
        if album:
            audio.add(TALB(encoding=3, text=album))

        # Caratula: se baja la imagen de Spotify y se incrusta en el archivo.
        if cover_url:
            try:
                resp = requests.get(cover_url, timeout=15)
                if resp.status_code == 200:
                    audio.add(APIC(
                        encoding=3,
                        mime="image/jpeg",
                        type=3,          # 3 = portada frontal
                        desc="Cover",
                        data=resp.content
                    ))
            except Exception as e:
                # Que falle la portada no debe arruinar el resto de las etiquetas.
                print(f"No se pudo incrustar la caratula de '{title}': {e}")

        audio.save(filepath, v2_version=3)
        return True

    except Exception as e:
        print(f"No se pudieron escribir los metadatos de '{title}': {e}")
        return False


def download_single_spotify_track(
    title: str,
    artist: str,
    download_folder: Path,
    bitrate: str = "192",
    album: str = "",
    cover_url: Optional[str] = None
) -> Tuple[Optional[str], Optional[str]]:
    """
    Busca una canción en YouTube y la guarda como MP3.

    Devuelve (ruta_del_mp3, motivo_del_error). Antes devolvia solo la ruta y
    el motivo se perdia en un print; si fallaban todas las pistas el usuario
    veia "no se pudo descargar ninguna" sin saber por que.
    """
    safe_title = sanitize_filename(title)
    safe_artist = sanitize_filename(artist)
    query = f"{title} {artist} audio"
    logger = _LoggerYtdlp()

    ydl_opts = _opciones_base_ytdlp(logger)
    ydl_opts.update({
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': str(bitrate),
        }],
        'outtmpl': str(download_folder / f'{safe_title} - {safe_artist}.%(ext)s'),
        'default_search': 'ytsearch1',
        'noplaylist': True,
    })

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=True)
            if 'entries' in info and info['entries']:
                info = info['entries'][0]

            final_filepath = None
            descargas = info.get('requested_downloads')
            if descargas:
                final_filepath = descargas[0].get('filepath')

            if not final_filepath or not os.path.exists(final_filepath):
                final_filepath = str(download_folder / f'{safe_title} - {safe_artist}.mp3')

            if os.path.exists(final_filepath):
                # Escribir las etiquetas ID3 con los datos reales de Spotify,
                # que son mucho mas confiables que el titulo del video de YouTube.
                embed_mp3_metadata(final_filepath, title, artist, album, cover_url)
                return final_filepath, None
            return None, "yt-dlp no generó el archivo MP3 (¿falta ffmpeg en el servidor?)."
    except Exception as e:
        motivo = _explicar_error_ytdlp(e, logger.avisos)
        print(f"Error descargando pista '{title} - {artist}': {motivo}")
        return None, motivo


def download_spotify_batch(
    task_id: str,
    tracks: List[Dict[str, Any]],
    collection_name: str,
    as_zip: bool = True,
    audio_bitrate: str = "192"
):
    """
    Descarga una lista de canciones de Spotify, actualizando el progreso
    y opcionalmente creando un archivo ZIP con toda la colección.
    """
    safe_collection = sanitize_filename(collection_name)
    dest_folder = get_download_path(safe_collection)

    total_tracks = len(tracks)
    successful_tracks: List[str] = []
    ultimo_error: Optional[str] = None

    update_task(
        task_id,
        status="downloading",
        progress=0.0,
        completed_items=0,
        total_items=total_tracks,
        title=f"Descargando {safe_collection} (0/{total_tracks})"
    )

    for index, track in enumerate(tracks, 1):
        # Verificar si la tarea fue cancelada
        current = get_task(task_id)
        if current and current.get("status") == "cancelled":
            return

        title = track.get("title", "Canción")
        artists = track.get("artists", "")
        item_label = f"{title} - {artists}"

        update_task(
            task_id,
            current_item=item_label,
            status="downloading",
            progress=round(((index - 1) / total_tracks) * 100, 1),
            title=f"[{index}/{total_tracks}] {item_label}"
        )

        file_path, motivo_error = download_single_spotify_track(
            title,
            artists,
            dest_folder,
            audio_bitrate,
            album=track.get("album", ""),
            cover_url=track.get("cover_url")
        )
        if file_path and os.path.exists(file_path):
            successful_tracks.append(file_path)
        elif motivo_error:
            ultimo_error = motivo_error

        update_task(
            task_id,
            completed_items=len(successful_tracks),
            progress=round((index / total_tracks) * 100, 1)
        )

    # Si se solicitó empaquetar en ZIP
    if as_zip and successful_tracks:
        update_task(task_id, status="converting", current_item="Comprimiendo archivo ZIP...")
        zip_filename = f"{safe_collection}.zip"
        zip_path = dest_folder.parent / zip_filename

        with zipfile.ZipFile(str(zip_path), 'w', zipfile.ZIP_DEFLATED) as zipf:
            for fpath in successful_tracks:
                zipf.write(fpath, arcname=os.path.basename(fpath))

        file_size_mb = round(os.path.getsize(str(zip_path)) / (1024 * 1024), 2)
        rel_path = os.path.relpath(str(zip_path), BASE_DIR / get_config()["download_dir"]).replace("\\", "/")
        # quote(): los nombres de playlist con "#", "?" o "&" rompian el link.
        download_url = f"/api/files/download/{urllib.parse.quote(rel_path, safe='/')}"

        update_task(
            task_id,
            status="completed",
            progress=100.0,
            file_path=str(zip_path),
            file_name=zip_filename,
            file_size=f"{file_size_mb} MB",
            download_url=download_url,
            title=f"¡Colección completada! {len(successful_tracks)}/{total_tracks} pistas guardadas"
        )
    elif successful_tracks:
        # Sin ZIP no hay un unico archivo que ofrecer. Antes download_url
        # apuntaba a la CARPETA de la coleccion, pero el endpoint de descarga
        # directa solo sirve archivos, asi que el boton "Guardar" del frontend
        # devolvia 404. Se deja en None y el frontend oculta ese boton: los MP3
        # quedan listados uno por uno en la pestaña Biblioteca.
        update_task(
            task_id,
            status="completed",
            progress=100.0,
            file_path=str(dest_folder),
            file_name=safe_collection,
            download_url=None,
            title=f"¡Descarga completada! {len(successful_tracks)}/{total_tracks} canciones guardadas en la Biblioteca"
        )
    else:
        # Se agrega el motivo del ultimo fallo. En un hosting la causa casi
        # siempre es el bloqueo de YouTube a la IP del servidor, y sin verlo
        # el usuario no tenia forma de saber que hacer.
        detalle = f" Último error: {ultimo_error}" if ultimo_error else ""
        update_task(
            task_id,
            status="error",
            error="No se pudo descargar ninguna de las canciones seleccionadas." + detalle,
            title="Error en la descarga de la colección"
        )


# =====================================================================
# GESTIÓN DE ARCHIVOS Y BIBLIOTECA
# =====================================================================

def list_downloaded_files() -> List[Dict[str, Any]]:
    """Lista todos los archivos descargados disponibles en la biblioteca."""
    cfg = get_config()
    base_dir = BASE_DIR / cfg["download_dir"]
    if not base_dir.exists():
        return []

    files_list = []
    allowed_exts = {".mp3", ".mp4", ".m4a", ".webm", ".wav", ".zip"}

    for item in base_dir.rglob("*"):
        if item.is_file() and item.suffix.lower() in allowed_exts:
            try:
                stats = item.stat()
                size_mb = round(stats.st_size / (1024 * 1024), 2)
                rel_path = item.relative_to(base_dir).as_posix()
                subfolder = item.parent.relative_to(base_dir).as_posix() if item.parent != base_dir else ""

                files_list.append({
                    "name": item.name,
                    "rel_path": rel_path,
                    "subfolder": subfolder,
                    "extension": item.suffix.lower().replace(".", ""),
                    "size_mb": size_mb,
                    "size_str": f"{size_mb} MB" if size_mb >= 1 else f"{round(stats.st_size / 1024, 1)} KB",
                    "modified_time": stats.st_mtime,
                    # Antes se insertaba rel_path crudo. Un archivo llamado
                    # "Tema #1.mp3" generaba un link que el navegador cortaba
                    # en el "#", y la descarga daba 404.
                    "stream_url": f"/api/files/stream/{urllib.parse.quote(rel_path, safe='/')}",
                    "download_url": f"/api/files/download/{urllib.parse.quote(rel_path, safe='/')}",
                })
            except Exception:
                continue

    # Ordenar por fecha de modificación descendente (más nuevos primero)
    files_list.sort(key=lambda x: x["modified_time"], reverse=True)
    return files_list


def delete_file(rel_path: str) -> bool:
    """
    Elimina un archivo descargado de forma segura.

    Recibe SIEMPRE una ruta ya decodificada: desde el JSON del frontend viene
    tal cual, y en el endpoint DELETE la decodifica Starlette. Por eso ya no
    se llama a unquote() aca.
    """
    # Antes: rel_path = urllib.parse.unquote(rel_path)
    # Se comento porque decodificaba de mas. Un archivo llamado "abc%41.mp3"
    # se convertia en "abcA.mp3" y el borrado apuntaba al archivo equivocado
    # (o a ninguno). Ademas era la segunda decodificacion en el camino DELETE.
    # import urllib.parse
    # rel_path = urllib.parse.unquote(rel_path)
    cfg = get_config()
    base_dir = (BASE_DIR / cfg["download_dir"]).resolve()

    # Antes: comparacion por texto con startswith(), que es vulnerable a
    # path traversal (ver explicacion completa en config.safe_resolve_under).
    # Era el bug mas grave de los tres porque esta funcion, si el target es
    # una carpeta, ejecuta shutil.rmtree() mas abajo.
    # target = (base_dir / rel_path).resolve()
    # if not str(target).startswith(str(base_dir)):
    #     return False
    target = safe_resolve_under(base_dir, rel_path)
    if target is None:
        print(f"[SEGURIDAD] Intento de borrado fuera de la carpeta de descargas: {rel_path}")
        return False

    # Proteccion extra: nunca permitir que se borre la carpeta base entera.
    if target == base_dir:
        print("[SEGURIDAD] Intento de borrar la carpeta raiz de descargas. Bloqueado.")
        return False

    if target.is_file():
        try:
            target.unlink()
            return True
        except Exception as e:
            print(f"Error al eliminar archivo {target}: {e}")
            return False
    elif target.is_dir():
        try:
            shutil.rmtree(target, ignore_errors=True)
            return True
        except Exception as e:
            print(f"Error al eliminar carpeta {target}: {e}")
            return False
    return False


def open_downloads_folder():
    """Abre la carpeta de descargas en el explorador de archivos de Windows o del SO."""
    cfg = get_config()
    base_dir = BASE_DIR / cfg["download_dir"]
    base_dir.mkdir(parents=True, exist_ok=True)

    try:
        import platform
        system = platform.system()
        if system == "Windows":
            os.startfile(str(base_dir))
            return True
        elif system == "Darwin":
            os.system(f'open "{base_dir}"')
            return True
        else:
            os.system(f'xdg-open "{base_dir}"')
            return True
    except Exception as e:
        print(f"No se pudo abrir la carpeta en el explorador: {e}")
        return False
