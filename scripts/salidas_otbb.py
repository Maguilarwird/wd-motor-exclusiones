"""
Las cuatro salidas que consume el OTBB, de una sola corrida.

    python scripts/salidas_otbb.py base.csv --contexto santander,cl

El flujo del OTBB se bifurca, y cada rama necesita algo distinto del mismo
procesamiento. Por eso son cuatro archivos y no uno con banderas:

  _maestro.csv                  los correos que SÍ se analizan. Sin las
                                exclusiones y sin las filas que el archivo trae
                                repetidas. Es lo que se manda a categorizar, y
                                donde se ahorran los tokens.

  _conversacional.csv           todas las filas, en orden de hilo y turno, con
                                el texto limpio y la marca al lado. El análisis
                                del hilo necesita ver el "gracias" para saber
                                cómo se cerró la conversación.

  _excluidos.csv                la lista de lo excluido, correo por correo, con
                                hilo, turno, categoría, regla y evidencia. Para
                                poder responder "¿por qué salió éste?" y para
                                cruzar volumetría por conversación.

  _exclusiones_data_maestro.csv lo mismo agregado, en el formato exacto de la
                                hoja Data_maestro: Tipificación, N, %, y hasta
                                cinco ejemplos reales por tipificación. Se pega
                                sin transformar nada.

La etiqueta se agrega ACÁ, no en el motor. El motor conserva el texto siempre;
si reemplazara el cuerpo por la categoría, el maestro quedaría igual (descarta
la fila entera) y el análisis conversacional se quedaría sin nada que leer.
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from motor import columnas  # noqa: E402
from motor.exclusion_engine import ExclusionEngine, ErrorCatalogo, load_catalog  # noqa: E402
from motor.pipeline import procesar  # noqa: E402

RAIZ = Path(__file__).resolve().parents[2]
TIPIFICACIONES = RAIZ / "catalogos" / "tipificaciones.csv"

# El orden de columnas de la hoja Data_maestro, tal como viene. Se respeta
# exacto: si cambia el orden, la fila no se puede pegar.
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


def leer_tipificaciones(ruta=None):
    """(Categoría, Origen) -> código WM. Las pendientes quedan sin código."""
    ruta = Path(ruta or TIPIFICACIONES)
    if not ruta.exists():
        return {}
    with open(ruta, encoding="utf-8-sig", newline="") as fh:
        return {(r["Categoría"], r["Origen"]): r["Tipificación"].strip()
                for r in csv.DictReader(fh) if r.get("Tipificación", "").strip()}


def una_linea(texto, largo=LARGO_EJEMPLO):
    return re.sub(r"\s+", " ", str(texto or "")).strip()[:largo]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base", help="csv o xlsx con los correos")
    ap.add_argument("--contexto", default="", help="ej: santander,cl")
    ap.add_argument("--catalogo", default=None)
    ap.add_argument("--salida", default=None, help="carpeta (default: junto a la base)")
    ap.add_argument("--texto", default=None)
    ap.add_argument("--asunto", default=None)
    ap.add_argument("--hilo", default=None)
    ap.add_argument("--orden", dest="fecha", default=None)
    ap.add_argument("--remitente", default=None)
    ap.add_argument("--msgid", dest="msg_id", default=None)
    args = ap.parse_args()

    base = Path(args.base)
    if not base.exists():
        sys.exit(f"No existe: {base}")
    destino = Path(args.salida) if args.salida else base.parent
    destino.mkdir(parents=True, exist_ok=True)
    contexto = [t for t in args.contexto.split(",") if t.strip()]

    try:
        engine = ExclusionEngine(load_catalog(args.catalogo))
    except ErrorCatalogo as e:
        sys.exit(f"Catálogo inválido: {e}")

    df = columnas.leer_base(base)
    forzadas = {k: getattr(args, k) for k in
                ("texto", "asunto", "hilo", "fecha", "remitente", "msg_id")}
    col = columnas.detectar(df, forzadas)
    print(f"{len(engine.rules)} reglas | {len(df)} filas de {base.name}")
    print("columnas -> " + "  ".join(f"{k}={v!r}" for k, v in col.items()
                                     if not k.startswith("ref_")))
    if not col["hilo"]:
        print("AVISO: sin columna de hilo no hay turnos, ni dedup, ni insistencias.")

    r = procesar(columnas.mensajes_desde(df, col), contexto=contexto, engine=engine)
    m = pd.DataFrame(r["mensajes"])
    for c in ("perdedores", "bloques_removidos"):
        m[c] = m[c].map(lambda v: ", ".join(v) if isinstance(v, list) else v)

    # El motor devuelve los mensajes en el orden en que entraron, así que se
    # pegan por posición contra la base original.
    full = pd.concat([df.reset_index(drop=True),
                      m.drop(columns=["id"]).reset_index(drop=True)], axis=1)
    dup = full["fila_estado"].str.startswith("duplicado")
    exc = full["excluido"].astype(bool)

    # La marca se pone sobre lo que de verdad pasó. Una fila que el archivo trae
    # repetida sale vacía y SIN marca: ponerle la etiqueta haría leer dos
    # "gracias" donde hubo uno, y eso inventa un turno que nadie escribió.
    full["_marca"] = [
        "" if d else (f"[{c}] {t}".strip() if e else t)
        for d, e, c, t in zip(dup, exc, full["categoria"],
                              full["texto_para_prompt"].fillna(""))
    ]
    stem = base.stem

    # ── 1. maestro: lo que sí se analiza ──
    maestro = full[~exc & ~dup].drop(columns=["_marca"])
    p1 = destino / f"{stem}_maestro.csv"
    maestro.to_csv(p1, index=False, encoding="utf-8-sig")

    # ── 2. conversacional: todo, con marca ──
    conv = full.copy()
    conv["texto_para_prompt"] = conv["_marca"]
    conv = conv.drop(columns=["_marca"])
    if col["hilo"]:
        conv = conv.sort_values(["hilo", "posicion_en_hilo"], kind="stable")
    p2 = destino / f"{stem}_conversacional.csv"
    conv.to_csv(p2, index=False, encoding="utf-8-sig")

    # ── 3. excluidos: la lista, para responder por qué ──
    cols_exc = ["hilo", "posicion_en_hilo", "mensajes_en_hilo", "categoria",
                "origen", "regla_id", "texto_regla", "condicion", "evidencia",
                # `via` dice si lo encontró la búsqueda de texto o la
                # lematización. Es la única forma de ver qué aporta spaCy.
                "via",
                # ¿El mensaje cierra la conversación o espera algo más? Es
                # información, no decide nada. Ver motor/lemas.continuidad().
                "continua", "motivo_continuidad",
                "tratamiento", "accionable", "definicion", "fila_estado",
                "chars_original"]
    excl = full[exc].copy()
    if col["texto"]:
        excl["texto"] = excl[col["texto"]].map(una_linea)
        cols_exc.append("texto")
    p3 = destino / f"{stem}_excluidos.csv"
    excl[[c for c in cols_exc if c in excl.columns]].to_csv(
        p3, index=False, encoding="utf-8-sig")

    # ── 4. agregado en formato Data_maestro ──
    codigos = leer_tipificaciones()
    total = len(full)
    filas, sin_codigo = [], []
    for (cat, ori), g in excl.groupby(["categoria", "origen"], dropna=False):
        cod = codigos.get((cat, ori), "")
        if not cod:
            sin_codigo.append((cat, ori, len(g)))
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
                  (g[col["texto"]] if col["texto"] else g["texto_para_prompt"])]
        # Ejemplos distintos entre sí: cinco veces el mismo texto no es una muestra.
        unicos = list(dict.fromkeys(t for t in textos if t))[:MAX_EJEMPLOS]
        for i, t in enumerate(unicos, 1):
            fila[f"Ejemplo {i}"] = t
        filas.append(fila)
    filas.sort(key=lambda f: -f["N"])
    p4 = destino / f"{stem}_exclusiones_data_maestro.csv"
    pd.DataFrame(filas, columns=DATA_MAESTRO).to_csv(
        p4, index=False, encoding="utf-8-sig")

    res = r["resumen"]
    print("\n=== RESUMEN ===")
    print(f"correos en la base            {total}")
    print(f"  exclusiones marcadas        {int(exc.sum())}  ({exc.mean()*100:.1f}%)")
    print(f"  filas repetidas del archivo {int(dup.sum())}  ({dup.mean()*100:.1f}%)")
    print(f"  van al maestro              {len(maestro)}  ({len(maestro)/total*100:.1f}%)")
    print(f"hilos                         {res['hilos']}")
    print(f"banners removidos             {res['banners_removidos']}")
    print(f"insistencias                  {res['insistencias']} en "
          f"{res['hilos_con_insistencia']} hilos")
    print(f"texto: {res['chars_original']:,} -> {res['chars_para_prompt']:,} caracteres "
          f"({res['ahorro_pct']}% menos)")

    if sin_codigo:
        print(f"\nAVISO: {len(sin_codigo)} tipificación(es) sin código WM. La columna "
              "Tipificación va vacía y la fila no se puede pegar todavía:")
        for cat, ori, n in sorted(sin_codigo, key=lambda x: -x[2]):
            print(f"  {n:5} correos  {cat}  |  {ori}")
        print(f"  → completar en {TIPIFICACIONES.name}")

    for p in (p1, p2, p3, p4):
        print(f"\n{p}")

    if res["alertas_limpieza"]:
        print(f"\nFALLA: en {res['alertas_limpieza']} filas la limpieza dejó el "
              "mensaje casi vacío.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
