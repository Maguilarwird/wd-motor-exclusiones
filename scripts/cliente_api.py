"""
Cliente del servicio HTTP: manda una base entera al motor y guarda el resultado.

    python scripts/cliente_api.py base.csv  --contexto santander,cl
    python scripts/cliente_api.py base.xlsx --url http://192.168.1.50:8000

Existe porque procesar_exclusiones.py corre el motor dentro del proceso, y eso
sirve para desarrollar, no para verificar lo que está desplegado. Acá se prueba
el mismo catálogo, la misma versión y la misma API key que va a usar el OTBB.

Diferencias con procesar_exclusiones.py, a propósito:
  - acepta CSV además de XLSX. La mayoría de las bases llegan en CSV y
    convertir a mano antes de cada corrida es una fuente de errores.
  - parte el envío en lotes SIN CORTAR UN HILO. El servicio deduplica contra
    los mensajes anteriores del hilo: si la mitad del hilo va en otra llamada,
    esa mitad no tiene con qué compararse y el ahorro se reporta de menos.

La API key NUNCA se pasa por argumento: sale de --key-file o de la variable
API_KEY. Un argumento queda en el historial del shell.
"""

import argparse
import json
import os
import sys
from pathlib import Path

import pandas as pd
import requests

# Las mismas candidatas que procesar_exclusiones.py: si un día se agrega una
# columna nueva, tiene que valer para las dos vías o dan resultados distintos.
TEXT_COLUMNS = ["text clean", "texto", "Texto", "Content", "combined_msg",
                "bodyAnonymized", "body", "mensaje", "Texto-anonimizado"]
SUBJECT_COLUMNS = ["subjectAnonymized", "Subject", "asunto", "Asunto"]
SENDER_COLUMNS = ["from", "From", "remitente", "Remitente", "sender"]
THREAD_COLUMNS = ["Thread Id", "thread_id", "hilo", "Hilo", "conversationId"]
DATE_COLUMNS = ["Date", "fecha", "Fecha", "sentDateTime"]
MSGID_COLUMNS = ["Message ID", "message_id", "msg_id", "internetMessageId"]


def find_column(df, candidates, forzada=None):
    if forzada:
        if forzada not in df.columns:
            sys.exit(f"La columna {forzada!r} no está en la base. Hay: {list(df.columns)}")
        return forzada
    for c in candidates:
        if c in df.columns:
            return c
    return None


def leer_base(ruta):
    if ruta.suffix.lower() in (".csv", ".txt"):
        return pd.read_csv(ruta, dtype=str, keep_default_na=False)
    return pd.read_excel(ruta, dtype=str).fillna("")


def leer_key(key_file):
    if key_file:
        return Path(key_file).read_text(encoding="utf-8").strip()
    key = os.environ.get("API_KEY", "").strip()
    if not key:
        sys.exit("Sin API key. Pasá --key-file RUTA o exportá API_KEY. "
                 "No la pases por argumento: queda en el historial del shell.")
    return key


def lotes_por_hilo(mensajes, maximo):
    """
    Corta en lotes de a lo más `maximo` mensajes, sin partir un hilo.

    Agrupar por hilo REORDENA los mensajes, así que cada uno viaja con su
    posición original en la base. El servicio devuelve el lote en el mismo
    orden en que lo recibió; sin la posición no habría cómo volver a alinear
    el resultado con las filas del archivo, y el id de la base puede venir
    repetido, así que no sirve como llave.

    Un hilo más grande que el máximo se manda igual, entero, y el servicio
    devolverá 413. Es lo correcto: partirlo en silencio daría un resultado
    que parece bien y no lo está.
    """
    grupos = {}
    for i, m in enumerate(mensajes):
        grupos.setdefault(m["hilo"] or f"__solo_{i}__", []).append((i, m))

    lotes, actual = [], []
    for pares in grupos.values():
        if actual and len(actual) + len(pares) > maximo:
            lotes.append(actual)
            actual = []
        actual.extend(pares)
    if actual:
        lotes.append(actual)
    return lotes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base", help="csv o xlsx con los correos")
    ap.add_argument("--url", default=os.environ.get("MOTOR_URL", "http://localhost:8000"))
    ap.add_argument("--key-file", default=None, help="archivo con la API key")
    ap.add_argument("--contexto", default="", help="tokens de alcance, ej: santander,cl")
    ap.add_argument("--texto", default=None)
    ap.add_argument("--asunto", default=None)
    ap.add_argument("--hilo", default=None)
    ap.add_argument("--orden", default=None)
    ap.add_argument("--remitente", default=None)
    ap.add_argument("--msgid", default=None)
    ap.add_argument("--sin-asunto", action="store_true",
                    help="no anteponer el asunto al cuerpo antes de evaluar")
    ap.add_argument("--max-lote", type=int, default=5000)
    ap.add_argument("--timeout", type=int, default=300)
    args = ap.parse_args()

    base = Path(args.base)
    if not base.exists():
        sys.exit(f"No existe: {base}")

    key = leer_key(args.key_file)
    contexto = [t for t in args.contexto.split(",") if t.strip()]

    # Antes de mandar nada: ¿el servicio está vivo y con qué catálogo?
    try:
        h = requests.get(f"{args.url}/health", timeout=10)
    except requests.RequestException as e:
        sys.exit(f"No se pudo hablar con {args.url}: {e}")
    if h.status_code != 200:
        sys.exit(f"El servicio no está sano: {h.status_code} {h.text}")
    salud = h.json()
    print(f"{args.url} | {salud['reglas_cargadas']} reglas | "
          f"catálogo {salud['catalogo']} | v{salud['version']}")

    df = leer_base(base)
    print(f"{len(df)} filas leídas de {base.name}")

    col = {
        "texto": find_column(df, TEXT_COLUMNS, args.texto),
        "asunto": None if args.sin_asunto else find_column(df, SUBJECT_COLUMNS, args.asunto),
        "hilo": find_column(df, THREAD_COLUMNS, args.hilo),
        "fecha": find_column(df, DATE_COLUMNS, args.orden),
        "remitente": find_column(df, SENDER_COLUMNS, args.remitente),
        "msg_id": find_column(df, MSGID_COLUMNS, args.msgid),
    }
    if not col["texto"] and not col["asunto"]:
        sys.exit(f"Ninguna columna de texto reconocida. Hay: {list(df.columns)}")
    print("columnas -> " + "  ".join(f"{k}={v!r}" for k, v in col.items()))

    # Sin hilo el servicio no puede deduplicar ni detectar insistencia, que son
    # las dos etapas de mayor retorno. Vale la pena decirlo antes, no después.
    if not col["hilo"]:
        print("AVISO: sin columna de hilo no hay dedup de historial ni insistencia.")

    def celda(row, clave):
        c = col[clave]
        if not c:
            return ""
        v = row.get(c, "")
        return "" if pd.isna(v) else str(v)

    mensajes = [{
        "id": str(row.get(df.columns[0], i)),
        "texto": celda(row, "texto"),
        "asunto": celda(row, "asunto"),
        "hilo": celda(row, "hilo"),
        "fecha": celda(row, "fecha"),
        "remitente": celda(row, "remitente"),
        "msg_id": celda(row, "msg_id"),
    } for i, row in enumerate(df.to_dict("records"))]

    lotes = lotes_por_hilo(mensajes, args.max_lote)
    print(f"{len(lotes)} llamada(s), sin partir hilos")

    salida = [None] * len(mensajes)
    avisos, resumenes = [], []
    for n, lote in enumerate(lotes, 1):
        posiciones = [p for p, _ in lote]
        r = requests.post(f"{args.url}/procesar-lote",
                          headers={"x-api-key": key},
                          json={"mensajes": [m for _, m in lote], "contexto": contexto},
                          timeout=args.timeout)
        if r.status_code != 200:
            sys.exit(f"lote {n}: HTTP {r.status_code} {r.text[:400]}")
        d = r.json()
        if len(d["mensajes"]) != len(posiciones):
            sys.exit(f"lote {n}: el servicio devolvió {len(d['mensajes'])} mensajes "
                     f"para {len(posiciones)} enviados. No se puede alinear.")
        for p, m in zip(posiciones, d["mensajes"]):
            salida[p] = m
        avisos.extend(d["avisos"])
        resumenes.append(d["resumen"])
        print(f"  lote {n}/{len(lotes)}: {len(lote)} mensajes ok")

    # Sumar los resúmenes parciales. Los porcentajes se recalculan sobre los
    # totales: promediar porcentajes de lotes de distinto tamaño da un número
    # que no es el de la base.
    tot = {k: sum(x.get(k) or 0 for x in resumenes) for k in (
        "mensajes", "hilos", "chars_original", "chars_para_prompt",
        "tokens_original", "tokens_para_prompt", "excluidos",
        "duplicados_archivo", "insistencias", "reenvios_identicos",
        "hilos_con_insistencia", "solapamientos", "alertas_limpieza")}
    sanos = [s for s in salida if not s["fila_estado"].startswith("duplicado")]
    crudo_sano = sum(s["chars_original"] for s in sanos)
    final_sano = sum(s["chars_para_prompt"] for s in sanos)
    tot["ahorro_pct"] = (round((tot["chars_original"] - tot["chars_para_prompt"])
                               / tot["chars_original"] * 100, 1)
                         if tot["chars_original"] else 0.0)
    tot["ahorro_pct_sin_duplicados"] = (round((crudo_sano - final_sano) / crudo_sano * 100, 1)
                                        if crudo_sano else 0.0)

    print("\n=== RESUMEN ===")
    for k, v in tot.items():
        print(f"{k:30} {v}")
    if avisos:
        print("\n=== AVISOS ===")
        for a in dict.fromkeys(avisos):
            print(" -", a)

    res = pd.DataFrame(salida)
    for c in ("perdedores", "bloques_removidos"):
        res[c] = res[c].map(lambda v: ", ".join(v) if isinstance(v, list) else v)

    # Se pega por posición, no por id: el servicio devuelve los mensajes en el
    # mismo orden en que se mandaron y el id puede venir repetido en la base.
    final = pd.concat([df.reset_index(drop=True),
                       res.drop(columns=["id"]).reset_index(drop=True)], axis=1)

    destino = base.parent / f"{base.stem}_API.csv"
    final.to_csv(destino, index=False, encoding="utf-8-sig")
    with open(base.parent / f"{base.stem}_API_resumen.json", "w", encoding="utf-8") as fh:
        json.dump({"resumen": tot, "avisos": list(dict.fromkeys(avisos))},
                  fh, ensure_ascii=False, indent=2)
    print(f"\nGuardado: {destino}")

    if tot["alertas_limpieza"]:
        print(f"\nFALLA: en {tot['alertas_limpieza']} filas la limpieza dejó el "
              "mensaje casi vacío. Hay un banner mal clasificado.")
        sys.exit(1)


if __name__ == "__main__":
    main()
