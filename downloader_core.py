# -*- coding: utf-8 -*-
import os
import re
import time
import uuid
import shutil
import zipfile
import asyncio
from pathlib import Path
from typing import Dict, Any, List, Optional
import yt_dlp
import requests

from config import get_config, get_download_path, buscar_ffmpeg, BASE_DIR, safe_resolve_under

# Administrador de tareas en memoria
TASKS: Dict[str, Dict[str, Any]] = {}


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
                    "album": t["album"]["name"]
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
                    "album": album["name"]
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
                tracks_data.append({
                    "index": idx,
                    "id": item.get("id", str(idx)),
                    "title": item["name"],
                    "artists": t_artists,
                    "duration_ms": dur_ms,
                    "duration_str": format_duration(dur_ms // 1000),
                    "album": album_name
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
    carpeta_ffmpeg = buscar_ffmpeg()
    ydl_opts = {
        'quiet': True,
        'no_warnings': True,
        'skip_download': True,
        'noplaylist': True,
    }
    if carpeta_ffmpeg:
        ydl_opts['ffmpeg_location'] = carpeta_ffmpeg

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
        return {"success": False, "error": f"Error al extraer información: {str(e)}"}


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
    carpeta_ffmpeg = buscar_ffmpeg()

    ydl_opts: Dict[str, Any] = {
        'noplaylist': True,
        'quiet': True,
        'no_warnings': False,
        'progress_hooks': [make_ytdlp_progress_hook(task_id)],
    }

    if carpeta_ffmpeg:
        ydl_opts['ffmpeg_location'] = carpeta_ffmpeg

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
            
            # Construir URL relativa para descarga directa en el navegador
            rel_path = os.path.relpath(final_filepath, BASE_DIR / get_config()["download_dir"]).replace("\\", "/")
            download_url = f"/api/files/download/{rel_path}"

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

    except Exception as e:
        err_msg = str(e)
        update_task(task_id, status="error", error=err_msg, title="Error en la descarga")
        return None


def download_single_spotify_track(
    title: str,
    artist: str,
    download_folder: Path,
    bitrate: str = "192"
) -> Optional[str]:
    """Busca una canción en YouTube y la guarda como MP3."""
    safe_title = sanitize_filename(title)
    safe_artist = sanitize_filename(artist)
    query = f"{title} {artist} audio"
    carpeta_ffmpeg = buscar_ffmpeg()

    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': str(bitrate),
        }],
        'outtmpl': str(download_folder / f'{safe_title} - {safe_artist}.%(ext)s'),
        'quiet': True,
        'no_warnings': True,
        'default_search': 'ytsearch1',
        'noplaylist': True,
    }
    if carpeta_ffmpeg:
        ydl_opts['ffmpeg_location'] = carpeta_ffmpeg

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
                return final_filepath
            return None
    except Exception as e:
        print(f"Error descargando pista '{title} - {artist}': {e}")
        return None


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

        file_path = download_single_spotify_track(title, artists, dest_folder, audio_bitrate)
        if file_path and os.path.exists(file_path):
            successful_tracks.append(file_path)

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
        download_url = f"/api/files/download/{rel_path}"

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
        rel_folder = os.path.relpath(str(dest_folder), BASE_DIR / get_config()["download_dir"]).replace("\\", "/")
        update_task(
            task_id,
            status="completed",
            progress=100.0,
            file_path=str(dest_folder),
            file_name=safe_collection,
            download_url=f"/api/files/download/{rel_folder}",
            title=f"¡Descarga completada! {len(successful_tracks)}/{total_tracks} canciones guardadas"
        )
    else:
        update_task(
            task_id,
            status="error",
            error="No se pudo descargar ninguna de las canciones seleccionadas.",
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
                    "stream_url": f"/api/files/stream/{rel_path}",
                    "download_url": f"/api/files/download/{rel_path}",
                })
            except Exception:
                continue

    # Ordenar por fecha de modificación descendente (más nuevos primero)
    files_list.sort(key=lambda x: x["modified_time"], reverse=True)
    return files_list


def delete_file(rel_path: str) -> bool:
    """Elimina un archivo descargado de forma segura."""
    import urllib.parse
    rel_path = urllib.parse.unquote(rel_path)
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
