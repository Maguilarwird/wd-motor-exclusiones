# Pendientes

Lo que falta, lo que falla a propósito y lo que hay que devolverle a otro. Está
acá para que no se absorba en silencio ni se "arregle" sobreajustando.

Última revisión: 2026-08-26.

---

## Huecos del motor, en orden de retorno

### 1. Deduplicación del historial citado — no implementada

**Es el arreglo que más rinde: ~15,6% del ahorro de tokens.** Del segundo
mensaje de un hilo en adelante, entre el 40% y el 66% de cada correo es texto
que la generativa ya leyó.

Qué hay que hacer: ensamblar el hilo en orden cronológico y eliminar de cada
mensaje todo tramo de 60+ caracteres ya presente en un mensaje anterior del
mismo hilo.

No depende del catálogo, ni de un modelo lingüístico, ni de las cabeceras del
correo. Es puramente mecánico.

Requiere que el motor trabaje **por hilo**, no por mensaje suelto. Hoy trabaja
por mensaje.

### 2. Decisión por especificidad — no implementada

El motor recoge todos los matches y se queda con el primero según el orden de
condición. Debería ganar **la regla más específica**, la que exige más términos,
y las perdedoras quedar registradas.

La columna `Perdedores` ya existe y ya se llena, así que los solapamientos son
visibles. Lo que falta es que el ganador se elija por especificidad y que haya
un estado `dudoso` para los empates entre categorías distintas.

### 3. Match tolerante — no implementado. Es el que explica los falsos negativos

El motor matchea **superficie**. El catálogo está escrito en parte en **lemas**.
Por eso hay reglas que no disparan nunca:

| Regla del catálogo | No alcanza |
|---|---|
| `estar ausente` | «estaré ausente», «estoy ausente» |
| `encontrar de vacaciones` | «me encuentro de vacaciones» |
| `fuera de oficina` | «fuera de **la** oficina» |

Tres tolerancias, cada una para una causa distinta de falla:

- **Dos pistas:** buscar sobre el flujo de lemas *y* sobre la superficie sin
  tildes, porque el lematizador inventa lemas (`estare` → `estarar`).
- **Palabras vacías opcionales** entre los términos, para el caso del artículo.
- **Terminación flexible** en verbos escritos en infinitivo, hasta 3 letras.

Esto sí requiere spaCy, como preprocesador lingüístico y nunca como
clasificador. Con el nombre del modelo en la variable `MODELO_SPACY`.

`scripts/auditar_catalogo.py` lista hoy las reglas afectadas como avisos.

### 4. Condición `Sin petición` — no implementada

La frase excluye solo si el mensaje no trae un pedido de negocio. Es lo que
distingue un aviso de ausencia puro de uno que además pide algo.

Los verbos de redirección (`contactar`, `comunicar`) **no** cuentan como pedido:
«favor contactar a mi back up» es parte de la plantilla de ausencia.

**Consecuencia de orden:** las 15 reglas de OOO deben pasar de `Frase contenida`
a `Sin petición` en la planilla, pero **no antes de que el motor implemente la
condición**. Si se cambia primero la planilla, la auditoría falla — que es lo
correcto — y las 15 reglas dejan de aplicarse como se espera.

### 5. Capa 1 — no probada

Separar lo que no es conversación con cliente: hilo de un solo mensaje +
remitente con patrón de máquina + nunca respondido. Las tres condiciones juntas.

Las muestras disponibles son conversaciones seleccionadas a mano: no traen
boletas, promociones ni notificaciones. **Hacen falta datos sin curar.**

Ojo con el caso que hay que proteger: un cliente que escribió una vez y nadie le
contestó también es un hilo de un mensaje, y es uno de los hallazgos más
valiosos que puede entregar un OTBB.

---

## Huecos conocidos, a propósito

Están en `tests/casos_regresion.yaml` marcados con `hueco_conocido: true`.
**Fallan a propósito y no rompen la suite.** Si uno empieza a pasar, la suite
avisa que hay que promoverlo, no dejar la marca puesta.

- `fuera de la sucursal` no está cubierto. El catálogo dice `oficina`. Es una
  decisión de catálogo. **No ampliar la regla para hacer pasar el caso.**
- Los dos casos de match tolerante del hueco 3.

---

## Defectos del catálogo, para revisión de catálogo

- **Todas las reglas están en `Alcance: todos`.** Es deliberado: v0 no cambia el
  comportamiento del prototipo. Hay que marcar las locales. La auditoría lista
  las candidatas — entre ellas `Institucional (Caja Los Andes)`, que es
  específica de Chile.
- **`Evento de calendario` y `Evento de calendario (teams, google meet, zoom)`**
  son la misma cosa con dos etiquetas.
- **`read` como `Frase contenida`** en Acuso de lectura es muy amplio y se
  solapa con `your message was read`.
- **`encuentrar ooo hasta`** tiene un typo y, con límites de palabra, no dispara.
- Reglas escritas en lema y reglas escritas en superficie **conviven en la misma
  hoja** sin marca que las distinga. Mientras no exista el match tolerante, las
  primeras no funcionan.

---

## Capa de hilo — recién agregada, sin validación humana

`motor/hilos.py` distingue filas duplicadas del archivo de insistencias reales.
Los criterios están medidos pero **no validados a mano**:

- La insistencia se decide por turnos: mismo remitente, nadie contestó en medio.
  Sobre la muestra de Santander da 66 insistencias en 39 de 50 hilos. Falta que
  alguien confirme una por una que son insistencias de verdad.
- `Reenvio_identico` usa un umbral absoluto de 25 caracteres de aporte nuevo.
  El umbral es arbitrario y no está validado.
- Dos criterios anteriores se descartaron por falsos positivos: uno por
  proporción de texto repetido (marcaba como reenvío una respuesta corta que
  citaba la cadena entera) y uno por similitud contra un mensaje anterior (el
  mismo problema). Quedan anotados para que nadie los reintente.

Antes de reportarle un número de insistencias a un cliente, revisar la muestra.

---

## Hallazgos que hay que devolver, no arreglar acá

**El set de validación de Santander trae 28% de filas duplicadas, y el defecto
está en la carpeta SENT.**
111 de 400 filas repiten el Message ID de otra fila del mismo hilo. Medido por
carpeta: **210 de 266 filas SENT están duplicadas, contra 14 de 134 en INBOX**, y
41 de los 50 hilos están afectados. No es "la base viene sucia": el extractor
duplica los mensajes enviados. Eso también explica por qué los 50 hilos
tienen exactamente 8 mensajes: el set fue rellenado.

Avisarle a quien armó el set y **verificar si el defecto está en el extractor de
ingesta o solo en ese archivo.** Quitar duplicados no es una técnica de ahorro,
es tapar un defecto del archivo.

**`Is Promotion` e `Is Fwd` nunca se pueblan.**
`Is Promotion` = 0 en las 116 filas de INVEX y en las 400 de Santander.
`Is Fwd` = False en todas. Dos clientes seguidos: es del pipeline de ingesta, no
de un cliente. **No construir lógica sobre esos dos campos.** Para el threading,
usar `In Reply To` y `Thread Id`.

---

## Reglas de mantención

1. `python scripts/auditar_catalogo.py` **cada vez** que se toque el catálogo.
2. `python tests/test_regresion.py` cada vez que se toque el catálogo o se
   cambie de versión del modelo lingüístico. Es lo único que detecta un lema
   inventado.
3. Ninguna base de correos entra al repo. Ni recortada, ni anonimizada.
4. Cuando el catálogo declare un campo nuevo, verificar en el mismo commit que
   el motor lo consuma. Declarar no es implementar.
