"""
Probador interactivo. Escribe un texto y muestra qué decide el motor.

    python scripts/probar_exclusiones.py [--catalogo RUTA] [--contexto santander,cl]

No tiene ninguna ruta hardcodeada: el catálogo sale de --catalogo, de la
variable CATALOGO_EXCLUSIONES, o del default del repo.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from motor.exclusion_engine import ExclusionEngine, load_catalog  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalogo", default=None)
    ap.add_argument("--hoja", default="Exclusiones")
    ap.add_argument("--contexto", default="")
    args = ap.parse_args()

    contexto = [t for t in args.contexto.split(",") if t.strip()]
    reglas = load_catalog(args.catalogo, sheet=args.hoja)
    engine = ExclusionEngine(reglas)
    print(f"{len(reglas)} reglas | contexto: {contexto or ['todos']}")
    print("Ctrl+C para salir\n")

    while True:
        try:
            texto = input("texto> ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            return
        if not texto:
            continue
        r = engine.analizar(texto, contexto=contexto)
        if r["excluido"]:
            print(f"  EXCLUIDO  {r['categoria']}  [{r['regla_id']}]")
            print(f"  regla     «{r['texto_regla']}» ({r['condicion']})")
            print(f"  evidencia {r['evidencia']}")
            print(f"  trata     {r['tratamiento']}")
            if r["perdedores"]:
                print(f"  perdedores {', '.join(r['perdedores'])}")
        else:
            print("  GESTIONABLE")
        if r["bloques_removidos"]:
            print(f"  banners removidos: {', '.join(r['bloques_removidos'])}")
        if r["alerta_limpieza"]:
            print("  ALERTA: la limpieza dejó el mensaje casi vacío, se usó el crudo")
        print()


if __name__ == "__main__":
    main()
