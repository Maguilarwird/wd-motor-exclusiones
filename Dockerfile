FROM python:3.11-slim

# Sin .pyc y con salida sin buffer, para que los logs aparezcan en el momento
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# El modelo lingüístico no viene por pip: se baja aparte y pesa ~560 MB, que es
# casi todo el tamaño de esta imagen. Tiene que ser el grande: con el chico
# fallan los casos de fuera de oficina (ver requirements.txt).
# En su propia capa para que un cambio en el código no obligue a bajarlo de nuevo.
RUN python -m spacy download es_core_news_lg
ENV MODELO_SPACY=es_core_news_lg

COPY motor/ ./motor/
COPY tests/ ./tests/
COPY catalogos/banners.yaml ./catalogos/banners.yaml
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
    CMD python -c "import os,urllib.request,sys; p=os.environ.get('PORT','8000'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/health',timeout=2).status==200 else 1)"

# Forma shell, no exec: un PaaS inyecta el puerto por PORT y la forma exec
# no expande variables. Sin PORT queda en 8000, igual que antes.
CMD ["sh", "-c", "uvicorn api:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
