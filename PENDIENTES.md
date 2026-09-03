# Pendientes

Lo que falta, lo que falla a propósito y lo que hay que devolverle a otro. Está
acá para que no se absorba en silencio ni se "arregle" sobreajustando.

Última revisión: 2026-09-03.

---

## Bloqueado en otra persona

### Los códigos de tipificación `WMC###`

`catalogos/tipificaciones.csv` mapea (Categoría, Origen) → código WMC. Hay **6
mapeados y 9 sin código**. Sin ellos la columna `Tipificación` del
`_exclusiones_data_maestro.csv` sale vacía y la fila no se puede pegar en la
planilla. **No inventarlos.**

### Decisiones de catálogo medidas y sin aplicar

Medidas sobre una base de 80.340 correos, esperando decisión de catálogo:

| candidato | volumen | qué es |
|---|---|---|
| tag `[EXT]` | 65.110 correos (81 %) | etiqueta de transporte, candidata a cabecera |
| correos que quedan vacíos | ~2.227 | tras quitar banner y tag no queda texto, y van al maestro como válidos |
| `Obtener Outlook para Android/iOS` | 2.042 | candidata a pie |
| banner con encoding roto | 334 | el patrón no matchea porque el texto trae `rep??rtalo` |

---

## Huecos del motor, en orden de retorno

### 1. Decisión por especificidad — no implementada

El motor recoge todos los matches y desempata por condición, luego por la
**prioridad declarada de la categoría** (`catalogos/prioridades.csv`), luego por
ID. Lo que no hace es preferir **la regla más específica**, la que exige más
términos.

La prioridad tapa el caso que más dolía —que `No-reply` le ganara a una
categoría de contenido— pero no es lo mismo. Falta también un estado `dudoso`
para los empates entre categorías distintas. La columna `Perdedores` ya deja los
solapamientos visibles.

**Medido:** activar la prioridad cambia 1 etiqueta en 78.920 mensajes. El
problema es real pero hoy es chico; crece a medida que crece el catálogo.

### 2. `No-reply` mezcla dos dimensiones

`No-reply` describe el **mecanismo** —lo generó una máquina—, no el contenido.
Por eso se solapa con todas las demás categorías: todo auto-reply es un
no-reply. La prioridad 99 lo deja de último recurso y eso alcanza por ahora,
pero la solución de verdad es separar mecanismo de contenido en dos columnas.
`Institucional` tiene el mismo problema.

### 3. Condición `Sin petición` — no implementada

La frase excluye solo si el mensaje no trae un pedido de negocio. Es lo que
distingue un aviso de ausencia puro de uno que además pide algo.

Los verbos de redirección (`contactar`, `comunicar`) **no** cuentan como pedido:
«favor contactar a mi back up» es parte de la plantilla de ausencia.

**Consecuencia de orden:** las reglas de OOO deben pasar de `Frase contenida` a
`Sin petición` en la planilla, pero **no antes de que el motor implemente la
condición**. Si se cambia primero la planilla, la auditoría falla —que es lo
correcto— y esas reglas dejan de aplicarse como se espera.

### 4. Capa 1 — no probada

Separar lo que no es conversación con cliente: hilo de un solo mensaje +
remitente con patrón de máquina + nunca respondido. Las tres condiciones juntas.

Las muestras disponibles son conversaciones seleccionadas a mano: no traen
boletas, promociones ni notificaciones. **Hacen falta datos sin curar.**

Ojo con el caso que hay que proteger: un cliente que escribió una vez y nadie le
contestó también es un hilo de un mensaje, y es uno de los hallazgos más
valiosos que puede entregar un OTBB.

---

## Cerrados, para que no se reabran

- **Deduplicación del historial citado** — implementada en
  `motor/deduplicacion.py` y usada por el pipeline. Era el hueco de mayor
  retorno.
- **Match tolerante / lematización** — implementada en `motor/lemas.py` con
  spaCy `es_core_news_lg`, como preprocesador y nunca como clasificador. La
  columna `Via` dice si cada match salió por superficie o por lema, y `SIN_LEMAS=1`
  permite el A/B. Dos trampas quedaron resueltas y anotadas ahí: el lematizador
  borra el tiempo verbal (por eso los verbos en pasado finito se marcan aparte) y
  un lema de una sola palabra genera falsos positivos (por eso `MIN_PALABRAS`).

---

## Huecos conocidos, a propósito

Están en `tests/casos_regresion.yaml` marcados con `hueco_conocido: true`.
**Fallan a propósito y no rompen la suite.** Si uno empieza a pasar, la suite
avisa que hay que promoverlo, no dejar la marca puesta.

Son 3 sobre 23 casos. Entre ellos, `fuera de la sucursal`: el catálogo dice
`oficina`. Es una decisión de catálogo. **No ampliar la regla para hacer pasar
el caso.**

---

## Defectos del catálogo, para revisión de catálogo

De los 86 errores que marcó la validación humana sobre 406 exclusiones, **quedan
53 sin corregir**. Los de mayor volumen ya se atacaron: la precisión pasó de
67,8 % a 75,5 %.

El criterio que salió de esa validación, y que conviene aplicar antes de agregar
cualquier regla nueva:

> **Las reglas que fallan son las de una palabra suelta y común con condición
> `Frase contenida`. Las de frase específica o condición `Solo aislado`
> aciertan.** La pregunta antes de agregar una regla es la condición, no la
> frase.

| regla | condición | acierto |
|---|---|---|
| `ok/gracias` | Solo aislado | 96 % |
| `happy hour` | Frase contenida, específica | 100 % |
| `evento/invitación` | Frase contenida, palabra suelta | 67 % |
| `de vacaciones` | Frase contenida, palabra suelta | 21 % |
| `estar con licencia` | Frase contenida, palabra suelta | 3 % |

Dos señales que se repitieron y sirven para escribir reglas nuevas:

- **Un aviso automático DERIVA o DA UNA FECHA; una persona contando algo, no.**
  «vacaciones desde el 18/06 hasta el 29» y «vendrá a mi reemplazo Paola»
  aciertan; «estuve de vacaciones y por eso dejé configurado» falla.
- **Una frase que vive en el ASUNTO no sirve como regla: quien responde hereda
  el asunto.** Medido: `respuesta automática` aparece 67 veces en el asunto y 4
  en el cuerpo, nunca en los dos.

Pendientes concretos:

- **Todas las reglas están en `Alcance: todos`.** Es deliberado: el motor sirve a
  cualquier cliente con correos y hoy no hay ninguna regla específica de uno.
- **`Evento de calendario` y `Evento de calendario (teams, google meet, zoom)`**
  siguen siendo la misma cosa con dos etiquetas.
- **`read` como `Frase contenida`** (EXC-0003) es muy amplio y se solapa con
  `your message was read`.
- **`encuentrar ooo hasta`** tiene un typo y, con límites de palabra, no dispara.
- **Los IDs `EXC-0048` y `EXC-0049` están retirados y no se vuelven a usar.**

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
