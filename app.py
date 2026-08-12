# -*- coding: utf-8 -*-
import os
import json
import secrets
import asyncio
from pathlib import Path
from typing import Dict, Any, List, Optional
from pydantic import BaseModel

from fastapi import FastAPI, HTTPException, BackgroundTasks, Request, Depends
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import config
import downloader_core
import urllib.parse


# =====================================================================
# AUTENTICACIÓN (HTTP Basic opcional)
# =====================================================================
# Por que existe: la app NO tenia ningun tipo de autenticacion. Corriendo en
# tu PC no importa, pero al publicarla en Render cualquiera con la URL podia
# ver, descargar y BORRAR tus archivos, y reescribir tu configuracion.
#
# Como funciona:
#   - Si APP_PASSWORD esta vacio  -> no pide nada (modo local, comodo).
#   - Si APP_PASSWORD tiene valor -> el navegador muestra su propio cuadro de
#     login automaticamente. No hace falta tocar nada del frontend porque
#     fetch() y EventSource reenvian las credenciales solos.
#
# Se implementa como dependencia de FastAPI y NO como middleware ASGI a
# proposito: BaseHTTPMiddleware puede bufferear las respuestas en streaming
# y romper el progreso en tiempo real (SSE) de las descargas.

security_scheme = HTTPBasic(auto_error=False)


def require_auth(credentials: Optional[HTTPBasicCredentials] = Depends(security_scheme)):
    """Valida usuario y contraseña si APP_PASSWORD esta configurado."""
    cfg = config.get_config()

    if not cfg.get("auth_enabled"):
        return None

    unauthorized = HTTPException(
        status_code=401,
        detail="Credenciales incorrectas.",
        headers={"WWW-Authenticate": 'Basic realm="Media & Spotify Downloader"'},
    )

    if credentials is None:
        raise unauthorized

    # compare_digest evita filtrar informacion por el tiempo de comparacion.
    user_ok = secrets.compare_digest(credentials.username, cfg["app_username"])
    pass_ok = secrets.compare_digest(credentials.password, cfg["app_password"])
    if not (user_ok and pass_ok):
        raise unauthorized

    return credentials.username


app = FastAPI(
    title="Media & Spotify Downloader Pro",
    description="Descargador multimedia avanzado para YouTube, MP3 y Spotify",
    version="2.0.0",
    # Aplica require_auth a TODAS las rutas de la app de una sola vez.
    dependencies=[Depends(require_auth)],
)

# CORS.
# Antes era allow_origins=["*"] junto con allow_credentials=True. Esa
# combinacion es invalida segun la spec (el navegador la rechaza) y ademas
# dejaba que cualquier web de internet llamara a tu API.
# El frontend se sirve desde el mismo origen que la API, asi que no necesita
# CORS abierto; se deja solo localhost para desarrollo con otro puerto.
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://localhost:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)


# =====================================================================
# MODELOS PYDANTIC
# =====================================================================

class AnalyzeRequest(BaseModel):
    url: str


class YouTubeDownloadRequest(BaseModel):
    url: str
    media_type: str = "audio"  # 'audio' o 'video'
    audio_bitrate: str = "192"
    video_quality: str = "best"
    custom_subfolder: Optional[str] = None


class SpotifyDownloadRequest(BaseModel):
    tracks: List[Dict[str, Any]]
    collection_name: str
    as_zip: bool = True
    audio_bitrate: str = "192"


class ConfigUpdateRequest(BaseModel):
    spotify_client_id: Optional[str] = None
    spotify_client_secret: Optional[str] = None
    download_dir: Optional[str] = None
    default_audio_bitrate: Optional[str] = None
    default_video_quality: Optional[str] = None


class SpotifyValidateRequest(BaseModel):
    client_id: str
    client_secret: str


class FileDeleteRequest(BaseModel):
    file_path: str


# =====================================================================
# ENDPOINTS DE API
# =====================================================================

@app.post("/api/analyze")
async def analyze_url_endpoint(req: AnalyzeRequest):
    """Analiza una URL de YouTube o Spotify y devuelve su información detallada."""
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="La URL no puede estar vacía.")

    # Ejecutar en threadpool para no bloquear el bucle de eventos.
    # get_event_loop() esta deprecado dentro de una corrutina (avisa en 3.12+
    # y va a dar error en versiones futuras). get_running_loop() es el correcto.
    # loop = asyncio.get_event_loop()
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, downloader_core.analyze_url, url)

    if not result.get("success"):
        return JSONResponse(status_code=400, content=result)
    return result


@app.post("/api/download/youtube")
async def download_youtube_endpoint(req: YouTubeDownloadRequest, background_tasks: BackgroundTasks):
    """Inicia la descarga de video o audio de YouTube en segundo plano."""
    url = req.url.strip()
    if not url:
        raise HTTPException(status_code=400, detail="URL no proporcionada.")

    task_id = downloader_core.create_task(
        task_type="youtube_" + req.media_type,
        title=f"Preparando descarga de {req.media_type.upper()}..."
    )

    # Iniciar la descarga en segundo plano.
    # La linea de abajo se comento: creaba una referencia al bucle de eventos
    # que no se usaba en ningun lado (BackgroundTasks maneja su propio hilo).
    # loop = asyncio.get_event_loop()
    background_tasks.add_task(
        downloader_core.download_youtube_media,
        task_id=task_id,
        url=url,
        media_type=req.media_type,
        audio_bitrate=req.audio_bitrate,
        video_quality=req.video_quality,
        custom_subfolder=req.custom_subfolder
    )

    return {"success": True, "task_id": task_id, "status": "queued"}


@app.post("/api/download/spotify")
async def download_spotify_endpoint(req: SpotifyDownloadRequest, background_tasks: BackgroundTasks):
    """Inicia la descarga por lotes de canciones de Spotify en segundo plano."""
    if not req.tracks:
        raise HTTPException(status_code=400, detail="No se seleccionaron canciones para descargar.")

    task_id = downloader_core.create_task(
        task_type="spotify_collection",
        title=f"Preparando descarga de '{req.collection_name}' ({len(req.tracks)} pistas)..."
    )

    background_tasks.add_task(
        downloader_core.download_spotify_batch,
        task_id=task_id,
        tracks=req.tracks,
        collection_name=req.collection_name,
        as_zip=req.as_zip,
        audio_bitrate=req.audio_bitrate
    )

    return {"success": True, "task_id": task_id, "status": "queued"}


@app.get("/api/tasks/{task_id}")
async def get_task_status_endpoint(task_id: str):
    """Obtiene el estado actual de una tarea de descarga."""
    task = downloader_core.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Tarea no encontrada.")
    return task


@app.get("/api/tasks/{task_id}/events")
async def task_events_sse(task_id: str, request: Request):
    """Transmite eventos en tiempo real (SSE) sobre el progreso de una tarea."""
    task = downloader_core.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Tarea no encontrada.")

    async def event_generator():
        last_progress = -1
        last_status = ""
        while True:
            # Cortar si el navegador cerro la conexion (pestaña cerrada,
            # recarga, etc). Antes el generador seguia girando cada 0.5s
            # hasta que la tarea terminara, aunque ya no hubiera nadie
            # escuchando: cada descarga abandonada dejaba un bucle vivo.
            if await request.is_disconnected():
                break

            current_task = downloader_core.get_task(task_id)
            if not current_task:
                break

            status = current_task.get("status")
            progress = current_task.get("progress", 0)

            # Enviar actualización si hubo cambio
            if progress != last_progress or status != last_status:
                last_progress = progress
                last_status = status
                yield f"data: {json.dumps(current_task)}\n\n"

            if status in ("completed", "error", "cancelled"):
                yield f"data: {json.dumps(current_task)}\n\n"
                break

            await asyncio.sleep(0.5)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task_endpoint(task_id: str):
    """Cancela una tarea activa."""
    task = downloader_core.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Tarea no encontrada.")
    downloader_core.update_task(task_id, status="cancelled", title="Descarga cancelada por el usuario.")
    return {"success": True, "message": "Tarea cancelada."}


@app.get("/api/files")
async def list_files_endpoint():
    """Lista todos los archivos descargados disponibles en el almacenamiento."""
    files = downloader_core.list_downloaded_files()
    return {"success": True, "files": files, "total": len(files)}


@app.get("/api/files/download/{file_path:path}")
async def download_file_direct(file_path: str):
    """
    Descarga un archivo directamente hacia el navegador del usuario (Attachment).
    Funciona desde cualquier dispositivo (celular, tablet, otra PC).
    """
    cfg = config.get_config()
    base_dir = (BASE_DIR / cfg["download_dir"]).resolve()

    # Antes se hacia unquote() aca. Se comento porque Starlette YA decodifica
    # el parametro de ruta, y decodificar dos veces permitia colar un "../"
    # escapado doble (%252e%252e -> %2e%2e -> ..). Ademas rompia los nombres
    # de archivo que contienen un % literal.
    # decoded_path = urllib.parse.unquote(file_path)
    # target = (base_dir / decoded_path).resolve()
    #
    # Y la validacion era por texto (startswith), vulnerable a path traversal:
    # if not str(target).startswith(str(base_dir)):
    #     raise HTTPException(status_code=403, detail="Acceso denegado.")
    target = config.safe_resolve_under(base_dir, file_path)
    if target is None:
        raise HTTPException(status_code=403, detail="Acceso denegado.")

    if not target.is_file():
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")

    ext = target.suffix.lower()
    media_type = "audio/mpeg" if ext == ".mp3" else ("video/mp4" if ext == ".mp4" else ("application/zip" if ext == ".zip" else "application/octet-stream"))
    return FileResponse(
        path=str(target),
        filename=target.name,
        media_type=media_type,
        content_disposition_type="attachment"
    )


@app.get("/api/files/stream/{file_path:path}")
async def stream_file_preview(file_path: str):
    """Permite reproducir el archivo de audio o video directamente en el navegador."""
    cfg = config.get_config()
    base_dir = (BASE_DIR / cfg["download_dir"]).resolve()

    # Mismo arreglo que en el endpoint de descarga: sin doble unquote y
    # validando la ruta por jerarquia real en vez de por texto.
    # decoded_path = urllib.parse.unquote(file_path)
    # target = (base_dir / decoded_path).resolve()
    # if not str(target).startswith(str(base_dir)) or not target.is_file():
    #     raise HTTPException(status_code=404, detail="Archivo no encontrado.")
    target = config.safe_resolve_under(base_dir, file_path)
    if target is None or not target.is_file():
        raise HTTPException(status_code=404, detail="Archivo no encontrado.")

    ext = target.suffix.lower()
    media_type = "audio/mpeg" if ext == ".mp3" else ("video/mp4" if ext == ".mp4" else "application/octet-stream")
    return FileResponse(path=str(target), media_type=media_type)


@app.post("/api/files/delete")
async def delete_file_post_endpoint(req: FileDeleteRequest):
    """Elimina un archivo o carpeta de la biblioteca vía POST JSON."""
    file_path = req.file_path.strip()
    if not file_path:
        raise HTTPException(status_code=400, detail="Ruta de archivo no especificada.")
    success = downloader_core.delete_file(file_path)
    if not success:
        raise HTTPException(status_code=404, detail="No se pudo eliminar el archivo.")
    return {"success": True, "message": "Archivo eliminado correctamente."}


@app.delete("/api/files/{file_path:path}")
async def delete_file_endpoint(file_path: str):
    """Elimina un archivo de la biblioteca."""
    # Starlette ya entrega el parametro de ruta decodificado; volver a llamar
    # a unquote() era una segunda decodificacion (mismo bug que en los
    # endpoints de descarga y streaming).
    # import urllib.parse
    # file_path = urllib.parse.unquote(file_path)
    success = downloader_core.delete_file(file_path)
    if not success:
        raise HTTPException(status_code=404, detail="No se pudo eliminar el archivo.")
    return {"success": True, "message": "Archivo eliminado correctamente."}


def _es_peticion_local(request: Request) -> bool:
    """
    Indica si la peticion viene de la misma maquina donde corre el servidor.

    Sirve para las acciones que solo tienen sentido en uso local, como abrir
    el explorador de archivos: si entras desde el celular o desde un deploy
    en Render, abrir una carpeta la abriria EN EL SERVIDOR, no en tu equipo.
    """
    host = getattr(request.client, "host", "") if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")


@app.post("/api/files/open-folder")
async def open_folder_endpoint(request: Request):
    """Abre la carpeta de descargas en el Explorador de archivos del sistema operativo."""
    # Antes se ejecutaba siempre. En un deploy remoto era, en el mejor de los
    # casos, inutil (intenta abrir una ventana en un contenedor sin escritorio).
    if not _es_peticion_local(request):
        return {
            "success": False,
            "error": "Esta accion solo funciona cuando abris la app en la misma PC donde corre el servidor."
        }

    success = downloader_core.open_downloads_folder()
    return {"success": success}


@app.get("/api/config")
async def get_config_endpoint():
    """Obtiene la configuración actual de .env (con client secret parcialmente ofuscado)."""
    cfg = config.get_config()
    secret = cfg.get("spotify_client_secret", "")
    masked_secret = (secret[:4] + "..." + secret[-4:]) if len(secret) > 8 else ("Configurado" if secret else "")

    # El Client ID tambien se enmascara. Antes se devolvia completo y en claro,
    # y este endpoint es publico: bastaba con abrir /api/config para llevarselo.
    # El frontend solo lo usa para saber si hay credenciales cargadas
    # (has_spotify_credentials), nunca necesita el valor real.
    client_id = cfg.get("spotify_client_id", "")
    masked_client_id = (client_id[:4] + "..." + client_id[-4:]) if len(client_id) > 8 else ("Configurado" if client_id else "")

    return {
        # "spotify_client_id": cfg.get("spotify_client_id", ""),
        "spotify_client_id_masked": masked_client_id,
        "spotify_client_secret_masked": masked_secret,
        "has_spotify_credentials": cfg.get("has_spotify_credentials", False),
        "download_dir": cfg.get("download_dir", "descargas"),
        "default_audio_bitrate": cfg.get("default_audio_bitrate", "192"),
        "default_video_quality": cfg.get("default_video_quality", "best"),
    }


@app.post("/api/config")
async def update_config_endpoint(req: ConfigUpdateRequest):
    """Actualiza las variables de configuración en el archivo .env."""
    updates = {}
    if req.spotify_client_id is not None:
        updates["SPOTIFY_CLIENT_ID"] = req.spotify_client_id.strip()
    if req.spotify_client_secret is not None and req.spotify_client_secret.strip() and not req.spotify_client_secret.startswith("..."):
        updates["SPOTIFY_CLIENT_SECRET"] = req.spotify_client_secret.strip()
    if req.download_dir is not None:
        updates["DOWNLOAD_DIR"] = req.download_dir.strip()
    if req.default_audio_bitrate is not None:
        updates["DEFAULT_AUDIO_BITRATE"] = req.default_audio_bitrate.strip()
    if req.default_video_quality is not None:
        updates["DEFAULT_VIDEO_QUALITY"] = req.default_video_quality.strip()

    updated = config.update_config(updates)
    return {"success": True, "config": updated}


@app.post("/api/spotify/validate")
async def validate_spotify_credentials(req: SpotifyValidateRequest):
    """Prueba si las credenciales de Spotify Client ID y Secret son válidas."""
    c_id = req.client_id.strip()
    c_secret = req.client_secret.strip()
    if not c_id or not c_secret:
        return {"valid": False, "error": "Debes ingresar tanto el Client ID como el Client Secret."}

    try:
        import spotipy
        from spotipy.oauth2 import SpotifyClientCredentials
        auth = SpotifyClientCredentials(client_id=c_id, client_secret=c_secret)
        sp = spotipy.Spotify(auth_manager=auth)
        # Prueba ligera consultando una categoría o artista
        sp.categories(limit=1)
        return {"valid": True, "message": "¡Credenciales de Spotify válidas y conectadas con éxito!"}
    except Exception as e:
        return {"valid": False, "error": f"Credenciales inválidas: {str(e)}"}


@app.get("/api/diagnostics")
async def get_diagnostics_endpoint(request: Request):
    """Obtiene el diagnóstico del entorno (ffmpeg, yt-dlp, python, almacenamiento)."""
    diagnostics = config.get_system_diagnostics()
    # El frontend usa este dato para ocultar los botones "Abrir en Explorador"
    # cuando no estas en la misma maquina que el servidor.
    diagnostics["is_local_request"] = _es_peticion_local(request)
    return diagnostics


# =====================================================================
# SERVICIO DE ARCHIVOS ESTÁTICOS / FRONTEND
# =====================================================================

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def root():
    """Sirve la página principal de la aplicación web."""
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return HTMLResponse("<h1>Multimedia Downloader</h1><p>Cargando interfaz...</p>")


if __name__ == "__main__":
    import uvicorn
    cfg = config.get_config()
    uvicorn.run("app:app", host=cfg["host"], port=cfg["port"], reload=True)
