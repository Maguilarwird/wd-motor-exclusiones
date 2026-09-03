"""
Corre la suite de regresión en proceso, para que el servicio pueda validar un
catálogo antes de aceptarlo.

Es la misma suite que corre `tests/test_regresion.py` desde la terminal: una
sola implementación, dos formas de invocarla. Si fueran dos, tarde o temprano
divergen y el servicio validaría con criterios distintos a los del repositorio.
"""

from pathlib import Path

import yaml

from .exclusion_engine import ExclusionEngine, load_catalog, normalizar_contexto

RAIZ = Path(__file__).resolve().parents[1]
CASOS_DEFAULT = RAIZ / "tests" / "casos_regresion.yaml"


def correr_suite(engine=None, ruta_casos=None, ruta_catalogo=None):
    """
    Devuelve un dict con el resultado. `ok` es False si un caso normal falla o si
    un hueco conocido empezó a pasar (hay que promoverlo, no dejar la marca).
    """
    if engine is None:
        engine = ExclusionEngine(load_catalog(ruta_catalogo))

    casos = yaml.safe_load(Path(ruta_casos or CASOS_DEFAULT).read_text("utf-8"))["casos"]
    detalle = []
    ok = huecos = fallan = promover = 0

    for c in casos:
        r = engine.analizar(c["texto"], c.get("remitente", ""), c.get("contexto"))
        obtenido = r["categoria"] if r["excluido"] else None

        problemas = []
        if "esperado" in c and obtenido != c["esperado"]:
            problemas.append(f"esperaba {c['esperado']!r}, obtuvo {obtenido!r}")
        if ("debe_contener_en_limpio" in c
                and c["debe_contener_en_limpio"] not in r["texto_limpio"]):
            problemas.append(f"el texto limpio no contiene {c['debe_contener_en_limpio']!r}")
        if ("no_debe_contener_en_limpio" in c
                and c["no_debe_contener_en_limpio"] in r["texto_limpio"]):
            problemas.append(f"el texto limpio todavía contiene {c['no_debe_contener_en_limpio']!r}")

        # `texto_limpio` es la versión normalizada, la que ven las reglas.
        # Estas dos miran el texto que de verdad se le manda al paso siguiente:
        # con mayúsculas y tildes, y sin los bloques institucionales. Son
        # comprobaciones distintas y las dos hacen falta — un banner puede estar
        # bien quitado para comparar y seguir viajando en el texto de salida.
        if "debe_contener_en_envio" in c or "no_debe_contener_en_envio" in c:
            envio, _ = engine.banners.limpiar_texto(
                c["texto"], normalizar_contexto(c.get("contexto")))
            if ("debe_contener_en_envio" in c
                    and c["debe_contener_en_envio"] not in envio):
                problemas.append(
                    f"el texto de envío no contiene {c['debe_contener_en_envio']!r}")
            if ("no_debe_contener_en_envio" in c
                    and c["no_debe_contener_en_envio"] in envio):
                problemas.append(
                    f"el texto de envío todavía contiene {c['no_debe_contener_en_envio']!r}")

        if r["alerta_limpieza"]:
            problemas.append("la limpieza dejó el mensaje casi vacío")

        hueco = bool(c.get("hueco_conocido"))
        if problemas and hueco:
            estado, huecos = "hueco", huecos + 1
        elif problemas:
            estado, fallan = "falla", fallan + 1
        elif hueco:
            estado, promover = "promover", promover + 1
        else:
            estado, ok = "ok", ok + 1

        detalle.append({"nombre": c["nombre"], "estado": estado, "problemas": problemas})

    return {
        "ok": fallan == 0 and promover == 0,
        "total": len(casos),
        "pasan": ok,
        "huecos_conocidos": huecos,
        "fallan": fallan,
        "a_promover": promover,
        "detalle": detalle,
    }
