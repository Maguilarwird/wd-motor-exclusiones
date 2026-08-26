"""
Auditoría del catálogo. Falla (exit 1) si algo está declarado y el motor no lo
consume, o si el catálogo tiene defectos que producen comportamiento silencioso.

    python scripts/auditar_catalogo.py [--catalogo RUTA] [--hoja Exclusiones]

Existe porque declarar no es implementar. Un campo que la planilla declara y el
motor nunca lee produce errores que parecen falta de precisión del motor: en el
proyecto de entidades, `aplica_a` estaba declarado y no consultado, y eso generó
37 falsos positivos que se investigaron como un problema de precisión.
"""

import argparse
import collections
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from motor.exclusion_engine import (  # noqa: E402
    CONDICIONES_IMPLEMENTADAS, Banners, ErrorCatalogo,
    load_catalog, parse_rule_pattern, normalize,
)

ALCANCES_DECLARADOS = {
    "todos",
    "cl", "mx", "ec", "pe", "co",
    "santander", "bch", "invex", "openbank", "consorcio", "bci", "walmart",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalogo", default=None)
    ap.add_argument("--hoja", default="Exclusiones")
    args = ap.parse_args()

    errores, avisos = [], []

    try:
        reglas = load_catalog(args.catalogo, sheet=args.hoja)
    except ErrorCatalogo as e:
        print(f"ERROR de contrato: {e}")
        return 1

    print(f"{len(reglas)} reglas leídas\n")

    # 1 — IDs presentes y únicos
    ids = [r["id"] for r in reglas]
    for rid, n in collections.Counter(ids).items():
        if n > 1:
            errores.append(f"ID duplicado: {rid} aparece {n} veces")
    for r in reglas:
        if not re.fullmatch(r"[A-Z]{3}-\d{4}", r["id"]):
            avisos.append(f"ID con formato inesperado: {r['id']!r}")

    # 2 — Condiciones declaradas que el motor no implementa
    for cond, n in collections.Counter(r["condicion"] for r in reglas).items():
        if cond not in CONDICIONES_IMPLEMENTADAS:
            errores.append(
                f"condición DECLARADA Y NO IMPLEMENTADA: {cond!r} en {n} reglas. "
                "El motor las va a evaluar como 'Frase contenida' o ignorarlas: "
                "arreglar el motor o corregir la planilla, no dejarlo así."
            )

    # 3 — Alcances fuera del vocabulario
    for r in reglas:
        for tok in {t.strip().lower() for t in str(r["alcance"]).split(",") if t.strip()}:
            if tok not in ALCANCES_DECLARADOS:
                errores.append(f"{r['id']}: alcance no declarado {tok!r}")

    # 4 — Patrones que no compilan
    for r in reglas:
        try:
            parse_rule_pattern(r["texto"])
        except re.error as e:
            errores.append(f"{r['id']}: el patrón no compila ({e})")

    # 5 — Términos en colisión entre categorías distintas
    por_texto = collections.defaultdict(set)
    for r in reglas:
        por_texto[normalize(r["texto"])].add(r["categoria"])
    for texto, cats in por_texto.items():
        if len(cats) > 1:
            errores.append(
                f"término en colisión: «{texto}» está en {len(cats)} categorías "
                f"({', '.join(sorted(cats))}). El resultado lo decide el orden de "
                "la lista, no el mensaje, que es lo que una capa determinista no "
                "debe hacer."
            )

    # 6 — Reglas escritas en infinitivo: el motor v0 matchea superficie, no lemas
    INFINITIVOS = re.compile(r"\b\w+(ar|er|ir)\b")
    LEMAS_SOSPECHOSOS = ("estar", "encontrar", "encontrarse", "haber", "ser", "tener")
    for r in reglas:
        t = normalize(r["texto"])
        if any(re.search(r"\b" + v + r"\b", t) for v in LEMAS_SOSPECHOSOS):
            avisos.append(
                f"{r['id']}: «{r['texto']}» parece escrita en lema/infinitivo. "
                "El motor v0 matchea superficie: esta regla NO dispara sobre "
                "«estaré», «me encuentro», etc. Ver PENDIENTES hueco 3."
            )

    # 7 — Categorías casi duplicadas (misma cosa con dos etiquetas)
    cats = sorted({r["categoria"] for r in reglas})
    for i, a in enumerate(cats):
        for b in cats[i + 1:]:
            na, nb = normalize(a), normalize(b)
            if na and nb and (na in nb or nb in na):
                avisos.append(f"categorías que se solapan por nombre: {a!r} y {b!r}")

    # 8 — banners.yaml
    try:
        banners = Banners.desde_archivo()
        print(f"{len(banners.bloques)} bloques de banners cargados")
        for b in banners.bloques:
            if b["alcance"] == {"todos"} and b["tipo"] == "pie":
                avisos.append(
                    f"{b['id']}: es un pie con alcance 'todos'. Un pie corta el "
                    "mensaje desde el match: confirmar que aplica a todo cliente."
                )
    except Exception as e:
        errores.append(f"banners.yaml: {e}")

    # 9 — Reglas de un cliente sin alcance declarado
    for r in reglas:
        if "(" in r["categoria"] and str(r["alcance"]).strip().lower() == "todos":
            avisos.append(
                f"{r['id']}: la categoría {r['categoria']!r} nombra a una entidad "
                "concreta pero el alcance es 'todos'. Candidata a alcance por "
                "cliente o país."
            )

    # ── Informe ──
    print()
    for a in avisos:
        print(f"AVISO   {a}")
    print()
    for e in errores:
        print(f"ERROR   {e}")
    print()
    print(f"{len(errores)} errores, {len(avisos)} avisos")
    return 1 if errores else 0


if __name__ == "__main__":
    sys.exit(main())
