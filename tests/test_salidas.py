"""
Pruebas de las cuatro salidas OTBB.

    python tests/test_salidas.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from motor.pipeline import procesar  # noqa: E402
from motor.salidas import armar_salidas, salidas_a_json  # noqa: E402

CTX = ["santander", "cl"]
fallos = []
corridas = []


def check(nombre, condicion, detalle=""):
    corridas.append(nombre)
    print(f"{'ok     ' if condicion else 'FALLA  '} {nombre}")
    if not condicion:
        fallos.append(nombre)
        if detalle:
            print(f"         {detalle}")


def main():
    filas = [
        {"id": "1", "hilo": "H1", "fecha": "2026-06-15 10:00:00",
         "remitente": "cliente@correo.cl", "msg_id": "m1",
         "asunto": "Consulta de cupo", "texto": "Buenos días, necesito ampliar mi línea."},
        {"id": "2", "hilo": "H1", "fecha": "2026-06-15 11:00:00",
         "remitente": "ejecutivo@banco.cl", "msg_id": "m2",
         "asunto": "Consulta de cupo", "texto": "Le confirmo que quedó ingresada."},
        {"id": "3", "hilo": "H1", "fecha": "2026-06-15 12:00:00",
         "remitente": "auto@banco.cl", "msg_id": "m3",
         "asunto": "Consulta de cupo",
         "texto": "Estoy fuera de oficina hasta el lunes. Gracias."},
    ]
    r = procesar(filas, contexto=CTX)
    salidas = armar_salidas(filas, r)

    check("genera las cuatro salidas",
          all(k in salidas for k in ("maestro", "conversacional", "excluidos",
                                     "exclusiones_data_maestro")))
    check("el excluido no va al maestro", len(salidas["maestro"]) == 2)
    check("el conversacional trae todas las filas", len(salidas["conversacional"]) == 3)
    check("el excluido lleva marca en conversacional",
          "[Fuera de oficina" in salidas["conversacional"].iloc[2]["texto_para_prompt"])
    check("excluidos lista solo los marcados", len(salidas["excluidos"]) == 1)
    check("data_maestro tiene una fila agregada",
          len(salidas["exclusiones_data_maestro"]) == 1)

    js = salidas_a_json(salidas)
    check("serializa a JSON con las cuatro tablas",
          all(k in js for k in ("maestro", "conversacional", "excluidos",
                                "exclusiones_data_maestro", "resumen")))
    check("resumen trae conteos OTBB", js["resumen"]["van_al_maestro"] == 2)

    print(f"\n{len(corridas) - len(fallos)}/{len(corridas)} ok | {len(fallos)} fallan")
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
