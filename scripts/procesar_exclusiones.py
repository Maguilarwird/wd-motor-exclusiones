"""
Procesa una base de correos contra el catálogo de exclusiones.

    python scripts/procesar_exclusiones.py <base.xlsx> [--catalogo RUTA]
                                                       [--contexto santander,cl]
                                                       [--hoja Exclusiones]

Ninguna ruta de cliente vive dentro del script: la base se pasa por argumento
y el catálogo se resuelve por --catalogo, por la variable de entorno
CATALOGO_EXCLUSIONES, o por el default del repo.

La salida se escribe al lado de la base de entrada. Nunca sobreescribe el
archivo original y nunca borra columnas: sólo agrega.
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from motor.exclusion_engine import (  # noqa: E402
    ExclusionEngine, load_catalog, ErrorCatalogo,
)
from motor.deduplicacion import deduplicar_hilo  # noqa: E402
from motor.hilos import clasificar_hilo  # noqa: E402

TEXT_COLUMNS = ["text clean", "texto", "Texto", "Content", "combined_msg",
                "bodyAnonymized", "body", "mensaje", "Texto-anonimizado"]
SUBJECT_COLUMNS = ["subjectAnonymized", "Subject", "asunto", "Asunto"]
SENDER_COLUMNS = ["from", "From", "remitente", "Remitente", "sender"]


def find_column(df, candidates):
    for c in candidates:
        if c in df.columns:
            return c
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base", help="xlsx con los correos")
    ap.add_argument("--catalogo", default=None)
    ap.add_argument("--hoja", default="Exclusiones")
    ap.add_argument("--contexto", default="",
                    help="tokens de alcance separados por coma, ej: santander,cl")
    ap.add_argument("--hilo", default=None, help='columna de hilo, ej: "Thread Id"')
    ap.add_argument("--orden", default=None, help="columna de fecha, para ordenar el hilo")
    ap.add_argument("--msgid", default=None, help="columna de Message ID")
    args = ap.parse_args()

    base = Path(args.base)
    if not base.exists():
        sys.exit(f"No existe: {base}")

    contexto = [t for t in args.contexto.split(",") if t.strip()]

    try:
        reglas = load_catalog(args.catalogo, sheet=args.hoja)
        engine = ExclusionEngine(reglas)
    except ErrorCatalogo as e:
        sys.exit(f"Catálogo inválido: {e}")
    print(f"{len(reglas)} reglas compiladas | contexto: {contexto or ['todos']}")

    df = pd.read_excel(base)
    print(f"{len(df)} filas leídas de {base.name}")

    text_col = find_column(df, TEXT_COLUMNS)
    subj_col = find_column(df, SUBJECT_COLUMNS)
    sender_col = find_column(df, SENDER_COLUMNS)
    if not text_col and not subj_col:
        text_col = df.columns[0]
        print(f"AVISO: ninguna columna de texto conocida, uso {text_col!r}")
    print(f"texto={text_col!r} asunto={subj_col!r} remitente={sender_col!r}")

    # Sanidad del archivo: si muchas filas tienen la primera columna vacía,
    # el archivo probablemente está mal cortado. Avisar en vez de procesar.
    vacias = df[df.columns[0]].isna().mean()
    if vacias > 0.2:
        print(f"AVISO: {vacias:.0%} de las filas tienen la primera columna vacía. "
              "Revisa si el archivo está mal cortado antes de creerle a las métricas.")

    # ── capa de hilo ──
    col_hilo = args.hilo if args.hilo in df.columns else None
    col_msgid = args.msgid if args.msgid in df.columns else None
    col_orden = args.orden if args.orden in df.columns else None
    dedup = [None] * len(df)
    clasif = [None] * len(df)

    def texto_de(i):
        partes = []
        if subj_col and pd.notna(df[subj_col].iloc[i]):
            partes.append(str(df[subj_col].iloc[i]))
        if text_col and pd.notna(df[text_col].iloc[i]):
            partes.append(str(df[text_col].iloc[i]))
        return " ".join(partes)

    if col_hilo:
        from collections import defaultdict
        orden = sorted(range(len(df)),
                       key=lambda i: (str(df[col_hilo].iloc[i]),
                                      str(df[col_orden].iloc[i]) if col_orden else i))
        grupos = defaultdict(list)
        for i in orden:
            grupos[str(df[col_hilo].iloc[i])].append(i)
        for ids in grupos.values():
            textos_hilo = [texto_de(i) for i in ids]
            for i, t in zip(ids, deduplicar_hilo(textos_hilo)):
                dedup[i] = t
            msgs = [{"texto": texto_de(i),
                     "msg_id": str(df[col_msgid].iloc[i]) if col_msgid else "",
                     "remitente": str(df[sender_col].iloc[i]) if sender_col else "",
                     "fecha": str(df[col_orden].iloc[i]) if col_orden else ""}
                    for i in ids]
            for i, c in zip(ids, clasificar_hilo(msgs)):
                clasif[i] = c
    else:
        print("AVISO: sin --hilo no se deduplica el historial citado ni se detecta "
              "insistencia. Se procesa mensaje por mensaje.")

    filas = []
    for _, row in df.iterrows():
        partes = []
        if subj_col and pd.notna(row.get(subj_col)):
            partes.append(str(row[subj_col]))
        if text_col and pd.notna(row.get(text_col)):
            partes.append(str(row[text_col]))
        sender = str(row[sender_col]) if sender_col and pd.notna(row.get(sender_col)) else ""
        filas.append(engine.analizar(" ".join(partes), sender, contexto))

    df["Tipo"] = ["Exclusión" if r["excluido"] else "Gestionable" for r in filas]
    df["Exclusión"] = [r["categoria"] for r in filas]
    df["Regla_ID"] = [r["regla_id"] for r in filas]
    df["Regla"] = [r["texto_regla"] for r in filas]
    df["Condición"] = [r["condicion"] for r in filas]
    df["Evidencia"] = [r["evidencia"] for r in filas]
    df["Accionable"] = [r["accionable"] for r in filas]
    df["Tratamiento"] = [r["tratamiento"] for r in filas]
    df["Perdedores"] = [", ".join(r["perdedores"]) for r in filas]
    df["Bloques_removidos"] = [", ".join(r["bloques_removidos"]) for r in filas]
    df["Alerta_limpieza"] = [r["alerta_limpieza"] for r in filas]

    if col_hilo:
        # El texto que se le manda al paso siguiente: sin el historial ya citado.
        df["Texto_para_prompt"] = [d if d is not None else "" for d in dedup]
        # ¿esta fila existe de verdad, o el archivo la trae dos veces?
        df["Fila_estado"] = [c["fila_estado"] if c else "" for c in clasif]
        df["Repite_a"] = [c["repite_a"] if c else "" for c in clasif]
        # Cuántas veces seguidas escribió el mismo remitente sin que le contestaran.
        # El texto repetido se ahorra, pero el hecho de haber insistido NO se pierde:
        # es fricción operativa, y uno de los hallazgos que el OTBB existe para dar.
        df["Insistencia_n"] = [c["insistencia_n"] if c else "" for c in clasif]
        df["Horas_sin_respuesta"] = [c["horas_sin_respuesta"] if c else "" for c in clasif]
        df["Reenvio_identico"] = [c["reenvio_identico"] if c else "" for c in clasif]
        df["Motivo_fila"] = [c["motivo"] if c else "" for c in clasif]

    total = len(df)
    excl = int((df["Tipo"] == "Exclusión").sum())
    alertas = int(df["Alerta_limpieza"].sum())
    solapes = int((df["Perdedores"] != "").sum())

    print("\n=== RESULTADO ===")
    print(f"Total          {total}")
    print(f"Exclusiones    {excl} ({excl/total*100:.1f}%)")
    print(f"Gestionables   {total-excl} ({(total-excl)/total*100:.1f}%)")
    print(f"Solapamientos  {solapes} filas donde más de una regla matcheó")
    if col_hilo:
        dups = sum(1 for c in clasif if c and c["fila_estado"].startswith("duplicado"))
        inss = sum(1 for c in clasif if c and c["insistencia_n"])
        rein = sum(1 for c in clasif if c and c["reenvio_identico"])
        hilos_ins = len({str(df[col_hilo].iloc[i]) for i, c in enumerate(clasif)
                         if c and c["insistencia_n"]})
        print(f"Filas repetidas por el archivo   {dups}  (defecto de datos, no ahorro)")
        print(f"Insistencias                     {inss}  en {hilos_ins} de "
              f"{df[col_hilo].nunique()} hilos  ({rein} reenvíos literales)")
    if excl:
        print("\nPor categoría:")
        print(df.loc[df["Tipo"] == "Exclusión", "Exclusión"].value_counts().to_string())

    salida = base.parent / f"{base.stem}_EXCLUSIONES.xlsx"
    df.to_excel(salida, index=False)
    print(f"\nGuardado: {salida}")

    if alertas:
        print(f"\nFALLA: en {alertas} filas la limpieza dejó el mensaje casi vacío. "
              "Se usó el texto crudo para no destruir el cuerpo, pero hay un bloque "
              "de banners mal clasificado (una cabecera tratada como pie).")
        sys.exit(1)


if __name__ == "__main__":
    main()
