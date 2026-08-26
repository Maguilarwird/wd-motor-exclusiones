r"""
Deduplicación de historial citado.

Del segundo mensaje de un hilo en adelante, entre el 40% y el 66% de cada
correo es texto que la generativa ya leyó. Quitarlo es el arreglo de mayor
retorno y el más barato: no depende del catálogo, ni de un modelo lingüístico,
ni de las cabeceras del correo.

Dos operaciones distintas:

  deduplicar_hilo    quita de cada mensaje lo que ya apareció en un mensaje
                     ANTERIOR del mismo hilo. Necesita saber el orden de los
                     mensajes: sin hilos no hay nada contra qué comparar.

  deduplicar_interno quita bloques repetidos DENTRO de un mismo mensaje. Sirve
                     sin hilos — por ejemplo en las comunicaciones masivas que
                     traen el mismo texto en dos idiomas, o en las plantillas
                     que repiten el bloque de firma.

Método: se comparan ventanas de N palabras (shingles), no líneas ni oraciones.
Comparar por línea falla cuando el cliente de correo reflowea el texto, y
comparar por oración falla cuando la cita viene sin puntuación.
"""

import re
import unicodedata

VENTANA = 8          # palabras por shingle
MIN_TRAMO = 60       # caracteres: tramos más cortos no se tocan


def _normalizar(texto):
    t = unicodedata.normalize("NFD", str(texto).lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"[^\w\s]", " ", t)


def _palabras(texto):
    """[(palabra_normalizada, inicio, fin)] sobre el texto original."""
    out = []
    for m in re.finditer(r"\S+", texto):
        w = _normalizar(m.group(0)).strip()
        if w:
            out.append((w, m.start(), m.end()))
    return out


def _shingles(palabras, ventana=VENTANA):
    for i in range(len(palabras) - ventana + 1):
        yield i, " ".join(p[0] for p in palabras[i:i + ventana])


def _tramos_cubiertos(cubiertas, palabras, min_tramo=MIN_TRAMO):
    """Corridas de palabras cubiertas que superan min_tramo, como spans (a, b)."""
    tramos, inicio = [], None
    for i in range(len(palabras) + 1):
        dentro = i < len(palabras) and i in cubiertas
        if dentro and inicio is None:
            inicio = i
        elif not dentro and inicio is not None:
            a, b = palabras[inicio][1], palabras[i - 1][2]
            if b - a >= min_tramo:
                tramos.append((a, b))
            inicio = None
    return tramos


def _quitar_tramos(texto, cubiertas, palabras, min_tramo=MIN_TRAMO):
    tramos = _tramos_cubiertos(cubiertas, palabras, min_tramo)
    if not tramos:
        return texto
    salida, cursor = [], 0
    for a, b in tramos:
        salida.append(texto[cursor:a])
        cursor = b
    salida.append(texto[cursor:])
    return re.sub(r"\s+", " ", " ".join(salida)).strip()


def deduplicar_interno(texto, ventana=VENTANA, min_tramo=MIN_TRAMO):
    """Quita bloques repetidos dentro del mismo mensaje."""
    palabras = _palabras(texto)
    if len(palabras) < ventana * 2:
        return texto
    vistos, cubiertas = {}, set()
    for i, sh in _shingles(palabras, ventana):
        if sh in vistos:
            cubiertas.update(range(i, i + ventana))
        else:
            vistos[sh] = i
    return _quitar_tramos(texto, cubiertas, palabras, min_tramo)


def deduplicar_hilo(mensajes, ventana=VENTANA, min_tramo=MIN_TRAMO):
    """
    mensajes: textos del hilo EN ORDEN CRONOLÓGICO.
    Devuelve la lista con el aporte nuevo de cada mensaje.
    El primero nunca se toca: no tiene historia previa.
    """
    vistos, salida = set(), []
    for texto in mensajes:
        palabras = _palabras(texto)
        cubiertas = set()
        propios = []
        for i, sh in _shingles(palabras, ventana):
            if sh in vistos:
                cubiertas.update(range(i, i + ventana))
            propios.append(sh)
        salida.append(_quitar_tramos(texto, cubiertas, palabras, min_tramo))
        vistos.update(propios)
    return salida


def estimar_tokens(texto):
    """Aproximación: ~4 caracteres por token. Sirve para comparar, no para facturar."""
    return len(texto) / 4


def tramos_repetidos_en_hilo(mensajes, ventana=VENTANA, min_tramo=MIN_TRAMO):
    """
    Para revisión humana. Por cada mensaje del hilo devuelve los tramos que ya
    habían aparecido antes, con la posición del mensaje donde aparecieron por
    primera vez:

        [[(inicio, fin, origen), ...], ...]

    `origen` es 1-based sobre el hilo. Saber de dónde viene cada tramo es lo que
    permite validar el recorte: sin eso, un tramo tachado es un acto de fe.
    """
    vistos = {}          # shingle -> posición (1-based) del mensaje que lo dijo primero
    salida = []
    for pos, texto in enumerate(mensajes, 1):
        palabras = _palabras(texto)
        origen_por_palabra = {}
        propios = []
        for i, sh in _shingles(palabras, ventana):
            if sh in vistos:
                for k in range(i, i + ventana):
                    origen_por_palabra.setdefault(k, vistos[sh])
            propios.append((i, sh))

        tramos = []
        for a, b in _tramos_cubiertos(set(origen_por_palabra), palabras, min_tramo):
            fuentes = [o for k, o in origen_por_palabra.items()
                       if palabras[k][1] >= a and palabras[k][2] <= b]
            origen = min(fuentes) if fuentes else pos - 1
            tramos.append((a, b, origen))
        salida.append(tramos)

        for i, sh in propios:
            vistos.setdefault(sh, pos)
    return salida
