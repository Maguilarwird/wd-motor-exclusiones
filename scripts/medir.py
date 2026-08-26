"""
Mide, sobre UNA base concreta, cuánto texto se le ahorra a la generativa y
cuánto aporta cada etapa por separado.

    python scripts/medir.py base.xlsx --texto "text clean"
    python scripts/medir.py base.csv  --texto combined_msg --hilo "Thread Id" --orden Date
    python scripts/medir.py base.xlsx --contexto santander,cl

No entrega un número fijo: entrega el número de ESTA base. Ese es el punto —
un porcentaje en una lámina se discute, un comando que corre sobre los datos de
quien pregunta, no.

Advertencias que emite solo si corresponden, porque callarlas infla el resultado:
  - filas duplicadas literales (no es ahorro, es un defecto del archivo)
  - texto que ya viene limpio (la limpieza de banners no puede aportar nada)
  - falta de hilos (la deduplicación de historial no se puede medir)
"""

import argparse
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from motor.exclusion_engine import ExclusionEngine, load_catalog, normalize  # noqa: E402
from motor.deduplicacion import (  # noqa: E402
    deduplicar_hilo, deduplicar_interno, estimar_tokens,
)

TEXTO_CANDIDATAS = ["text clean", "combined_msg", "Texto", "texto", "Content",
                    "body", "bodyAnonymized", "Texto-anonimizado", "mensaje"]
HILO_CANDIDATAS = ["Thread Id", "thread_id", "ThreadId", "Conversation ID", "In Reply To"]
ORDEN_CANDIDATAS = ["Date", "date_email", "Fecha", "fecha"]
REMITENTE_CANDIDATAS = ["from", "From", "remitente", "sender"]


def leer(ruta):
    import pandas as pd
    ruta = Path(ruta)
    if ruta.suffix.lower() in (".csv", ".tsv"):
        # csv.reader vía pandas con engine python: los CSV de correo traen
        # saltos de línea dentro de los campos y comillas desbalanceadas.
        sep = "\t" if ruta.suffix.lower() == ".tsv" else ","
        return pd.read_csv(ruta, sep=sep, engine="python", dtype=str, keep_default_na=False)
    return pd.read_excel(ruta, dtype=str).fillna("")


def elegir(df, explicito, candidatas):
    if explicito:
        if explicito not in df.columns:
            sys.exit(f"La columna {explicito!r} no existe. Hay: {list(df.columns)[:15]}")
        return explicito
    for c in candidatas:
        if c in df.columns:
            return c
    return None


def fila(nombre, chars, base_chars):
    tok = estimar_tokens_int(chars)
    ahorro = (base_chars - chars) / base_chars * 100 if base_chars else 0
    return f"{nombre:<34}{chars:>10,}{tok:>12,}{ahorro:>10.1f}%"


def estimar_tokens_int(chars):
    return int(chars / 4)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base")
    ap.add_argument("--texto", default=None)
    ap.add_argument("--hilo", default=None)
    ap.add_argument("--orden", default=None)
    ap.add_argument("--remitente", default=None)
    ap.add_argument("--contexto", default="")
    ap.add_argument("--sin-duplicados", action="store_true",
                    help="descarta filas de texto duplicado literal antes de medir, "
                         "y reporta el número sobre base sana")
    ap.add_argument("--catalogo", default=None)
    args = ap.parse_args()

    contexto = [t for t in args.contexto.split(",") if t.strip()]
    df = leer(args.base)

    col_texto = elegir(df, args.texto, TEXTO_CANDIDATAS)
    if not col_texto:
        sys.exit(f"No encuentro columna de texto. Usa --texto. Hay: {list(df.columns)[:15]}")
    col_hilo = elegir(df, args.hilo, HILO_CANDIDATAS)
    col_orden = elegir(df, args.orden, ORDEN_CANDIDATAS)
    col_remit = elegir(df, args.remitente, REMITENTE_CANDIDATAS)

    if args.sin_duplicados:
        clave = df[col_texto].map(normalize)
        if col_hilo:
            clave = df[col_hilo].astype(str) + "||" + clave
        antes = len(df)
        df = df[~clave.duplicated()].reset_index(drop=True)
        print(f"\nBase sana: se descartaron {antes - len(df)} filas duplicadas "
              f"de {antes}. El resultado de abajo NO incluye ese ahorro.")

    textos = [str(t) for t in df[col_texto].tolist()]
    n = len(textos)

    print(f"\nBase           {Path(args.base).name}")
    print(f"Mensajes       {n:,}")
    print(f"Columna texto  {col_texto!r}")
    print(f"Columna hilo   {col_hilo!r}" + ("" if col_hilo else "   <-- sin hilos"))
    print(f"Contexto       {contexto or ['todos']}")

    avisos = []

    # ── Sanidad del archivo ──
    dups = n - len({normalize(t) for t in textos})
    if dups:
        avisos.append(
            f"{dups} de {n} filas ({dups/n*100:.1f}%) son texto duplicado literal. "
            "Quitarlas no es ahorro, es tapar un defecto del archivo. "
            "El cálculo de abajo NO las quita."
        )
    vacios = sum(1 for t in textos if not t.strip())
    if vacios:
        avisos.append(f"{vacios} filas con texto vacío.")

    engine = ExclusionEngine(load_catalog(args.catalogo))

    # ── Etapa 0: crudo ──
    crudo = sum(len(t) for t in textos)

    # ── Etapa 1: limpieza de bloques institucionales ──
    limpios, alertas, bloques = [], 0, Counter()
    for t in textos:
        r = engine.analizar(t, "", contexto)
        limpios.append(r["texto_limpio"])
        alertas += bool(r["alerta_limpieza"])
        bloques.update(r["bloques_removidos"])
    tras_limpieza = sum(len(t) for t in limpios)
    if tras_limpieza >= crudo * 0.999:
        avisos.append(
            "La limpieza de bloques no quitó nada. O la base ya viene procesada, "
            "o faltan los banners de este cliente en catalogos/banners.yaml. "
            "No es que la limpieza no sirva: es que acá no tiene nada que hacer."
        )

    # ── Etapa 2: deduplicación interna ──
    internos = [deduplicar_interno(t) for t in limpios]
    tras_interno = sum(len(t) for t in internos)

    # ── Etapa 3: deduplicación de historial citado ──
    if col_hilo:
        idx = list(range(n))
        if col_orden:
            idx.sort(key=lambda i: (str(df[col_hilo].iloc[i]), str(df[col_orden].iloc[i])))
        else:
            idx.sort(key=lambda i: str(df[col_hilo].iloc[i]))
        por_hilo = defaultdict(list)
        for i in idx:
            por_hilo[str(df[col_hilo].iloc[i])].append(i)
        dedup = list(internos)
        for _, ids in por_hilo.items():
            for i, texto in zip(ids, deduplicar_hilo([internos[i] for i in ids])):
                dedup[i] = texto
        tras_hilo = sum(len(t) for t in dedup)
        n_hilos = len(por_hilo)
    else:
        dedup, tras_hilo, n_hilos = internos, tras_interno, 0
        avisos.append(
            "Sin columna de hilo no se puede medir la deduplicación de historial "
            "citado, que es la etapa de mayor retorno. Hace falta una base con "
            "Thread Id o In Reply To."
        )

    # ── Etapa 4: exclusiones ──
    finales, cats, solapes = [], Counter(), 0
    for i, t in enumerate(dedup):
        remit = str(df[col_remit].iloc[i]) if col_remit else ""
        r = engine.analizar(textos[i], remit, contexto)
        if r["excluido"]:
            cats[r["categoria"]] += 1
            finales.append(f"[{r['categoria']}]")
        else:
            finales.append(t)
        solapes += bool(r["perdedores"])
    tras_excl = sum(len(t) for t in finales)

    # ── Informe ──
    print("\n" + "=" * 68)
    print(f"{'ETAPA':<34}{'CHARS':>10}{'TOKENS~':>12}{'ACUM.':>11}")
    print("-" * 68)
    print(fila("0. Crudo", crudo, crudo))
    print(fila("1. + limpieza de bloques", tras_limpieza, crudo))
    print(fila("2. + dedup dentro del mensaje", tras_interno, crudo))
    if col_hilo:
        print(fila("3. + dedup de historial citado", tras_hilo, crudo))
    print(fila("4. + exclusiones del catálogo", tras_excl, crudo))
    print("=" * 68)

    print("\nAPORTE DE CADA ETAPA (sobre el crudo)")
    etapas = [("limpieza de bloques", crudo - tras_limpieza),
              ("dedup dentro del mensaje", tras_limpieza - tras_interno)]
    if col_hilo:
        etapas.append(("dedup de historial citado", tras_interno - tras_hilo))
    etapas.append(("exclusiones del catálogo", tras_hilo - tras_excl))
    for nombre, delta in sorted(etapas, key=lambda x: -x[1]):
        print(f"  {nombre:<32}{delta/crudo*100:>7.1f}%")
    print(f"  {'TOTAL':<32}{(crudo-tras_excl)/crudo*100:>7.1f}%")

    print(f"\nEXCLUSIONES   {sum(cats.values()):,} de {n:,} mensajes "
          f"({sum(cats.values())/n*100:.1f}%)")
    for c, k in cats.most_common(12):
        print(f"  {k:>5}  {c}")
    if solapes:
        print(f"\nSolapamientos: {solapes} mensajes donde más de una regla matcheó "
              "(ver columna Perdedores)")
    if bloques:
        print("\nBloques institucionales removidos:")
        for b, k in bloques.most_common():
            print(f"  {k:>5}  {b}")
    if alertas:
        print(f"\nALERTA: {alertas} mensajes donde la limpieza dejó el texto casi "
              "vacío. Se usó el crudo. Hay un banner mal clasificado.")
    if n_hilos:
        print(f"\nHilos: {n_hilos:,} | promedio {n/n_hilos:.1f} mensajes por hilo")

    if avisos:
        print("\nADVERTENCIAS")
        for a in avisos:
            print(f"  - {a}")
    print()


if __name__ == "__main__":
    main()
