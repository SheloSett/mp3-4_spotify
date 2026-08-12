FROM python:3.11-slim

# Instalar FFmpeg y dependencias del sistema
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copiar requerimientos e instalar dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar código de la aplicación
COPY . .

# Crear carpeta de descargas
RUN mkdir -p descargas

# Puerto expuesto
ENV PORT=8000
ENV HOST=0.0.0.0
EXPOSE 8000

# Comando de inicio.
# Antes el puerto estaba fijo en 8000. Render (y la mayoria de los PaaS)
# inyectan su propio $PORT y esperan que la app escuche ahi; si no coincide,
# el deploy queda colgado en "waiting for open port" y falla.
# La forma "shell" del CMD permite expandir la variable, con 8000 de respaldo.
# CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}
