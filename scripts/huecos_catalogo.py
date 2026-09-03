"""
Qué le falta al catálogo, con evidencia de la propia base.

    python scripts/huecos_catalogo.py base_conversacional.csv

Lee la salida `_conversacional.csv` de salidas_otbb.py y busca, entre los correos
que el motor NO marcó, los que probablemente deberían estar marcados. No cambia
nada ni propone reglas: lista candidatos ordenados por evidencia, para que quien
tiene el criterio lingüístico decida.

Dos señales, y la primera es la fuerte:

  1. TEXTO QUE SE REPITE ENTRE HILOS DISTINTOS
     Un cuerpo idéntico en 40 conversaciones que no tienen nada que ver entre sí
     no lo escribió una persona: es una plantilla. Esta señal no depende del
     idioma ni de adivinar palabras clave, y encuentra los no-reply, los avisos
     de fuera de oficina y las notificaciones que el catálogo todavía no cubre.

     Se cuentan HILOS, no mensajes. El mismo texto veinte veces dentro de una
     conversación es una cadena citada; en veinte conversaciones distintas es
     una plantilla.

  2. MENSAJES MUY CORTOS
     Un cuerpo de pocas palabras que no matcheó ninguna regla suele ser un acuse
     que al catálogo le falta —«Recibido», «Acuso recibo documento»—. Es una
     señal más débil: un mensaje corto también puede ser una petición real
     («La llamo Patricia»), así que hay que mirarlos uno por uno.

Las filas que el archivo trae repetidas se descartan de las dos señales: son un
defecto de la ingesta y contarlas infla todo.
"""

import argparse
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path

import pandas as pd

MIN_HILOS = 3          # plantillas: aparecer en menos hilos no es evidencia
MAX_CORTO = 45         # caracteres para considerar un mensaje "corto"
MIN_LARGO_TEXTO = 12   # por debajo de esto el texto no identifica una plantilla


def norma(t):
    t = unicodedata.normalize("NFD", str(t or "").lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", t).strip()


def col_texto(df):
    """
    Se mira el texto YA LIMPIO, no el original.

    Con el original, el banner institucional está en casi todos los correos
    —79.926 de 80.340 en la base de Santander— así que todas las "plantillas"
    que salen son el mismo banner y tapan lo que se está buscando. Y la señal de
    mensajes cortos queda inservible: ningún cuerpo mide menos de 45 caracteres
    si todos arrastran 215 de aviso de phishing.
    """
    for c in ("texto_para_prompt", "Texto_para_prompt"):
        if c in df.columns:
            return c
    sys.exit("este archivo no tiene texto_para_prompt: hay que pasarle la salida "
             f"_conversacional.csv de salidas_otbb.py. Columnas: {list(df.columns)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("conversacional", help="el archivo _conversacional.csv")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--salida", default=None, help="csv con los candidatos")
    args = ap.parse_args()

    ruta = Path(args.conversacional)
    if not ruta.exists():
        sys.exit(f"No existe: {ruta}")
    df = pd.read_csv(ruta, dtype=str, keep_default_na=False, low_memory=False)
    tx = col_texto(df)

    sanas = df[(df["excluido"].str.lower() != "true") &
               (df["fila_estado"] == "unico")].copy()
    print(f"{len(df)} correos | {len(sanas)} sin marcar y sin repetir")

    sanas["_n"] = sanas[tx].map(norma)
    grupos = defaultdict(lambda: {"hilos": set(), "n": 0, "ej": ""})
    for n, hilo, orig in zip(sanas["_n"], sanas.get("hilo", ""), sanas[tx]):
        if len(n) < MIN_LARGO_TEXTO:
            continue
        g = grupos[n]
        g["hilos"].add(hilo)
        g["n"] += 1
        g["ej"] = g["ej"] or orig

    plantillas = sorted(
        ({"hilos": len(g["hilos"]), "correos": g["n"], "chars": len(g["ej"]),
          "texto": re.sub(r"\s+", " ", g["ej"]).strip()}
         for g in grupos.values() if len(g["hilos"]) >= MIN_HILOS),
        key=lambda d: -d["hilos"])

    print(f"\n{'='*100}\n1. TEXTO IDÉNTICO EN VARIOS HILOS — candidatos a plantilla\n{'='*100}")
    if not plantillas:
        print("  (ninguno)")
    for p in plantillas[:args.top]:
        print(f"\n  {p['hilos']:5} hilos · {p['correos']:5} correos · {p['chars']:5} caracteres")
        print(f"        {p['texto'][:220]}")

    cortos = sanas[sanas[tx].str.len().between(1, MAX_CORTO)]
    conteo = (cortos.groupby(cortos[tx].map(norma))
              .agg(correos=(tx, "size"), ejemplo=(tx, "first"))
              .sort_values("correos", ascending=False))
    print(f"\n{'='*100}\n2. MENSAJES CORTOS SIN MARCAR — candidatos a acuse\n{'='*100}")
    print(f"  {len(cortos)} correos de hasta {MAX_CORTO} caracteres, "
          f"{len(conteo)} textos distintos\n")
    for _, r in conteo.head(args.top).iterrows():
        print(f"  {r['correos']:5}x  {re.sub(r'\s+', ' ', str(r['ejemplo'])).strip()[:70]!r}")

    if args.salida:
        filas = [{"señal": "plantilla", "hilos": p["hilos"], "correos": p["correos"],
                  "caracteres": p["chars"], "texto": p["texto"]} for p in plantillas]
        filas += [{"señal": "mensaje corto", "hilos": "", "correos": int(r["correos"]),
                   "caracteres": len(str(r["ejemplo"])),
                   "texto": re.sub(r"\s+", " ", str(r["ejemplo"])).strip()}
                  for _, r in conteo.iterrows()]
        pd.DataFrame(filas).to_csv(args.salida, index=False, encoding="utf-8-sig")
        print(f"\nguardado: {args.salida}")

    print("\nNinguna de las dos señales decide nada: un texto repetido puede ser una "
          "plantilla del banco que SÍ interesa analizar, y un mensaje corto puede ser "
          "una petición real. Hay que mirarlos.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
