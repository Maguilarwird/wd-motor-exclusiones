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
  2. Clasificar las filas del hilo.
     Distingue la fila que el archivo trae dos veces (defecto de datos) de la
     insistencia real (alguien volvió a escribir porque no le contestaron).
  3. Deduplicar el historial citado.
     Quita de cada mensaje los tramos que ya aparecieron antes en el hilo.
  4. Aplicar el catálogo sobre el TEXTO ORIGINAL, no sobre el deduplicado.
     Una regla tiene que poder ver el mensaje como lo escribió la persona. Si se
     evaluara sobre el texto ya recortado, un mensaje cuyo saludo quedó cortado
     dejaría de matchear reglas que sí corresponden.

El motor nunca borra ni reemplaza un campo de entrada: solo agrega. Eso es lo
que permite que quien consuma decida después qué usar, sin coordinarse antes.
"""

from collections import defaultdict

from .deduplicacion import deduplicar_hilo, estimar_tokens
from .exclusion_engine import ExclusionEngine, load_catalog
from .hilos import clasificar_hilo, resumen_hilo

CAMPOS_ENTRADA = ("id", "hilo", "fecha", "remitente", "msg_id", "asunto", "texto")


def _texto_completo(m):
    partes = [str(m.get("asunto") or "").strip(), str(m.get("texto") or "").strip()]
    return " ".join(p for p in partes if p)


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

    for clave, ids in grupos.items():
        textos = [_texto_completo(mensajes[i]) for i in ids]

        # ── 2. filas duplicadas e insistencia ──
        clasificacion = clasificar_hilo([
            {"texto": t,
             "msg_id": str(mensajes[i].get("msg_id") or ""),
             "remitente": str(mensajes[i].get("remitente") or ""),
             "fecha": mensajes[i].get("fecha")}
            for i, t in zip(ids, textos)
        ])
        resumen_hilos[clave] = resumen_hilo(clasificacion)

        # ── 3. deduplicación del historial citado ──
        aportes = deduplicar_hilo(textos)

        # ── 4. catálogo, sobre el texto original ──
        for pos, (i, texto, cls, aporte) in enumerate(
                zip(ids, textos, clasificacion, aportes), 1):
            r = engine.analizar(texto, str(mensajes[i].get("remitente") or ""), contexto)

            if r["excluido"]:
                para_prompt = f"[{r['categoria']}]"
            elif cls["fila_estado"].startswith("duplicado"):
                para_prompt = ""
            else:
                para_prompt = aporte

            salida[i] = {
                "id": mensajes[i].get("id", i),
                "hilo": clave if not clave.startswith("__sin_hilo_") else None,
                "posicion_en_hilo": pos,
                "mensajes_en_hilo": len(ids),

                "texto_para_prompt": para_prompt,
                "chars_original": len(texto),
                "chars_para_prompt": len(para_prompt),

                "excluido": r["excluido"],
                "categoria": r["categoria"],
                "regla_id": r["regla_id"],
                "texto_regla": r["texto_regla"],
                "condicion": r["condicion"],
                "tratamiento": r["tratamiento"],
                "accionable": r["accionable"],
                "evidencia": r["evidencia"],
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
        "alertas_limpieza": alertas,
        "reglas_activas": salida[0] and len(engine.rules),
    }

    return {"mensajes": salida, "resumen": resumen, "avisos": avisos}
