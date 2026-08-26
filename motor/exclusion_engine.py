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
    "alcance":     ("Alcance",),
}
COLUMNAS_OPCIONALES = {
    "accionable": ("¿Trae señal accionable?", "Trae señal accionable"),
    "definicion": ("Definición", "Definicion"),
    "origen":     ("Origen",),
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

    @classmethod
    def desde_archivo(cls, ruta=None):
        ruta = Path(ruta or BANNERS_DEFAULT)
        with open(ruta, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return cls(data.get("bloques", []))

    def limpiar(self, texto_norm, contexto):
        """Retorna (texto_limpio, ids_de_bloques_removidos)."""
        removidos = []
        t = texto_norm

        for b in self.bloques:
            if b["tipo"] != "cabecera" or not _aplica(b["alcance"], contexto):
                continue
            nuevo, n = b["rx"].subn(" ", t)
            if n:
                removidos.append(b["id"])
                t = nuevo

        corte = None
        for b in self.bloques:
            if b["tipo"] != "pie" or not _aplica(b["alcance"], contexto):
                continue
            m = b["rx"].search(t)
            if m and (corte is None or m.start() < corte):
                corte = m.start()
                pie_id = b["id"]
        if corte is not None:
            removidos.append(pie_id)
            t = t[:corte]

        return re.sub(r"\s+", " ", t).strip(), removidos


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


# ── Alcance ───────────────────────────────────────────────────────────

def _parse_alcance(valor):
    if valor is None:
        return {"todos"}
    tokens = {t.strip().lower() for t in str(valor).split(",") if t.strip()}
    return tokens or {"todos"}


def _aplica(alcance, contexto):
    return "todos" in alcance or bool(alcance & contexto)


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

    def __init__(self, rules, banners=None):
        self.banners = banners if banners is not None else Banners.desde_archivo()
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
            vistos.add(rid)
            self.rules.append({
                **r,
                "id": rid,
                "alcance_set": _parse_alcance(r.get("alcance")),
                "pattern": parse_rule_pattern(r["texto"]),
            })
        self.rules.sort(key=lambda r: self.ORDEN_CONDICION.get(r["condicion"], 2))

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

    def analizar(self, texto, remitente="", contexto=None):
        """
        Devuelve siempre un dict. Nunca descarta en silencio: si excluye, dice
        con qué regla, con qué evidencia y bajo qué condición, y deja anotadas
        las otras reglas que también matchearon.
        """
        ctx = normalizar_contexto(contexto)
        crudo = normalize(texto)
        limpio, bloques = self.banners.limpiar(crudo, ctx)

        alerta = not revisar_limpieza(crudo, limpio)
        if alerta:
            limpio, bloques = crudo, []

        remitente_norm = normalize(remitente)

        matches = []
        for rule in self.rules:
            if not _aplica(rule["alcance_set"], ctx):
                continue
            cond = rule["condicion"]
            if cond == "Remitente / dominio":
                ev = self._find_match(rule["pattern"], remitente_norm) if remitente_norm else None
            elif cond == "Solo aislado":
                ev = self._match_solo_aislado(rule["pattern"], limpio)
            else:
                ev = self._find_match(rule["pattern"], limpio)
            if ev:
                matches.append((rule, ev))

        base = {
            "texto_limpio": limpio,
            "bloques_removidos": bloques,
            "alerta_limpieza": alerta,
            "reglas_evaluadas": sum(1 for r in self.rules if _aplica(r["alcance_set"], ctx)),
        }

        if not matches:
            return {**base, "excluido": False, "categoria": "", "regla_id": "",
                    "texto_regla": "", "condicion": "", "accionable": "",
                    "tratamiento": "", "evidencia": "", "perdedores": []}

        rule, ev = matches[0]
        return {
            **base,
            "excluido": True,
            "categoria": rule["categoria"],
            "regla_id": rule["id"],
            "texto_regla": rule["texto"],
            "condicion": rule["condicion"],
            "accionable": rule.get("accionable", ""),
            "tratamiento": rule.get("tratamiento", ""),
            "evidencia": ev,
            # Las reglas que también matchearon. Que dos compitan por el mismo
            # mensaje es la señal de un solapamiento del catálogo; sin esto los
            # solapamientos son invisibles.
            "perdedores": [r["id"] for r, _ in matches[1:]],
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
