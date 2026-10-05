"""
Servicio HTTP del motor de exclusiones.

    uvicorn api:app --host 0.0.0.0 --port 8000

Rutas
    GET  /health              vivo, y con qué catálogo
    GET  /catalogo            qué reglas tiene cargadas ahora mismo
    POST /procesar            un mensaje
    POST /procesar-lote       un hilo o una base completa
    POST /procesar-otbb       las cuatro salidas OTBB (JSON o ZIP)
    POST /validar-catalogo    corre la suite de regresión contra el catálogo en disco
    POST /recargar-catalogo   lo carga en memoria, solo si la suite pasa

El catálogo se lee de la ruta que indique CATALOGO_EXCLUSIONES. Vive FUERA de la
imagen a propósito: editar una regla no puede requerir un despliegue. Y la
recarga corre la suite antes de aceptar: autoservicio sin ese test es cómo una
regla mala llega a producción en silencio.
"""

import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from motor import lemas
from motor.exclusion_engine import ErrorCatalogo, ExclusionEngine, load_catalog
from motor.pipeline import procesar
from motor.salidas import armar_salidas, salidas_a_json, salidas_a_zip
from motor.validacion import correr_suite

def _leer_api_key():
    ruta = os.environ.get("API_KEY_FILE")
    if ruta:
        try:
            with open(ruta, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError as exc:
            raise RuntimeError(f"no se pudo leer API_KEY_FILE={ruta}: {exc}") from exc
    return os.environ.get("API_KEY", "")


API_KEY = _leer_api_key()
MAX_MENSAJES = int(os.environ.get("MAX_MENSAJES_POR_LOTE", "5000"))

app = FastAPI(
    title="Motor de exclusiones OTBB",
    description="Capa determinista: limpia, deduplica el historial citado, "
                "marca insistencias y aplica el catálogo de exclusiones.",
    version="0.2.0",
)

_lock = threading.Lock()
_estado: Dict[str, Any] = {"engine": None, "reglas": 0, "cargado": None, "ruta": None}


def _cargar():
    reglas = load_catalog()
    engine = ExclusionEngine(reglas)
    _estado.update({
        "engine": engine,
        "reglas": len(reglas),
        "cargado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ruta": os.environ.get("CATALOGO_EXCLUSIONES", "(default del repo)"),
    })
    return engine


def _publicar_engine(engine):
    """Publica exactamente el motor que ya fue validado."""
    _estado.update({
        "engine": engine,
        "reglas": len(engine.rules),
        "cargado": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ruta": os.environ.get("CATALOGO_EXCLUSIONES", "(default del repo)"),
    })


def _engine():
    if _estado["engine"] is None:
        with _lock:
            if _estado["engine"] is None:
                _cargar()
    return _estado["engine"]


def _autorizar(x_api_key: Optional[str]):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="API key inválida o ausente")


class Mensaje(BaseModel):
    texto: str = Field(..., description="cuerpo del correo")
    id: Optional[Any] = Field(None, description="se devuelve igual, para que reconozcas la fila")
    hilo: Optional[str] = Field(None, description="Thread Id. Sin esto no hay dedup ni insistencia")
    fecha: Optional[str] = Field(None, description="para ordenar dentro del hilo")
    remitente: Optional[str] = Field(None, description="para detectar insistencia")
    msg_id: Optional[str] = Field(None, description="Message ID, para distinguir filas repetidas")
    asunto: Optional[str] = None


class Lote(BaseModel):
    mensajes: List[Mensaje]
    contexto: List[str] = Field(default_factory=list,
                                description='alcance, ej ["santander","cl"]')


class LoteOtbb(Lote):
    nombre_base: Optional[str] = Field(
        None, description="prefijo de archivos cuando formato=zip (default: salida)")


class Uno(BaseModel):
    mensaje: Mensaje
    contexto: List[str] = Field(default_factory=list)


@app.on_event("startup")
def _startup():
    # El catálogo se compila una sola vez al arrancar, no en cada request.
    try:
        _cargar()
    except ErrorCatalogo as e:
        # Arrancar igual: /health lo va a reportar y así se ve el error real
        # en vez de un contenedor que reinicia en loop sin decir por qué.
        _estado["error"] = str(e)


@app.get("/health")
def health():
    # Carga perezosa además del startup: el health check no debe depender del
    # orden de arranque, y un 503 tiene que significar "el catálogo está malo",
    # no "todavía no lo intenté".
    if _estado["engine"] is None:
        try:
            _engine()
        except ErrorCatalogo as e:
            _estado["error"] = str(e)
    if _estado["engine"] is None:
        raise HTTPException(status_code=503, detail={
            "estado": "sin catálogo",
            "error": _estado.get("error", "no cargado"),
        })
    return {
        "estado": "ok",
        "reglas_cargadas": _estado["reglas"],
        "catalogo": _estado["ruta"],
        "cargado_en": _estado["cargado"],
        "version": app.version,
        # Si el modelo lingüístico está o no cambia lo que el motor encuentra:
        # sin él, las reglas escritas en infinitivo no disparan. No es un fallo
        # —el servicio funciona igual— pero tiene que poder verse sin entrar al
        # contenedor a adivinar.
        "lematizacion": lemas.info(),
    }


@app.get("/catalogo")
def catalogo(x_api_key: Optional[str] = Header(None)):
    _autorizar(x_api_key)
    engine = _engine()
    por_categoria: Dict[str, int] = {}
    por_alcance: Dict[str, int] = {}
    for r in engine.rules:
        por_categoria[r["categoria"]] = por_categoria.get(r["categoria"], 0) + 1
        for a in r["alcance_set"]:
            por_alcance[a] = por_alcance.get(a, 0) + 1
    return {"reglas": len(engine.rules), "cargado_en": _estado["cargado"],
            "por_categoria": por_categoria, "por_alcance": por_alcance}


@app.post("/procesar")
def procesar_uno(body: Uno, x_api_key: Optional[str] = Header(None)):
    _autorizar(x_api_key)
    r = procesar([body.mensaje.model_dump()], contexto=body.contexto, engine=_engine())
    return {"mensaje": r["mensajes"][0], "avisos": r["avisos"]}


def _validar_lote(mensajes: List[Mensaje]):
    if len(mensajes) > MAX_MENSAJES:
        raise HTTPException(status_code=413, detail=(
            f"{len(mensajes)} mensajes supera el máximo de {MAX_MENSAJES}. "
            "Partir en lotes por hilo: un hilo nunca debe quedar partido entre dos "
            "llamadas, porque la deduplicación necesita los mensajes anteriores."))


@app.post("/procesar-lote")
def procesar_lote(body: Lote, x_api_key: Optional[str] = Header(None)):
    _autorizar(x_api_key)
    _validar_lote(body.mensajes)
    return procesar([m.model_dump() for m in body.mensajes],
                    contexto=body.contexto, engine=_engine())


@app.post("/procesar-otbb")
def procesar_otbb(
    body: LoteOtbb,
    x_api_key: Optional[str] = Header(None),
    formato: str = Query("json", pattern="^(json|zip)$",
                         description="json: cuatro tablas en el cuerpo; zip: CSV empaquetados"),
):
    """
    Procesa un lote y devuelve las cuatro salidas OTBB en una sola llamada:

    maestro, conversacional, excluidos y exclusiones_data_maestro.

    Mismo contrato de entrada que /procesar-lote. Un hilo no puede quedar partido
    entre dos llamadas.
    """
    _autorizar(x_api_key)
    _validar_lote(body.mensajes)
    filas = [m.model_dump() for m in body.mensajes]
    resultado = procesar(filas, contexto=body.contexto, engine=_engine())
    salidas = armar_salidas(filas, resultado)

    if salidas["resumen"].get("alertas_limpieza"):
        raise HTTPException(status_code=422, detail={
            "mensaje": "En uno o más mensajes la limpieza dejó el texto casi vacío. "
                       "Hay un banner mal clasificado.",
            "alertas_limpieza": salidas["resumen"]["alertas_limpieza"],
            "avisos": salidas["avisos"],
        })

    if formato == "zip":
        stem = (body.nombre_base or "salida").strip() or "salida"
        buf = salidas_a_zip(salidas, stem=stem)
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{stem}_otbb.zip"'},
        )
    return salidas_a_json(salidas)


@app.post("/validar-catalogo")
def validar_catalogo(x_api_key: Optional[str] = Header(None)):
    _autorizar(x_api_key)
    try:
        engine = ExclusionEngine(load_catalog())
    except ErrorCatalogo as e:
        return {"ok": False, "error": str(e)}
    r = correr_suite(engine=engine)
    r["reglas"] = len(engine.rules)
    return r


@app.post("/recargar-catalogo")
def recargar_catalogo(x_api_key: Optional[str] = Header(None)):
    _autorizar(x_api_key)
    try:
        nuevo = ExclusionEngine(load_catalog())
    except ErrorCatalogo as e:
        raise HTTPException(status_code=400, detail=f"catálogo inválido: {e}")

    suite = correr_suite(engine=nuevo)
    if not suite["ok"]:
        raise HTTPException(status_code=400, detail={
            "mensaje": "el catálogo nuevo no pasa la suite de regresión, "
                       "no se cargó. Sigue activo el anterior.",
            "suite": suite,
        })
    with _lock:
        _publicar_engine(nuevo)
    return {"ok": True, "reglas_cargadas": _estado["reglas"],
            "cargado_en": _estado["cargado"], "suite": suite}
