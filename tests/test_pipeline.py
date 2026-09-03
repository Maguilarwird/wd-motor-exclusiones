"""
Pruebas del pipeline: lo que sólo se puede comprobar mirando un hilo entero.

    python tests/test_pipeline.py

casos_regresion.yaml prueba el catálogo mensaje por mensaje. Acá va lo que
depende de la conversación completa: que el asunto se mande una sola vez, que
se mande sobre un mensaje que efectivamente viaje, y que quitar el banner no se
lleve el cuerpo.

Los textos son inventados. Ninguno viene de una base de cliente.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from motor.pipeline import procesar  # noqa: E402

ASUNTO = "Solicitud de aumento de cupo // 14.226.255-K"
BANNER = ("ALERTA: Este mensaje proviene de un remitente EXTERNO – ten cuidado "
          "especialmente con enlaces y archivos adjuntos. Si sospechas, "
          "repórtalo de inmediato con el botón del pez o reenvíalo a "
          "phishing@gruposantander.com.")
CTX = ["santander", "cl"]

fallos = []
corridas = []


def check(nombre, condicion, detalle=""):
    # El total se cuenta solo: escrito a mano, se olvida al agregar una prueba
    # y el resumen dice 11/11 cuando corrieron 12.
    corridas.append(nombre)
    print(f"{'ok     ' if condicion else 'FALLA  '} {nombre}")
    if not condicion:
        fallos.append(nombre)
        if detalle:
            print(f"         {detalle}")


def correr(mensajes):
    return procesar(mensajes, contexto=CTX)["mensajes"]


def msg(n, texto, remitente="cliente@correo.cl", msg_id=None, asunto=ASUNTO):
    return {"id": n, "hilo": "H1", "fecha": f"2026-06-15 1{n}:00:00",
            "remitente": remitente, "msg_id": msg_id or f"m{n}",
            "asunto": asunto, "texto": texto}


def main():
    # ── el asunto viaja una sola vez ──
    r = correr([msg(1, "Buenos días, necesito ampliar mi línea."),
                msg(2, "Le confirmo que quedó ingresada.", "ejecutivo@banco.cl"),
                msg(3, "Perfecto, quedo atento entonces.")])
    veces = sum(1 for m in r if ASUNTO in m["texto_para_prompt"])
    check("el asunto sale una sola vez en todo el hilo", veces == 1,
          f"aparece en {veces} mensajes")
    check("y sale en el primero", ASUNTO in r[0]["texto_para_prompt"])
    check("los otros conservan su cuerpo",
          "quedó ingresada" in r[1]["texto_para_prompt"]
          and "quedo atento" in r[2]["texto_para_prompt"])

    # ── si el primero es una fila repetida, el asunto pasa al siguiente ──
    # Un duplicado de archivo sale con texto vacío. Si el asunto se quedara ahí
    # el hilo entero perdería de qué trata.
    r = correr([msg(1, "Mismo texto repetido por la ingesta.", msg_id="dup"),
                msg(2, "Mismo texto repetido por la ingesta.", msg_id="dup"),
                msg(3, "Le confirmo la gestión solicitada.", "ejecutivo@banco.cl")])
    con_asunto = [i for i, m in enumerate(r) if ASUNTO in m["texto_para_prompt"]]
    check("el asunto no se pierde en una fila duplicada",
          len(con_asunto) == 1 and not r[con_asunto[0]]["fila_estado"].startswith("duplicado"),
          f"quedó en {con_asunto}")

    # ── si el primero está excluido, el asunto viaja igual ──
    r = correr([msg(1, "Estoy fuera de oficina hasta el lunes.", "ejecutivo@banco.cl"),
                msg(2, "Gracias, quedo esperando su respuesta.")])
    check("el asunto viaja aunque el primero esté excluido",
          ASUNTO in r[0]["texto_para_prompt"])
    # El motor marca en su propio campo y NO toca el texto. Los dos flujos que
    # consumen esto piden cosas opuestas: el maestro descarta la fila y el
    # análisis conversacional necesita leerla. La etiqueta se agrega al armar
    # cada salida, no acá.
    check("un mensaje marcado conserva su texto",
          r[0]["excluido"] and "fuera de oficina" in r[0]["texto_para_prompt"].lower(),
          repr(r[0]["texto_para_prompt"]))
    check("y la marca viaja aparte, no dentro del texto",
          r[0]["categoria"] == "Fuera de oficina / OOO"
          and "[" not in r[0]["texto_para_prompt"],
          repr(r[0]["texto_para_prompt"]))

    # ── el banner no viaja ──
    r = correr([msg(1, BANNER + " Buenos días, ¿me confirma el estado?"),
                msg(2, BANNER + " Ahora puede llamarme")])
    check("el banner no llega al texto de salida",
          not any("phishing@" in m["texto_para_prompt"] for m in r))
    check("quitar el banner no se lleva un cuerpo corto",
          "Ahora puede llamarme" in r[1]["texto_para_prompt"],
          repr(r[1]["texto_para_prompt"]))
    check("y conserva mayúsculas y tildes",
          "¿me confirma el estado?" in r[0]["texto_para_prompt"])

    # ── el asunto incrustado en el cuerpo también se quita ──
    # Hay exportaciones que lo meten adentro del texto en vez de dejarlo aparte.
    r = correr([msg(1, "Buenos días, necesito ampliar mi línea."),
                msg(2, f"RE: {ASUNTO} Le confirmo que quedó ingresada.",
                    "ejecutivo@banco.cl")])
    check("el asunto incrustado en el cuerpo no se repite",
          ASUNTO not in r[1]["texto_para_prompt"],
          repr(r[1]["texto_para_prompt"]))
    check("pero el cuerpo queda entero",
          "quedó ingresada" in r[1]["texto_para_prompt"])

    print(f"\n{len(corridas) - len(fallos)}/{len(corridas)} ok | {len(fallos)} fallan")
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
