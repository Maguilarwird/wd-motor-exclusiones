"""
Las cuatro salidas que consume el OTBB, de una sola corrida.

    python scripts/salidas_otbb.py base.csv --contexto santander,cl

Para el mismo flujo vía HTTP:

    POST /procesar-otbb   (JSON o ZIP con las cuatro salidas)

Ver motor/salidas.py y README.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from motor import columnas  # noqa: E402
from motor.exclusion_engine import ExclusionEngine, ErrorCatalogo, load_catalog  # noqa: E402
from motor.pipeline import procesar  # noqa: E402
from motor.salidas import armar_salidas, escribir_salidas, leer_tipificaciones  # noqa: E402


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
    salidas = armar_salidas(df.to_dict("records"), r, col=col)
    rutas = escribir_salidas(salidas, destino, base.stem)

    res = salidas["resumen"]
    total = res["correos_en_base"]
    print("\n=== RESUMEN ===")
    print(f"correos en la base            {total}")
    print(f"  exclusiones marcadas        {res['exclusiones_marcadas']}  "
          f"({res['pct_exclusiones']}%)")
    print(f"  filas repetidas del archivo {res['filas_repetidas_archivo']}  "
          f"({res['pct_duplicados']}%)")
    print(f"  van al maestro              {res['van_al_maestro']}  "
          f"({res['pct_maestro']}%)")
    print(f"hilos                         {res['hilos']}")
    print(f"banners removidos             {res['banners_removidos']}")
    print(f"insistencias                  {res['insistencias']} en "
          f"{res['hilos_con_insistencia']} hilos")
    print(f"texto: {res['chars_original']:,} -> {res['chars_para_prompt']:,} caracteres "
          f"({res['ahorro_pct']}% menos)")

    if salidas["sin_codigo"]:
        print(f"\nAVISO: {len(salidas['sin_codigo'])} tipificación(es) sin código WM. "
              "La columna Tipificación va vacía y la fila no se puede pegar todavía:")
        for item in sorted(salidas["sin_codigo"], key=lambda x: -x["n"]):
            print(f"  {item['n']:5} correos  {item['categoria']}  |  {item['origen']}")
        print("  → completar en catalogos/tipificaciones.csv")

    for p in rutas.values():
        print(f"\n{p}")

    if res.get("alertas_limpieza"):
        print(f"\nFALLA: en {res['alertas_limpieza']} filas la limpieza dejó el "
              "mensaje casi vacío.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
