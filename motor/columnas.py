"""
Cómo se reconoce una base de correos, en un solo lugar.

Cada script que lee una base tiene que adivinar qué columna trae el cuerpo, el
asunto y el hilo. Cuando cada uno lleva su propia lista, agregar un nombre nuevo
en un script y no en otro hace que la misma base dé resultados distintos según
por dónde entre. Acá viven las listas y la lectura del archivo.

El orden de cada lista es de preferencia: la primera que exista en la base gana.
"""

from pathlib import Path

import pandas as pd

TEXTO = ["text clean", "texto", "Texto", "Content", "combined_msg",
         "bodyAnonymized", "body", "mensaje", "Texto-anonimizado"]
ASUNTO = ["subjectAnonymized", "Subject", "asunto", "Asunto"]
REMITENTE = ["from", "From", "remitente", "Remitente", "sender", "From Email"]
HILO = ["Thread Id", "thread_id", "hilo", "Hilo", "conversationId"]
FECHA = ["Date", "fecha", "Fecha", "sentDateTime", "Date Cl"]
MSGID = ["Message ID", "message_id", "msg_id", "internetMessageId"]

# La marca de referencia, cuando la base viene etiquetada por otro sistema y se
# quiere comparar contra lo que decide el motor.
REF_ES_EXCLUSION = ["Es Exclusion", "Es Exclusión", "es_exclusion",
                    "Is Excluded", "Exclusion", "Exclusión"]
REF_CATEGORIA = ["Categoria Exclusion", "Categoría Exclusión",
                 "categoria_exclusion", "Subcategoría", "Subcategoria"]

CAMPOS = {
    "texto": TEXTO, "asunto": ASUNTO, "remitente": REMITENTE,
    "hilo": HILO, "fecha": FECHA, "msg_id": MSGID,
    "ref_excluido": REF_ES_EXCLUSION, "ref_categoria": REF_CATEGORIA,
}


def buscar(df, candidatas, forzada=None):
    """La primera candidata que exista en la base, o `forzada` si se pidió."""
    if forzada:
        if forzada not in df.columns:
            raise KeyError(f"la columna {forzada!r} no está en la base. "
                           f"Hay: {list(df.columns)}")
        return forzada
    for c in candidatas:
        if c in df.columns:
            return c
    return None


def detectar(df, forzadas=None):
    """{campo: nombre_de_columna_o_None} para todos los campos conocidos."""
    forzadas = forzadas or {}
    return {k: buscar(df, v, forzadas.get(k)) for k, v in CAMPOS.items()}


def leer_base(ruta):
    """
    Lee CSV o XLSX. Todo como texto y sin convertir vacíos a NaN: una base de
    correos no tiene números que calcular, y dejar que pandas infiera tipos
    convierte un RUT o un folio en float y le agrega un '.0'.
    """
    ruta = Path(ruta)
    if ruta.suffix.lower() in (".csv", ".txt"):
        return pd.read_csv(ruta, dtype=str, keep_default_na=False,
                           low_memory=False)
    return pd.read_excel(ruta, dtype=str).fillna("")


def mensajes_desde(df, col):
    """Las filas de la base como los dicts que espera motor.pipeline.procesar."""
    def celda(row, clave):
        c = col.get(clave)
        if not c:
            return ""
        v = row.get(c, "")
        return "" if v is None or pd.isna(v) else str(v)

    primera = df.columns[0]
    return [{
        "id": str(row.get(primera, i)),
        "texto": celda(row, "texto"),
        "asunto": celda(row, "asunto"),
        "hilo": celda(row, "hilo"),
        "fecha": celda(row, "fecha"),
        "remitente": celda(row, "remitente"),
        "msg_id": celda(row, "msg_id"),
    } for i, row in enumerate(df.to_dict("records"))]
