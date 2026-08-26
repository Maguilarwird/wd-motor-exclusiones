r"""
Clasificación de filas dentro de un hilo.

Dos cosas se ven idénticas en los datos y significan cosas opuestas:

  duplicado_archivo  la misma fila viene dos veces. No pasó nada en la realidad:
                     es un defecto de la ingesta. Se descarta del análisis y se
                     cuenta aparte como defecto de datos.

  insistencia        el remitente volvió a escribir lo mismo porque no le
                     contestaron. Sí pasó, y es fricción operativa: uno de los
                     hallazgos que un OTBB existe para encontrar.

En los dos casos el TEXTO se deja de mandar. La diferencia es que la insistencia
deja rastro: qué mensaje repite, cuál número de intento es, y cuántas horas pasaron.
Ahorrar tokens no puede costar el dato.

Cómo se distinguen:

  1. Message ID repetido dentro del hilo  -> duplicado_archivo.
     Si no hay Message ID, se cae a (fecha + remitente + texto) idénticos y el
     resultado se marca como `duplicado_sospechoso`, porque sin identificador
     único no se puede afirmar.
  2. Message ID distinto, mismo remitente, y el mensaje casi no aporta texto
     nuevo respecto de uno anterior suyo  -> insistencia.
"""

import re
from datetime import datetime

from .deduplicacion import (_normalizar, _palabras, _shingles, VENTANA,
                            deduplicar_hilo)

# Un reenvío real no aporta poco: no aporta NADA. El criterio es absoluto, no
# porcentual: en un correo que cita la cadena entera, una frase nueva legítima
# ("puede ser a las 15:00 por favor") igual sale como un 12% del texto, y con un
# umbral porcentual se contaría como reenvío. Eso convierte una respuesta del
# cliente en ruido, que es el error más caro que puede cometer esta capa.
MAX_CARACTERES_NUEVOS = 25
# ...y si un mensaje anterior del mismo remitente cubre al menos esto de su texto.
MIN_COBERTURA_ORIGEN = 0.80


def _cobertura(texto, referencia, ventana=VENTANA):
    """Qué proporción de las palabras de `texto` ya está en `referencia`."""
    pal_t = _palabras(texto)
    if len(pal_t) < ventana:
        a, b = _normalizar(texto).strip(), _normalizar(referencia)
        return 1.0 if a and a in b else 0.0
    ref = {sh for _, sh in _shingles(_palabras(referencia), ventana)}
    cubiertas = set()
    for i, sh in _shingles(pal_t, ventana):
        if sh in ref:
            cubiertas.update(range(i, i + ventana))
    return len(cubiertas) / len(pal_t)


def _fecha(valor):
    if not valor:
        return None
    s = str(valor).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M",
                "%d-%m-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:len(fmt) + 2].strip(), fmt)
        except ValueError:
            continue
    return None


def clasificar_hilo(mensajes):
    """
    mensajes: lista de dicts EN ORDEN CRONOLÓGICO, con las claves
        texto      (obligatoria)
        msg_id     (opcional pero muy recomendable)
        remitente  (recomendado: quién escribe. Es lo que define un turno.)
        lado       (opcional: si se pasa 'SENT'/'INBOX' la insistencia se mide por
                    lado de la conversación en vez de por persona. Más amplio.)
        fecha      (opcional)

    Devuelve una lista alineada, un dict por mensaje.

    Dos clasificaciones INDEPENDIENTES, porque son preguntas distintas:

    fila_estado          unico | duplicado_archivo | duplicado_sospechoso
                         ¿esta fila existe de verdad, o el archivo la trae dos veces?

    insistencia_n        None, o 2, 3, 4...
                         ¿cuántas veces seguidas escribió este lado sin que el otro
                         contestara? El 1 no se marca: escribir una vez es normal.

    La insistencia se decide por TURNOS, no por texto. Un reenvío idéntico y un
    "¿alguna novedad?" son la misma fricción: alguien tuvo que volver a escribir
    porque no le respondieron. Y al revés, dos mensajes seguidos con texto casi
    igual pueden ser una respuesta corta que cita la cadena entera — eso no es
    insistencia y ningún umbral de similitud lo distingue.

    reenvio_identico     True cuando además el mensaje no aporta texto nuevo.
                         Es el subconjunto más duro: literalmente lo volvió a mandar.
    """
    salida = []
    vistos_id = {}
    vistos_huella = {}

    textos = [str(m.get("texto") or "") for m in mensajes]
    aportes = deduplicar_hilo(textos)

    def lado_de(m):
        v = m.get("lado") or m.get("remitente") or ""
        return _normalizar(str(v)).strip()

    # ── 1. filas que el archivo trae repetidas ──
    for pos, m in enumerate(mensajes, 1):
        msg_id = str(m.get("msg_id") or "").strip()
        estado, repite_a, motivo = "unico", None, ""
        if msg_id and msg_id in vistos_id:
            estado, repite_a = "duplicado_archivo", vistos_id[msg_id]
            motivo = f"mismo Message ID que el mensaje {repite_a}"
        elif not msg_id:
            huella = (str(m.get("fecha") or ""), lado_de(m), _normalizar(textos[pos - 1]))
            if huella in vistos_huella:
                estado, repite_a = "duplicado_sospechoso", vistos_huella[huella]
                motivo = (f"misma fecha, remitente y texto que el mensaje {repite_a}. "
                          "Sin Message ID no se puede afirmar que sea la misma fila.")
            else:
                vistos_huella[huella] = pos
        if msg_id:
            vistos_id.setdefault(msg_id, pos)
        salida.append({"fila_estado": estado, "repite_a": repite_a, "motivo": motivo,
                       "insistencia_n": None, "horas_sin_respuesta": None,
                       "reenvio_identico": False,
                       "aporte_nuevo_chars": len(" ".join(w for w, _, _ in _palabras(aportes[pos - 1])))})

    # ── 2. turnos seguidos del mismo lado, sin respuesta en medio ──
    reales = [(pos, m) for pos, m in enumerate(mensajes, 1)
              if not salida[pos - 1]["fila_estado"].startswith("duplicado")]

    seguidos, primero = 0, None
    lado_previo = None
    for pos, m in reales:
        lado = lado_de(m)
        if lado and lado == lado_previo:
            seguidos += 1
            r = salida[pos - 1]
            r["insistencia_n"] = seguidos + 1
            r["repite_a"] = r["repite_a"] or primero
            f0, f1 = _fecha(mensajes[primero - 1].get("fecha")), _fecha(m.get("fecha"))
            if f0 and f1:
                r["horas_sin_respuesta"] = round((f1 - f0).total_seconds() / 3600, 1)
            r["reenvio_identico"] = r["aporte_nuevo_chars"] <= MAX_CARACTERES_NUEVOS
            r["motivo"] = (f"el mismo lado ya había escrito en el mensaje {primero} y "
                           "nadie respondió en medio"
                           + (". Además no aporta texto nuevo: es un reenvío literal"
                              if r["reenvio_identico"] else ""))
        else:
            seguidos, primero, lado_previo = 0, pos, lado

    return salida


def resumen_hilo(clasificacion):
    """Cifras por hilo, para el reporte."""
    ins = [c for c in clasificacion if c["insistencia_n"]]
    dup = [c for c in clasificacion if c["fila_estado"].startswith("duplicado")]
    horas = [c["horas_sin_respuesta"] for c in ins if c["horas_sin_respuesta"] is not None]
    return {
        "mensajes": len(clasificacion),
        "duplicados_archivo": len(dup),
        "mensajes_reales": len(clasificacion) - len(dup),
        "insistencias": len(ins),
        "hubo_insistencia": bool(ins),
        "max_intentos": max([c["insistencia_n"] for c in ins], default=1),
        "reenvios_identicos": sum(1 for c in ins if c["reenvio_identico"]),
        "horas_hasta_insistir": round(sum(horas) / len(horas), 1) if horas else None,
    }
