FROM python:3.11-slim

# Sin .pyc y con salida sin buffer, para que los logs aparezcan en el momento
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY motor/ ./motor/
COPY tests/ ./tests/
COPY api.py .

# El catálogo NO se copia a la imagen. Vive en un volumen montado, para que
# cambiar una regla no requiera reconstruir ni desplegar. La ruta se inyecta:
#   CATALOGO_EXCLUSIONES=/catalogos/exclusiones.csv
ENV CATALOGO_EXCLUSIONES=/catalogos/exclusiones.csv
VOLUME ["/catalogos"]

# Usuario sin privilegios
RUN useradd --create-home --uid 10001 motor && chown -R motor:motor /app
USER motor

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=2).status==200 else 1)"

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
