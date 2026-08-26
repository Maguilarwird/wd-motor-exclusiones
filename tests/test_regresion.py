"""
Suite de regresión.

    python tests/test_regresion.py

Correrla CADA VEZ que se toque el catálogo o se cambie de versión del modelo
lingüístico. Es lo único que detecta un lema inventado o una regla que dejó de
disparar.

La lógica vive en motor/validacion.py, para que el servicio valide con
exactamente el mismo criterio que la terminal.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from motor.validacion import correr_suite  # noqa: E402


def main():
    r = correr_suite()
    etiqueta = {"ok": "ok     ", "hueco": "HUECO  ",
                "falla": "FALLA  ", "promover": "PROMOVER"}
    for d in r["detalle"]:
        print(f"{etiqueta[d['estado']]} {d['nombre']}")
        if d["estado"] == "falla":
            for p in d["problemas"]:
                print(f"         {p}")
        if d["estado"] == "promover":
            print("         era hueco conocido y ahora pasa: quitarle la marca")
    print(f"\n{r['pasan']}/{r['total']} ok | {r['huecos_conocidos']} huecos conocidos "
          f"| {r['fallan']} fallan | {r['a_promover']} a promover")
    return 0 if r["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
