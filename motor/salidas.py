"""
Las cuatro salidas OTBB, en un solo lugar.

El script de terminal y el servicio HTTP llaman acá. Si mañana cambia cómo se
arma el conversacional o el Data_maestro, cambia en un solo sitio.
"""

import csv
import io
import json
import os
import re
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import pandas as pd

from . import columnas

RAIZ = Path(__file__).resolve().parents[1]
TIPIFICACIONES_DEFAULT = RAIZ / "catalogos" / "tipificaciones.csv"

DATA_MAESTRO = [
    "Tipo", "Tipificación", "Industria", "Segmento", "Origen", "Accionable",
    "Tratamiento", "Macro Producto", "Producto", "Módulo Journey",
    "Decision Flow", "Caso de Uso", "Categoría", "Subcategoría",
    "Etapa Comercial", "Tipo Interacción", "Área Responsable", "Adjuntos",
    "Definición", "Capacidad Wird", "N", "%",
    "Ejemplo 1", "Ejemplo 2", "Ejemplo 3", "Ejemplo 4", "Ejemplo 5",
]
MAX_EJEMPLOS = 5
LARGO_EJEMPLO = 300

COLS_EXCLUIDOS = [
    "hilo", "posicion_en_hilo", "mensajes_en_hilo", "categoria",
    "origen", "regla_id", "texto_regla", "condicion", "evidencia",
    "via", "continua", "motivo_continuidad",
    "tratamiento", "accionable", "definicion", "fila_estado",
    "chars_original",
]


def leer_tipificaciones(ruta=None) -> Dict[tuple, str]:
    """(Categoría, Origen) -> código WM. Las pendientes quedan sin código."""
    ruta = Path(ruta or os.environ.get("TIPIFICACIONES_EXCLUSIONES")
                or TIPIFICACIONES_DEFAULT)
    if not ruta.exists():
        return {}
    with open(ruta, encoding="utf-8-sig", newline="") as fh:
        return {(r["Categoría"], r["Origen"]): r["Tipificación"].strip()
                for r in csv.DictReader(fh) if r.get("Tipificación", "").strip()}


def una_linea(texto, largo=LARGO_EJEMPLO) -> str:
    return re.sub(r"\s+", " ", str(texto or "")).strip()[:largo]


def _serializar_listas(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for c in ("perdedores", "bloques_removidos"):
        if c in out.columns:
            out[c] = out[c].map(lambda v: ", ".join(v) if isinstance(v, list) else v)
    return out


def armar_salidas(filas_originales: List[dict], resultado: dict,
                  col: Optional[dict] = None,
                  tipificaciones: Optional[dict] = None) -> dict:
    """
    Arma las cuatro salidas OTBB a partir de las filas de entrada y el resultado
    de motor.pipeline.procesar().

    filas_originales: filas de la base (CSV) o mensajes de la API, en el mismo
        orden en que entraron al pipeline.
    resultado: {"mensajes": [...], "resumen": {...}, "avisos": [...]}
    col: mapeo {campo: nombre_columna}. Si falta, se infiere de las filas.
    """
    if col is None:
        col = columnas.detectar(pd.DataFrame(filas_originales or [{}]))

    orig = pd.DataFrame(filas_originales).reset_index(drop=True)
    motor = _serializar_listas(pd.DataFrame(resultado["mensajes"]))
    motor = motor.drop(columns=["id"], errors="ignore").reset_index(drop=True)
    # Si la entrada ya trae campos con el mismo nombre que el motor (p. ej. hilo
    # en la API), prevalecen los del motor: son los que usan posicion_en_hilo.
    solapadas = [c for c in motor.columns if c in orig.columns]
    full = pd.concat([orig.drop(columns=solapadas, errors="ignore"), motor], axis=1)

    dup = full["fila_estado"].str.startswith("duplicado")
    exc = full["excluido"].astype(bool)

    full["_marca"] = [
        "" if d else (f"[{c}] {t}".strip() if e else t)
        for d, e, c, t in zip(dup, exc, full["categoria"],
                              full["texto_para_prompt"].fillna(""))
    ]

    maestro = full[~exc & ~dup].drop(columns=["_marca"])
    conversacional = full.copy()
    conversacional["texto_para_prompt"] = conversacional["_marca"]
    conversacional = conversacional.drop(columns=["_marca"])
    if col.get("hilo"):
        conversacional = conversacional.sort_values(
            ["hilo", "posicion_en_hilo"], kind="stable")

    excl = full[exc].copy()
    cols_exc = list(COLS_EXCLUIDOS)
    if col.get("texto"):
        excl["texto"] = excl[col["texto"]].map(una_linea)
        cols_exc.append("texto")

    codigos = tipificaciones if tipificaciones is not None else leer_tipificaciones()
    total = len(full)
    filas_dm, sin_codigo = [], []
    for (cat, ori), g in excl.groupby(["categoria", "origen"], dropna=False):
        cod = codigos.get((cat, ori), "")
        if not cod:
            sin_codigo.append({"categoria": cat, "origen": ori, "n": len(g)})
        fila = {c: "" for c in DATA_MAESTRO}
        fila.update({
            "Tipo": "Exclusión",
            "Tipificación": cod,
            "Origen": ori,
            "Accionable": g["accionable"].iloc[0],
            "Tratamiento": g["tratamiento"].iloc[0],
            "Categoría": "Exclusión",
            "Subcategoría": cat,
            "Definición": g["definicion"].iloc[0],
            "N": len(g),
            "%": round(len(g) / total * 100, 1) if total else 0.0,
        })
        textos = [una_linea(t) for t in
                  (g[col["texto"]] if col.get("texto") else g["texto_para_prompt"])]
        unicos = list(dict.fromkeys(t for t in textos if t))[:MAX_EJEMPLOS]
        for i, t in enumerate(unicos, 1):
            fila[f"Ejemplo {i}"] = t
        filas_dm.append(fila)
    filas_dm.sort(key=lambda f: -f["N"])
    data_maestro = pd.DataFrame(filas_dm, columns=DATA_MAESTRO)

    excluidos = excl[[c for c in cols_exc if c in excl.columns]]

    resumen = dict(resultado.get("resumen") or {})
    resumen.update({
        "correos_en_base": total,
        "exclusiones_marcadas": int(exc.sum()),
        "pct_exclusiones": round(exc.mean() * 100, 1) if total else 0.0,
        "filas_repetidas_archivo": int(dup.sum()),
        "pct_duplicados": round(dup.mean() * 100, 1) if total else 0.0,
        "van_al_maestro": len(maestro),
        "pct_maestro": round(len(maestro) / total * 100, 1) if total else 0.0,
    })

    return {
        "maestro": maestro,
        "conversacional": conversacional,
        "excluidos": excluidos,
        "exclusiones_data_maestro": data_maestro,
        "resumen": resumen,
        "avisos": list(resultado.get("avisos") or []),
        "sin_codigo": sin_codigo,
        "columnas": {k: v for k, v in col.items() if not k.startswith("ref_")},
    }


def escribir_salidas(salidas: dict, destino: Union[str, Path], stem: str) -> dict:
    """Escribe las cuatro CSV al disco. Devuelve {nombre: Path}."""
    destino = Path(destino)
    destino.mkdir(parents=True, exist_ok=True)
    rutas = {
        "maestro": destino / f"{stem}_maestro.csv",
        "conversacional": destino / f"{stem}_conversacional.csv",
        "excluidos": destino / f"{stem}_excluidos.csv",
        "exclusiones_data_maestro": destino / f"{stem}_exclusiones_data_maestro.csv",
    }
    for clave, ruta in rutas.items():
        salidas[clave].to_csv(ruta, index=False, encoding="utf-8-sig")
    return rutas


def _df_a_registros(df: pd.DataFrame) -> List[dict]:
    if df.empty:
        return []
    return json.loads(df.fillna("").to_json(orient="records", force_ascii=False))


def salidas_a_json(salidas: dict) -> dict:
    """Serializa las salidas para respuesta HTTP JSON."""
    return {
        "resumen": salidas["resumen"],
        "avisos": salidas["avisos"],
        "sin_codigo": salidas["sin_codigo"],
        "columnas": salidas["columnas"],
        "maestro": _df_a_registros(salidas["maestro"]),
        "conversacional": _df_a_registros(salidas["conversacional"]),
        "excluidos": _df_a_registros(salidas["excluidos"]),
        "exclusiones_data_maestro": _df_a_registros(salidas["exclusiones_data_maestro"]),
    }


def salidas_a_zip(salidas: dict, stem: str = "salida") -> io.BytesIO:
    """Empaqueta las cuatro CSV en un ZIP en memoria."""
    buf = io.BytesIO()
    nombres = {
        "maestro": f"{stem}_maestro.csv",
        "conversacional": f"{stem}_conversacional.csv",
        "excluidos": f"{stem}_excluidos.csv",
        "exclusiones_data_maestro": f"{stem}_exclusiones_data_maestro.csv",
    }
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for clave, nombre in nombres.items():
            csv_bytes = salidas[clave].to_csv(index=False, encoding="utf-8-sig")
            zf.writestr(nombre, csv_bytes)
        meta = {
            "resumen": salidas["resumen"],
            "avisos": salidas["avisos"],
            "sin_codigo": salidas["sin_codigo"],
            "columnas": salidas["columnas"],
        }
        zf.writestr(f"{stem}_resumen.json",
                    json.dumps(meta, ensure_ascii=False, indent=2))
    buf.seek(0)
    return buf
