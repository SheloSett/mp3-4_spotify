# -*- coding: utf-8 -*-
import yt_dlp
import os
import glob
import shutil


def buscar_ffmpeg():
    """
    Localiza la carpeta que contiene ffmpeg.exe y ffprobe.exe.

    Por que existe esto: al instalar ffmpeg con winget, el PATH del sistema se
    actualiza, pero las terminales (y VS Code) que ya estaban abiertas siguen con
    el PATH viejo y no lo ven. Eso provoca el error:
        "Postprocessing: ffprobe and ffmpeg not found"
    aunque ffmpeg SI este instalado. Buscandolo a mano evitamos depender del PATH.

    Returns:
        str: Carpeta con los binarios, o None si el PATH ya funciona / no se encontro
    """
    # Caso ideal: el PATH ya los ve, dejamos que yt-dlp los resuelva solo.
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return None

    # Ubicaciones tipicas en Windows. Se usa comodin en la version (ffmpeg-*)
    # para que siga funcionando cuando ffmpeg se actualice.
    patrones = [
        os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\ffmpeg-*\bin"),
        os.path.expandvars(r"%ProgramData%\chocolatey\bin"),
        r"C:\ffmpeg\bin",
    ]

    for patron in patrones:
        for carpeta in glob.glob(patron):
            # yt-dlp necesita AMBOS binarios, no solo ffmpeg.
            tiene_ffmpeg = os.path.isfile(os.path.join(carpeta, "ffmpeg.exe"))
            tiene_ffprobe = os.path.isfile(os.path.join(carpeta, "ffprobe.exe"))
            if tiene_ffmpeg and tiene_ffprobe:
                return carpeta

    return None


def descargar_mp3_youtube(url, carpeta_destino="descargas"):
    """
    Descarga el audio de un video de YouTube y lo guarda como MP3.

    Args:
        url (str): URL del video de YouTube
        carpeta_destino (str): Carpeta donde se guardará el archivo MP3

    Returns:
        str: Ruta del archivo descargado o None si hay error
    """
    # Crear carpeta de destino si no existe
    if not os.path.exists(carpeta_destino):
        os.makedirs(carpeta_destino)

    # Opciones de configuración para yt-dlp
    opciones = {
        # COMENTADO: forzaba m4a como contenedor preferido. Para convertir a MP3 conviene
        # dejar que yt-dlp elija el mejor audio disponible sin atarlo a un contenedor,
        # porque ffmpeg lo va a recodificar a mp3 de todas formas.
        # 'format': 'bestaudio[ext=m4a]/bestaudio',  # Mejor audio, preferiblemente m4a
        'format': 'bestaudio/best',  # Mejor audio disponible (ffmpeg lo convierte a mp3)

        # Convierte el audio descargado a MP3 usando ffmpeg.
        # OJO: requiere tener ffmpeg instalado y accesible en el PATH.
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',  # kbps
        }],

        # Si la URL trae "&list=..." (mixes/radio de YouTube), sin esto yt-dlp
        # intenta bajar la playlist completa (llegó a expandir 410 items).
        'noplaylist': True,

        'outtmpl': os.path.join(carpeta_destino, '%(title)s.%(ext)s'),  # Plantilla de nombre
        'quiet': False,  # Mostrar progreso
        'no_warnings': False,
    }

    # Si ffmpeg no esta en el PATH pero si instalado, le pasamos la ruta explicita.
    # Asi no hace falta reiniciar la terminal despues de instalarlo.
    carpeta_ffmpeg = buscar_ffmpeg()
    if carpeta_ffmpeg:
        opciones['ffmpeg_location'] = carpeta_ffmpeg
        print(f"ffmpeg encontrado en: {carpeta_ffmpeg}")
    elif not shutil.which("ffmpeg"):
        # Avisamos ANTES de descargar, para no bajar el audio y fallar al convertir.
        print("AVISO: no se encontro ffmpeg. No se podra convertir a MP3.")
        print("       Instalalo con:  winget install Gyan.FFmpeg")

    try:
        print(f"Descargando audio de: {url}")
        with yt_dlp.YoutubeDL(opciones) as ydl:
            info = ydl.extract_info(url, download=True)

            # Aunque usemos noplaylist, algunas URLs siguen devolviendo un
            # contenedor con 'entries'; nos quedamos con el primer video.
            if 'entries' in info:
                info = info['entries'][0]

            # COMENTADO: esta forma de armar la ruta tenía dos fallos.
            # 1) 'ext' es la extensión ANTES del postprocesado, así que tras convertir
            #    a mp3 devolvía una ruta .m4a/.webm que ya no existe.
            # 2) info['title'] es el título crudo; yt-dlp sanitiza el nombre al escribir
            #    en disco (quita \ / : * ? " < > |), así que la ruta no coincidía con el
            #    archivo real cuando el título traía esos caracteres.
            # ext = info.get('ext', 'm4a')
            # archivo_audio = os.path.join(carpeta_destino, f"{info['title']}.{ext}")

            # yt-dlp reporta en 'requested_downloads' la ruta final, ya con el
            # postprocesado aplicado y el nombre sanitizado.
            archivo_audio = None
            descargas = info.get('requested_downloads')
            if descargas:
                archivo_audio = descargas[0].get('filepath')

            # Fallback: reconstruir el nombre sanitizado y forzar extensión .mp3
            if not archivo_audio:
                archivo_audio = os.path.splitext(ydl.prepare_filename(info))[0] + '.mp3'

            print(f"\nDescarga completada: {archivo_audio}")
            return archivo_audio

    except Exception as e:
        print(f"Error al descargar: {str(e)}")
        return None


if __name__ == "__main__":
    # Solicitar URL al usuario
    url_video = input("Ingresa la URL del video de YouTube: ")

    # Opcionalmente, solicitar carpeta de destino
    carpeta = input("Carpeta de destino (Enter para 'descargas'): ").strip()
    if not carpeta:
        carpeta = "descargas"

    # Descargar el MP3
    archivo_descargado = descargar_mp3_youtube(url_video, carpeta)

    if archivo_descargado:
        print(f"\nArchivo de audio guardado exitosamente!")
    else:
        print("\nNo se pudo completar la descarga.")
