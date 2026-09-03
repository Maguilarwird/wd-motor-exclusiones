"""
Lematización con spaCy: la capa que hace que el catálogo reconozca conjugaciones.

El motor v0 compara superficie: la regla «estar ausente» busca esas dos palabras
tal como están escritas, y nadie escribe así — escriben «estaré ausente» o «me
encuentro ausente». Siete reglas del catálogo están redactadas en infinitivo y
por eso no disparan nunca. auditar_catalogo.py las viene marcando desde antes de
que esto existiera.

Acá se resuelve comparando LEMAS: la forma base de cada palabra.

    texto  "Me encuentro fuera de la oficina"  ->  "yo encontrar fuera de el oficina"
    regla  "encontrar fuera de la oficina"     ->  "encontrar fuera de el oficina"
                                                    ^ matchea

Dos cosas que se aprendieron midiendo y que explican el diseño:

  1. Se lematiza el texto ORIGINAL, antes de quitarle los acentos. El acento es
     información morfológica: «Estaré» lematiza a «estar», pero «estare» sin
     acento el modelo no puede saber que es un verbo y devuelve «estarir». Si se
     normalizara antes de lematizar, esta capa no serviría para nada.

  2. La regla también se lematiza. «de vacaciones» en el texto queda como «de
     vacacion» (singular) y «de la oficina» como «de el oficina». Comparar el
     lema del texto contra la superficie de la regla no matchea nunca.

Es OPCIONAL a propósito. Sin spaCy instalado o sin el modelo, `disponible()`
devuelve False y el motor se comporta exactamente como antes. Un despliegue no
se cae porque falte un modelo lingüístico.

    MODELO_SPACY=es_core_news_lg   (default; se cae a _sm si no está)
"""

import itertools
import os
import re
import unicodedata
from functools import lru_cache

PREFERIDOS = ("es_core_news_lg", "es_core_news_sm")
# El parser y el NER no se usan y son lo más caro del pipeline. Sin ellos son
# ~550 correos por segundo; con ellos, una fracción de eso.
INNECESARIOS = ["parser", "ner", "senter"]
# Palabras que phrase_to_regex trata especial al final de una alternancia
# («andes/araucana/los heroes»). Si aparecen, no se expande: reproducir mal esa
# regla es peor que dejarla solo en superficie.
COLGANTES = {"los", "las"}
TOPE_EXPANSION = 24
# Una regla de una sola palabra no se lematiza. Generalizar el número de un
# sustantivo común y frecuente abre la puerta sin dejar entrar nada útil: la
# regla «evento/invitación» empezó a marcar como Evento de calendario un correo
# de apelación de crédito que decía «en junio no habíamos tenido eventos».
# Con dos palabras o más el contexto acota, y las siete reglas en infinitivo que
# esta capa existe para arreglar tienen todas dos o más.
MIN_PALABRAS = 2

_nlp = None
_estado = {"modelo": None, "error": None, "intentado": False}


def _cargar():
    global _nlp
    if _estado["intentado"]:
        return _nlp
    _estado["intentado"] = True
    # Interruptor para medir: permite correr la misma base con y sin lemas y
    # comparar. Sin poder apagarlo no hay forma de saber qué aporta la capa.
    if os.environ.get("SIN_LEMAS", "").strip() not in ("", "0", "false", "no"):
        _estado["error"] = "apagado por SIN_LEMAS"
        return None
    try:
        import spacy
    except ImportError:
        _estado["error"] = "spaCy no está instalado"
        return None

    pedido = os.environ.get("MODELO_SPACY")
    candidatos = [pedido] if pedido else list(PREFERIDOS)
    for nombre in candidatos:
        try:
            _nlp = spacy.load(nombre, disable=INNECESARIOS)
            _estado["modelo"] = nombre
            return _nlp
        except OSError:
            continue
    _estado["error"] = (f"no se pudo cargar ningún modelo de {candidatos}. "
                        "Instalar con: python -m spacy download es_core_news_lg")
    return None


def disponible():
    return _cargar() is not None


def info():
    """Qué modelo quedó cargado, o por qué no. Para que /health lo pueda decir."""
    _cargar()
    return {"disponible": _nlp is not None,
            "modelo": _estado["modelo"],
            "error": _estado["error"]}


def _componer(texto):
    """
    Acentos en forma compuesta (NFC), antes de lematizar.

    Un «é» puede venir como un carácter o como «e» + acento combinante. Se ven
    idénticos y el modelo los trata distinto: «Estaré» compuesto lematiza a
    «estar», descompuesto a «estare» y la regla no dispara. Cuesta nada y evita
    que la capa entera falle según cómo exportó el correo el sistema de origen.
    """
    return unicodedata.normalize("NFC", texto)


def _normalizar(texto):
    """Minúsculas, sin acentos, espacios colapsados. Igual que exclusion_engine."""
    t = unicodedata.normalize("NFD", texto.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", t).strip()


# Sufijo con el que se marca un verbo en pasado. Tiene que ser caracter de
# palabra para que `\bestar\b` NO matchee «estar_pas»: con un guion o una
# virgulilla el límite de palabra cae ahí y la marca no serviría de nada.
MARCA_PASADO = "_pas"


def _lema_de_token(tok):
    """
    El lema del token, con el pasado marcado.

    Lematizar borra el tiempo verbal, y el tiempo verbal es justamente lo que
    distingue un auto-reply de un cliente contando su situación:

        "estoy con licencia médica"   -> auto-reply, es exclusión
        "estuve con licencia médica"  -> el cliente pidiendo reprogramar su cuota

    Las dos lematizan a «estar con licencia» y la regla del catálogo marcaba las
    dos. Había un caso de regresión escrito justo para eso y esta capa lo rompió.

    Se marca sólo el pasado FINITO, no el participio: «no ha SIDO posible» y
    «fue GENERADO automáticamente» son participios y las reglas de rebote y de
    no-reply están escritas contando con ellos.
    """
    lema = tok.lemma_
    if (tok.pos_ in ("VERB", "AUX")
            and "Past" in tok.morph.get("Tense")
            and "Fin" in tok.morph.get("VerbForm")):
        return lema + MARCA_PASADO
    return lema


def _lemas_de(doc):
    return _normalizar(" ".join(_lema_de_token(t) for t in doc if not t.is_space))


@lru_cache(maxsize=20000)
def lemas(texto):
    """
    Los lemas del texto, normalizados. "" si spaCy no está disponible.

    Con caché porque estas bases traen el mismo texto repetido muchas veces
    —en la base de validación, 111 de 400 filas— y lematizar dos veces lo mismo
    es tiempo tirado.
    """
    nlp = _cargar()
    if nlp is None or not texto or not texto.strip():
        return ""
    return _lemas_de(nlp(_componer(texto)))


def lemas_en_lote(textos, batch_size=64):
    """Igual que lemas() pero en lote. Para bases grandes."""
    nlp = _cargar()
    if nlp is None:
        return ["" for _ in textos]
    return [_lemas_de(d) for d in nlp.pipe(textos, batch_size=batch_size)]


# Verbos que NO cuentan como acción: son parte del propio acuse o de la fórmula
# de cierre. «acuso recibo» y «le saluda» llevan verbo y no dicen que la
# conversación siga. Los copulativos y auxiliares tampoco son acciones.
#
# `quedar` NO está en la lista, a propósito: «ok quedo atenta» sí espera algo.
# Lo tenía y Jo lo corrigió mirando sus propios casos.
VERBOS_NEUTROS = {"acusar", "saludar", "agradecer", "ser", "estar", "haber"}


def continuidad(texto):
    """
    ¿El mensaje espera algo más, o cierra la conversación?

    Devuelve (continua, motivo). Dos señales, y las dos salen de cómo se
    reconocían los acuses en el BI antes de este motor:

      - un verbo de ACCIÓN: «lo reviso», «te escribo», «le aviso a la clienta».
        Un acuse puro no lleva verbo: nadie cierra diciendo «muchas gracias
        reviso». Medido contra la validación de Jo sobre 238 acuses: coincide
        con su criterio en el 93 % de los casos.

      - un signo de PREGUNTA: «gracias y el plástico de débito?» no tiene ningún
        verbo de acción y obviamente sigue. Esta señal faltaba en la primera
        versión y la agregó ella.

    "" en el motivo cuando spaCy no está: sin él esto no se puede responder, y
    es mejor decir que no se sabe que afirmar que la conversación cerró.
    """
    nlp = _cargar()
    if nlp is None or not texto or not texto.strip():
        return None, ""
    if "?" in texto or "¿" in texto:
        return True, "pregunta"
    doc = nlp(_componer(texto[:400]))
    verbos = [t.lemma_ for t in doc
              if t.pos_ == "VERB" and t.lemma_ not in VERBOS_NEUTROS]
    if verbos:
        return True, "verbo: " + ", ".join(dict.fromkeys(verbos))[:60]
    return False, "sin verbo de accion ni pregunta"


def _expandir_alternancias(frase):
    """
    «estar/encontrar con licencia» -> ["estar con licencia", "encontrar con licencia"]

    Se expande antes de lematizar porque el tokenizador parte en la barra y
    lematizar "estar/encontrar" no da nada útil. Devuelve [] cuando no conviene
    expandir: ahí la regla queda solo en superficie, que es lo que hace hoy.
    """
    grupos = []
    for palabra in frase.split():
        if "/" in palabra:
            alts = [a for a in palabra.split("/") if a]
            if any(a in COLGANTES for a in alts):
                return []
            grupos.append(alts)
        else:
            grupos.append([palabra])
    total = 1
    for g in grupos:
        total *= len(g)
    if total > TOPE_EXPANSION:
        return []
    return [" ".join(c) for c in itertools.product(*grupos)]


def lematizar_regla(frase):
    """
    Las variantes lematizadas de una frase del catálogo, listas para compilar.

    Devuelve [] cuando no corresponde lematizar:
      - spaCy no está disponible
      - la frase usa el meta-token `fecha`, que se resuelve en superficie
      - la alternancia no se puede expandir sin arriesgar cambiar la regla
    """
    nlp = _cargar()
    if nlp is None:
        return []
    frase = str(frase or "").strip()
    if not frase or re.search(r"\bfecha\b", frase, re.I):
        return []
    variantes = _expandir_alternancias(frase)
    if not variantes:
        return []
    # Se devuelve la variante lematizada AUNQUE sea idéntica a la original. Es
    # contraintuitivo y fue un error caro: lo que cambia al lematizar es el
    # TEXTO, no la regla. «estar ausente» ya está en infinitivo y lematizarla no
    # la mueve, pero es exactamente la regla que necesita comparar contra lemas
    # para encontrar «estaré ausente». Descartar las que no cambian dejaba
    # afuera a las siete reglas que esta capa existe para arreglar.
    salida = [lem for lem in (_lemas_de(nlp(v)) for v in variantes)
              if lem and len(lem.split()) >= MIN_PALABRAS]
    return list(dict.fromkeys(salida))
