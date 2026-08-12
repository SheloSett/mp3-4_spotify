import os
import re
import sys
import shutil

try:
    import spotipy
    from spotipy.oauth2 import SpotifyClientCredentials
except ImportError:
    print("Error: Spotipy no est� instalado.")
    print("Instala con: pip install spotipy")
    sys.exit(1)

try:
    import yt_dlp
except ImportError:
    print("Error: yt-dlp no esta instalado.")
    print("Instala con: pip install yt-dlp")
    sys.exit(1)

# Reutilizamos el buscador de ffmpeg de mp3_2.py en vez de duplicar la logica.
# Si ese archivo no estuviera, seguimos igual pero dependiendo del PATH.
try:
    from mp3_2 import buscar_ffmpeg
except ImportError:
    def buscar_ffmpeg():
        return None


class SpotifyPlaylistDownloader:
    def __init__(self):
        # Buscamos ffmpeg UNA sola vez aca, no por cada cancion.
        # Sin ffmpeg el postprocesador falla y queda el .webm sin convertir,
        # que era justo el problema: descargaba pero no pasaba a MP3.
        self.carpeta_ffmpeg = buscar_ffmpeg()
        if self.carpeta_ffmpeg:
            print(f"ffmpeg encontrado en: {self.carpeta_ffmpeg}")
        elif not shutil.which("ffmpeg"):
            # Avisamos ANTES de empezar: si no, se descarga la playlist entera
            # y falla la conversion en cada tema.
            print("AVISO: no se encontro ffmpeg. Los archivos NO se convertiran a MP3.")
            print("       Instalalo con:  winget install Gyan.FFmpeg")

        # Configurar credenciales de Spotify
        # Obten tus credenciales en: https://developer.spotify.com/dashboard
        self.client_id = input("Ingresa tu Spotify Client ID: ").strip()
        self.client_secret = input("Ingresa tu Spotify Client Secret: ").strip()

        try:
            auth_manager = SpotifyClientCredentials(
                client_id=self.client_id,
                client_secret=self.client_secret
            )
            self.sp = spotipy.Spotify(auth_manager=auth_manager)
        except Exception as e:
            print(f"Error al autenticar con Spotify: {e}")
            sys.exit(1)

    def extract_playlist_id(self, playlist_url):
        """Extrae el ID de la playlist desde la URL"""
        pattern = r'playlist/([a-zA-Z0-9]+)'
        match = re.search(pattern, playlist_url)
        if match:
            return match.group(1)
        return None

    def get_playlist_tracks(self, playlist_id):
        """Obtiene todas las canciones de una playlist"""
        try:
            results = self.sp.playlist_tracks(playlist_id)
            tracks = results['items']

            # Manejar paginaci�n si hay m�s de 100 canciones
            while results['next']:
                results = self.sp.next(results)
                tracks.extend(results['items'])

            return tracks
        except Exception as e:
            print(f"Error al obtener canciones de la playlist: {e}")
            return []

    def search_and_download(self, track_name, artist_name, download_folder):
        """Busca la cancion en YouTube y la descarga en formato MP3"""
        query = f"{track_name} {artist_name} audio"

        # El nombre de archivo se arma con f-string, o sea que Python sustituye
        # el titulo ANTES de que yt-dlp vea la plantilla. Si el titulo trae "/" o "\"
        # (ej: "AC/DC") se interpreta como separador de carpetas y la ruta se rompe.
        # El nombre de la playlist ya se limpiaba asi mas abajo; faltaba hacerlo aca.
        track_seguro = re.sub(r'[<>:"/\\|?*]', '', track_name)
        artista_seguro = re.sub(r'[<>:"/\\|?*]', '', artist_name)

        # Configuracion de yt-dlp
        ydl_opts = {
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': '192',
            }],
            # COMENTADO: usaba los nombres crudos, sin limpiar los caracteres
            # invalidos para rutas de Windows.
            # 'outtmpl': os.path.join(download_folder, f'{track_name} - {artist_name}.%(ext)s'),
            'outtmpl': os.path.join(download_folder, f'{track_seguro} - {artista_seguro}.%(ext)s'),
            'quiet': False,
            'no_warnings': False,
            'default_search': 'ytsearch1',
            'noplaylist': True,
        }

        # Ruta explicita a ffmpeg cuando no esta en el PATH (ver __init__).
        if self.carpeta_ffmpeg:
            ydl_opts['ffmpeg_location'] = self.carpeta_ffmpeg

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                print(f"\nBuscando: {track_name} - {artist_name}")
                ydl.download([query])
                print(f" Descargado: {track_name} - {artist_name}")
                return True
        except Exception as e:
            print(f" Error al descargar '{track_name} - {artist_name}': {e}")
            return False

    def download_playlist(self, playlist_url):
        """Descarga todas las canciones de una playlist"""
        # Extraer ID de la playlist
        playlist_id = self.extract_playlist_id(playlist_url)
        if not playlist_id:
            print("Error: URL de playlist inv�lida")
            return

        # Obtener informaci�n de la playlist
        try:
            playlist_info = self.sp.playlist(playlist_id)
            playlist_name = playlist_info['name']
            print(f"\n=� Playlist: {playlist_name}")
            print(f"=d Creador: {playlist_info['owner']['display_name']}")
        except Exception as e:
            print(f"Error al obtener informaci�n de la playlist: {e}")
            playlist_name = "spotify_playlist"

        # Crear carpeta de descargas
        # Limpiar nombre de playlist para usarlo como nombre de carpeta
        safe_playlist_name = re.sub(r'[<>:"/\\|?*]', '', playlist_name)
        download_folder = os.path.join(os.getcwd(), safe_playlist_name)

        if not os.path.exists(download_folder):
            os.makedirs(download_folder)
            print(f"\n=� Carpeta creada: {download_folder}")
        else:
            print(f"\n=� Usando carpeta: {download_folder}")

        # Obtener canciones
        tracks = self.get_playlist_tracks(playlist_id)
        total_tracks = len(tracks)

        if total_tracks == 0:
            print("No se encontraron canciones en la playlist")
            return

        print(f"\n<� Total de canciones: {total_tracks}\n")
        print("=" * 50)

        # Descargar cada canci�n
        successful = 0
        failed = 0

        for i, item in enumerate(tracks, 1):
            track = item['track']
            if track is None:
                continue

            track_name = track['name']
            artists = ', '.join([artist['name'] for artist in track['artists']])

            print(f"\n[{i}/{total_tracks}]")

            if self.search_and_download(track_name, artists, download_folder):
                successful += 1
            else:
                failed += 1

        # Resumen final
        print("\n" + "=" * 50)
        print(f"\n Descarga completada!")
        print(f"   Exitosas: {successful}/{total_tracks}")
        print(f"   Fallidas: {failed}/{total_tracks}")
        print(f"   Ubicaci�n: {download_folder}\n")


def main():
    print("=" * 50)
    print("  DESCARGADOR DE PLAYLISTS DE SPOTIFY")
    print("=" * 50)
    print("\nNOTA: Necesitas credenciales de Spotify API.")
    print("Obt�n tus credenciales en: https://developer.spotify.com/dashboard\n")

    # Inicializar descargador
    downloader = SpotifyPlaylistDownloader()

    # Solicitar URL de playlist
    playlist_url = input("\nIngresa la URL de la playlist de Spotify: ").strip()

    if not playlist_url:
        print("Error: No ingresaste ninguna URL")
        return

    # Descargar playlist
    downloader.download_playlist(playlist_url)


if __name__ == "__main__":
    main()
