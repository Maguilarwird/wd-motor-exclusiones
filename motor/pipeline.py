r"""
La orquestación, en una sola función.

`procesar()` es lo único que necesita saber cualquier consumidor: el script de
terminal, el servicio HTTP y el pipeline del OTBB llaman todos acá. Si mañana
cambia el orden de las etapas, cambia en un solo lugar.

No lee archivos, no imprime, no sabe de pandas ni de Excel. Entra una lista de
mensajes, sale una lista de mensajes con más campos.

Orden de las etapas, y por qué ese orden:

  1. Agrupar por hilo y ordenar por fecha.
     Sin orden no hay "mensaje anterior", y sin eso no hay deduplicación.
  2. Quitar los bloques institucionales del texto que se va a mandar.
     El banner de phishing del banco no aporta nada al análisis, se repite en
     cada correo y se paga en tokens igual que el resto. Se quita acá, antes de
     deduplicar, para que tampoco ensucie la comparación entre mensajes.
  3. Clasificar las filas del hilo.
     Distingue la fila que el archivo trae dos veces (defecto de datos) de la
     insistencia real (alguien volvió a escribir porque no le contestaron).
  4. Deduplicar el historial citado.
     Quita de cada mensaje los tramos que ya aparecieron antes en el hilo.
  5. Aplicar el catálogo sobre el TEXTO ORIGINAL, no sobre el deduplicado ni
     sobre el limpio. Una regla tiene que poder ver el mensaje como lo escribió
     la persona. Si se evaluara sobre el texto ya recortado, un mensaje cuyo
     saludo quedó cortado dejaría de matchear reglas que sí corresponden.

`chars_original` sigue midiendo el texto tal como entró, con banner incluido.
Descontarlo de la entrada además de la salida escondería justamente el ahorro
que esta capa produce.

El motor nunca borra ni reemplaza un campo de entrada: solo agrega. Eso es lo
que permite que quien consuma decida después qué usar, sin coordinarse antes.
"""

import re
from collections import defaultdict

from . import lemas
from .deduplicacion import deduplicar_hilo, estimar_tokens
from .exclusion_engine import (ExclusionEngine, load_catalog, normalize,
                               normalize_con_mapa, normalizar_contexto,
                               revisar_limpieza)
from .hilos import clasificar_hilo, resumen_hilo

CAMPOS_ENTRADA = ("id", "hilo", "fecha", "remitente", "msg_id", "asunto", "texto")


def _texto_completo(m):
    partes = [str(m.get("asunto") or "").strip(), str(m.get("texto") or "").strip()]
    return " ".join(p for p in partes if p)


# "RE:", "RV:", "Fwd:" y etiquetas como "[EXT]" que el correo va acumulando
# adelante. No son parte del asunto: son marcas del transporte.
_RX_PREFIJO_ASUNTO = re.compile(r"^\s*(\[[^\]]{1,12}\]\s*|(re|rv|fw|fwd)\s*:\s*)+")


def _raiz_asunto(asunto):
    """El asunto sin los prefijos que se acumulan al responder y reenviar."""
    return _RX_PREFIJO_ASUNTO.sub("", normalize(asunto)).strip()


def _cuerpo_sin_asunto(cuerpo, asunto):
    """
    Quita el asunto del principio del cuerpo, si viene incrustado ahí.

    Hay formatos de exportación que meten el asunto dentro del texto en vez de
    dejarlo como campo aparte. En la base de validación pasa en 4 de 400
    correos, pero basta con que un cliente exporte distinto para que el asunto
    vuelva a viajar una vez por mensaje.

    Sólo se corta al PRINCIPIO y sólo si el asunto es largo: cortar en cualquier
    parte, o cortar un asunto de dos palabras, es adivinar. Y adivinar acá
    significa borrar texto del cliente.
    """
    raiz = _raiz_asunto(asunto)
    if len(raiz) < 15:
        return cuerpo
    norm, mapa = normalize_con_mapa(cuerpo)
    m = _RX_PREFIJO_ASUNTO.match(norm)
    ini = m.end() if m else 0
    if not norm.startswith(raiz, ini):
        return cuerpo
    return cuerpo[mapa[ini + len(raiz)]:].strip()


def procesar(mensajes, contexto=None, engine=None, catalogo=None):
    """
    mensajes: lista de dicts. Único campo obligatorio: `texto`.
        id        lo que el llamador use para reconocer la fila; se devuelve igual
        hilo      identificador de conversación. Sin esto no hay dedup ni insistencia
        fecha     para ordenar dentro del hilo
        remitente para detectar insistencia
        msg_id    para distinguir fila duplicada de insistencia
        asunto    se antepone al texto antes de evaluar

    contexto: tokens de alcance, ej. ["santander", "cl"]. 'todos' aplica siempre.

    Devuelve {"mensajes": [...], "resumen": {...}, "avisos": [...]}
    """
    engine = engine or ExclusionEngine(load_catalog(catalogo))
    mensajes = list(mensajes)
    n = len(mensajes)
    avisos = []

    if not n:
        return {"mensajes": [], "resumen": {"mensajes": 0}, "avisos": ["lote vacío"]}

    hay_hilo = any(str(m.get("hilo") or "").strip() for m in mensajes)
    if not hay_hilo:
        avisos.append(
            "Ningún mensaje trae hilo. Sin hilo no se puede deduplicar el historial "
            "citado ni detectar insistencia, que son las dos etapas de mayor retorno. "
            "Cada mensaje se procesa como si fuera una conversación de uno."
        )
    if hay_hilo and not any(str(m.get("fecha") or "").strip() for m in mensajes):
        avisos.append(
            "Los mensajes no traen fecha. Se respeta el orden en que vinieron en el "
            "lote; si ese orden no es cronológico, la deduplicación quita los tramos "
            "equivocados."
        )
    if hay_hilo and not any(str(m.get("msg_id") or "").strip() for m in mensajes):
        avisos.append(
            "Los mensajes no traen msg_id. Las filas repetidas se detectan por fecha, "
            "remitente y texto, y quedan marcadas como 'duplicado_sospechoso' porque "
            "sin identificador único no se puede afirmar."
        )

    # ── 1. agrupar y ordenar ──
    grupos = defaultdict(list)
    for i, m in enumerate(mensajes):
        grupos[str(m.get("hilo") or f"__sin_hilo_{i}__")].append(i)
    for clave, ids in grupos.items():
        ids.sort(key=lambda i: (str(mensajes[i].get("fecha") or ""), i))

    salida = [None] * n
    resumen_hilos = {}

    ctx = normalizar_contexto(contexto)
    banners_removidos = 0

    for clave, ids in grupos.items():
        crudos = [_texto_completo(mensajes[i]) for i in ids]

        # ── 2a. el asunto, una sola vez por hilo ──
        # El asunto es el mismo en toda la conversación —47 de los 50 hilos de
        # la base de validación tienen uno solo, descontando RE:/RV:/[EXT]— y el
        # flujo de destino une los textos del hilo en uno. Repetirlo en cada
        # mensaje es pagarlo tantas veces como correos tenga el hilo sin agregar
        # nada. Se emite abajo, sobre el primer mensaje que efectivamente viaja.
        #
        # Ojo: se saca de lo que se ENVÍA, nunca de lo que se compara. Las
        # reglas lo necesitan —"Respuesta automática:" vive en el asunto— y por
        # eso `crudos` sigue llevándolo.
        asunto_hilo = str(mensajes[ids[0]].get("asunto") or "").strip()
        cuerpos = [_cuerpo_sin_asunto(str(mensajes[i].get("texto") or "").strip(),
                                      mensajes[i].get("asunto"))
                   for i in ids]

        # ── 2b. bloques institucionales ──
        # El texto que sigue viaje va sin banner. La red de seguridad se aplica
        # sólo cuando se cortó un PIE: ése corta hasta el final del mensaje y un
        # patrón mal clasificado se lleva el cuerpo entero. Una cabecera borra
        # exactamente lo que matchea, así que un mensaje que queda corto después
        # de quitarla no es un mensaje destruido: es un mensaje que era casi
        # todo banner. "Ahora puede llamarme" detrás de 216 caracteres de aviso
        # de phishing es el caso normal, no una falla.
        # La comparación es contra el texto SIN CABECERAS, no contra el que
        # entró: si una cabecera se llevó 215 caracteres de banner, el resultado
        # queda corto de forma legítima y mirar el encogimiento total hacía
        # sonar la alarma y devolver el banner. Lo que hay que preguntar es si
        # el PIE se comió el mensaje.
        textos = []
        for cuerpo in cuerpos:
            tras_cab, limpio, quitados = engine.banners.limpiar_texto_en_dos_pasos(
                cuerpo, ctx)
            if (quitados and engine.banners.corto_un_pie(quitados)
                    and not revisar_limpieza(tras_cab, limpio)):
                limpio, quitados = cuerpo, []
            banners_removidos += len(quitados)
            textos.append(limpio)

        # ── 3. filas duplicadas e insistencia ──
        clasificacion = clasificar_hilo([
            {"texto": t,
             "msg_id": str(mensajes[i].get("msg_id") or ""),
             "remitente": str(mensajes[i].get("remitente") or ""),
             "fecha": mensajes[i].get("fecha")}
            for i, t in zip(ids, textos)
        ])
        resumen_hilos[clave] = resumen_hilo(clasificacion)

        # ── 4. deduplicación del historial citado ──
        aportes = deduplicar_hilo(textos)

        # El asunto va sobre el primer mensaje que de verdad viaja. No sobre el
        # primero a secas: si ese resultó ser una fila repetida por el archivo,
        # su texto queda vacío y el asunto se perdería para todo el hilo.
        # Si el primero está excluido, el asunto se antepone a su etiqueta: son
        # 46 caracteres una vez, contra perder de qué trata la conversación.
        vivos = [n for n, c in enumerate(clasificacion)
                 if not c["fila_estado"].startswith("duplicado")]
        pos_asunto = vivos[0] if vivos else 0

        # ── 5. catálogo, sobre el texto crudo ──
        for pos, (i, crudo, cls, aporte) in enumerate(
                zip(ids, crudos, clasificacion, aportes), 1):
            # `crudo` lleva el asunto, para que las reglas de "Frase contenida"
            # puedan verlo. El cuerpo va aparte, para que "Solo aislado" pueda
            # preguntar por el mensaje y no por el mensaje más su asunto.
            r = engine.analizar(crudo, str(mensajes[i].get("remitente") or ""), contexto,
                                cuerpo=str(mensajes[i].get("texto") or "").strip())

            # El texto NO se reemplaza por la etiqueta. Un mensaje marcado sale
            # con su texto y su marca aparte, porque los dos flujos que consumen
            # esto necesitan cosas distintas: el maestro descarta la fila entera
            # (le da igual el texto) y el análisis conversacional necesita leerla.
            # Reemplazar acá servía a uno y arruinaba al otro. La etiqueta se
            # agrega al armar cada salida — ver scripts/salidas_otbb.py.
            if cls["fila_estado"].startswith("duplicado"):
                para_prompt = ""
            else:
                para_prompt = aporte

            if pos - 1 == pos_asunto and asunto_hilo:
                para_prompt = f"{asunto_hilo} {para_prompt}".strip()

            # ¿Este mensaje cierra la conversación o espera algo más? Sólo se
            # calcula para los marcados: es ahí donde la pregunta tiene sentido
            # —un acuse puede cerrar o continuar— y calcularlo para los 80.000
            # de una base costaría una pasada de spaCy que no se usa.
            # Es INFORMACIÓN, no decide nada: no cambia si el mensaje se marca
            # ni si sale del maestro. Lo usa quien consuma la salida.
            if r["excluido"]:
                continua, motivo_cont = lemas.continuidad(textos[pos - 1])
            else:
                continua, motivo_cont = None, ""

            salida[i] = {
                "id": mensajes[i].get("id", i),
                "hilo": clave if not clave.startswith("__sin_hilo_") else None,
                "posicion_en_hilo": pos,
                "mensajes_en_hilo": len(ids),

                "texto_para_prompt": para_prompt,
                "chars_original": len(crudo),
                "chars_para_prompt": len(para_prompt),

                "excluido": r["excluido"],
                "categoria": r["categoria"],
                "regla_id": r["regla_id"],
                "texto_regla": r["texto_regla"],
                "condicion": r["condicion"],
                "tratamiento": r["tratamiento"],
                "accionable": r["accionable"],
                "definicion": r.get("definicion", ""),
                "origen": r.get("origen", ""),
                "evidencia": r["evidencia"],
                # "superficie" o "lema": con qué comparación se encontró.
                "via": r.get("via", ""),
                # Sólo en los marcados: si el mensaje espera algo más.
                "continua": continua,
                "motivo_continuidad": motivo_cont,
                "perdedores": r["perdedores"],

                "bloques_removidos": r["bloques_removidos"],
                "alerta_limpieza": r["alerta_limpieza"],

                "fila_estado": cls["fila_estado"],
                "repite_a": cls["repite_a"],
                "insistencia_n": cls["insistencia_n"],
                "horas_sin_respuesta": cls["horas_sin_respuesta"],
                "reenvio_identico": cls["reenvio_identico"],
                "motivo_fila": cls["motivo"],
            }

    crudo = sum(s["chars_original"] for s in salida)
    final = sum(s["chars_para_prompt"] for s in salida)
    # Las filas que el archivo trae repetidas no se cuentan como ahorro: vaciarlas
    # tapa un defecto de la ingesta, no reduce trabajo real. Se reporta aparte para
    # que el número que se muestra afuera no venga inflado.
    sanos = [s for s in salida if not s["fila_estado"].startswith("duplicado")]
    crudo_sano = sum(s["chars_original"] for s in sanos)
    final_sano = sum(s["chars_para_prompt"] for s in sanos)
    alertas = sum(1 for s in salida if s["alerta_limpieza"])
    if alertas:
        avisos.append(
            f"En {alertas} mensajes la limpieza dejó el texto casi vacío y se usó el "
            "crudo. Hay un bloque de banners mal clasificado: una cabecera tratada "
            "como pie borra el cuerpo del mensaje."
        )

    resumen = {
        "mensajes": n,
        "hilos": len([c for c in grupos if not c.startswith("__sin_hilo_")]),
        "chars_original": crudo,
        "chars_para_prompt": final,
        "tokens_original": int(estimar_tokens(" " * crudo)),
        "tokens_para_prompt": int(estimar_tokens(" " * final)),
        "ahorro_pct": round((crudo - final) / crudo * 100, 1) if crudo else 0.0,
        "ahorro_pct_sin_duplicados": (round((crudo_sano - final_sano) / crudo_sano * 100, 1)
                                      if crudo_sano else 0.0),
        "excluidos": sum(1 for s in salida if s["excluido"]),
        "duplicados_archivo": sum(1 for s in salida
                                  if s["fila_estado"].startswith("duplicado")),
        "insistencias": sum(1 for s in salida if s["insistencia_n"]),
        "reenvios_identicos": sum(1 for s in salida if s["reenvio_identico"]),
        "hilos_con_insistencia": sum(1 for r in resumen_hilos.values()
                                     if r["hubo_insistencia"]),
        "solapamientos": sum(1 for s in salida if s["perdedores"]),
        "banners_removidos": banners_removidos,
        "alertas_limpieza": alertas,
        "reglas_activas": salida[0] and len(engine.rules),
    }

    return {"mensajes": salida, "resumen": resumen, "avisos": avisos}
