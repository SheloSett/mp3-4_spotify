# -*- coding: utf-8 -*-
import os
import sys
import time
import webbrowser
import threading
from pathlib import Path
import uvicorn

import config

def open_browser(url: str, delay: float = 1.2):
    """Abre el navegador web tras un breve retardo para que el servidor termine de arrancar."""
    def _open():
        time.sleep(delay)
        print(f"\n[+] Abriendo la interfaz web en tu navegador: {url}\n")
        webbrowser.open(url)
    
    t = threading.Thread(target=_open)
    t.daemon = True
    t.start()


def main():
    print("=" * 60)
    print("   MEDIA & SPOTIFY DOWNLOADER PRO - SERVIDOR LOCAL")
    print("=" * 60)

    cfg = config.get_config()
    host = cfg.get("host", "127.0.0.1")
    if host == "0.0.0.0":
        display_host = "localhost"
    else:
        display_host = host
    port = cfg.get("port", 8000)
    url = f"http://{display_host}:{port}"

    print(f"[*] Iniciando servidor en: {url}")
    print(f"[*] Carpeta de descargas: {config.BASE_DIR / cfg.get('download_dir', 'descargas')}")
    print("[*] Presiona Ctrl + C en esta ventana para detener el servidor.\n")

    # Abrir navegador automáticamente
    open_browser(url)

    # Iniciar servidor Uvicorn
    uvicorn.run("app:app", host=cfg.get("host", "0.0.0.0"), port=port, log_level="info")


if __name__ == "__main__":
    main()
