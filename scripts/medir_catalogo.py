# -*- coding: utf-8 -*-
"""
Mide un catálogo candidato contra un set validado a mano.

    python scripts/medir_catalogo.py base.xlsx --validado validado.xlsx \
        --catalogo catalogos/exclusiones.csv --texto Content --contexto cliente,cl

Responde tres preguntas, y la tercera es la que evita el desastre:

  - de los ERRORES marcados a mano, ¿cuántos arregla el catálogo nuevo?
  - de los ACIERTOS marcados a mano, ¿cuántos pierde?      <- el costo
  - ¿qué marcas NUEVAS aparecen?                           <- nadie las revisó

Sin esto, cambiar una regla es apostar. Una regla que arregla 20 errores y de
paso pierde 30 aciertos parece una mejora mirando sólo la primera cifra: el
motor marca menos y el número de exclusiones baja. Por eso las tres se imprimen
juntas y el costo va con una flecha.

## El archivo de validación

**No está en este repo y no puede estar**: trae el texto real de los correos.
Se pasa por `--validado`. Es la salida `_excluidos.csv` de una corrida, con una
columna agregada a mano:

    texto           el cuerpo del correo (viene de la salida)
    categoria       la categoría que asignó el motor (viene de la salida)
    regla_id        la regla que la asignó (viene de la salida)
    Validación Jo   S = la exclusión está bien · N = está mal
    Mejorar         opcional, texto libre: qué habría que corregir

El apareo entre el set validado y la corrida nueva se hace por **huella del
texto**, no por número de fila: el orden cambia entre corridas porque el
pipeline agrupa por hilo. La huella baja a minúsculas, saca tildes y
puntuación, y corta a 120 caracteres.

## Por qué el nombre de la columna de texto se pasa por argumento

Hay bases que traen dos columnas de texto —una cruda y otra con el asunto ya
pegado adelante—. Si el motor elige la equivocada, mide el asunto dos veces y
todos los números salen mal sin que nada falle. Con `--texto` se fija cuál es.
"""
import argparse
import re
import sys
import unicodedata
from pathlib import Path

import pandas as pd

# Este script vive en scripts/, y el motor está un nivel arriba.
RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from motor import columnas                                         # noqa: E402
from motor.exclusion_engine import ExclusionEngine, load_catalog    # noqa: E402
from motor.pipeline import procesar                                 # noqa: E402

# La consola de Windows va en cp1252 y no puede imprimir todo lo que trae un
# correo real. Sin esto el script se cae justo al mostrar los casos que hay
# que revisar, que es lo único que uno vino a mirar.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

# Cc = caracteres de control. Cf = formato invisible, ahí caen el BOM y las
# marcas de dirección del texto. Zl y Zp = separadores de línea y de párrafo.
# Se sacan por categoría y no con una lista de escapes: la lista siempre se
# queda corta con correos reales, y un escape mal copiado mete el carácter
# literal en el código fuente.
INVISIBLES = {"Cc", "Cf", "Zl", "Zp"}


def visible(t, n=88):
    """El texto de un correo, en una línea y sin lo que rompe la consola."""
    limpio = "".join(
        " " if unicodedata.category(c) in INVISIBLES else c
        for c in str(t or "")
    )
    return " ".join(limpio.split())[:n]


def clave(t):
    """Huella del texto, para aparear el set validado con una corrida nueva."""
    t = unicodedata.normalize("NFD", str(t or "").lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9 ]", "", re.sub(r"\s+", " ", t)).strip()[:120]


def correr(base, catalogo, col_texto, contexto):
    engine = ExclusionEngine(load_catalog(catalogo))
    df = columnas.leer_base(base)
    col = columnas.detectar(df, {"texto": col_texto} if col_texto else None)
    r = procesar(columnas.mensajes_desde(df, col), contexto=contexto, engine=engine)
    m = pd.DataFrame(r["mensajes"])
    m["_k"] = df[col["texto"]].reset_index(drop=True).map(clave)
    return len(engine.rules), m[m.excluido].copy()


def main():
    p = argparse.ArgumentParser(
        description="Mide un catálogo candidato contra un set validado a mano.")
    p.add_argument("base", help="la base de correos a procesar")
    p.add_argument("--validado", required=True,
                   help="xlsx/csv con la columna de validación S/N")
    p.add_argument("--catalogo", default=None,
                   help="catálogo candidato. Por defecto, el del repo")
    p.add_argument("--texto", default=None,
                   help="nombre de la columna de texto, si hay más de una")
    p.add_argument("--contexto", default="",
                   help="tokens de alcance separados por coma, ej: cliente,cl")
    p.add_argument("--col-validacion", default="Validación Jo",
                   help="columna con S/N en el archivo validado")
    a = p.parse_args()

    contexto = [t.strip() for t in a.contexto.split(",") if t.strip()]

    lector = pd.read_excel if str(a.validado).lower().endswith(
        (".xlsx", ".xlsm", ".xls")) else pd.read_csv
    v = lector(a.validado, dtype=str).fillna("")
    if a.col_validacion not in v.columns:
        p.error(f"el archivo validado no tiene la columna {a.col_validacion!r}. "
                f"Tiene: {', '.join(v.columns[:12])}")
    # "D" era "dudoso" y se resolvió como acierto en la validación original.
    v["V"] = v[a.col_validacion].str.strip().str.upper().replace({"D": "S"})
    v["_k"] = v["texto"].map(clave)
    buenas = set(v[v.V == "S"]._k) - {""}
    malas = set(v[v.V == "N"]._k) - {""}
    if not buenas and not malas:
        p.error("no se leyó ninguna etiqueta S/N: revisá --col-validacion")

    n_reglas, exc = correr(a.base, a.catalogo, a.texto, contexto)
    marcadas = set(exc._k) - {""}

    arregladas = malas - marcadas
    siguen_mal = malas & marcadas
    conservadas = buenas & marcadas
    perdidas = buenas - marcadas
    nuevas = marcadas - buenas - malas

    print(f"catálogo: {n_reglas} reglas | exclusiones: {len(exc)}")
    print()
    print(f"  errores arreglados             {len(arregladas):5} de {len(malas)}")
    print(f"  errores que siguen             {len(siguen_mal):5}")
    print(f"  aciertos conservados           {len(conservadas):5} de {len(buenas)}")
    print(f"  aciertos PERDIDOS              {len(perdidas):5}   <- el costo")
    print(f"  marcas nuevas sin etiquetar    {len(nuevas):5}   <- revisar")
    tot = len(conservadas) + len(siguen_mal)
    if tot:
        print(f"\n  precisión sobre lo etiquetado: {len(conservadas) / tot * 100:.1f}%")

    if perdidas:
        print("\n=== ACIERTOS PERDIDOS (los que dejó de marcar) ===")
        for k in list(perdidas)[:10]:
            f = v[v._k == k].iloc[0]
            print(f"  [{f.get('categoria', '')}] {f.get('regla_id', '')}  {visible(f.texto)}")
    if nuevas:
        print("\n=== MARCAS NUEVAS (no estaban en la validación) ===")
        for k in list(nuevas)[:12]:
            f = exc[exc._k == k].iloc[0]
            print(f"  [{f.categoria}] {f.regla_id} via={f.get('via', '')}  {visible(k, 80)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
