r"""
Motor de exclusiones determinista — OTBB
========================================

El motor no sabe nada del dominio. Todo el criterio vive en los catálogos:

    catalogos/exclusiones.xlsx   hoja "Exclusiones"  — las reglas
    catalogos/banners.yaml                           — bloques institucionales

Si para cambiar un criterio hay que editar este archivo, el criterio quedó en
el lugar equivocado.

Sintaxis del campo "Texto Contenido"
------------------------------------
  "|"     entre términos          co-ocurrencia (AND entre grupos)
  " / "   (slash con espacios)    frases alternativas completas (OR)
  "a/b/c" (slash sin espacios)    alternancia de palabra dentro de la frase
                                  «mi/el/su correo ahora/nuevo es»
  "los" al final de una alternancia se une al token siguiente
                                  «andes/araucana/los heroes»
  "fecha"                         meta-token que matchea fechas reales

Condiciones de match implementadas
----------------------------------
  Frase contenida       el patrón aparece en el texto
  Solo aislado          el mensaje ES esencialmente la frase (<=3 tokens extra)
  Remitente / dominio   match contra el remitente

Cualquier otro valor en la columna "Condición de match" es una condición
DECLARADA Y NO IMPLEMENTADA: scripts/auditar_catalogo.py falla si aparece.
Esa verificación existe porque declarar no es implementar — un campo que el
catálogo declara y el motor nunca lee produce errores que parecen falta de
precisión del motor.

Alcance
-------
Cada regla declara en qué contexto aplica. 'todos' aplica siempre; cualquier
otro valor (un país, un cliente) aplica solo si ese token viene en el contexto
de la llamada. Nunca hay un `if` con el nombre de un cliente en este archivo.
"""

import os
import re
import unicodedata
from pathlib import Path

import yaml

from . import lemas

# ── Contrato con el catálogo ──────────────────────────────────────────

CONDICIONES_IMPLEMENTADAS = {
    "Frase contenida",
    "Solo aislado",
    "Remitente / dominio",
}

# Se lee por NOMBRE de columna, nunca por posición: insertar una columna en la
# planilla no debe cambiar lo que el motor lee.
COLUMNAS_REQUERIDAS = {
    "id":          ("ID",),
    "categoria":   ("Categoría", "Categoria"),
    "texto":       ("Texto Contenido", "Texto"),
    "condicion":   ("Condición de match", "Condicion de match"),
    "tratamiento": ("Tratamiento",),
}
COLUMNAS_OPCIONALES = {
    "accionable": ("¿Trae señal accionable?", "Trae señal accionable"),
    "definicion": ("Definición", "Definicion"),
    "origen":     ("Origen",),
    # Sin columna Alcance toda regla aplica siempre. Fue una decisión explícita:
    # una exclusión es la misma en Chile o en México —un no-reply es un no-reply—
    # así que las 141 reglas tenían 'todos' y la columna no decidía nada. El
    # alcance por cliente o país sigue vivo donde sí importa: banners.yaml, donde
    # el banner de Santander es de Santander y el aviso de la CMF es de Chile.
    "alcance":    ("Alcance",),
}

RAIZ = Path(__file__).resolve().parents[1]
CATALOGO_DEFAULT = RAIZ / "catalogos" / "exclusiones.csv"
BANNERS_DEFAULT = RAIZ / "catalogos" / "banners.yaml"

_HANGING_WORDS = {"los", "las"}

_DATE_RX = (
    r"(\d{1,2}([/-]\d{1,2}([/-]\d{2,4})?)?"
    r"|\d{1,2}\s+de\s+\w+"
    r"|lunes|martes|miercoles|jueves|viernes|sabado|domingo"
    r"|enero|febrero|marzo|abril|mayo|junio|julio|agosto"
    r"|septiembre|octubre|noviembre|diciembre)"
)


class ErrorCatalogo(Exception):
    """El catálogo no cumple el contrato. Nunca se degrada en silencio."""


# ── Normalización ─────────────────────────────────────────────────────

def normalize(text) -> str:
    """Minúsculas, sin acentos, espacios colapsados."""
    if not isinstance(text, str):
        return ""
    text = text.lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", text).strip()


def normalize_con_mapa(text):
    """
    Lo mismo que normalize(), y además de dónde salió cada carácter.

    Devuelve (texto_normalizado, mapa). `mapa[i]` es la posición en el texto
    ORIGINAL donde empieza el carácter i del normalizado. Tiene un elemento de
    más, apuntando al final del original, para que un tramo [i, j) del
    normalizado se recorte del original como [mapa[i], mapa[j]).

    Existe por una razón concreta: los patrones de banners están escritos
    contra el texto normalizado —en minúsculas y sin tildes, que es lo que
    permite que matcheen— pero el texto que se le manda al paso siguiente
    tiene que ser el que escribió la persona. Sin este mapa la única salida
    era quedarse con el normalizado, y eso significa entregar los correos del
    cliente en minúsculas y sin acentos.

    Debe producir exactamente la misma cadena que normalize(): si las dos se
    separan, los patrones dejan de encontrar en un lado lo que encuentran en
    el otro. tests/test_regresion.py lo verifica.
    """
    if not isinstance(text, str):
        return "", [0]

    out, mapa = [], []
    i, n = 0, len(text)
    while i < n:
        if text[i].isspace():
            # Una corrida de espacios colapsa a uno solo, anclado al primero.
            j = i
            while j < n and text[j].isspace():
                j += 1
            out.append(" ")
            mapa.append(i)
            i = j
            continue
        for c in unicodedata.normalize("NFD", text[i].lower()):
            if unicodedata.category(c) != "Mn":
                out.append(c)
                mapa.append(i)
        i += 1

    # El .strip() de normalize(), aplicado también al mapa.
    ini, fin = 0, len(out)
    while ini < fin and out[ini] == " ":
        ini += 1
    while fin > ini and out[fin - 1] == " ":
        fin -= 1
    out, mapa = out[ini:fin], mapa[ini:fin]

    mapa.append(n)
    return "".join(out), mapa


# ── Limpieza de bloques institucionales ───────────────────────────────

class Banners:
    """
    Cabeceras y pies se tratan distinto:
      cabecera  se borra donde esté, el resto del mensaje se conserva
      pie       se corta desde ahí hasta el final

    Tratar una cabecera como pie borra el cuerpo del mensaje. En la muestra de
    INVEX eso destruyó 39 de 116 cuerpos y produjo un "ahorro" de 45% que era
    información perdida. Por eso existe revisar_limpieza().
    """

    def __init__(self, bloques):
        self.bloques = []
        for b in bloques:
            tipo = str(b.get("tipo", "")).strip().lower()
            if tipo not in ("cabecera", "pie"):
                raise ErrorCatalogo(
                    f"banners.yaml: bloque {b.get('id')} tiene tipo {tipo!r}; "
                    "solo se aceptan 'cabecera' y 'pie'"
                )
            self.bloques.append({
                "id": b.get("id", "SIN-ID"),
                "tipo": tipo,
                "alcance": _parse_alcance(b.get("alcance", "todos")),
                "descripcion": b.get("descripcion", ""),
                "rx": re.compile(b["patron"]),
            })
        self.tipo_por_id = {b["id"]: b["tipo"] for b in self.bloques}

    def corto_un_pie(self, ids):
        """
        ¿Alguno de estos bloques fue un pie?

        Importa porque los dos tipos tienen riesgos distintos. Una cabecera
        borra exactamente lo que matchea el patrón: por construcción no puede
        llevarse nada más. Un pie corta desde el match hasta el final del
        mensaje, y ahí sí un patrón mal clasificado se lleva el cuerpo entero.
        La red de seguridad (revisar_limpieza) corresponde al segundo caso.
        """
        return any(self.tipo_por_id.get(i) == "pie" for i in ids)

    @classmethod
    def desde_archivo(cls, ruta=None):
        ruta = Path(ruta or os.environ.get("BANNERS_EXCLUSIONES") or BANNERS_DEFAULT)
        with open(ruta, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return cls(data.get("bloques", []))

    def tramos(self, texto_norm, contexto):
        """
        Dónde están los bloques institucionales dentro de `texto_norm`.

        Retorna (tramos, ids_de_bloques_removidos). Cada tramo es un par
        [inicio, fin) en las coordenadas del texto que ENTRÓ, no de una versión
        ya recortada: por eso cada bloque encontrado se tapa con espacios del
        mismo largo en vez de borrarse. Si se borrara, todo lo que viene
        después se correría y las posiciones dejarían de servir para recortar
        el texto original.

        Es la única función que sabe encontrar un banner. limpiar() y
        limpiar_texto() se construyen sobre ella para que no puedan discrepar.
        """
        removidos, tramos = [], []
        t = texto_norm

        for b in self.bloques:
            if b["tipo"] != "cabecera" or not _aplica(b["alcance"], contexto):
                continue
            encontrados = [(m.start(), m.end()) for m in b["rx"].finditer(t)]
            if not encontrados:
                continue
            removidos.append(b["id"])
            tramos.extend(encontrados)
            for ini, fin in encontrados:
                t = t[:ini] + " " * (fin - ini) + t[fin:]

        # Los tramos de cabecera van completos antes del pie: quien necesite
        # saber qué se llevó el PIE y qué las cabeceras usa `n_cabeceras`.
        n_cabeceras = len(tramos)

        corte, pie_id = None, None
        for b in self.bloques:
            if b["tipo"] != "pie" or not _aplica(b["alcance"], contexto):
                continue
            m = b["rx"].search(t)
            if m and (corte is None or m.start() < corte):
                corte, pie_id = m.start(), b["id"]
        if corte is not None:
            removidos.append(pie_id)
            tramos.append((corte, len(texto_norm)))

        return tramos, removidos, n_cabeceras

    def limpiar_en_dos_pasos(self, texto_norm, contexto):
        """
        (tras_cabeceras, final, ids). El intermedio es lo que hace falta para
        preguntar bien si el pie destruyó algo.

        La red de seguridad comparaba el texto que ENTRÓ contra el final, y eso
        deja de servir cuando una cabecera quita mucho: un correo cuyo cuerpo es
        215 caracteres de banner más «Hola mi carnet» termina en 26 caracteres
        legítimos, y la guarda lo leía como un mensaje destruido y devolvía el
        banner. Medido: 29 correos de 80.340.

        Lo que hay que preguntar es si el PIE se comió el mensaje, así que se
        compara lo que había DESPUÉS de las cabeceras contra el resultado final.
        """
        tramos, removidos, n_cab = self.tramos(texto_norm, contexto)
        limpiar = lambda tr: re.sub(r"\s+", " ", self._recortar(texto_norm, tr)).strip()
        tras_cab = limpiar(tramos[:n_cab]) if n_cab else re.sub(r"\s+", " ", texto_norm).strip()
        final = limpiar(tramos) if tramos else tras_cab
        return tras_cab, final, removidos

    @staticmethod
    def _recortar(texto, tramos):
        """Saca del texto los tramos indicados, fusionando los que se solapen."""
        partes, cursor = [], 0
        for ini, fin in sorted(tramos):
            if ini > cursor:
                partes.append(texto[cursor:ini])
            cursor = max(cursor, fin)
        partes.append(texto[cursor:])
        return "".join(partes)

    def limpiar(self, texto_norm, contexto):
        """
        Retorna (texto_limpio, ids_de_bloques_removidos), sobre el normalizado.

        Es la versión que usan las reglas: entra texto en minúsculas y sin
        tildes, y sale igual. No sirve para mandar a ningún lado.
        """
        tramos, removidos, _ = self.tramos(texto_norm, contexto)
        limpio = self._recortar(texto_norm, tramos) if tramos else texto_norm
        return re.sub(r"\s+", " ", limpio).strip(), removidos

    def limpiar_texto(self, texto, contexto):
        """
        Retorna (texto_limpio, ids), sobre el texto ORIGINAL.

        El correo como lo escribió la persona —mayúsculas, tildes, saltos de
        línea— menos los bloques institucionales. Esto es lo que se le manda al
        paso siguiente: es la diferencia entre detectar el banner y dejar de
        pagarlo en tokens.

        Los banners se buscan en el normalizado, porque es ahí donde los
        patrones matchean, y el tramo encontrado se traslada al original con el
        mapa de posiciones. Ver normalize_con_mapa().
        """
        _, limpio, removidos = self.limpiar_texto_en_dos_pasos(texto, contexto)
        return limpio, removidos

    def limpiar_texto_en_dos_pasos(self, texto, contexto):
        """
        (tras_cabeceras, final, ids) sobre el texto ORIGINAL.

        Igual que limpiar_en_dos_pasos() pero conservando mayúsculas y tildes.
        El intermedio sirve para lo mismo: preguntar si el PIE se comió el
        mensaje, en vez de mirar cuánto encogió el texto en total.
        """
        if not isinstance(texto, str) or not texto.strip():
            return texto, texto, []
        norm, mapa = normalize_con_mapa(texto)
        tramos, removidos, n_cab = self.tramos(norm, contexto)
        if not tramos:
            return texto, texto, removidos
        # Sólo espacios horizontales: colapsar saltos de línea cambiaría la
        # forma del correo, y eso no es quitar un banner.
        pulir = lambda s: re.sub(r"[ \t]{2,}", " ", s).strip()
        al_original = lambda tr: [(mapa[i], mapa[j]) for i, j in tr]
        tras_cab = (pulir(self._recortar(texto, al_original(tramos[:n_cab])))
                    if n_cab else texto)
        final = pulir(self._recortar(texto, al_original(tramos)))
        return tras_cab, final, removidos


def revisar_limpieza(crudo, limpio, min_crudo=90, min_final=30):
    """
    False = la limpieza se comió el mensaje. El llamador debe descartar el
    resultado limpio y quedarse con el crudo, y contar la alerta.
    """
    return not (len(crudo) > min_crudo and len(limpio) < min_final)


# ── Compilación de patrones ───────────────────────────────────────────

def phrase_to_regex(phrase) -> re.Pattern:
    """Frase del catálogo → regex con límites de palabra."""
    tokens = normalize(phrase).split()
    parts = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "fecha":
            parts.append(r"((el|la|los|las)\s+)?" + _DATE_RX)
            i += 1
        elif "/" in tok:
            alts = [a for a in tok.split("/") if a]
            if alts and alts[-1] in _HANGING_WORDS and i + 1 < len(tokens):
                nxt = tokens[i + 1]
                alts[-1] = re.escape(alts[-1]) + r"\s+" + re.escape(nxt)
                alts[:-1] = [re.escape(a) for a in alts[:-1]]
                i += 1
            else:
                alts = [re.escape(a) for a in alts]
            parts.append("(" + "|".join(alts) + ")")
            i += 1
        else:
            parts.append(re.escape(tok))
            i += 1
    return re.compile(r"\b" + r"\s+".join(parts) + r"\b")


def parse_rule_pattern(raw):
    """Lista de grupos AND; cada grupo es una lista de regex en OR."""
    groups = []
    for and_part in str(raw).split("|"):
        and_part = and_part.strip()
        if not and_part:
            continue
        alts = [p.strip() for p in re.split(r"\s+/\s+", and_part) if p.strip()]
        groups.append([phrase_to_regex(p) for p in alts])
    return groups


def parse_rule_pattern_lemas(raw):
    """
    El mismo patrón, pero para comparar contra los LEMAS del texto.

    Devuelve [] cuando lematizar no cambia nada: ahí el patrón de superficie ya
    cubre el caso y compilar un duplicado solo hace el motor más lento sin
    encontrar nada nuevo.

    Un grupo cuya frase no cambió al lematizar conserva su regex de superficie:
    el texto lematizado también contiene las palabras que ya estaban en su forma
    base, así que «no haber sido posible | enviar archivo» funciona con el
    primer grupo en lema y el segundo tal cual.
    """
    groups, hubo_lema = [], False
    for and_part in str(raw).split("|"):
        and_part = and_part.strip()
        if not and_part:
            continue
        alts = [p.strip() for p in re.split(r"\s+/\s+", and_part) if p.strip()]
        rxs = []
        for p in alts:
            variantes = lemas.lematizar_regla(p)
            if variantes:
                hubo_lema = True
                rxs.extend(phrase_to_regex(v) for v in variantes)
            else:
                rxs.append(phrase_to_regex(p))
        groups.append(rxs)
    return groups if hubo_lema else []


# ── Alcance ───────────────────────────────────────────────────────────

def _parse_alcance(valor):
    if valor is None:
        return {"todos"}
    tokens = {t.strip().lower() for t in str(valor).split(",") if t.strip()}
    return tokens or {"todos"}


def _aplica(alcance, contexto):
    return "todos" in alcance or bool(alcance & contexto)


# ── Prioridad entre categorías ────────────────────────────────────────

PRIORIDADES_DEFAULT = RAIZ / "catalogos" / "prioridades.csv"
# Una categoría que no está en el archivo cede ante todas las declaradas, pero
# gana a No-reply. Así agregar una categoría nueva al catálogo no la manda al
# fondo en silencio ni la pone a competir con las que sí se pensaron.
PRIORIDAD_DEFAULT = 60


def cargar_prioridades(ruta=None):
    """
    {categoría: prioridad}. Número más bajo gana cuando dos reglas matchean.

    Vive en un archivo aparte y no en el catálogo de reglas porque es una
    decisión por CATEGORÍA, no por regla: declararla 101 veces invita a que
    dos filas de la misma categoría queden con prioridades distintas.

    Si el archivo no está, todas empatan y el desempate vuelve a ser el ID. No
    se cae: sólo se pierde la precedencia declarada.
    """
    import csv as _csv
    ruta = Path(ruta or os.environ.get("PRIORIDADES_EXCLUSIONES")
                or PRIORIDADES_DEFAULT)
    if not ruta.exists():
        return {}
    with open(ruta, encoding="utf-8-sig", newline="") as fh:
        salida = {}
        for row in _csv.DictReader(fh):
            cat = (row.get("Categoría") or row.get("Categoria") or "").strip()
            try:
                p = int(str(row.get("Prioridad", "")).strip())
            except ValueError:
                continue
            if cat:
                salida[cat] = p
        return salida


def normalizar_contexto(contexto):
    if contexto is None:
        return {"todos"}
    if isinstance(contexto, str):
        contexto = [contexto]
    return {"todos"} | {str(c).strip().lower() for c in contexto if str(c).strip()}


# ── Motor ─────────────────────────────────────────────────────────────

class ExclusionEngine:
    """
        engine = ExclusionEngine(load_catalog(ruta))
        r = engine.analizar(texto, remitente, contexto={"santander", "cl"})
    """

    ORDEN_CONDICION = {"Remitente / dominio": 0, "Solo aislado": 1}

    def __init__(self, rules, banners=None, prioridades=None):
        self.banners = banners if banners is not None else Banners.desde_archivo()
        self.prioridades = (prioridades if prioridades is not None
                            else cargar_prioridades())
        self.rules = []
        vistos = set()
        for r in rules:
            rid = str(r.get("id") or "").strip()
            if not rid:
                raise ErrorCatalogo(
                    f"regla sin ID: {r.get('texto')!r}. Los IDs no se generan por "
                    "posición: si se generan, insertar una fila mueve todos los "
                    "de abajo y cualquier referencia queda apuntando a otra regla."
                )
            if rid in vistos:
                raise ErrorCatalogo(f"ID duplicado en el catálogo: {rid}")
            condicion = str(r.get("condicion") or "").strip()
            if condicion not in CONDICIONES_IMPLEMENTADAS:
                raise ErrorCatalogo(
                    f"regla {rid}: condición no implementada: {condicion!r}. "
                    f"Valores permitidos: {sorted(CONDICIONES_IMPLEMENTADAS)}"
                )
            vistos.add(rid)
            self.rules.append({
                **r,
                "id": rid,
                "condicion": condicion,
                "alcance_set": _parse_alcance(r.get("alcance")),
                "pattern": parse_rule_pattern(r["texto"]),
                # Vacío si spaCy no está o si lematizar no aporta nada.
                "pattern_lemas": parse_rule_pattern_lemas(r["texto"]),
            })
        # Cuando dos reglas matchean el mismo correo, gana la primera de esta
        # lista. Antes el desempate salía del NÚMERO DE ID, que es accidental:
        # 'fuera de oficina' (EXC-0041) le ganaba a 'respuesta automática'
        # (EXC-0072) sólo porque 41 < 72, y una regla nueva con ID alto perdía
        # sin que nada lo explicara.
        #
        # Ahora la prioridad es un dato declarado, en catalogos/prioridades.csv.
        # Es como se resolvía en el BI: las reglas se ordenan por prioridad, y
        # 'fuera de oficina' siempre gana sobre 'no reply'.
        self.rules.sort(key=lambda r: (
            self.ORDEN_CONDICION.get(r["condicion"], 2),
            self.prioridades.get(r["categoria"], PRIORIDAD_DEFAULT),
            r["id"],
        ))

    # ── matchers ──

    def _find_match(self, pattern, text):
        evidence = []
        for group in pattern:
            hit = None
            for rx in group:
                m = rx.search(text)
                if m:
                    hit = m.group(0)
                    break
            if not hit:
                return None
            evidence.append(hit)
        return " + ".join(evidence)

    def _match_solo_aislado(self, pattern, text, max_extra=3):
        evidence = self._find_match(pattern, text)
        if not evidence:
            return None
        remainder = text
        for piece in evidence.split(" + "):
            remainder = remainder.replace(piece, " ")
        extra = [t for t in remainder.split() if len(t) > 2]
        return evidence if len(extra) <= max_extra else None

    # ── API ──

    def analizar(self, texto, remitente="", contexto=None, cuerpo=None):
        """
        Devuelve siempre un dict. Nunca descarta en silencio: si excluye, dice
        con qué regla, con qué evidencia y bajo qué condición, y deja anotadas
        las otras reglas que también matchearon.

        `cuerpo` es el mensaje sin el asunto, y sólo lo usan las reglas de
        condición "Solo aislado". Esa condición pregunta si el mensaje ES la
        frase, y el asunto no es el mensaje: es un dato de al lado que el
        llamador antepone para que las reglas de "Frase contenida" puedan verlo.
        Evaluando "Solo aislado" contra asunto + cuerpo la condición no se puede
        cumplir nunca —siempre sobran las palabras del asunto— y las 14 reglas
        de acuse quedan muertas aunque estén bien escritas.

        Si no se pasa, se usa `texto`: para quien llama con un mensaje suelto,
        el texto Y el cuerpo son la misma cosa.
        """
        ctx = normalizar_contexto(contexto)
        crudo = normalize(texto)
        tras_cab, limpio, bloques = self.banners.limpiar_en_dos_pasos(crudo, ctx)

        # La red de seguridad corresponde sólo a los pies: ésos cortan hasta el
        # final del mensaje y un patrón mal clasificado se lleva el cuerpo
        # entero. Una cabecera borra exactamente lo que matchea, así que un
        # mensaje que queda corto después de quitarla no está destruido: era
        # casi todo banner.
        #
        # Medido sobre una base de 80.340 correos, aplicarla también a las
        # cabeceras daba 2.571 alertas falsas: correos con un adjunto y sin
        # cuerpo, donde el cuerpo era literalmente el banner y el contenido
        # real estaba en el asunto («Cartola octubre», «Anexo indefinido»).
        # Además de ensuciar el reporte, devolvía el banner a las reglas.
        alerta = (self.banners.corto_un_pie(bloques)
                  and not revisar_limpieza(tras_cab, limpio))
        if alerta:
            limpio, bloques = crudo, []

        remitente_norm = normalize(remitente)

        # El texto contra el que se pregunta "¿el mensaje ES esta frase?".
        # Sin `cuerpo` es el mismo de siempre; con `cuerpo`, el mensaje sin el
        # asunto que le antepuso el llamador.
        if cuerpo is None:
            aislado = limpio
        else:
            crudo_cuerpo = normalize(cuerpo)
            aislado, _ = self.banners.limpiar(crudo_cuerpo, ctx)
            if not revisar_limpieza(crudo_cuerpo, aislado):
                aislado = crudo_cuerpo

        # Los lemas se calculan una sola vez por mensaje, y sólo si alguna regla
        # los necesita. Se lematiza el texto ORIGINAL sin banners, nunca el
        # normalizado: sin acentos el modelo no puede saber que «estare» es un
        # verbo. Ver motor/lemas.py.
        cache_lemas = {}

        def lemas_de(cual):
            if cual not in cache_lemas:
                original = texto if cual == "texto" else cuerpo
                sin_banner, _ = self.banners.limpiar_texto(original or "", ctx)
                cache_lemas[cual] = lemas.lemas(sin_banner)
            return cache_lemas[cual]

        matches = []
        for rule in self.rules:
            if not _aplica(rule["alcance_set"], ctx):
                continue
            cond = rule["condicion"]
            via = "superficie"
            if cond == "Remitente / dominio":
                # Una dirección de correo no tiene morfología que lematizar.
                ev = self._find_match(rule["pattern"], remitente_norm) if remitente_norm else None
            elif cond == "Solo aislado":
                ev = self._match_solo_aislado(rule["pattern"], aislado)
                if not ev and rule["pattern_lemas"]:
                    ev = self._match_solo_aislado(
                        rule["pattern_lemas"],
                        lemas_de("cuerpo" if cuerpo is not None else "texto"))
                    via = "lema"
            elif cond == "Frase contenida":
                ev = self._find_match(rule["pattern"], limpio)
                if not ev and rule["pattern_lemas"]:
                    ev = self._find_match(rule["pattern_lemas"], lemas_de("texto"))
                    via = "lema"
            else:  # El constructor impide llegar aquí; defensa ante mutaciones en memoria.
                raise RuntimeError(f"condición no validada: {cond!r}")
            if ev:
                matches.append((rule, ev, via))

        base = {
            "texto_limpio": limpio,
            "bloques_removidos": bloques,
            "alerta_limpieza": alerta,
            "reglas_evaluadas": sum(1 for r in self.rules if _aplica(r["alcance_set"], ctx)),
        }

        if not matches:
            return {**base, "excluido": False, "categoria": "", "regla_id": "",
                    "texto_regla": "", "condicion": "", "accionable": "",
                    "tratamiento": "", "definicion": "", "origen": "",
                    "evidencia": "", "via": "", "perdedores": []}

        rule, ev, via = matches[0]
        return {
            **base,
            "excluido": True,
            "categoria": rule["categoria"],
            "regla_id": rule["id"],
            "texto_regla": rule["texto"],
            "condicion": rule["condicion"],
            "accionable": rule.get("accionable", ""),
            "tratamiento": rule.get("tratamiento", ""),
            # Definición y Origen se leían del catálogo y no salían. Los necesita
            # la fila agregada del Data_maestro: sin ellos la fila queda
            # incompleta y alguien tiene que volver a buscarlos a mano.
            "definicion": rule.get("definicion", ""),
            "origen": rule.get("origen", ""),
            "evidencia": ev,
            # "superficie" o "lema": con qué comparación se encontró. Sin esto no
            # hay forma de saber cuánto aporta spaCy ni de revisar aparte lo que
            # empezó a matchear al generalizar.
            "via": via,
            # Las reglas que también matchearon. Que dos compitan por el mismo
            # mensaje es la señal de un solapamiento del catálogo; sin esto los
            # solapamientos son invisibles.
            "perdedores": [r["id"] for r, _, _ in matches[1:]],
        }

    def detect(self, texto, remitente="", contexto=None):
        """Compatibilidad: None cuando el mensaje es gestionable."""
        r = self.analizar(texto, remitente, contexto)
        return r if r["excluido"] else None


# ── Carga del catálogo ────────────────────────────────────────────────

def _mapear_columnas(encabezado):
    disponibles = {}
    for idx, celda in enumerate(encabezado):
        if celda is not None:
            disponibles[str(celda).strip()] = idx

    mapa, faltan = {}, []
    for clave, nombres in COLUMNAS_REQUERIDAS.items():
        for n in nombres:
            if n in disponibles:
                mapa[clave] = disponibles[n]
                break
        else:
            faltan.append(nombres[0])
    if faltan:
        raise ErrorCatalogo(
            "faltan columnas obligatorias en la hoja: " + ", ".join(faltan)
            + ". Columnas encontradas: " + ", ".join(sorted(disponibles))
        )
    for clave, nombres in COLUMNAS_OPCIONALES.items():
        for n in nombres:
            if n in disponibles:
                mapa[clave] = disponibles[n]
                break
    return mapa


def load_catalog(ruta=None, sheet="Exclusiones"):
    """
    Lee el catálogo desde CSV (recomendado) o XLSX.

    El CSV es la fuente de verdad del repo: un xlsx en git es un binario, así
    que no se puede ver qué regla cambió entre dos versiones. Con CSV el diff
    se lee línea por línea y se puede editar desde cualquier planilla,
    incluida Google Sheets, sin licencia de Office.
    """
    ruta = Path(ruta or os.environ.get("CATALOGO_EXCLUSIONES") or CATALOGO_DEFAULT)
    if not ruta.exists():
        raise ErrorCatalogo(f"no existe el catálogo: {ruta}")

    if ruta.suffix.lower() == ".csv":
        filas = _filas_csv(ruta)
    else:
        filas = _filas_xlsx(ruta, sheet)

    encabezado = next(filas, None)
    if encabezado is None:
        raise ErrorCatalogo(f"{ruta.name} está vacío")
    mapa = _mapear_columnas(encabezado)

    def celda(row, clave, default=""):
        idx = mapa.get(clave)
        if idx is None or idx >= len(row) or row[idx] is None:
            return default
        return str(row[idx]).strip()

    reglas = []
    for row in filas:
        if not any(c not in (None, "") for c in row):
            continue
        if not celda(row, "categoria") or not celda(row, "texto"):
            continue
        reglas.append({
            "id":          celda(row, "id"),
            "categoria":   celda(row, "categoria"),
            "texto":       celda(row, "texto"),
            "condicion":   celda(row, "condicion", "Frase contenida"),
            "accionable":  celda(row, "accionable", "No"),
            "tratamiento": celda(row, "tratamiento"),
            "alcance":     celda(row, "alcance", "todos"),
            "definicion":  celda(row, "definicion"),
            "origen":      celda(row, "origen"),
        })
    if not reglas:
        raise ErrorCatalogo(f"el catálogo {ruta.name} no tiene reglas")
    return reglas


def _filas_csv(ruta):
    import csv
    # utf-8-sig: Google Sheets y Excel escriben BOM al exportar CSV. Sin esto,
    # la primera columna del encabezado llega como "\ufeffID" y no matchea.
    with open(ruta, encoding="utf-8-sig", newline="") as fh:
        for row in csv.reader(fh):
            yield row


def _filas_xlsx(ruta, sheet):
    import openpyxl
    wb = openpyxl.load_workbook(ruta, read_only=True, data_only=True)
    if sheet not in wb.sheetnames:
        raise ErrorCatalogo(f"la hoja {sheet!r} no existe en {ruta.name}. Hojas: {wb.sheetnames}")
    for row in wb[sheet].iter_rows(values_only=True):
        yield list(row)
